#!/usr/bin/env python3
"""Every task that only reads also runs in check mode.

    python3 tests/check_mode.py

In check mode (`ansible-playbook site.yml --check --diff`) Ansible skips
`uri` calls and `command`/`shell` tasks, so a read that later tasks use would
register nothing. A task that only reads must say `check_mode: false`:

  - a `uri` task with no `method`, or `method: GET`;
  - a `command` or `shell` task with `changed_when: false`.

And never on any other `uri`, `command` or `shell` task: those may change
something.

Left out: a task (or its block) with `when: not ansible_check_mode`, whose
dependents skip in check mode too (SKIPPED_FILES: the files included from
such a block), and the checks in PROBES below, which
verify what the run has just changed and so mean nothing in check mode.
Offline: reads the task files only.
"""
import glob
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Checks of the result of a real run: skipped in check mode on purpose.
PROBES = {
    "Check that the other preview zones ask for a login",
    "Check that the public zones reach ci-1 and nothing but previews",
    "Check that Traefik refuses the protected zones without a token",
    "Check that Traefik refuses the tailnet-only zones through Cloudflare",
}

# Included only from coolify_apps' "Configure the applications" block, which
# skips in check mode: its API token is minted by the run.
SKIPPED_FILES = {
    "roles/coolify_apps/tasks/app.yml",
    "roles/coolify_apps/tasks/env.yml",
    "roles/coolify_apps/tasks/preview_app.yml",
}

READ_COMMANDS = {"command", "shell"}


def skipped_in_check_mode(task):
    when = task.get("when")
    conditions = when if isinstance(when, list) else [when]
    return any(isinstance(c, str) and "not ansible_check_mode" in c for c in conditions)


def module(task):
    for key in task:
        short = key.split(".")[-1]
        if short in READ_COMMANDS or short == "uri":
            return short, task[key]
    return None, None


def walk(tasks, path, problems, inherited_skip=False):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        skip = inherited_skip or skipped_in_check_mode(task)
        for section in ("block", "rescue", "always"):
            if section in task:
                walk(task[section], path, problems, skip)
        name, args = module(task)
        if name is None or task.get("name") in PROBES:
            continue
        if skip:
            if task.get("check_mode") is False:
                problems.append(f"{os.path.relpath(path, ROOT)}: {task.get('name')!r} skips in check mode: drop check_mode: false")
            continue
        if name == "uri":
            method = str(args.get("method", "GET")) if isinstance(args, dict) else "GET"
            reads = method.upper() == "GET"
        else:
            reads = task.get("changed_when") is False
        where = f"{os.path.relpath(path, ROOT)}: {task.get('name')!r}"
        if reads and task.get("check_mode") is not False:
            problems.append(f"{where} only reads: add check_mode: false")
        elif not reads and task.get("check_mode") is False:
            problems.append(f"{where} may change something: never check_mode: false")


def main():
    problems = []
    files = sorted(glob.glob(os.path.join(ROOT, "roles/*/tasks/*.yml")))
    for path in files:
        if os.path.relpath(path, ROOT) in SKIPPED_FILES:
            continue
        with open(path) as f:
            walk(yaml.safe_load(f), path, problems)
    for problem in problems:
        print(problem)
    if problems:
        sys.exit(1)
    print(f"ok: {len(files)} task files, every read runs in check mode")


if __name__ == "__main__":
    main()
