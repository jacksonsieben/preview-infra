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

**Redirects, set by hand in the Cloudflare dashboard.** Coolify serves plain HTTP
behind the tunnel (see [Why `http://`](#why-http-in-coolify)), so turn on
*SSL/TLS → Edge Certificates → Always Use HTTPS*. For www → apex, add a
*Rules → Redirect Rules* rule: hostname equals `www.<domain>`, dynamic target
`concat("https://<domain>", http.request.uri.path)`, 301, preserve query string.

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

**The GitHub App, once per Coolify install.** GitHub only creates apps from a
browser, so this is the one step the playbook can't do:

1. **Sources → New Source → GitHub App**, a name (e.g. `coolify-<machine>`),
   *Pull request previews* on → **Register With GitHub**. Keep the webhook
   endpoint on the instance (tailnet) address: the browser comes back to it
   after GitHub, and you're logged in only there.
2. GitHub opens with the app filled in: **Create GitHub App**. Back in Coolify,
   **Install Repositories** → *Only select repositories* → the ones that should
   deploy here. Add more later in the app's *Configure* page on GitHub.
3. GitHub can't reach the tailnet address the app was registered with. On
   GitHub → Settings → Developer settings → GitHub Apps → your app → *Webhook
   URL*, change the host to the public one:
   `https://coolify.example.com/webhooks/source/github/events`. Keep the path
   and the secret. *Advanced → Recent Deliveries → Redeliver* on the first ping
   should then show a green check.

Put the app's name in `local.yml` as `coolify_github_app`.

**The projects, in `local.yml`.** Each entry of `coolify_apps` becomes a
Coolify application, created or corrected on every run:

```yaml
coolify_github_app: coolify-preview-host
coolify_apps:
  - repo: you/site
    project: Site
    domains: ["site.{{ domain }}"]   # http:// in Coolify, see below
    port: 8080                        # the container's port
```

From there the playbook sets the Dockerfile build pack, auto deploy on push,
and preview deployments at `pr-<n>-preview.<domain>` for PRs from repository
members only. An application it creates is deployed right away. Every option
and its default is in `group_vars/all/defaults.yml` and
`roles/coolify_apps/defaults/main.yml`. Coolify's API is enabled only for the
run, with a token deleted at the end.

- **Production on the apex.** Add the domain to `production_hostnames` (Step
  2), and `"{{ domain }}"` (plus `www.{{ domain }}`) to the app's `domains`.
- **More projects.** PR numbers repeat across repositories, so give each other
  project its own `preview_name` (`blog-preview` → `pr-36-blog-preview.…`).
  Keep it to one level (`pr-36-preview.…`, not `36.preview.…`): the free
  certificate doesn't cover deeper subdomains. The tunnel and DNS don't change.
- **Environment variables** stay in Coolify (*Environment Variables*; preview
  deployments have their own list), since they are secrets.

Open a PR. Coolify builds it and comments with the link. Pushing to the PR
redeploys it, and closing the PR removes it.

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
site.yml                         playbook: coolify, tailscale_dashboard, cloudflare_tunnel, preview_cleanup, coolify_apps
inventory/hosts.yml              which machine is the preview host
group_vars/all/defaults.yml      every setting and its default
group_vars/all/local.yml         your settings (not in git; template: local.example.yml)
roles/coolify/                   runs Coolify's installer once, binds its ports to loopback
roles/tailscale_dashboard/       dashboard on the tailnet with Tailscale Serve
roles/cloudflare_tunnel/         tunnel + routes + DNS via API, cloudflared in Docker
roles/preview_cleanup/           daily removal of unused Coolify images, Coolify cleanup mode
roles/coolify_apps/              the applications in coolify_apps, through Coolify's API
```
