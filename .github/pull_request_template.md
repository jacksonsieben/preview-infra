Closes #

## What changed

<!-- One or two sentences. Which role or file, and why. -->

## How to test

The operator should be able to follow this from a phone without reading the code.

**Preview:** <!-- Paste the preview link, or "Coolify bot comment on this PR" (the preview is built and linked there), or "No preview" -->

<!-- If there is no preview, replace the steps below with the one command and the expected output, e.g.:
Run: `ansible-playbook site.yml --syntax-check`
Expected: `playbook: site.yml` and exit code 0. -->

**Setup / test data:** <!-- Anything needed before the steps (log in, create a profile, which secrets). "None" if not needed. -->

**Steps:**

1. <!-- Exact URL or path to open, or the exact API call: method, path, headers, copy-paste curl -->
   **Expected:** <!-- status code, visible text or element, JSON fields and values -->
2. <!-- next step -->
   **Expected:** <!-- ... -->

## Regressions

<!-- What else must still work, and what must not change. "None" if you checked nothing else. -->

- [ ] <!-- e.g. /pt-BR/cv still renders -->

## Risk checklist (infra)

<!-- Tick each one this PR changes, otherwise leave it unticked. Ticked items must be explained above. -->

- [ ] Changes sshd (`/etc/ssh/sshd_config`, `Match` blocks, root login)
- [ ] Changes ufw rules
- [ ] Changes DNS records (Cloudflare API)
- [ ] Changes the Cloudflare tunnel (routes, ingress, token, cloudflared)

<!-- If any box is ticked: say what the operator should check by hand after a real playbook run (which host or service, the URL or command, and the expected output). The playbook is never run from a PR; only the syntax check is. -->
