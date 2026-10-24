#!/usr/bin/env python3
"""preview_env and production_env against a fake Coolify API.

    python3 tests/coolify_env.py

Runs tests/coolify_env.yml (the coolify_apps role) several times against an
in-process stand-in for Coolify's REST API, with `docker` replaced by
tests/bin/docker. Offline: nothing outside this process is called or changed,
and group_vars/all/local.yml is not read. Checks that the role creates,
updates and deletes only its own variables, keeps preview and production
apart, that a second run changes nothing, and that no value or the token that
reads them shows in the output of `ansible-playbook --diff -v`.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN, ENV_TOKEN = "fake-token", "fake-env-token"
SECRETS = {
    # Templating it would make 42; Coolify must get it as written.
    "FAKE_PREVIEW_CODE": "sentinel-code-a-{{ 7 * 6 }}-$HOME",
    "FAKE_PREVIEW_CODE_2": "sentinel-code-b-7f3a",
    "FAKE_PROD_SECRET": "sentinel-prod-9c1e",
}
SEEDED = {"old": "sentinel-old-value-55", "hand": "sentinel-hand-made-31"}
COMMENT = "Managed by preview-infra (preview_env/production_env in local.yml); deleted when taken out."
ENV_FIELDS = {"key", "value", "is_preview", "is_literal", "is_multiline", "is_shown_once",
              "is_runtime", "is_buildtime", "comment"}


class FakeCoolify:
    """Coolify's application and environment variable endpoints, as in v4.

    Like Coolify, it hides values from a token without read:sensitive, trims
    them, and copies a new production variable to the previews when they don't
    have its key.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.projects = []
        self.apps = []
        self.envs = {}

    def add_app(self, repo, name):
        app = {"uuid": "app-" + uuid.uuid4().hex[:8], "name": name, "git_repository": repo}
        self.apps.append(app)
        self.envs[app["uuid"]] = []
        return app["uuid"]

    def add_env(self, app_uuid, key, value, is_preview, comment=None):
        env = {"uuid": "env-" + uuid.uuid4().hex[:8], "key": key, "value": value.strip(),
               "is_preview": is_preview, "is_literal": False, "is_multiline": False,
               "is_shown_once": False, "is_runtime": True, "is_buildtime": True, "comment": comment}
        self.envs[app_uuid].append(env)
        return env

    def env(self, app_uuid, key, is_preview):
        found = [e for e in self.envs[app_uuid] if e["key"] == key and e["is_preview"] == is_preview]
        return found[0] if found else None

    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]

    def handle(self, method, path, token, body):
        path = path.split("?")[0].removeprefix("/api/v1")
        self.requests.append((method, path, token))
        if token not in (TOKEN, ENV_TOKEN):
            return 401, {"message": "Unauthenticated."}
        parts = path.strip("/").split("/")

        if (method, path) == ("GET", "/servers"):
            return 200, [{"uuid": "srv-1", "name": "localhost", "settings": {"is_reachable": True}}]
        if (method, path) == ("GET", "/github-apps"):
            return 200, [{"uuid": "gh-1", "name": "coolify-example", "installation_id": 1}]
        if (method, path) == ("GET", "/projects"):
            return 200, self.projects
        if (method, path) == ("POST", "/projects"):
            project = {"uuid": "prj-" + uuid.uuid4().hex[:8], "name": body["name"], "environments": []}
            self.projects.append(project)
            return 201, {"uuid": project["uuid"]}
        if parts[0] == "projects" and len(parts) == 3:
            project = next(p for p in self.projects if p["uuid"] == parts[1])
            if method == "POST" and parts[2] == "environments":
                project["environments"].append(body["name"])
                return 201, {"uuid": "envr-1"}
            return (200, {"name": parts[2]}) if parts[2] in project["environments"] else (404, {})
        if (method, path) == ("GET", "/applications"):
            return 200, [{k: a[k] for k in ("uuid", "name", "git_repository")} for a in self.apps]
        if (method, path) == ("POST", "/applications/private-github-app"):
            app_uuid = self.add_app(body["git_repository"], body.get("name", body["git_repository"]))
            app = self.apps[-1]
            app.update({k: body[k] for k in ("ports_exposes", "git_branch", "build_pack", "preview_url_template")})
            app["fqdn"] = body["domains"]
            app["settings"] = {k: body[k] for k in ("is_auto_deploy_enabled", "is_preview_deployments_enabled",
                                                    "is_pr_deployments_public_enabled")}
            app["created_with"] = body
            return 201, {"uuid": app_uuid, "domains": body["domains"]}

        if parts[0] != "applications" or len(parts) < 2:
            return 404, {"message": "Not found."}
        app = next((a for a in self.apps if a["uuid"] == parts[1]), None)
        if app is None:
            return 404, {"message": "Application not found"}
        if len(parts) == 2:
            if method == "GET":
                return 200, app
            if method == "PATCH":
                app.update(body)
                return 200, {"uuid": app["uuid"]}
        if parts[2:] == ["start"] and method == "POST":
            return 200, {"message": "Deployment request queued.", "deployment_uuid": "dep-1"}
        if parts[2] != "envs":
            return 404, {"message": "Not found."}

        envs = self.envs[app["uuid"]]
        if method == "GET" and len(parts) == 3:
            shown = [dict(e, real_value=e["value"]) for e in envs]
            if token != ENV_TOKEN:
                shown = [{k: v for k, v in e.items() if k not in ("value", "real_value")} for e in shown]
            return 200, shown
        if method == "DELETE" and len(parts) == 4:
            found = [e for e in envs if e["uuid"] == parts[3]]
            if not found:
                return 404, {"message": "Environment variable not found."}
            envs.remove(found[0])
            return 200, {"message": "Environment variable deleted."}
        if method in ("POST", "PATCH") and len(parts) == 3:
            extra = set(body) - ENV_FIELDS
            if extra:
                return 422, {"message": "Validation failed.", "errors": {k: "not allowed" for k in extra}}
            preview = body.get("is_preview", False)
            env = self.env(app["uuid"], body["key"], preview)
            if method == "POST":
                if env:
                    return 409, {"message": "Environment variable already exists. Use PATCH request to update it."}
                env = self.add_env(app["uuid"], body["key"], body.get("value") or "", preview, body.get("comment"))
                env.update({k: body[k] for k in ENV_FIELDS - {"key", "value", "comment"} if k in body})
                if not preview and not self.env(app["uuid"], body["key"], True):
                    copy = self.add_env(app["uuid"], env["key"], env["value"], True, env["comment"])
                    copy.update({k: env[k] for k in ("is_literal", "is_multiline", "is_runtime", "is_buildtime")})
                return 201, {"uuid": env["uuid"]}
            if not env:
                return 404, {"message": "Environment variable not found."}
            env.update({k: body[k] for k in ENV_FIELDS - {"key", "value"} if k in body})
            env["value"] = (body.get("value") or "").strip()
            return 201, {k: v for k, v in env.items() if k != "value"}
        return 404, {"message": "Not found."}


def serve(api):
    class Handler(BaseHTTPRequestHandler):
        def do(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            with api.lock:
                status, payload = api.handle(self.command, self.path, token, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_DELETE = do

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Run:
    def __init__(self, tmp, port):
        self.tmp, self.port, self.count = tmp, port, 0
        self.docker_log = os.path.join(tmp, "docker.log")

    def __call__(self, coolify_apps, preview_apps):
        self.count += 1
        extra = os.path.join(self.tmp, f"case{self.count}.json")
        with open(extra, "w") as f:
            json.dump({"coolify_api": f"http://127.0.0.1:{self.port}/api/v1", "domain": "example.com",
                       "coolify_github_app": "coolify-example",
                       "coolify_apps": coolify_apps, "preview_apps": preview_apps}, f)
        open(self.docker_log, "w").close()
        env = dict(os.environ, **SECRETS, PATH=os.path.join(ROOT, "tests", "bin") + os.pathsep + os.environ["PATH"],
                   FAKE_DOCKER_LOG=self.docker_log, ANSIBLE_CONFIG=os.path.join(ROOT, "ansible.cfg"),
                   ANSIBLE_ROLES_PATH=os.path.join(ROOT, "roles"), ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0")
        proc = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "tests/coolify_env.yml", "--diff", "-v", "-e", "@" + extra],
            cwd=ROOT, env=env, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        leaked = [s for s in list(SECRETS.values()) + list(SEEDED.values()) + [ENV_TOKEN] if s in out]
        check(not leaked, f"run {self.count}: no value or token in the output" + (f" (found {leaked})" if leaked else ""))
        with open(self.docker_log) as f:
            self.session = f.readline()
        return proc.returncode, out


FAILED = []


def check(ok, message, output=None):
    print(("ok    " if ok else "FAIL  ") + message)
    if not ok:
        FAILED.append(message)
        if output:
            print(output[-6000:])


def recap_changed(out):
    found = re.search(r"localhost\s+:.*changed=(\d+)", out)
    return int(found.group(1)) if found else None


def main():
    api = FakeCoolify()
    server = serve(api)
    myapp = api.add_app("you/myapp", "myapp")
    api.add_env(myapp, "HAND_MADE", SEEDED["hand"], False)
    api.add_env(myapp, "HAND_MADE", SEEDED["hand"], True)
    api.add_env(myapp, "LUOTSI_PREVIEW_CODE", SEEDED["old"], True, COMMENT)
    api.add_env(myapp, "OLD_FLAG", SEEDED["old"], True, COMMENT)
    hand_made = json.dumps([e for e in api.envs[myapp] if e["key"] == "HAND_MADE"], sort_keys=True)

    def preview_app(**env):
        return {"name": "myapp", "repository": "you/myapp", "zone": "example.dev", "port": 8740,
                "healthcheck_path": "/health", **env}

    # Not in Coolify yet, and sets nothing: skipped.
    not_yet = {"name": "notyet", "repository": "you/notyet", "zone": "example.dev", "port": 80,
               "healthcheck_path": "/"}

    def site(**env):
        return {"repo": "you/site", "project": "Site", "domains": ["site.example.com"], "port": 8080, **env}

    def scope(app_uuid, preview):
        return {e["key"]: e for e in api.envs[app_uuid] if e["is_preview"] == preview}

    def hand_made_untouched():
        return json.dumps([e for e in api.envs[myapp] if e["key"] == "HAND_MADE"], sort_keys=True) == hand_made

    with tempfile.TemporaryDirectory() as tmp:
        run = Run(tmp, server.server_address[1])
        lookup = "{{ lookup('env', '%s') }}"
        full = ([site(preview_env={"SITE_PREVIEW": "1"}, production_env={"SITE_MODE": lookup % "FAKE_PROD_SECRET"})],
                [preview_app(preview_env={"LUOTSI_PREVIEW": "1",
                                          "LUOTSI_PREVIEW_CODE": lookup % "FAKE_PREVIEW_CODE"}), not_yet])

        print("# 1. create, update, delete")
        rc, out = run(*full)
        check(rc == 0, "the run succeeds", out)
        check("read:sensitive" in run.session, "the session mints a token that reads values")
        siteapp = next((a["uuid"] for a in api.apps if a["git_repository"] == "you/site"), None)
        check(siteapp is not None, "you/site is created")
        if siteapp is None:
            return 1
        prev, prod = scope(myapp, True), scope(myapp, False)
        check(prev.get("LUOTSI_PREVIEW", {}).get("value") == "1", "LUOTSI_PREVIEW is created for previews")
        check(prev.get("LUOTSI_PREVIEW_CODE", {}).get("value") == SECRETS["FAKE_PREVIEW_CODE"],
              "LUOTSI_PREVIEW_CODE is updated, exactly as 1Password would give it")
        check(all(prev.get(k, {}).get("comment") == COMMENT for k in ("LUOTSI_PREVIEW", "LUOTSI_PREVIEW_CODE")),
              "the role's variables carry its comment")
        check(all(prev.get(k, {}).get("is_literal") and prev[k]["is_runtime"] and not prev[k]["is_buildtime"]
                  for k in ("LUOTSI_PREVIEW", "LUOTSI_PREVIEW_CODE")), "they are literal and runtime-only")
        check("OLD_FLAG" not in prev, "OLD_FLAG, the role's and no longer listed, is deleted")
        check(not {"LUOTSI_PREVIEW", "LUOTSI_PREVIEW_CODE"} & set(prod), "production of myapp has no LUOTSI_*")
        check(hand_made_untouched(), "HAND_MADE, made by hand, is untouched")
        check(set(scope(siteapp, True)) == {"SITE_PREVIEW"}, "previews of you/site get only SITE_PREVIEW "
              "(Coolify's copy of SITE_MODE is deleted)")
        check(set(scope(siteapp, False)) == {"SITE_MODE"} and
              scope(siteapp, False)["SITE_MODE"].get("value") == SECRETS["FAKE_PROD_SECRET"],
              "production of you/site gets only SITE_MODE")
        app = next(a for a in api.apps if a["uuid"] == siteapp)
        check(app["created_with"]["instant_deploy"] is False, "you/site is created without deploying")
        site_writes = [(m, p) for m, p, _ in api.writes() if siteapp in p]
        check(site_writes and site_writes[-1] == ("POST", f"/applications/{siteapp}/start"),
              "and deployed after its variables are set")
        check(all("/envs" in p for _, p, t in api.requests if t == ENV_TOKEN),
              "the token that reads values is used for environment variables only")

        print("# 2. the same again")
        before = len(api.requests)
        rc, out = run(*full)
        writes = [r for r in api.requests[before:] if r[0] != "GET"]
        check(rc == 0 and not writes, f"nothing is written (writes: {writes})", out)
        check(recap_changed(out) == 0, "and the recap says changed=0", out)

        print("# 3. a value changes, variables are taken out")
        changed = ([site()], [preview_app(preview_env={"LUOTSI_PREVIEW_CODE": lookup % "FAKE_PREVIEW_CODE_2"})])
        rc, out = run(*changed)
        check(rc == 0, "the run succeeds", out)
        prev = scope(myapp, True)
        check(prev.get("LUOTSI_PREVIEW_CODE", {}).get("value") == SECRETS["FAKE_PREVIEW_CODE_2"],
              "LUOTSI_PREVIEW_CODE gets the new value")
        check("LUOTSI_PREVIEW" not in prev, "LUOTSI_PREVIEW is deleted")
        check(not api.envs[siteapp], "you/site has no variables left")
        check(hand_made_untouched(), "HAND_MADE is untouched")
        check("Updated LUOTSI_PREVIEW_CODE (preview)" in out and "Deleted LUOTSI_PREVIEW (preview)" in out,
              "the output names what changed", out)
        before = len(api.requests)
        rc, out = run(*changed)
        check(rc == 0 and not [r for r in api.requests[before:] if r[0] != "GET"], "a second run writes nothing", out)

        print("# 4. a key made by hand")
        before = len(api.requests)
        rc, out = run([site()], [preview_app(production_env={"HAND_MADE": "x"})])
        check(rc != 0 and "HAND_MADE (production)" in out and "made by hand" in out,
              "the run stops and names it", out)
        check(not [r for r in api.requests[before:] if r[0] != "GET"] and hand_made_untouched(),
              "and changes nothing")

        print("# 5. a key that is not a name")
        rc, out = run([site()], [preview_app(preview_env={"1BAD": "x"})])
        check(rc != 0 and "1BAD" in out and "not a valid name" in out, "the run stops and names it", out)

        print("# 6. no app sets any")
        rc, out = run([site()], [preview_app(), not_yet])
        check(rc == 0, "the run succeeds", out)
        check("read:sensitive" not in run.session, "the session's token can't read values")
        check(not [e for e in api.envs[myapp] if e["comment"] == COMMENT], "the role's last variable is deleted")
        check(hand_made_untouched(), "HAND_MADE is untouched")

        print("# 7. an app that sets some is not in Coolify")
        rc, out = run([site()], [dict(not_yet, preview_env={"X": "1"})])
        check(rc != 0 and "no single application for you/notyet" in out, "the run stops and says so", out)

    server.shutdown()
    print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
