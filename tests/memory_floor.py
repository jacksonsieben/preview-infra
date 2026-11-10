#!/usr/bin/env python3
"""memory-floor: how much memory it holds, and when it warns.

    python3 tests/memory_floor.py

Loads roles/ci_host/files/memory-floor as it is installed. The host is a fake
/proc/meminfo in a temporary directory whose available memory goes down by
what the keeper holds, and the ntfy server is a local HTTP server. Offline:
the keeper holds a few MiB at most.
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "roles/ci_host/files/memory-floor")
GIB = 1024 ** 3
MIB = 1024 ** 2

# No __pycache__ next to the script the role installs.
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("memory_floor", SCRIPT)
spec = importlib.util.spec_from_loader("memory_floor", loader)
mf = importlib.util.module_from_spec(spec)
loader.exec_module(mf)


def check(name, got, expected):
    assert got == expected, f"{name}: got {got}, expected {expected}"
    print(f"ok  {name}")


def test_plan():
    total, chunk = 12 * GIB, 64 * MIB
    target = total * 30 / 100            # 3.6 GiB, 57.6 chunks

    def plan(others, held):
        return mf.plan(total, total - others - held * chunk, held, chunk, 30)

    check("idle host: hold up to the target", plan(1 * GIB, 0), 41)
    check("idle host, rounded up: keep", plan(1 * GIB, 42), 42)
    check("idle host, two over: shed one", plan(1 * GIB, 43), 42)
    check("nothing held, nothing running", plan(0, 0), 57)
    check("held exactly at the target", plan(0, 57), 57)
    # The real load grows by 2 GiB: hand back what it now uses itself.
    check("load grows: release", plan(3 * GIB, 41), 10)
    check("load past the target: release all", plan(target + MIB, 20), 0)
    check("load above the target: hold nothing", plan(10 * GIB, 30), 0)
    check("load above the target, nothing held", plan(10 * GIB, 0), 0)
    # Swings smaller than a chunk do not move the count either way.
    held = plan(1 * GIB, 0)
    for others in (1 * GIB + 10 * MIB, 1 * GIB - 10 * MIB, 1 * GIB + 30 * MIB):
        check(f"swing of {(others - GIB) // MIB} MiB: keep",
              plan(others, held), held)
    used = (1 * GIB + plan(1 * GIB, 0) * chunk) / total * 100
    assert 29 < used <= 30, used
    print(f"ok  idle host ends at {used:.1f}% used")


def test_alarm():
    alarm = mf.Alarm(20, after=3600, retry=300)
    check("above the limit", alarm.update(0, 25), False)
    check("just below", alarm.update(100, 19), False)
    check("below for 59 min", alarm.update(100 + 3599, 19), False)
    check("below for 1 h: warn", alarm.update(100 + 3600, 19), True)
    alarm.done(3700, ok=False)
    check("not sent: wait before trying again", alarm.update(3800, 19), False)
    check("not sent: try again", alarm.update(4000, 19), True)
    alarm.done(4000, ok=True)
    check("sent: once per episode", alarm.update(9000, 15), False)
    check("back above", alarm.update(9100, 30), False)
    check("below again: new episode, not yet", alarm.update(9200, 10), False)
    check("new episode: warn again", alarm.update(9200 + 3600, 10), True)
    check("a brief rise resets the hour",
          [mf.Alarm(20, 3600).update(t, p) for t, p in
           ((0, 10), (1800, 25), (3600, 10), (5400, 10))], [False] * 4)


class Ntfy(BaseHTTPRequestHandler):
    posts = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        Ntfy.posts.append((self.path, dict(self.headers), body))
        self.send_response(Ntfy.status)
        self.end_headers()

    def log_message(self, *_):
        pass


def write_meminfo(path, total, available):
    with open(path, "w") as f:
        f.write(f"MemTotal:       {total // 1024} kB\n"
                f"MemFree:        {available // 2048} kB\n"
                f"MemAvailable:   {available // 1024} kB\n"
                f"Buffers:        0 kB\n")


def test_keeper():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/secret-topic"
    with tempfile.TemporaryDirectory() as tmp:
        meminfo = os.path.join(tmp, "meminfo")
        conf = mf.settings({
            "MEMORY_FLOOR_TARGET_PERCENT": "30",
            "MEMORY_FLOOR_ALERT_AFTER": "60",
            "MEMORY_FLOOR_CHUNK_MB": "1",
            "MEMORY_FLOOR_NTFY_URL": url,
            "MEMORY_FLOOR_HOST": "ci-test",
            "MEMORY_FLOOR_MEMINFO": meminfo,
        })
        # A 20 MiB host: the target is 6 MiB used.
        total, chunk = 20 * MIB, conf["chunk"]
        keeper = mf.Keeper(conf)

        def step(now, others):
            write_meminfo(meminfo, total,
                          total - others - len(keeper.ballast) * chunk)
            return keeper.step(now)

        check("settings read", (conf["target"], conf["alert"], conf["after"],
                                conf["interval"]), (30, 20, 60, 10))
        check("starts by holding the missing memory", step(0, 2 * MIB), 4)
        check("the memory is really held",
              [len(m) for m in keeper.ballast.chunks], [chunk] * 4)
        check("steady", step(10, 2 * MIB), 4)
        check("load grows: releases", step(20, 5 * MIB), 1)
        check("load passes the target: releases all", step(30, 8 * MIB), 0)
        check("load gone: holds again", step(40, 2 * MIB), 4)
        check("no warning while above 20%", Ntfy.posts, [])

        # memory-floor cannot reach the target (here: told to aim at 10%).
        keeper.conf["target"] = 10
        check("lower target: releases", step(100, 1 * MIB), 1)
        step(101, 1 * MIB)
        step(160, 1 * MIB)
        check("below 20% for under the limit: quiet", Ntfy.posts, [])
        step(161, 1 * MIB)
        check("below 20% for the limit: one post", len(Ntfy.posts), 1)
        path, headers, body = Ntfy.posts[0]
        check("posted to the topic", path, "/secret-topic")
        check("title", headers["Title"], "ci-test: memory use below 20%")
        assert "ci-test" in body and "memory-floor" in body, body
        step(300, 1 * MIB)
        check("once per episode", len(Ntfy.posts), 1)

        # The topic answers an error: try again 5 minutes later.
        Ntfy.posts.clear()
        Ntfy.status = 500
        step(400, 10 * MIB)
        step(500, 1 * MIB)
        step(560, 1 * MIB)
        check("refused post", len(Ntfy.posts), 1)
        step(600, 1 * MIB)
        check("no retry within 5 minutes", len(Ntfy.posts), 1)
        Ntfy.status = 200
        step(860, 1 * MIB)
        check("retried after 5 minutes", len(Ntfy.posts), 2)
        step(900, 1 * MIB)
        check("then quiet", len(Ntfy.posts), 2)

        keeper.ballast.resize(0)
    server.shutdown()


if __name__ == "__main__":
    test_plan()
    test_alarm()
    test_keeper()
    print("all memory-floor checks passed")
