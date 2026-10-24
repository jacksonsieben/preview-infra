# preview-infra

Vercel-style preview deployments on a machine you own. Every pull request on
a connected repository is built from its `Dockerfile`, served at its own URL
(`https://pr-36-preview.example.com`), and linked in a PR comment. The
preview is removed when the PR closes.

- **[Coolify](https://coolify.io)** builds and runs the previews and comments
  on the PR (through its GitHub App).
- **Cloudflare Tunnel** publishes the machine without opening a router port or
  needing a fixed IP. Cloudflare terminates HTTPS.
- **Tailscale Serve** puts the Coolify dashboard on your tailnet only. The
  tunnel exposes just the webhook path GitHub needs.
- **This playbook** installs both and sets up the tunnel routes and DNS
  through the Cloudflare API, so you can rebuild the whole thing on another
  machine with one command.
- **A GitHub Actions runner** (optional) runs the Actions jobs of your private
  repositories here, one container per job, instead of on GitHub's minutes.

```
PR opened / pushed ─► GitHub App webhook ─► coolify.example.com/webhooks/ ─┐
                                                                            │ tunnel
https://pr-36-preview.example.com ─► Cloudflare ─► cloudflared ─► Traefik :80 ─► preview container

you, on the tailnet ─► https://<machine>.<tailnet>.ts.net ─► Tailscale Serve ─► Coolify dashboard
```

What Ansible does **not** do: the Coolify UI steps in
[Step 4](#step-4--connect-github-and-a-project) (GitHub App, projects). Those
are one-off clicks per project and are documented below.

---

## Step 1 — Pick the host

Coolify takes over ports 80, 443, 8000, 6001 and 6002 and installs Docker. It
supports Ubuntu, Debian, Arch and others. A spare machine or VM is better than
your daily desktop. The preview links work only while the host is on.

Edit `inventory/hosts.yml`: `ansible_connection: local` to run on the machine
you are sitting at, or `ansible_host`/`ansible_user` for a machine over SSH.

## Step 2 — Your settings and secrets

Everything specific to you lives in one file that git ignores:

```bash
cp local.example.yml group_vars/all/local.yml
```

Set `domain` to a zone on your Cloudflare account and fill in the secrets
below. Every other setting, with its default, is in
`group_vars/all/defaults.yml`; copy the ones you want to change into
`local.yml`.

**Keep a copy of `local.yml`.** It is the only part of the setup not in git.
With 1Password, store it as a document once, and fetch it on a new clone or
machine:

```bash
# save (or update) the copy
op document create group_vars/all/local.yml --vault Ansible --title "preview-infra local.yml"
op document edit "preview-infra local.yml" group_vars/all/local.yml --vault Ansible

# restore it
op document get "preview-infra local.yml" --vault Ansible --out-file group_vars/all/local.yml
```

**Cloudflare API token.** Cloudflare dashboard → My Profile → API Tokens →
Create Token → Custom token:

| Permission | Access |
|---|---|
| Account → Cloudflare One Connector: cloudflared | Write/Edit |
| Zone → DNS | Edit |
| Zone → Zone | Read |

The tunnel permission has had several names. Any one of these is enough:
*Cloudflare One Connector: cloudflared*, *Cloudflare One Connectors*, or the
older *Cloudflare Tunnel* (shown as *Argo Tunnel (legacy)* in some views).
*Network Services → Connectivity Services* is a different product and does not
cover tunnels.

The account permission applies to the whole account. Limit the zone
permissions to `example.com`.

**Coolify admin account.** The playbook creates it when it installs Coolify,
so nobody else can claim the registration page. The password needs upper and
lower case letters, a digit, and only `-` `_` `.` as symbols (Coolify's
installer writes it into a `.env` with `sed`).

Give the playbook both secrets in one of two ways:

| | Environment variables | 1Password (`op` CLI), set in `local.yml` |
|---|---|---|
| Cloudflare token | `CLOUDFLARE_API_TOKEN` | `cloudflare_api_token_ref: op://<vault>/<item>/credential` |
| Coolify account | `COOLIFY_ROOT_USERNAME`, `COOLIFY_ROOT_EMAIL`, `COOLIFY_ROOT_PASSWORD` | `coolify_root_ref: op://<vault>/<item>`, a Login item with `username` = email, `password`, and a `Name` field with the username |

The environment wins when both are set. References can use item names or
their IDs (`op item list` shows them); IDs survive renaming the item. The
Coolify account is only read on the first run, when Coolify gets installed.

**Tailscale** (with `coolify_dashboard: tailscale`, the default). The host must
be logged in to your tailnet (`tailscale up`). In the admin console →
[DNS](https://login.tailscale.com/admin/dns), turn on *MagicDNS* and *HTTPS
Certificates*. The playbook stops with a message if either is missing. Set
`coolify_dashboard: public` to skip Tailscale and serve the dashboard at
`coolify.<domain>` instead.

**Cloudflare settings for the zone.** SSL/TLS → Edge Certificates → turn on
*Always Use HTTPS*. Coolify serves plain HTTP behind the tunnel (see
[Why `http://`](#why-http-in-coolify)), and this makes every link HTTPS.

## Step 3 — Run the playbook

```bash
ansible-galaxy collection install -r requirements.yml   # once
ansible-playbook site.yml -K          # -K asks for the sudo password
```

It is idempotent; rerun it any time. Afterwards:

- `https://<machine>.<tailnet>.ts.net` (from any device on your tailnet) and
  `http://127.0.0.1:8000` (on the host) open the Coolify dashboard. Log in with
  the Coolify account. `https://coolify.example.com` answers 404 except
  under `/webhooks/`.
- Cloudflare → Zero Trust → Networks → Tunnels shows `preview-host` as
  *Healthy*.
- DNS has proxied CNAMEs `coolify` and `*` pointing at the tunnel.

## Step 4 — Connect GitHub and a project

In the Coolify dashboard, once:

1. **Settings → General → Instance's Domain:** the playbook already set it to
   the tailnet address, so the links Coolify posts open only on your tailnet.
2. **Sources → + Add → GitHub App.** Create it on your account and install
   it only on the repositories that should get previews.
3. Coolify registers the app's webhook at its instance domain, which GitHub
   cannot reach. On GitHub → Settings → Developer settings → GitHub Apps →
   your app → *Webhook URL*, change the host to the public one:
   `https://coolify.example.com/webhooks/source/github/events`. Keep the
   path and the secret.

For each project (e.g. a website repository):

1. **Projects → + Add → Resource → Private Repository (with GitHub App)** →
   choose the repository and the `main` branch.
2. **Build Pack:** Dockerfile. **Ports Exposes:** `8080`.
3. **Domains:** `http://site.example.com`. The `main` branch is deployed
   here and works as staging.
4. **Advanced:** turn on *Preview Deployments* and *Auto Deploy*.
5. **Preview Deployments → URL Template:** `pr-{{pr_id}}-preview.example.com`.
   Keep the name to one level (`pr-36-preview.…`, not `36.preview.…`), because
   the free certificate does not cover deeper subdomains.
6. Set app variables under **Environment Variables**. Preview deployments
   have their own list.

Open a PR. Coolify builds it and comments with the link. Pushing to the PR
redeploys it, and closing the PR removes it.

Each new project only needs this per-project list again. PR numbers repeat
across repositories, so give each other project its own name in the template
(`pr-{{pr_id}}-blog-preview.example.com`). The tunnel and DNS do not
change.

## Security

- **Pull requests from forks run code on your machine.** On a public
  repository, anyone can open a PR. Make sure Coolify does not deploy PRs from
  forks automatically (check the preview settings for your Coolify version), or
  keep previews limited to repositories where only you open PRs.
- **The dashboard is not on the internet.** Coolify runs as root on the host,
  and its login page has had serious vulnerabilities. With
  `coolify_dashboard: tailscale` only your tailnet reaches it; the tunnel
  serves `coolify.<domain>/webhooks/` alone, and GitHub signs every webhook.
  With `public`, consider Cloudflare Access in front of it, leaving
  `/webhooks/*` outside the policy.
- **Coolify's ports are bound to 127.0.0.1.** Docker's published ports bypass
  ufw, so by default the dashboard (8000, 6001, 6002) and Traefik (80, 443)
  answer to the whole LAN, Traefik even for the dashboard's hostname. The
  playbook rebinds them to loopback with `docker-compose.custom.yml`, which
  Coolify's upgrades keep, and through Coolify's proxy configuration.
- **Coolify is root on the host.** It manages the machine over SSH as root
  from its Docker network (`coolify`). On hosts that refuse root logins or
  firewall port 22 (Omarchy), the playbook adds one exception: a
  `Match Address <coolify subnet>` block at the end of `/etc/ssh/sshd_config`
  with `PermitRootLogin prohibit-password`, and a ufw rule for port 22 from
  that subnet only. Every other source keeps the host's own SSH rules.

## Cleanup

Every preview build leaves an image of about the size of the app image (560 MB
for this site). Coolify removes the preview container when the PR closes, but
not its image. The `preview_cleanup` role installs `coolify-prune-images`, run
daily by a systemd timer (`systemctl list-timers coolify-prune-images.timer`):

- removes every preview image (`<app uuid>:pr-*`) that no container uses;
- keeps the newest `preview_cleanup_images_to_keep` images of each application
  for rollback, plus the one running, and removes older ones;
- trims the BuildKit cache down to `preview_cleanup_build_cache_max` (10 GB),
  least recently used first;
- never touches images that do not belong to a Coolify application.

Coolify has its own daily cleanup, and it is broader: it removes **every**
unused image on the host that is not Coolify's, and the whole build cache. On a
machine that also does other work, that wipes your own images every night. So
with `preview_host_dedicated: false` (the default) the playbook turns Coolify's
forced run off and raises its disk threshold to 95%: Coolify then cleans only
when the disk is nearly full. On a dedicated host, set
`preview_host_dedicated: true` and Coolify's defaults stay.

Run it now with `sudo systemctl start coolify-prune-images` and check what it
removed with `journalctl -u coolify-prune-images`.

## GitHub Actions runner (optional)

Private repositories can run their GitHub Actions jobs on this host instead of
using GitHub's minutes. The `github_runner` role is off by default
(`github_runner_enabled: false`). It runs GitHub's own runner image
(`ghcr.io/actions/actions-runner`), **one job per container**:

- `github-runner@<owner>-<repo>-<n>.service`, `github_runner_instances` of
  them per repository. Each runner takes one job at a time.
- For every job, `github-runner-job` checks that the repository is private,
  registers a just-in-time runner on GitHub that is valid for that single job,
  and starts it with `docker compose`, next to its own Docker daemon. When the
  job ends it removes both containers, their volumes, the work directory and the
  registration, and systemd starts a fresh runner for the next job.
- Only the tool cache carries over between jobs: `/opt/hostedtoolcache`, where
  `actions/setup-node`, `setup-python` and similar keep downloaded versions.
  It lives in `/var/lib/github-runner/<instance>/tool-cache`, and is emptied
  before a job when it is larger than `github_runner_tool_cache_max_gb` (5 GB).
- npm, cargo and Playwright caches are dropped with the container. Restore them
  with `actions/cache` or the `cache:` option of the `setup-*` actions, as on
  GitHub's runners.
- The image is a plain Ubuntu with the runner, the Docker CLI and `sudo`. It
  does not have the toolchains that come preinstalled on `ubuntu-latest`: set
  them up with the `setup-*` actions (`actions/setup-node`,
  `dtolnay/rust-toolchain`, ...), and install browsers with
  `npx playwright install --with-deps`.
- A daily timer (`github-runner-update.timer`) pulls the runner image again,
  so new jobs get GitHub's current runner. GitHub stops sending jobs to
  runners that fall too far behind. The timer also removes the image versions
  it replaced.

### Turn it on

1. **Token.** The runner needs a token to register itself for every job.
   GitHub → Settings → Developer settings → Personal access tokens →
   *Fine-grained tokens* → *Generate new token*:

   | | Scope `repo` (personal account) | Scope `org` |
   |---|---|---|
   | Resource owner | your account | the organization |
   | Repository access | *Only select repositories*: the private ones | none needed |
   | Permission | Repository → **Administration: Read and write** | Organization → **Self-hosted runners: Read and write** |

   GitHub has no narrower permission for repository runners, and
   *Administration* also allows changing the repository's settings. The token
   is kept on the host in `/etc/github-runner/token`, readable by root only.
   Give the playbook the token as `GITHUB_RUNNER_TOKEN`, or keep it in
   1Password and set `github_runner_token_ref: op://<vault>/<item>/credential`.
   Fine-grained tokens expire: when it does, the runners stop with
   `Refused: ... HTTP 401` in the journal; create a new one and rerun.
2. **Settings** in `group_vars/all/local.yml`:

   ```yaml
   github_runner_enabled: true
   github_runner_scope: repo          # or org, with github_runner_org: <org>
   github_runner_repos:
     - your-user/private-repo
   github_runner_instances: 1         # runners per repository
   ```

   Repositories under a personal account can only have repository runners
   (`repo`). With `org`, the runners join the organization's default runner
   group (`github_runner_group_id: 1`), which must not allow public
   repositories.
3. **Run the playbook** (Step 3). It stops if a repository is public, or the
   token cannot read it.
4. **Check.** The repository's Settings → Actions → Runners lists
   `preview-host-<owner>-<repo>-1-<time>` as *Idle*. On the host:
   `systemctl status 'github-runner@*'` and
   `journalctl -u 'github-runner@*' -f`.
5. **Use it** in a workflow of that repository:

   ```yaml
   runs-on: [self-hosted, preview-host]
   ```

   The labels are `github_runner_labels` (`self-hosted`, `linux`, `x64`,
   `preview-host`).

### Turn it off, back to GitHub's runners

1. Change `runs-on` back to `ubuntu-latest` in the workflows. Jobs that ask for
   `self-hosted` wait in the queue while no runner is online.
2. Set `github_runner_enabled: false` (or take a repository out of
   `github_runner_repos`) and rerun the playbook. It stops those runners and
   removes their registrations; a job they are running is cancelled.
3. Revoke the token on GitHub. The files stay on the host; to remove them too:
   `sudo rm -r /etc/github-runner /var/lib/github-runner /usr/local/bin/github-runner-* /etc/systemd/system/github-runner* /etc/systemd/system/github_runner.slice && sudo systemctl daemon-reload`.

### Limits

Every runner, its Docker daemon and the containers its jobs start run in one
systemd slice, `github_runner.slice`, with limits for all of them together.
That way the jobs cannot take the CPU or memory the previews need:

| Setting | Default | |
|---|---|---|
| `github_runner_cpu_quota` | `200%` | two cores, for all runners together |
| `github_runner_memory_max` | `4G` | past it, the kernel kills processes of a job, not of the previews; no swap |
| `github_runner_tasks_max` | `4096` | processes and threads |
| `github_runner_min_free_disk_gb` | `20` | a runner waits for this much free disk before it takes a job |

Under contention, the runners also get half the CPU and disk weight of
everything else. Check the usage with `systemd-cgtop github_runner.slice`. The
limits need Docker's systemd cgroup driver, the default with cgroup v2; the
playbook stops if Docker uses another one.

A single job's disk use has no hard limit. Its files are in
`/var/lib/github-runner/<instance>/work` and in Docker volumes, and are removed
when the job ends.

### Runner security

- **Private repositories only.** A pull request from a fork of a public
  repository would run anyone's code on this host. The playbook refuses a
  public repository, and so does `github-runner-job` before every registration,
  so a repository made public later stops getting runners: the unit stays
  stopped (exit code 78) with `Refused: ... is not private` in the journal.
  With `org`, the runner group must have *Allow public repositories* turned
  off. **This repository is public and must never use the runner.**
- On the private repositories, keep *Run workflows from fork pull requests*
  turned off (Settings → Actions → General), as it is by default.
- **Jobs never get the host's Docker socket**, which would give them Coolify,
  the previews and the tunnel token. With `github_runner_docker: true` (the
  default) every job gets its own Docker daemon (Docker in Docker), removed with
  the job. That daemon has to run privileged, so a job that tries can still
  become root on the host. That is the same trust as the code you deploy as
  previews, and the reason this runner serves your private repositories only.
  Set `github_runner_docker: false` if the jobs do not build or run
  containers; the runner container is then unprivileged.
- Jobs reach the network like any machine on the LAN. Coolify's ports are on
  127.0.0.1 and not reachable from the jobs.

## Moving to another machine

1. On the old host: `docker compose -f /opt/cloudflared/compose.yml down`. Two
   hosts running the same tunnel token would split traffic between them.
2. Point `inventory/hosts.yml` at the new host and run Step 3. The tunnel and
   DNS are reused, not recreated.
3. Reconnect Step 4, or move Coolify's state: copy `/data/coolify` from the
   old host (including `source/.env`, which holds `APP_KEY`) before running the
   playbook on the new host. The installer is skipped when that file exists.

## Troubleshooting

- **Proxy exits right after Start Proxy.** Coolify's default Traefik compose
  publishes its dashboard on host port 8080, which collides with anything
  else there (a local `bootRun`, for one). The playbook removes that port; if
  you reset the proxy configuration in Coolify, rerun the playbook.
- **Dashboard says realtime is not connected** on the tailnet address. Check
  `tailscale serve status`: it should list `/`, `/app` and `/terminal/ws`.
- **"Permission denied (publickey)" on the first deploy.** The application was
  created as *Private Repository (with Deploy Key)* and picked the GitHub App's
  key, which GitHub does not accept for SSH. Create it as *Private Repository
  (with GitHub App)* instead; that is also what makes PR previews and comments
  work.
- **Changed a domain and Traefik still routes the old one.** Coolify stores the
  container labels with the application. After changing a domain, use
  *Container labels → Reset To Defaults* (or save from the General page) and
  redeploy.

## Why `http://` in Coolify

Cloudflare already serves HTTPS and the tunnel delivers plain HTTP to Traefik
on port 80. With `https://` domains, Traefik would redirect that HTTP request
to HTTPS. The redirect comes back through the tunnel as HTTP again, so the
browser loops. Using `http://` domains plus *Always Use HTTPS* at Cloudflare
avoids the loop. The link in the PR comment shows `http://`, and Cloudflare
redirects it to HTTPS.

## Layout

```
site.yml                         playbook: coolify, tailscale_dashboard, cloudflare_tunnel, preview_cleanup, github_runner
inventory/hosts.yml              which machine is the preview host
group_vars/all/defaults.yml      every setting and its default
group_vars/all/local.yml         your settings (not in git; template: local.example.yml)
roles/coolify/                   runs Coolify's installer once, binds its ports to loopback
roles/tailscale_dashboard/       dashboard on the tailnet with Tailscale Serve
roles/cloudflare_tunnel/         tunnel + routes + DNS via API, cloudflared in Docker
roles/preview_cleanup/           daily removal of unused Coolify images, Coolify cleanup mode
roles/github_runner/             optional GitHub Actions runners for private repositories, one container per job
```
