#!/usr/bin/env python3
"""preview-limit: which previews it stops, against a stand-in for `docker`.

    python3 tests/preview_limit.py

Loads roles/ci_previews/files/preview-limit as it is installed, checks its
pieces, then runs it whole with a fake `docker` first on PATH that answers
`ps` and `inspect` from a JSON file and logs every `stop`. Offline.
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "roles/ci_previews/files/preview-limit")

sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("preview_limit", SCRIPT)
spec = importlib.util.spec_from_loader("preview_limit", loader)
pl = importlib.util.module_from_spec(spec)
loader.exec_module(pl)

FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_DOCKER_STATE"]
containers = json.load(open(state))
command, args = sys.argv[1], sys.argv[2:]
if command == "ps":
    print("\\n".join(c["Id"] for c in containers
                     if c["State"]["Running"] and "coolify.pullRequestId" in c["Config"]["Labels"]))
elif command == "inspect":
    print(json.dumps([c for c in containers if c["Id"] in args]))
elif command == "stop":
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
        log.write(" ".join(args) + "\\n")
else:
    sys.exit("unexpected: " + " ".join(sys.argv))
"""


def check(name, got, expected):
    assert got == expected, f"{name}: got {got!r}, expected {expected!r}"
    print(f"ok  {name}")


def container(name, started, app="1", pr="0", kind="application", running=True):
    labels = {"coolify.type": kind, "coolify.applicationId": app}
    if pr is not None:
        labels["coolify.pullRequestId"] = pr
    return {"Id": "id-" + name, "Name": "/" + name, "Config": {"Labels": labels},
            "State": {"Running": running, "StartedAt": started}}


def test_pieces():
    check("Z time", pl.started_at("2026-11-15T10:00:00Z"), 1794736800.0)
    check("nanoseconds", pl.started_at("2026-11-15T10:00:00.5Z"), 1794736800.5)
    # Docker trims trailing zeros: .12 is before .123, which a text sort gets wrong.
    check("trimmed fractions compare as numbers",
          pl.started_at("2026-11-15T10:00:00.12Z") < pl.started_at("2026-11-15T10:00:00.123Z"), True)
    check("offset", pl.started_at("2026-11-15T07:00:00-03:00"), 1794736800.0)
    check("never started", pl.started_at("0001-01-01T00:00:00Z") < 0, True)
    check("garbage", pl.started_at("soon"), 0.0)

    previews = pl.running_previews([
        container("app1", "2026-11-15T09:00:00Z"),                              # main, pr 0
        container("app1-pr-7", "2026-11-15T10:00:00Z", pr="7"),
        container("app1-pr-7-worker", "2026-11-15T10:05:00Z", pr="7"),
        container("app2-pr-7", "2026-11-15T08:00:00Z", app="2", pr="7"),     # same PR number, other app
        container("svc-pr-3", "2026-11-15T11:00:00Z", pr="3", kind="service"),  # not an application
        container("odd", "2026-11-15T11:00:00Z", pr="x"),
    ])
    check("previews are an app and a PR", sorted(previews), [("1", 7), ("2", 7)])
    check("a preview's containers together", sorted(previews[("1", 7)]["containers"]),
          ["app1-pr-7", "app1-pr-7-worker"])
    check("started when its newest container started", previews[("1", 7)]["started"],
          pl.started_at("2026-11-15T10:05:00Z"))
    check("within the limit nothing stops", pl.over_the_limit(previews, 2), [])
    check("past it, the oldest stops", [key for key, _ in pl.over_the_limit(previews, 1)], [("2", 7)])


def run(directory, containers, limit):
    state = os.path.join(directory, "state.json")
    log = os.path.join(directory, "stops")
    with open(state, "w") as file:
        json.dump(containers, file)
    open(log, "w").close()
    env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"],
               FAKE_DOCKER_STATE=state, FAKE_DOCKER_LOG=log)
    result = subprocess.run([sys.executable, "-I", SCRIPT, str(limit)], env=env,
                            capture_output=True, text=True)
    with open(log) as file:
        return result, file.read().splitlines()


def test_whole(directory):
    fake = os.path.join(directory, "docker")
    with open(fake, "w") as file:
        file.write(FAKE_DOCKER)
    os.chmod(fake, 0o755)

    containers = [
        container("site", "2026-11-14T08:00:00Z", app="9"),
        container("site-pr-1", "2026-11-15T08:00:00Z", app="9", pr="1"),
        container("luotsi-pr-40", "2026-11-15T09:00:00Z", app="5", pr="40"),
        container("luotsi-pr-41", "2026-11-15T10:00:00Z", app="5", pr="41"),
        container("luotsi-pr-42", "2026-11-15T11:00:00Z", app="5", pr="42"),
        container("luotsi-pr-39", "2026-11-13T11:00:00Z", app="5", pr="39", running=False),
        container("runner", "2026-11-10T11:00:00Z", pr=None, kind="none"),
    ]
    result, stops = run(directory, containers, 3)
    check("whole: runs", result.returncode, 0)
    check("whole: the oldest preview past 3 stops", stops, ["site-pr-1"])
    check("whole: and says so", "stopping the preview of PR 1 (application 9: site-pr-1)" in result.stdout, True)

    result, stops = run(directory, containers, 1)
    check("whole: limit 1 keeps the newest", stops, ["luotsi-pr-41", "luotsi-pr-40", "site-pr-1"])

    result, stops = run(directory, containers, 4)
    check("whole: within the limit", (result.returncode, stops), (0, []))

    result, stops = run(directory, [container("site", "2026-11-14T08:00:00Z")], 1)
    check("whole: no preview at all", (result.returncode, stops), (0, []))

    result, stops = run(directory, containers, 0)
    check("whole: a limit of 0 is refused", (result.returncode != 0, stops), (True, []))


def main():
    test_pieces()
    with tempfile.TemporaryDirectory() as directory:
        test_whole(directory)
    print("all ok")


if __name__ == "__main__":
    main()
