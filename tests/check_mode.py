#!/usr/bin/env python3
"""Every task that only reads also runs in check mode, and nothing else does.

    python3 tests/check_mode.py

In check mode (`ansible-playbook site.yml --check --diff`) Ansible skips
`uri` calls and `command`/`shell` tasks, so a read that later tasks use would
register nothing. `check_mode: false` runs a task anyway, so it goes only on
tasks that change nothing:

  - a `uri` task with no `method`, or `method: GET`: it must say
    `check_mode: false`, and no other `uri` task may;
  - a `command` or `shell` task: only with a `# read-only: <why>` comment in
    the comments right above the task, and that comment only with
    `check_mode: false`. `changed_when: false` is no proof: it only says how a
    task is reported (Coolify's API session mints tokens and says it).

Left out: a task (or its block) whose `when` is exactly `not ansible_check_mode`
(or has it as a list item), whose dependents skip in check mode too
(SKIPPED_FILES: the files included from such a block), and the checks in
PROBES below, which verify what the run has just changed and so mean nothing
in check mode. Reads every .yml and .yaml file under roles/*/tasks and
roles/*/handlers. Offline: reads the files only.
"""
import glob
import os
import re
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
}

COMMANDS = {"command", "shell"}
SKIP_CONDITION = "not ansible_check_mode"
MARKER = re.compile(r"^#\s*read-only:\s*\S")
LINE = "__line__"


class Loader(yaml.SafeLoader):
    """Keeps the line of each mapping, and reads Ansible's tags as plain data."""

    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep=deep)
        mapping[LINE] = node.start_mark.line
        return mapping


Loader.add_multi_constructor(
    "!", lambda loader, suffix, node: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else None
)


def skipped_in_check_mode(task):
    when = task.get("when")
    conditions = when if isinstance(when, list) else [when]
    return any(isinstance(c, str) and " ".join(c.split()) == SKIP_CONDITION for c in conditions)


def module(task):
    for key in task:
        short = str(key).split(".")[-1]
        if short in COMMANDS or short == "uri":
            return short, task[key]
    return None, None


def marked_read_only(task, lines):
    """A `# read-only:` comment among the comment lines right above the task."""
    i = task[LINE] - 1
    while i >= 0 and lines[i].strip().startswith("#"):
        if MARKER.match(lines[i].strip()):
            return True
        i -= 1
    return False


def walk(tasks, path, lines, problems, inherited_skip=False):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        skip = inherited_skip or skipped_in_check_mode(task)
        for section in ("block", "rescue", "always"):
            if section in task:
                walk(task[section], path, lines, problems, skip)
        name, args = module(task)
        if name is None or task.get("name") in PROBES:
            continue
        where = f"{os.path.relpath(path, ROOT)}:{task[LINE] + 1}: {task.get('name')!r}"
        runs = task.get("check_mode") is False
        marked = name in COMMANDS and marked_read_only(task, lines)
        if skip:
            if runs:
                problems.append(f"{where} skips in check mode: drop check_mode: false")
            continue
        if name == "uri":
            method = str(args.get("method", "GET")) if isinstance(args, dict) else "GET"
            if method.upper() == "GET" and not runs:
                problems.append(f"{where} only reads: add check_mode: false")
            elif method.upper() != "GET" and runs:
                problems.append(f"{where} may change something: never check_mode: false")
        elif runs and not marked:
            problems.append(
                f"{where} has check_mode: false: if it only reads, say why in a "
                "'# read-only: ...' comment right above it; if not, drop check_mode: false"
            )
        elif marked and not runs:
            problems.append(f"{where} is marked read-only: add check_mode: false, or drop the comment")


def task_files():
    found = set()
    for kind in ("tasks", "handlers"):
        for ext in ("yml", "yaml"):
            found.update(glob.glob(os.path.join(ROOT, "roles", "*", kind, "**", f"*.{ext}"), recursive=True))
    return sorted(found)


def main():
    problems = []
    files = task_files()
    for path in files:
        if os.path.relpath(path, ROOT) in SKIPPED_FILES:
            continue
        with open(path) as f:
            text = f.read()
        walk(yaml.load(text, Loader=Loader), path, text.splitlines(), problems)
    for problem in problems:
        print(problem)
    if problems:
        sys.exit(1)
    print(f"ok: {len(files)} task and handler files, every read runs in check mode, every command run is marked read-only")


if __name__ == "__main__":
    main()
