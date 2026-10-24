#!/usr/bin/env python3
"""github-runner-job against a fake GitHub API and a fake `docker`.

    python3 tests/github_runner_job.py

Runs roles/github_runner/files/github-runner-job as it is installed, with its
settings in a temporary directory (GITHUB_RUNNER_CONFIG_DIR) and `docker`
replaced by a script that logs each call. Offline: nothing outside this process
is called or changed. Checks that only private repositories (and organization
runner groups closed to public ones) get a runner, before every registration
and while the runner waits or runs, that the just-in-time config reaches only
`compose up`, that the registration is removed when the job ends or the
service stops, and the tool cache modes.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB = os.path.join(ROOT, "roles/github_runner/files/github-runner-job")
TOKEN, JIT = "fake-runner-token", "fake-jit-config"
REFUSED = 78

# Logs every call as a JSON line. `compose up` runs until `compose down` is
# called or FAKE_UP_SECONDS pass, like a runner waiting for its job.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "jit": os.environ.get("ACTIONS_RUNNER_INPUT_JITCONFIG")}) + "\n")
marker = os.environ["FAKE_DOCKER_LOG"] + ".down"
if args[:1] == ["info"]:
    print(os.environ["FAKE_DOCKER_ROOT"])
elif "down" in args:
    open(marker, "w").close()
elif "up" in args:
    if os.path.exists(marker):
        os.remove(marker)
    end = time.time() + float(os.environ.get("FAKE_UP_SECONDS", "0"))
    while time.time() < end and not os.path.exists(marker):
        time.sleep(0.1)
'''


class FakeGitHub:
    """The repository, runner group and self-hosted runner endpoints."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.requests = []
        self.repos = {"you/private": "private", "you/public": "public"}
        self.groups = {"acme": False, "open": True}
        self.post_delay = 0
        self.get_errors = 0
        # After the registration: the repository or group as it becomes.
        self.after_post = None

    def calls(self, method):
        return [r for r in self.requests if r[0] == method]

    def handle(self, method, path, token, body):
        with self.lock:
            self.requests.append((method, path, token, body))
        if token != f"Bearer {TOKEN}":
            return 401, {"message": "Bad credentials"}
        parts = path.strip("/").split("/")
        if method == "GET" and self.get_errors:
            self.get_errors -= 1
            return 500, {"message": "Server Error"}
        if method == "GET" and parts[0] == "repos":
            visibility = self.repos.get("/".join(parts[1:3]))
            if visibility is None:
                return 404, {"message": "Not Found"}
            return 200, {"private": visibility != "public", "visibility": visibility}
        if method == "GET" and parts[0] == "orgs" and parts[2:] == ["actions", "runner-groups", "1"]:
            if parts[1] not in self.groups:
                return 404, {"message": "Not Found"}
            return 200, {"id": 1, "allows_public_repositories": self.groups[parts[1]]}
        if method == "POST" and parts[-1] == "generate-jitconfig":
            time.sleep(self.post_delay)
            if self.after_post:
                self.after_post()
            return 201, {"runner": {"id": 7, "name": body["name"]}, "encoded_jit_config": JIT}
        if method == "DELETE" and parts[-2:] == ["runners", "7"]:
            return 204, None
        return 404, {"message": "Not Found"}


def serve(api):
    class Handler(BaseHTTPRequestHandler):
        def do(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, reply = api.handle(self.command, self.path, self.headers.get("Authorization"), body)
            self.send_response(status)
            self.end_headers()
            if reply is not None:
                self.wfile.write(json.dumps(reply).encode())

        do_GET = do_POST = do_DELETE = do

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Job:
    """Runs github-runner-job for one instance with the given settings."""

    def __init__(self, tmp, port):
        self.tmp = tmp
        self.config_dir = os.path.join(tmp, "etc")
        self.data_dir = os.path.join(tmp, "data")
        bin_dir = os.path.join(tmp, "bin")
        for d in (self.config_dir, self.data_dir, bin_dir):
            os.makedirs(d)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(FAKE_DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        with open(os.path.join(self.config_dir, "token"), "w") as f:
            f.write(TOKEN + "\n")
        self.port = port
        self.docker_log = os.path.join(tmp, "docker.log")
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"],
                        GITHUB_RUNNER_CONFIG_DIR=self.config_dir,
                        FAKE_DOCKER_LOG=self.docker_log, FAKE_DOCKER_ROOT=tmp)

    def start(self, instance, up_seconds=0, **settings):
        config = {
            "api": f"http://127.0.0.1:{self.port}",
            "instances": {"you-private-1": "repos/you/private", "you-public-1": "repos/you/public",
                          "you-gone-1": "repos/you/gone", "acme-1": "orgs/acme", "open-1": "orgs/open"},
            "labels": ["self-hosted", "linux", "x64", "preview-host"],
            "runner_group_id": 1, "name_prefix": "preview-host", "data_dir": self.data_dir,
            "uid": os.getuid(), "min_free_disk_gb": 0, "tool_cache_max_gb": 5,
            "visibility_check_seconds": 300,
        }
        config.update(settings)
        with open(os.path.join(self.config_dir, "config.json"), "w") as f:
            json.dump(config, f)
        open(self.docker_log, "w").close()
        env = dict(self.env, FAKE_UP_SECONDS=str(up_seconds))
        return subprocess.Popen([sys.executable, JOB, instance], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def run(self, instance, **kwargs):
        proc = self.start(instance, **kwargs)
        out = proc.communicate(timeout=60)[0]
        return proc.returncode, out

    def docker(self):
        with open(self.docker_log) as f:
            return [json.loads(line) for line in f]

    def compose(self):
        """The compose subcommands, in order: down, up, down..."""
        return [c["args"][c["args"].index("compose") + 5] for c in self.docker() if "compose" in c["args"]]


FAILED = []


def check(ok, message, output=None):
    print(("ok    " if ok else "FAIL  ") + message)
    if not ok:
        FAILED.append(message)
        if output:
            print(output[-4000:])


def wait_for(condition, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


def main():
    api = FakeGitHub()
    server = serve(api)
    with tempfile.TemporaryDirectory() as tmp:
        job = Job(tmp, server.server_port)

        print("# 1. a private repository")
        rc, out = job.run("you-private-1")
        check(rc == 0, "the job runs and exits 0", out)
        check([r[0] for r in api.requests] == ["GET", "POST", "DELETE"],
              "checks, registers, then removes the registration", out)
        post = api.calls("POST")[0]
        check(post[1] == "/repos/you/private/actions/runners/generate-jitconfig"
              and post[3]["labels"] == ["self-hosted", "linux", "x64", "preview-host"]
              and post[3]["name"].startswith("preview-host-you-private-1-"),
              "registers on the repository, with the labels")
        check(job.compose() == ["down", "up", "down"], "clears leftovers, runs the job, removes it")
        check([c["args"] for c in job.docker() if c["jit"] == JIT] == [c["args"] for c in job.docker() if "up" in c["args"]],
              "the just-in-time config reaches only compose up")
        check(TOKEN not in out and JIT not in out, "neither the token nor the config is printed", out)

        print("# 2. repositories that are not private")
        for instance, why in (("you-public-1", "is not private"), ("you-gone-1", "HTTP 404")):
            api.reset()
            rc, out = job.run(instance)
            check(rc == REFUSED and why in out and not api.calls("POST") and "up" not in job.compose(),
                  f"{instance}: refused with {REFUSED}, never registered", out)
        api.reset()
        rc, out = job.run("nowhere-1")
        check(rc == REFUSED and not api.requests, "an instance not in config.json is refused", out)

        print("# 3. organizations")
        api.reset()
        rc, out = job.run("open-1")
        check(rc == REFUSED and "allows public repositories" in out and not api.calls("POST"),
              "a runner group open to public repositories is refused", out)
        api.reset()
        rc, out = job.run("acme-1")
        post = api.calls("POST")
        check(rc == 0 and post and post[0][1] == "/orgs/acme/actions/runners/generate-jitconfig"
              and post[0][3]["runner_group_id"] == 1, "a closed group gets a runner in group 1", out)

        print("# 4. waiting for disk space")
        api.reset()
        proc = job.start("you-private-1", min_free_disk_gb=10 ** 9)
        time.sleep(1.5)
        idle = not api.requests and "up" not in job.compose()
        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=20)[0]
        check(idle and "waiting for" in out, "nothing is checked or registered before there is room", out)
        check(proc.returncode != REFUSED and not api.requests, "a stop while waiting registers nothing", out)

        print("# 5. a repository made public while its runner waits")
        api.reset()
        api.after_post = lambda: api.repos.update({"you/private": "public"})
        started = time.time()
        rc, out = job.run("you-private-1", up_seconds=30, visibility_check_seconds=1)
        check(rc == REFUSED and "is not private" in out and time.time() - started < 15,
              f"the runner is stopped within a check and exits {REFUSED}", out)
        check(job.compose()[:3] == ["down", "up", "down"] and api.calls("DELETE"),
              "its containers and its registration are removed", out)

        print("# 6. an organization's group opened while its runner waits")
        api.reset()
        api.after_post = lambda: api.groups.update({"acme": True})
        rc, out = job.run("acme-1", up_seconds=30, visibility_check_seconds=1)
        check(rc == REFUSED and "allows public repositories" in out and api.calls("DELETE"),
              "the runner is stopped and removed", out)

        print("# 7. a failed check while the job runs")
        api.reset()
        api.after_post = lambda: setattr(api, "get_errors", 1)
        rc, out = job.run("you-private-1", up_seconds=3, visibility_check_seconds=1)
        check(rc == 0 and "trying again later" in out and job.compose() == ["down", "up", "down"],
              "an API error does not stop the job; the next check passes", out)

        print("# 8. stopped while registering and while running")
        for when in ("registering", "running"):
            api.reset()
            api.post_delay = 1.5 if when == "registering" else 0
            proc = job.start("you-private-1", up_seconds=30)
            if when == "registering":
                wait_for(lambda: api.calls("POST"))
                time.sleep(0.3)
            else:
                wait_for(lambda: "up" in job.compose())
            proc.send_signal(signal.SIGTERM)
            out = proc.communicate(timeout=20)[0]
            check(proc.returncode == 128 + signal.SIGTERM and api.calls("DELETE")
                  and job.compose()[-1] == "down", f"stopped while {when}: containers and registration removed", out)

        print("# 9. the tool cache")
        cached = os.path.join(job.data_dir, "you-private-1", "tool-cache", "node", "marker")
        for limit, kept in ((5, True), (0, False)):
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            open(cached, "w").close()
            api.reset()
            rc, out = job.run("you-private-1", tool_cache_max_gb=limit)
            check(rc == 0 and os.path.exists(cached) == kept,
                  f"tool_cache_max_gb {limit}: {'kept' if kept else 'emptied'} for the next job", out)
        work = os.path.join(job.data_dir, "you-private-1", "work", "left-by-a-job")
        open(work, "w").close()
        job.run("you-private-1")
        check(not os.path.exists(work), "the work directory is emptied for every job")

    server.shutdown()
    print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
