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
- **More zones** (optional) get previews too, each behind **Cloudflare
  Access**: only the e-mails you list can open them. See
  [More zones and apps](#more-zones-and-apps).

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

With [more zones](#more-zones-and-apps), add every one of them to the zone
permissions, and one more account permission:

| Permission | Access |
|---|---|
| Account → Access: Apps and Policies | Edit |

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
- With more zones: each has a proxied `*` CNAME too, and Zero Trust → Access →
  Applications lists `preview-infra: <zone> previews`. The last task of the
  role opened `https://preview-infra-check.<zone>` and got the Access login.

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

## More zones and apps

Previews can live in other zones too, for example one domain per project.
Each extra zone gets the same tunnel route and wildcard DNS as `domain`, plus
a **Cloudflare Access** application on its whole wildcard: every hostname
there asks for a login first, and only the e-mails listed get in. That is
not optional. An app with a first-run screen (whoever opens it first becomes
its owner) must never be open to the internet, not even for a minute.

The zone's previews are one level deep, `pr-12.example.dev`: the free
certificate covers `*.example.dev` but not `*.preview.example.dev` (that
needs Cloudflare's paid Advanced Certificate Manager). The apex
`example.dev` is not matched by the wildcard and stays free for a project
site. A `www` or other public name later needs its own DNS record and its
own Access application with a *Bypass* policy, which wins over the wildcard
because it is more specific.

**Once per Cloudflare account**, before the first extra zone:

1. Cloudflare dashboard → Zero Trust. If it asks, create the organization
   (any team name) on the Free plan.
2. Zero Trust → Settings → Authentication → Login methods: *One-time PIN*
   must be listed (it is by default). Access mails a code to the address
   that tries to log in, and only the listed addresses get one that works.
3. Give the API token the extra permissions from
   [Step 2](#step-2--your-settings-and-secrets).

**Per zone:** add the domain to Cloudflare (same account as `domain`), turn on
SSL/TLS → Edge Certificates → *Always Use HTTPS*, and list it in `local.yml`:

```yaml
preview_access_emails: [you@example.com]
preview_zones:
  - zone: example.dev
```

`access_emails` on a zone overrides `preview_access_emails` for it. Rerun
the playbook. It creates the Access application before the DNS record, and
fails at the end if `https://preview-infra-check.example.dev` does not answer
with the Access login.

**Per app:** describe it in `local.yml`. The playbook checks that its zone is
routed and protected and, at the end, prints the values for Coolify. For
Luotsi, the values come from its `Dockerfile` (port 8740, health check on
`GET /api/v1/status`, data and secrets key in the `/data` volume):

```yaml
preview_apps:
  - name: luotsi
    repository: jacksonsieben/luotsi
    zone: example.dev                 # your zone
    port: 8740                        # the port the image listens on (EXPOSE)
    healthcheck_path: /api/v1/status  # GET, 200 when ready
    data_path: /data                  # where the image keeps its data, if anywhere
```

Then create it in Coolify as in [Step 4](#step-4--connect-github-and-a-project),
with these differences:

1. On GitHub → Settings → Applications → your Coolify GitHub App →
   *Configure*, add the repository to *Repository access*.
2. **Domains:** `http://main.example.dev` (the `main` branch). It is behind
   Access too.
3. **Health check:** on, with the path and port printed. Coolify runs it
   inside the container with `curl` or `wget`; an image without either needs
   its own `HEALTHCHECK` instead, with Coolify's turned off.
4. **Preview URL Template:** `pr-{{pr_id}}.example.dev`. With several apps
   on one zone, set `preview_name` on each and use the printed template
   (`pr-{{pr_id}}-<preview_name>.example.dev`).
5. **Persistent Storage** (only with `data_path`): + Add → *Volume Mount*,
   destination path `data_path`. Leave the preview suffix on: each preview
   gets its own `<volume>-pr-<n>`, empty at the start, and Coolify deletes it
   with the preview. Never a bind mount (*Directory Mount*): previews would
   share it with `main`. An image that declares `VOLUME` (Luotsi does, for
   `/data`) needs this mount: without it, every deploy leaves an anonymous
   volume behind that nothing removes.
6. **Environment Variables:** add nothing that grants access elsewhere (a
   GitHub token, a runner address), in either list. Previews start empty.

Open a PR: the GitHub App comments on it with the preview link, and updates
the comment on every push.

## Pull requests

Every PR fills in `.github/pull_request_template.md`. Its *How to test* section
must give the preview link (or the command to run locally, when there is no
preview), numbered steps with the exact URL or command and the expected result,
and the regressions to check. Any change to sshd, ufw, DNS or the Cloudflare
tunnel is ticked in the risk checklist and explained. Infra PRs are never run
for real: the check is `ansible-playbook site.yml --syntax-check`.

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
- **Previews on more zones are behind Cloudflare Access.** The playbook will
  not route a zone in `preview_zones` without an e-mail to allow, and checks
  after every run that the zone asks for the login. Previews on `domain`
  stay public, as before.
- **Access is only enforced at Cloudflare, not on this host.** Traefik
  listens on 127.0.0.1:80 and routes by `Host` header alone, so any process
  on the preview host, agent workers included, skips the login with
  `curl -H 'Host: main.example.dev' http://127.0.0.1/`. A local process could
  do an app's first run (and become its owner) before you do. Nothing on the
  LAN can: Traefik is bound to loopback. Until Traefik checks the Access token
  itself ([#6](https://github.com/jacksonsieben/preview-infra/issues/6):
  validate `Cf-Access-Jwt-Assertion` against the Access certs), do the first
  run of a new deployment yourself right away, and run on this host only
  processes you would trust with the previews.
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

The script works per Coolify application, not per domain, so previews on
every zone are covered. A preview's volume (`<volume>-pr-<n>`) is deleted by
Coolify itself when the PR closes; `docker volume ls --filter name=-pr-`
lists those still there.

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
site.yml                         playbook: coolify, tailscale_dashboard, cloudflare_tunnel, preview_cleanup
inventory/hosts.yml              which machine is the preview host
group_vars/all/defaults.yml      every setting and its default
group_vars/all/local.yml         your settings (not in git; template: local.example.yml)
roles/coolify/                   runs Coolify's installer once, binds its ports to loopback
roles/tailscale_dashboard/       dashboard on the tailnet with Tailscale Serve
roles/cloudflare_tunnel/         tunnel + routes + DNS via API, cloudflared in Docker,
                                 Cloudflare Access for the other zones (tasks/access.yml)
roles/preview_cleanup/           daily removal of unused Coolify images, Coolify cleanup mode
```
