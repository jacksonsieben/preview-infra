#!/usr/bin/env python3
"""battery-guard: when it stops the runners, and when it warns.

    python3 tests/battery_guard.py

Loads roles/ci_host/files/battery-guard as it is installed. The battery is a
directory of fake capacity and temp files, the stop command a Python one-liner
that counts its runs, and the ntfy server a local HTTP server. Offline:
nothing is stopped and nothing leaves the machine.
"""
import importlib.machinery
import importlib.util
import os
import shlex
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "roles/ci_host/files/battery-guard")

# No __pycache__ next to the script the role installs.
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("battery_guard", SCRIPT)
spec = importlib.util.spec_from_loader("battery_guard", loader)
bg = importlib.util.module_from_spec(spec)
loader.exec_module(bg)


def check(name, got, expected):
    assert got == expected, f"{name}: got {got!r}, expected {expected!r}"
    print(f"ok  {name}")


def test_problems():
    def problems(capacity, temp, error=None):
        return bg.problems(capacity, temp, error, 42, 25)

    check("within limits", problems(80, 35.0), [])
    check("at the limits: still fine", problems(25, 42.0), [])
    check("too hot", problems(80, 42.5), ["battery at 42.5 °C, above 42 °C"])
    check("too low", problems(24, 30.0), ["battery at 24%, below 25%"])
    check("both", len(problems(10, 50.0)), 2)
    check("no temperature: capacity alone", problems(80, None), [])
    check("unreadable: stop", problems(None, None, "cannot read x"), ["cannot read x"])


def write(directory, name, value):
    with open(os.path.join(directory, name), "w") as f:
        f.write(f"{value}\n")


def test_reading():
    with tempfile.TemporaryDirectory() as battery:
        check("no capacity file", bg.reading(battery)[2] is not None, True)
        write(battery, "capacity", 87)
        check("no temp file: none", bg.reading(battery), (87, None, None))
        write(battery, "temp", 385)
        check("tenths of a degree", bg.reading(battery), (87, 38.5, None))
        write(battery, "temp", "garbage")
        check("bad temp: error", bg.reading(battery)[2] is not None, True)


class Ntfy(BaseHTTPRequestHandler):
    posts = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        Ntfy.posts.append((self.path, dict(self.headers), body))
        self.send_response(Ntfy.status)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_check():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/secret-topic"
    with tempfile.TemporaryDirectory() as tmp:
        battery = os.path.join(tmp, "battery")
        os.mkdir(battery)
        stops = os.path.join(tmp, "stops")
        stop = [sys.executable, "-c",
                f"open({stops!r}, 'a').write('stop\\n')"]
        conf = bg.settings({
            "BATTERY_GUARD_DIR": battery,
            "BATTERY_GUARD_NTFY_URL": url,
            "BATTERY_GUARD_HOST": "ci-test",
            "BATTERY_GUARD_STATE": os.path.join(tmp, "run", "alerted"),
            "BATTERY_GUARD_STOP": shlex.join(stop),
        })
        check("default limits", (conf["max_temp"], conf["min_capacity"]), (42.0, 25.0))

        def stopped():
            if not os.path.exists(stops):
                return 0
            with open(stops) as f:
                return len(f.read().splitlines())

        def tick(capacity, temp):
            write(battery, "capacity", capacity)
            write(battery, "temp", temp)
            return bg.check(conf)

        check("within limits: nothing", tick(90, 300), [])
        check("within limits: no stop", stopped(), 0)
        check("within limits: no post", Ntfy.posts, [])

        check("too hot: stops", len(tick(90, 450)), 1)
        check("too hot: stop ran", stopped(), 1)
        check("too hot: one post", len(Ntfy.posts), 1)
        path, headers, body = Ntfy.posts[0]
        check("posted to the topic", path, "/secret-topic")
        check("title", headers["Title"], "ci-test: runners stopped, battery")
        assert "45 °C" in body and "are stopped" in body, body

        tick(90, 460)
        check("still hot: stops again", stopped(), 2)
        check("still hot: once per episode", len(Ntfy.posts), 1)

        tick(90, 350)
        check("cooled down: no stop", stopped(), 2)
        check("cooled down: episode over", os.path.exists(conf["state"]), False)

        # ntfy refuses: the warning is tried again at the next reading.
        Ntfy.status = 500
        tick(20, 300)
        check("too low: stop ran", stopped(), 3)
        check("refused post", len(Ntfy.posts), 2)
        check("refused: no episode yet", os.path.exists(conf["state"]), False)
        Ntfy.status = 200
        tick(20, 300)
        check("retried", len(Ntfy.posts), 3)
        tick(19, 300)
        check("then quiet", len(Ntfy.posts), 3)

        # No battery to read: stop, as when it is out of its limits.
        tick(90, 300)
        os.remove(os.path.join(battery, "capacity"))
        check("unreadable: stops", len(bg.check(conf)), 1)
        check("unreadable: stop ran", stopped(), 6)

        # A stop that fails is said so.
        tick(90, 300)
        conf["stop"] = [sys.executable, "-c", "raise SystemExit(1)"]
        tick(90, 500)
        assert "could NOT be stopped" in Ntfy.posts[-1][2], Ntfy.posts[-1]
        print("ok  failed stop: said so")

        # Without a topic it only logs.
        conf["ntfy_url"] = ""
        tick(90, 300)
        before = len(Ntfy.posts)
        tick(90, 500)
        check("no topic: no post", len(Ntfy.posts), before)
        check("no topic: episode recorded", os.path.exists(conf["state"]), True)
    server.shutdown()


def test_read_only():
    with open(SCRIPT) as f:
        source = f.read()
    # It opens files for reading only, and writes only its state file.
    check("one write, to the state file", source.count('"w"'), 1)
    assert 'open(conf["state"], "w")' in source


if __name__ == "__main__":
    test_problems()
    test_reading()
    test_check()
    test_read_only()
    print("all battery-guard checks passed")
