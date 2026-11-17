# preview-infra

Vercel-style preview deployments on a machine you own. Every pull request on
a connected repository is built from its `Dockerfile`, served at its own URL
(`https://pr-36-preview.example.com`), and linked in a PR comment. The
preview is removed when the PR closes.

- **[Coolify](https://coolify.io)** builds and runs the previews and comments
  on the PR (through its GitHub App).
- **Cloudflare Tunnel** publishes the machine without opening a router port or
  needing a fixed IP. Cloudflare terminates HTTPS.
- **Tailscale Serve** puts the Coolify dashboard on your tailnet only, at a
  name of your own (`https://coolify.example.dev`): it forwards port 443 of
  the host's tailnet address to Traefik, which has the certificate. The tunnel
  exposes just the webhook path GitHub needs.
- **This playbook** installs both and sets up the tunnel routes and DNS
  through the Cloudflare API, so you can rebuild the whole thing on another
  machine with one command.
- **More zones** (optional) get previews too, each behind **Cloudflare
  Access**: only the e-mails you list can open them. See
  [More zones and apps](#more-zones-and-apps). Or **on your tailnet only**,
  with no tunnel and no Access: see [Tailnet-only zones](#tailnet-only-zones).
- **A GitHub Actions runner** (optional) runs the Actions jobs of your private
  repositories here, one container per job, instead of on GitHub's minutes.
- **A remote host** (optional, `ci-1`) takes work off this one: Actions
  runners, and the PR previews themselves, which Coolify then builds and runs
  there so no PR code runs on this machine. See
  [Previews on ci-1](#previews-on-ci-1).

```
PR opened / pushed ─► GitHub App webhook ─► coolify.example.com/webhooks/ ─┐
                                                                            │ tunnel
https://pr-36-preview.example.com ─► Cloudflare ─► cloudflared ─► Traefik :80 ─► preview container

you, on the tailnet ─► https://coolify.example.dev ─► Tailscale Serve (TCP 443) ─► Traefik 127.0.0.1:443 ─► Coolify dashboard
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
The `ci` group is a separate remote host, reached only through the tailnet
([Remote host (ci-1)](#remote-host-ci-1)); it is skipped until you give it an
address.

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
| Account → Access: Organizations, Identity Providers, and Groups | Read |

The second one reads the team domain, whose keys sign the Access tokens that
Traefik checks on this host (see [Security](#security)).

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
be logged in to your tailnet (`tailscale up`), and the dashboard needs a name
on a [tailnet-only zone](#tailnet-only-zones), in `local.yml`:

```yaml
coolify_dashboard_hostname: coolify.example.dev
preview_zones:
  - zone: example.dev
    tailnet: true
```

Its DNS record points at the host's tailnet address, and Tailscale Serve
forwards port 443 there, as TCP, to Traefik on 127.0.0.1:443, which serves
the dashboard with a Let's Encrypt certificate it gets through Cloudflare
DNS-01. That needs a Cloudflare token in a file on the host (see
[Tailnet-only zones](#tailnet-only-zones) for the token and the file). The
host's `.ts.net` name no longer opens the dashboard: Traefik answers only for
the names it serves. Set `coolify_dashboard: public` to skip Tailscale and
serve the dashboard at `coolify.<domain>` instead.

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

It is idempotent; rerun it any time. To preview a run first, without changing
anything, add `--check --diff`:

```bash
ansible-playbook site.yml -K --check --diff               # the preview host
ansible-playbook site.yml -K --check --diff --limit ci    # ci-1 only
```

It reads the hosts, Cloudflare, Tailscale and GitHub as a real run does, and
shows each file, DNS record, route or setting the run would change (*changed*,
or *skipped* for an API call or a command that only a real run makes). Left
out: Coolify's applications and their environment variables (reading them
takes an API token that only a real run creates), and a host without Coolify
yet, where the check stops early. A check that depends on what the run would
create first (a new tunnel, Access application or DNS record, a systemd unit,
or Docker on a new `ci-1`) is reported as *ignored* instead of failing, so the
check of a new `ci-1` runs to the end. Traefik's DNS-01 settings are worked
out from the proxy's compose file, read directly (Coolify's own read may
write it), and reported; a proxy without one yet is reported too.

Afterwards:

- `https://<coolify_dashboard_hostname>` (from any device on your tailnet)
  and `http://127.0.0.1:8000` (on the host) open the Coolify dashboard. Log
  in with the Coolify account. `https://coolify.example.com` answers 404
  except under `/webhooks/`. `tailscale serve status` shows port 443 as a
  TCP forward to `127.0.0.1:443`, and no HTTPS routes.
- Cloudflare → Zero Trust → Networks → Tunnels shows `preview-host` as
  *Healthy*.
- DNS has proxied CNAMEs `coolify`, `*` and each of `production_hostnames`
  pointing at the tunnel.
- With a tailnet-only zone: the dashboard's name and that zone's
  `production_hostnames` have grey (DNS only) `A` records with the host's
  tailnet address, and no tunnel route.
- With more zones: each has a proxied `*` CNAME too, and Zero Trust → Access →
  Applications lists `preview-infra: <zone> previews`. The last task of the
  role opened `https://preview-infra-check.<zone>` and got the Access login,
  and the preview_access role got 403 for the same name straight from Traefik
  on 127.0.0.1 (no token). `docker ps` lists `preview-access`.

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
- **Environment variables:** `preview_env` and `production_env`, see
  [Environment variables](#environment-variables). Anything else stays in
  Coolify (*Environment Variables*; preview deployments have their own list).

Open a PR. Coolify builds it and comments with the link. Pushing to the PR
redeploys it, and closing the PR removes it.

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

`access_emails` on a zone overrides `preview_access_emails` for it. A name
there outside the wildcard's previews, served by an app here (`main.example.dev`
once the previews move to [ci-1](#previews-on-ci-1)), can go in
`production_hostnames`: it gets its own route and record in that zone, behind
the same Access login. Rerun
the playbook. It creates the Access application before the DNS record, and
fails at the end if `https://preview-infra-check.example.dev` does not answer
with the Access login. Traefik on this host then checks the Access token
too, so a local process can't skip the login either (see
[Security](#security)); the run fails if
`curl -H 'Host: preview-infra-check.example.dev' http://127.0.0.1/` is not
refused with 403.

**Per app:** describe it in `local.yml`, under `preview_apps`. Unlike
`coolify_apps` ([Step 4](#step-4--connect-github-and-a-project)), whose
previews are always on `domain` and which set no health check or storage,
these are created by hand in Coolify: the playbook checks that the app's zone
is routed and protected and, at the end, prints the values to enter. A
repository goes in one list or the other, never both. For
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

Then create it in Coolify: **Projects → + Add → Resource → Private Repository
(with GitHub App)**, the repository and the `main` branch, and the values
printed at the end of the run. Also:

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
   What the app needs goes in `preview_env` / `production_env` in `local.yml`
   ([Environment variables](#environment-variables)); the next run sets it.

Open a PR: the GitHub App comments on it with the preview link, and updates
the comment on every push.

### Tailnet-only zones

A zone of `preview_zones` with `tailnet: true` is served on your tailnet only.
Its names get grey (DNS only) `A` records with a tailnet address (`100.x`),
so anyone can look them up but only devices on your tailnet reach them, and
nothing on it goes through a Cloudflare tunnel or Access:

```yaml
preview_zones:
  - zone: example.dev
    tailnet: true
```

| On the zone | Record | Served by |
|---|---|---|
| `coolify_dashboard_hostname`, `production_hostnames` there | `A` → preview host's tailnet address | the preview host's Traefik |
| `*.<zone>` (PR previews), on the preview host | `A` → preview host's tailnet address | the preview host's Traefik |
| `*.<zone>` (PR previews), with the zone in `ci_previews_zones` | `A` → `ci-1`'s tailnet address | `ci-1`'s Traefik |

On each host, Tailscale Serve forwards TCP 443 of the tailnet address to
Traefik on 127.0.0.1:443, and Traefik gets its Let's Encrypt certificates
through Cloudflare DNS-01, since Let's Encrypt can't reach it for the HTTP
challenge. On `ci-1`, one wildcard certificate per zone (`<zone>` and
`*.<zone>`) covers every PR preview. In Coolify, the apps there use
`https://` domains (the run prints them). `preview-access` lets these hosts
through with no Access token, and refuses (403) a request for one of them
that came through Cloudflare, which carries `Cf-Connecting-Ip`: these names
are served from the tailnet only.

**Tunnel and Access, or tailnet only?** Per zone, not per name:

- **Tailnet only** when every person who opens the previews is on your
  tailnet: your own projects, a team that shares the tailnet. Nothing is on
  the internet and there is no login to get past. Who reaches what is up to
  the [Tailscale ACL](#tailscale-acl).
- **Tunnel and Access** (no `tailnet`) when someone outside the tailnet must
  open a preview: a client, a reviewer with only a browser. Access asks for
  their e-mail first.
- `domain` itself always keeps the tunnel: GitHub's webhooks reach Coolify
  through it.

**The Cloudflare tokens for DNS-01.** One per host, in a file you put there
yourself; the playbook checks the file exists and is not empty, stops with
a message if not, and never reads it (Traefik gets it mounted read-only).
Create each one in Cloudflare → My Profile → API Tokens → Custom token, with
*Zone → DNS → Edit* and *Zone → Zone → Read*, limited to the tailnet-only
zones that host serves. Then, on the host:

```bash
sudo install -d -m 0700 /etc/preview-infra
sudo sh -c 'umask 077; cat > /etc/preview-infra/cloudflare-dns-token'   # paste the token, Enter, Ctrl-D
```

| Host | Setting | Default path | Needed with |
|---|---|---|---|
| preview host | `coolify_dns_token_file` | `/etc/preview-infra/cloudflare-dns-token` | `coolify_dashboard: tailscale` |
| `ci-1` | `ci_previews_dns_token_file` | `/etc/preview-infra/cloudflare-dns-token` | a tailnet-only zone in `ci_previews_zones` |

Traefik reads the token through `CF_DNS_API_TOKEN_FILE`; a
`CF_DNS_API_TOKEN` written into Coolify's proxy configuration by hand is
removed. The HTTP challenge is removed too, `httpchallenge.entrypoint`
included (in Traefik 3, that line alone turns it back on).

**From a zone behind Access to tailnet only:** add `tailnet: true` and rerun.
The zone's records become grey `A` records; its Access application stays in
Zero Trust until you delete it there (Access → Applications →
`preview-infra: <zone> previews`). Its apps' domains in Coolify change to
`https://` (printed at the end of the run).

## Environment variables

Any entry of `coolify_apps` or `preview_apps` can set environment variables,
which the playbook writes through Coolify's API on every run:

```yaml
preview_apps:
  - name: luotsi
    repository: jacksonsieben/luotsi
    # ...
    preview_env:                      # only in PR deployments (is_preview: true)
      LUOTSI_PREVIEW: "1"
      LUOTSI_PREVIEW_CODE: "{{ lookup('community.general.onepassword', 'Luotsi preview code', field='credential', vault='Ansible') }}"
    production_env: {}                # only in production (is_preview: false)
```

- **Secrets from 1Password.** Create a
  [service account](https://developer.1password.com/docs/service-accounts/)
  with read access to the vault, and export its token before the run:
  `export OP_SERVICE_ACCOUNT_TOKEN=ops_...`. The lookup uses the `op` CLI, and
  with a service account it needs `vault`. Values live only in 1Password and
  Coolify: never in the repo, and no task prints them, not even with `--diff`
  or `-v`.
- **Previews and production stay apart.** Coolify keeps one list for each, and
  a deployment reads only its own. A `production_env` variable is not copied
  to previews.
- **Only the playbook's own.** Each variable it creates has the comment
  *Managed by preview-infra …* (`coolify_env_comment`). Taken out of the list,
  it is deleted on the next run. A variable made by hand in Coolify is never
  changed or deleted; a listed key that one already uses stops the run, so
  delete it in Coolify or pick another name.
- **As written, at runtime.** Values are literal (no `$VAR` expansion) and
  are not build arguments, which could end up in the image. Coolify applies a
  change on the app's next deployment: push to the PR, or redeploy `main`.
- A `preview_apps` entry that sets variables must already exist in Coolify. It
  is found by its repository, and by `name` when there are several.

To compare the values, the run mints a second short-lived token that can read
Coolify's secrets, only when some app sets variables. Only the tasks that set
them use it, all hidden from the output, and it is deleted with the other one.

## Pull requests

Every PR fills in `.github/pull_request_template.md`. Its *How to test* section
must give the preview link (or the command to run locally, when there is no
preview), numbered steps with the exact URL or command and the expected result,
and the regressions to check. Any change to sshd, ufw, DNS or the Cloudflare
tunnel is ticked in the risk checklist and explained. Infra PRs are never run
for real: the checks are `ansible-playbook site.yml --syntax-check` and
`ansible-playbook tests/tunnel_routes.yml`, which renders the tunnel routes and
DNS names offline and fails if production's would change, and
`python3 tests/coolify_env.py`, which runs the `coolify_apps` role against a
fake Coolify API and checks the environment variables.
`python3 tests/check_mode.py` checks that every `uri` task that only reads
(no `method`, or `GET`) also runs in check mode (`check_mode: false`), so
`--check --diff` works, and that a `command`/`shell` task runs there only with
a `# read-only: <why>` comment right above it: `changed_when: false` says how
a task is reported, not that it changes nothing.
`ansible-playbook tests/coolify_build_limit.yml` checks the build limit task
against a stand-in for `docker`.
`ansible-playbook tests/github_runner_settings.yml` checks the runner instance
names, labels, slices and defaults, with and without `github_runners`, and `python3 tests/github_runner_job.py` runs
`github-runner-job` against a fake GitHub API.
`ansible-playbook tests/ci_previews_settings.yml` checks the routes, hosts and
Traefik file of the previews on `ci-1`, `ansible-playbook
tests/ci_previews_coolify.yml` that their Coolify settings go to `ci-1`'s
server, and `python3 tests/preview_limit.py` which previews `preview-limit`
stops. `ansible-playbook tests/proxy_dns01.yml` checks Traefik's DNS-01
settings against a stand-in for `docker`, and `ansible-playbook
tests/proxy_dns01_check.yml --check` that check mode reads the compose file
and calls nothing. The *Checks* workflow
(`.github/workflows/checks.yml`) runs them all on every pull request, on
GitHub's runners.

## Build load

Builds are the heaviest thing on the host: one can take every CPU. Two settings
keep them in check.

**One build at a time.** `coolify_concurrent_builds` (default `1`) is the
number of builds Coolify runs at once on this host, previews and production
together. The others wait in its queue. It is Coolify's *Servers → your server
→ Advanced → Concurrent Builds*, and each run of the playbook sets it back to
this value, so change it in `local.yml`, not in the dashboard. Deployments wait
longer when several are queued, and past *Deployment Queue Limit* (25 by
default) Coolify refuses new ones.

**Previews on demand.** With `previews: false` on an entry of `coolify_apps` or
`preview_apps`, pushes and new PRs no longer build a preview. Production keeps
following `auto_deploy`. On a `preview_apps` entry, leaving `previews` out keeps
whatever Coolify has.

```yaml
preview_apps:
  - name: myapp
    repository: you/myapp
    previews: false
    # ...
```

The same switch in the dashboard, for an app you don't list or to try it first:
the application → *Advanced* → turn off *Preview Deployments*. If `local.yml`
sets `previews`, the next run puts that value back.

To get a preview of a PR:

1. Open the application → *Preview Deployments*.
2. *Load Pull Requests* (needs a GitHub App source), then *Configure* on the PR.
3. *Deploy*. The address follows the *Preview URL Template*, as before.

After that:

- A new push does not update the preview: press *Redeploy* on it.
- Closing or merging the PR still removes the preview.
- The preview's address is listed on that page.

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
- **PR code can run elsewhere.** With [Previews on ci-1](#previews-on-ci-1),
  Coolify builds and runs the PR previews of those apps on `ci-1`, each app
  on a network that reaches only the internet; this host keeps the webhooks
  and production. What stays open there is listed in that section.
- **Previews on more zones are behind Cloudflare Access, or on the tailnet
  only.** The playbook will not route a zone in `preview_zones` without an
  e-mail to allow, and checks after every run that the zone asks for the
  login. A [tailnet-only zone](#tailnet-only-zones) has no route: only your
  tailnet reaches it, as the [Tailscale ACL](#tailscale-acl) allows, and
  Traefik refuses its names on a request that came through Cloudflare.
  Previews on `domain` stay public, as before.
- **Traefik checks the Access token too, not only Cloudflare.** Traefik
  listens on 127.0.0.1:80 and routes by `Host` header, so without a check of
  its own any process on the preview host, agent workers included, would skip
  the login with `curl -H 'Host: main.example.dev' http://127.0.0.1/`, and
  could do an app's first run (and become its owner) before you do. The
  preview_access role puts a forward-auth service, `preview-access`, in front
  of every route of Coolify's Traefik: a request to a host on one of
  `preview_zones` passes only with the `Cf-Access-Jwt-Assertion` header that
  Cloudflare adds after the login, signed by your team's Access keys, for that
  zone's Access application, and not expired. Anything else gets 403. Hosts on
  `domain` pass untouched. The middleware goes on Traefik's entrypoints, in
  Coolify's proxy configuration (Coolify rewrites the labels of its routers on
  every deploy), so it covers every preview Coolify creates. Two things follow:
  - while `preview-access` is down, Traefik answers 500 to **every** request,
    `domain` and production included. It restarts with Docker; check it with
    `docker ps --filter name=preview-access` and
    `docker logs preview-access` (it logs each refused request).
  - if you reset the proxy configuration in Coolify, the check is gone until
    the next run of the playbook, which puts it back and fails if
    `curl -H 'Host: preview-infra-check.<zone>' http://127.0.0.1/` is not
    refused.
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

## Tailscale ACL

The hosts are tagged, and the tailnet policy decides what reaches what. The
playbook doesn't change the policy; set it by hand in the admin console →
[Access controls](https://login.tailscale.com/admin/acls).

| Tag | Host |
|---|---|
| `tag:coolify` | the preview host (Coolify, the playbook runs here) |
| `tag:web` | the web host |
| `tag:ci` | `ci-1` |

What the setup uses on the tailnet:

| From | To | For |
|---|---|---|
| your own devices | preview host :443 | the dashboard, previews of tailnet-only zones kept there |
| your own devices | `ci-1` :443 | PR previews of tailnet-only zones on `ci-1` |
| your own devices | any host :22 | SSH |
| `tag:coolify` | `tag:ci` :22 | Coolify manages `ci-1` over SSH; the playbook reaches `ci-1` the same way |
| `tag:coolify` | `tag:web` :22 | SSH from the preview host to the web host |

Nothing else: no host reaches your devices or another host, and nothing
here needs it. Let's Encrypt, Cloudflare's API and the tunnels go over the
internet, not the tailnet. The policy:

```json
{
  "tagOwners": {
    "tag:coolify": ["autogroup:admin"],
    "tag:web":     ["autogroup:admin"],
    "tag:ci":      ["autogroup:admin"]
  },
  "acls": [
    {"action": "accept", "src": ["autogroup:member"], "dst": ["*:*"]},
    {"action": "accept", "src": ["tag:coolify"], "dst": ["tag:web:22", "tag:ci:22"]}
  ]
}
```

Tag each host once, from the host: `sudo tailscale up --advertise-tags=tag:ci`
(`tag:coolify`, `tag:web` on the others), or in the admin console →
Machines → *Edit ACL tags*. A tagged host loses its owner's access rights:
only the rules above apply to it.

**Coolify Sentinel on another server** (its metrics in the dashboard) sends
them from that server to Coolify at `https://<coolify_dashboard_hostname>`,
on the preview host's tailnet address. That is traffic from a server to
another, which the policy above refuses: Sentinel's reports fail there, and
nothing else does. To have them, add the rule for that server's tag:

```json
{"action": "accept", "src": ["tag:ci"], "dst": ["tag:coolify:443"]}
```

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
every zone are covered. With [Previews on ci-1](#previews-on-ci-1) it runs on
`ci-1` too, where those previews are built. A preview's volume (`<volume>-pr-<n>`) is deleted by
Coolify itself when the PR closes; `docker volume ls --filter name=-pr-`
lists those still there.

## GitHub Actions runner (optional)

Private repositories can run their GitHub Actions jobs on this host instead of
using GitHub's minutes. The `github_runner` role is off by default
(`github_runner_enabled: false`). It runs GitHub's own runner image
(`ghcr.io/actions/actions-runner`), **one job per container**:

- `github-runner@<owner>-<repo>-<n>.service`, `github_runner_instances` of
  them per repository. Each runner takes one job at a time. To give a
  repository several runners with labels of their own (a heavy one and a
  light one), see [Several runners per repository](#several-runners-per-repository).
- For every job, `github-runner-job` checks that the repository is private,
  registers a just-in-time runner on GitHub that is valid for that single job,
  and starts it with `docker compose`, next to its own Docker daemon. When the
  job ends it removes both containers, their volumes, the work directory and the
  registration, and systemd starts a fresh runner for the next job.
- Only the tool cache carries over between jobs: `/opt/hostedtoolcache`, where
  `actions/setup-node`, `setup-python` and similar keep downloaded versions.
  It lives in `/var/lib/github-runner/<instance>/tool-cache`, and is emptied
  before a job when it is larger than `github_runner_tool_cache_max_gb`: 5 GB
  with `repo`, and 0 (emptied for every job) with `org`; see
  [Runner security](#runner-security).
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
   removes their registrations; a job they are running is cancelled. With
   `github_runner_enabled: false` it also deletes `/etc/github-runner/token`.
3. Revoke the token on GitHub. The other files stay on the host; to remove
   them too:
   `sudo rm -r /etc/github-runner /var/lib/github-runner /usr/local/bin/github-runner-* /etc/systemd/system/github-runner* /etc/systemd/system/github_runner*.slice && sudo systemctl daemon-reload`.

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
everything else. With `github_runners`, each entry also gets a slice of its own
inside it, with its own limits
([Several runners per repository](#several-runners-per-repository)). Check the
usage with `systemd-cgtop github_runner.slice`. The
limits need Docker's systemd cgroup driver, the default with cgroup v2; the
playbook stops if Docker uses another one.

A single job's disk use has no hard limit. Its files are in
`/var/lib/github-runner/<instance>/work` and in Docker volumes, and are removed
when the job ends.

### Runner security

- **Private repositories only.** A pull request from a fork of a public
  repository would run anyone's code on this host. The playbook refuses a
  public repository, and so does `github-runner-job` before every registration.
  A registered runner can wait days for its job, so while it waits or runs,
  `github-runner-job` checks again every
  `github_runner_visibility_check_seconds` (5 minutes). When a repository is
  made public, its runners are stopped within those minutes and removed from
  GitHub, and the units stay stopped (exit code 78) with
  `Refused: ... is not private` in the journal. With `org`, the runner group
  must have *Allow public repositories* turned off, and the same checks apply
  to that setting. **This repository is public and must never use the
  runner.**
- **The tool cache is shared between jobs.** It is the one thing a job leaves
  for the next: a job could replace a `node` or `python` there, and a later
  job of the same runner, one from `main` with secrets for one, would run it.
  With `repo`, only the repository's collaborators run jobs, and they can
  change its workflows anyway, so the cache stays (5 GB) to keep jobs fast.
  With `org`, one runner serves every repository of the organization, and the
  cache would let one repository's jobs reach another's. There the default is
  `github_runner_tool_cache_max_gb: 0`, the strict mode: the cache is emptied
  before every job, and the `setup-*` actions download their tools each time.
  Set 0 with `repo` too to get the strict mode, or a size with `org` if you
  accept the risk.
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

## Remote host (ci-1)

`ci-1` is an Oracle Cloud Always Free machine (ARM, 2 OCPU, 12 GB, 200 GB
disk, Ubuntu 24.04, user `ubuntu`) that takes work off the laptop. The
playbook sets up its base, the `ci_host` role: reachable only through the
tailnet, security updates, swap, never idle in Oracle's eyes, and Docker. On
top of it, GitHub Actions runners for private repositories
([Runner on ci-1](#runner-on-ci-1)), and the PR previews the preview host's
Coolify runs there ([Previews on ci-1](#previews-on-ci-1)).

### What is open

**Nothing on the internet.** Three locks, each enough on its own for SSH:

- **Oracle's security list** has no TCP port open, ICMP only (set by hand in
  the Oracle console, not by the playbook).
- **ufw on the host** denies every incoming connection and accepts everything
  that arrives on `tailscale0`. Outgoing is free. ufw's own defaults also let
  in replies to the host's connections, DHCP and ping.
- **sshd** takes keys only, never root, and only from tailnet addresses
  (`AllowUsers *@100.64.0.0/10 *@fd7a:115c:a1e0::/48`, in
  `/etc/ssh/sshd_config.d/00-preview-infra.conf`). It still listens on every
  address: bound to the Tailscale one, it would fail when it starts before
  `tailscaled` at boot, and lock the host.

Whoever is on your tailnet reaches every port of `ci-1` that the
[Tailscale ACL](#tailscale-acl) lets through. With a
[tailnet-only zone](#tailnet-only-zones) on `ci-1`, Tailscale Serve answers
on port 443 of its tailnet address and forwards it to Traefik on 127.0.0.1.

**Docker ignores ufw.** A container published with `-p 8080:80` answers on the
public address whatever ufw says, because Docker's rules come first. The
playbook adds two rules to `DOCKER-USER`, the chain Docker leaves to us (in
`/etc/ufw/after.rules`, so ufw restores them): new connections that arrive on
the public interface (`ens3`) toward a container are dropped. Through the
tailnet and on 127.0.0.1, published ports keep working. Still, publish on
`127.0.0.1:` or the tailnet address, never on all addresses.

**Oracle's own iptables rules** (port 22 from anywhere, the rest rejected,
restored at boot by `iptables-persistent`) are replaced by ufw. Their
`InstanceServices` chain, which lets only root reach the instance's storage
endpoints, is kept in `/etc/ufw/after.rules`. The original file stays at
`/etc/iptables/rules.v4.before-ufw`.

### Set it up

1. `ci-1` is on the tailnet (`sudo tailscale up`) and `ssh ubuntu@ci-1` works
   from your machine through it.
2. **Give `ubuntu` a password for the console** before anything else:
   `sudo passwd ubuntu`, and keep it in 1Password. SSH never accepts it; it
   is what lets you in through Oracle's console when the tailnet is down
   ([below](#when-the-tailnet-is-down)).
3. In `group_vars/all/local.yml` (this repository is public, so neither goes
   in git):

   ```yaml
   ci_host_address: ci-1            # its MagicDNS name or Tailscale address
   ci_host_ntfy_topic: <long random name>
   ```

   Subscribe to the same topic in the ntfy app. Whoever knows the topic can
   read and post to it.
4. Run the play alone (no `-K`: `ubuntu` has passwordless sudo on Oracle's
   images):

   ```bash
   ansible-playbook site.yml --limit ci
   ```

   Its first tasks check that the playbook reached `ci-1` through the
   tailnet; over any other path it stops before changing anything. While
   `ci_host_address` is empty, the play skips `ci-1`.

Afterwards, on `ci-1`:

| Check | Expected |
|---|---|
| `sudo ufw status verbose` | `Default: deny (incoming), allow (outgoing), deny (routed)` and `Anywhere on tailscale0  ALLOW IN  Anywhere` |
| `sudo sshd -T \| grep -E '^(passwordauth\|permitroot\|allowusers)'` | `passwordauthentication no`, `permitrootlogin no`, `allowusers *@100.64.0.0/10` ... |
| `swapon --show` | `/swapfile file 4G` |
| `journalctl -u memory-floor` | `holding <n> MiB`; `free -h` shows about 30% used |
| `cat /etc/apt/apt.conf.d/52preview-infra-upgrades` | `Automatic-Reboot-Time "07:30"` |
| `docker info --format '{{ .CgroupDriver }}'` | `systemd` |

From outside the tailnet, `nc -vz -w 5 <public IP> 22` times out.

### Updates and reboots

`unattended-upgrades` installs Ubuntu's security updates every day, when
`apt-daily-upgrade.timer` fires (06:00-07:00 by default,
`systemctl list-timers apt-daily-upgrade.timer`). An update that needs a
reboot (a kernel, libc) never reboots the host right away: it reboots at
`ci_host_reboot_time`, 07:30 of the host's clock. That is UTC on Oracle's
images, 04:30 in Brasília. `/var/run/reboot-required` exists while one is
pending; `journalctl -u unattended-upgrades` and
`/var/log/unattended-upgrades/` show what was installed.

### Swap

A 4 GB `/swapfile` (`ci_host_swap_mb`), so a build that outgrows the memory
slows down instead of being killed.

### Never idle

Oracle reclaims an Always Free instance when, over 7 days, the 95th percentile
of its CPU, its network **and** its memory use are all below 20%. Keeping one
of the three above is enough, and memory costs nothing to keep: the
`memory-floor` service holds memory until the host's use (`MemTotal -
MemAvailable`) reaches `ci_host_memory_floor_percent`, 30% (about 3.6 GB of
12). When the real load uses that much itself, it gives its memory back, in
chunks of 64 MB, checked every 10 seconds; otherwise it sleeps. Its memory is
locked so it is never swapped out (swapped, it would not count), and it is the
first process the kernel kills if the memory runs out.

If the use stays below 20% for an hour anyway (`ci_host_memory_alert_percent`,
`ci_host_memory_alert_after`), it posts once to the ntfy topic, and logs it
in `journalctl -u memory-floor`. Look at `systemctl status memory-floor`
then.

Oracle measures the memory through the Oracle Cloud Agent: in the Oracle
console, the instance's *Oracle Cloud Agent* tab should show *Compute Instance
Monitoring* enabled, and *Metrics* should show memory around 30%.

### Runner on ci-1

`ci-1` runs the same `github_runner` role as the preview host
([GitHub Actions runner](#github-actions-runner-optional)): one job per
container, private repositories only, each job with its own Docker daemon.
Its settings are `ci_host_runner_*` instead of `github_runner_*`, so the two
hosts' runners are turned on and off separately; the token, the images, Docker
in Docker and the tool cache are shared settings. Docker comes from Ubuntu's
packages (`docker.io`, `docker-compose-v2`), so the daily security updates
cover it.

1. **Token.** The same `GITHUB_RUNNER_TOKEN` or `github_runner_token_ref` as
   the preview host. The repository must be among the token's *selected
   repositories*.
2. **Settings** in `group_vars/all/local.yml`:

   ```yaml
   ci_host_runner_enabled: true
   ci_host_runner_repos:
     - your-user/private-repo
   ci_host_runner_instances: 1
   ```

3. **Run** `ansible-playbook site.yml --limit ci`. It stops if a repository is
   public or the token cannot read it.
4. **Check.** The repository's Settings → Actions → Runners lists
   `ci-1-<owner>-<repo>-1-<time>` as *Idle*, with the label `ARM64`. On
   `ci-1`: `systemctl status 'github-runner@*'`.
5. **Use it** in the workflow:

   ```yaml
   runs-on: [self-hosted, ARM64, ci-1]
   ```

   A job that asks for `preview-host` still goes to the laptop, so the same
   repository can move one job at a time. Once all of them are on `ci-1`,
   retire the laptop's runners of that repository
   ([Retiring a runner](#retiring-a-runner)).

| Setting | Default | |
|---|---|---|
| `ci_host_runner_labels` | `[self-hosted, linux, ARM64, ci-1]` | |
| `ci_host_runner_cpu_quota` | `200%` | both cores |
| `ci_host_runner_memory_max` | `8G` | leaves 4 GB to the system and Docker |
| `ci_host_runner_min_free_disk_gb` | `20` | |
| `ci_host_runners` | `[]` | several runners per repository, below |

### Several runners per repository

With one runner per repository, a 30-second lint waits behind a 15-minute
E2E. `ci_host_runners` (`github_runners` on the preview host) gives a
repository several runners, each with labels of its own, its own
`github-runner@` unit, compose project, work directory, tool cache and
limits. Nothing about the layout is in the code; for `ci-1` (2 cores, 12 GB):

```yaml
ci_host_runner_enabled: true
ci_host_runner_repos: []             # replaced by the list below
ci_host_runners:
  - repo: your-user/private-repo
    name: heavy                      # Rust builds, browser E2E
    labels: [ci-heavy]
  - repo: your-user/private-repo
    name: light                      # change detection, lint, audit, migrations, docs
    labels: [ci-light]
    cpu_quota: 50%                   # half a core
    memory_max: 2G
```

- **Each entry** has `repo` and `name` (lowercase letters, digits, `_`), and
  may have `labels`, `instances` (default 1), `cpu_quota` and `memory_max`.
  The list replaces `ci_host_runner_repos` and `ci_host_runner_instances`:
  the playbook stops if both are set. Repository runners only (scope `repo`).
- **Units.** `github-runner@<owner>-<repo>-<name>-<n>.service`, with its work
  directory and tool cache in `/var/lib/github-runner/<owner>-<repo>-<name>-<n>/`.
  On GitHub: `ci-1-<owner>-<repo>-<name>-<n>-<time>`.
- **Limits.** Each entry's jobs run in a slice of their own,
  `github_runner-<owner>_<repo>_<name>.slice`, inside `github_runner.slice`,
  whose limits (`ci_host_runner_cpu_quota`, `ci_host_runner_memory_max`)
  still cap all of them together. The light runner above never takes more
  than half a core and 2 GB, so the heavy one keeps at least 1.5 cores and
  6 GB. An entry without limits gets only the shared ones; its instances
  share its slice. `systemd-cgtop github_runner.slice` shows each one.
- **Labels describe capability, not the machine:** `ci-heavy`, `ci-light`
  and the arch (`ARM64`). A later CI router chooses by label, and other CI
  machines can join with the same labels. Each runner registers with
  `ci_host_runner_labels` plus its own, so the one above carries
  `[self-hosted, linux, ARM64, ci-1, ci-heavy]` or `[..., ci-1, ci-light]`.
  Workflows pick one with `runs-on: [self-hosted, ARM64, ci-light]`.
- **Keep `ci-1` on both runners for now.** Jobs already in the queue, and
  workflows not yet moved, ask for `[self-hosted, ARM64, ci-1]`: with `ci-1`
  on both runners either one takes them, and nothing is stranded. Move every
  workflow of the listed repositories to `ci-heavy` or `ci-light` first. Drop
  `ci-1` (`ci_host_runner_labels: [self-hosted, linux, ARM64]`) only once no
  workflow on any branch still says `ci-1` and no job waits for it
  (`gh run list --status queued`); a job that still asks for it then waits in
  the queue until it is cancelled.
- **Switching** from `ci_host_runner_repos` stops the old
  `github-runner@<owner>-<repo>-<n>` and cancels a job it is running: switch
  while it is idle. Taking an entry out stops its runners and removes its
  slice.

**ARM.** Jobs run on `linux/arm64`:

- The runner, `docker:dind`, `actions/setup-node`, `setup-python`,
  `dtolnay/rust-toolchain` and Playwright's Chromium all have arm64 builds.
  An action or tool that ships only an x64 binary fails with `exec format
  error`; pin an arm64 build or keep that job on `ubuntu-latest`.
- **Images built here are arm64.** The preview host is x64 and cannot run
  them. To publish one image for both, build with
  `docker buildx build --platform linux/amd64,linux/arm64` after
  `docker/setup-qemu-action`, which emulates x64 and is several times
  slower.

**Memory.** `memory-floor` holds memory only while the jobs use less than 30%
of the host, and gives it back, 64 MB every 10 seconds, as they grow. A job
that grows faster than that can push the host into swap for a few seconds;
`memory-floor` is the first process the kernel kills if memory runs out. Jobs
themselves never swap (`MemorySwapMax=0`): past 8 GB, the kernel kills a
process of the job.

To turn the runners off, set `ci_host_runner_enabled: false` and rerun
`--limit ci`; [Turn it off](#turn-it-off-back-to-githubs-runners) has the
rest. Docker stays installed.

### When the tailnet is down

Oracle's console reaches the host's serial console without the network:

1. Oracle console → Compute → Instances → `ci-1` → *Console connection* →
   *Launch Cloud Shell connection*. Press Enter and log in as `ubuntu` with
   the console password (Set it up, step 2).
2. Bring Tailscale back: `sudo tailscale status`, then
   `sudo systemctl restart tailscaled`, or `sudo tailscale up`, which prints a
   login URL to open on another device when the host was logged out.
3. Only if you need SSH without the tailnet: open port 22 in the security
   list to your own IP alone, then on the console
   `sudo ufw allow from <your IP> to any port 22 proto tcp` and
   `sudo rm /etc/ssh/sshd_config.d/00-preview-infra.conf && sudo systemctl reload ssh`.
   Once the tailnet is back, close the port in the security list again and
   rerun the playbook, which restores both. Remove the ufw rule by hand:
   `sudo ufw delete allow from <your IP> to any port 22 proto tcp`.

Without a console password, Oracle's *Run command* (Oracle Cloud Agent) or a
reboot into single-user mode from the serial console are the ways in; both
are in Oracle's documentation on console connections.

## Previews on ci-1

The PR previews of some apps can run on `ci-1` instead of the preview host,
so no pull request code runs on the preview host, not even its build. The
preview host's Coolify still receives GitHub's webhooks, and manages `ci-1`
as a second server over SSH through the tailnet. It builds each preview there
(natively, `linux/arm64`) and runs it on a Docker network of the app's own.
`ci-1` publishes the previews through a Cloudflare tunnel of its own, or,
on a [tailnet-only zone](#tailnet-only-zones), on its tailnet address only,
and still has no port open to the internet. Production stays where it is.

```
PR opened ─► GitHub App webhook ─► coolify.example.com/webhooks/ ─► Coolify on the preview host
                                                                        │ SSH as root, through the tailnet:
                                                                        │ builds and runs the preview on ci-1
                                                                        ▼
https://pr-12.example.dev ─────────┬─► Cloudflare ─► ci-1's tunnel ─► Traefik ─► preview-access ─► preview
https://pr-36-preview.example.com ─┘   (Access on      (cloudflared)   127.0.0.1   PR hosts only,     network
                                        example.dev)                               token, noindex     previews-<app>

https://main.example.dev ─► Cloudflare (Access) ─► preview host's tunnel ─► Traefik ─► production (unchanged)
```

**What moves.** Each zone of `ci_previews_zones` gets its wildcard DNS
record pointed at `ci-1`'s tunnel (`ci_previews_tunnel_name`), and loses its
wildcard route on the preview host's. Cloudflare can't route `pr-*` alone
(DNS and tunnel wildcards are `*.<zone>` only), so the names the preview host
keeps on those zones each get a record and a route of their own: list them
in `production_hostnames` (`main.example.dev`, for one). Coolify's webhook
host stays as it is. On `ci-1`, Traefik answers only for
`pr-<n>.<zone>` and `pr-<n>-<name>.<zone>` (`ci_previews_host_pattern`):
any other name there gets 403 from `preview-access`, with *this host
serves pull request previews only*. The zones of `preview_zones` stay behind
their Access application, which the preview host's play keeps, and
`preview-access` on `ci-1` checks the same token as on the preview host. The
others stay public, as before. Every answer from `ci-1` carries
`X-Robots-Tag: noindex, nofollow`, on top of the apps' own.

**Tailnet-only zones on `ci-1`.** A zone in both `ci_previews_zones` and
[tailnet-only](#tailnet-only-zones) gets no route on `ci-1`'s tunnel: its
wildcard is a grey `A` record with `ci-1`'s tailnet address, Tailscale Serve
there forwards TCP 443 to Traefik, and Traefik holds one wildcard
certificate for the zone, through DNS-01 with `ci-1`'s own token
(`ci_previews_dns_token_file`). No Access application is needed for it. With
every zone of `ci_previews_zones` tailnet-only, `ci-1` runs no cloudflared at
all, and the playbook takes it down.

**Two apps per repository.** Coolify deploys a PR preview on the server of
the application it belongs to. An app whose production stays on the preview
host gets a second Coolify application from the same repository, on `ci-1`,
with *Preview Deployments* on, and the first one gets them off. The one on
`ci-1` is never deployed itself: the playbook keeps its *Auto Deploy* off
(pushes to `main` skip it), and its domain (`previews.<zone>`, printed at the
end of the run) is refused there anyway. Its `preview_apps` entry has
`server` and `network`:

```yaml
preview_apps:
  - name: myapp                    # production, on the preview host
    repository: you/myapp
    zone: example.dev
    port: 8740
    healthcheck_path: /health
    data_path: /data
    previews: false
    production_env: {}
  - name: myapp-previews           # its PR previews, on ci-1
    repository: you/myapp
    zone: example.dev
    port: 8740
    healthcheck_path: /health
    data_path: /data
    server: ci-1                   # ci_previews_coolify_server
    network: myapp                 # one of ci_previews_networks
    previews: true
    memory: 768m                   # optional: ci_previews_memory otherwise
    preview_env: {}
```

An app with nothing on the preview host (production elsewhere) needs only
the second entry. Its old application on the preview host, if any: delete
it, or at least turn its *Preview Deployments* off, or its PRs get a preview
there too.

### What a preview reaches

| From a preview container on `ci-1` | |
|---|---|
| its own network (`previews-<app>`), Traefik included | yes |
| the internet | yes |
| the tailnet (the preview host, your devices) | no |
| `ci-1` itself (sshd, Docker, anything on its addresses) | no: ufw denies every interface but `tailscale0` |
| the runners' job networks, `coolify`, another app's previews | no |
| Oracle's network (`10.0.0.0/8`) and its metadata (`169.254.0.0/16`) | no, except DNS (port 53) to Oracle's resolver, `169.254.169.254` |

The `ci_host` role loads `/etc/preview-infra/preview-isolation.nft`, a table
of its own hooked before iptables' FORWARD chain: Tailscale's `ts-forward`
accepts everything that leaves on `tailscale0`, and can come before Docker's
`DOCKER-USER` there, so the rules can't live in `DOCKER-USER`. A drop in this
table is final whatever the others accept. Check it with
`sudo nft list table inet preview_isolation`.

Coolify logs in to `ci-1` as root, with the key in `ci_previews_coolify_key`,
from `ci_previews_coolify_address` only: a `Match User root Address` block at
the end of `/etc/ssh/sshd_config` and a `from=` on the key in
`/root/.ssh/authorized_keys`. Every other login keeps `ci_host`'s rules.

What is still open, and why:

- **Builds use the host's network.** Coolify runs `docker build --network
  host`, so a `RUN` step of a PR's Dockerfile can reach what `ci-1` reaches,
  the tailnet included. That is the same trust as the Actions jobs on
  `ci-1`, which are privileged: only repository members get previews (PRs
  from forks never do), and only private repositories get the runner.
  Narrow it in Tailscale's access controls: let nothing on `ci-1` open a
  connection to your other devices (Coolify's SSH goes the other way).
- **The tunnel token is on `ci-1`** (`/opt/cloudflared/.env`, root only). A
  job that becomes root could serve other names of `ci_previews_zones` with
  it. The Cloudflare API token never goes there: the play calls the API
  from the machine you run it on.
- **A PR closed while the preview host is off** loses its webhook, and its
  preview stays (stopped by `preview-limit` once newer ones need the room).
  Delete it in Coolify: the application → *Preview Deployments* → *Delete*.

### Limits

- **Each preview:** `ci_previews_cpus` (1) and `ci_previews_memory` (1g),
  without swap, or `cpus` and `memory` on its `preview_apps` entry. The
  playbook sets them in Coolify (*Resource Limits*), which applies them to
  every preview of the app.
- **At once:** `ci_previews_max` previews (3), all apps together.
  `preview-limit.timer` checks every minute and stops those deployed
  longest ago; a push to their PR, or *Redeploy* in Coolify, starts them
  again (`journalctl -u preview-limit` lists what it stopped).
- **Builds:** one at a time on `ci-1` (`ci_previews_concurrent_builds`),
  queued in Coolify. A build has no memory limit of its own.
- **Cleanup:** when a PR closes, Coolify removes its preview and its volume.
  `coolify-prune-images` runs daily on `ci-1` too (see [Cleanup](#cleanup)),
  and Coolify's own forced cleanup stays off there: it would remove the
  runners' images.

**Memory budget** (`ci-1`: 12 GB, 4 GB of swap, 2 cores):

| | Memory | |
|---|---|---|
| system, Docker, Tailscale, cloudflared, Traefik, preview-access | ~0.6 GB | no limit |
| Actions runners and their jobs (`github_runner.slice`) | up to 8 GB | `ci_host_runner_memory_max`, no swap; a light runner of `ci_host_runners` its own `memory_max` within it |
| previews | up to 3 GB | `ci_previews_max` × `ci_previews_memory`, no swap |
| one preview build | ~2-3 GB | no limit; it may swap |
| `memory-floor` | 0 to 3.6 GB | holds memory only while the rest uses less than 30% |

`memory-floor` never competes: it holds memory only while the host uses
less than 30% (3.6 GB), and gives it back 64 MB every 10 seconds as the rest
grows. Three idle previews and an idle runner already use a good part of
that. The limits together pass 12 GB only when the CI is at its 8 GB cap,
three previews run at their limit and a build runs, all at once. Then the
build and the system go into swap and slow down; the jobs and previews
cannot swap and are killed at their own limit, never each other's. If
`free -h` shows swap in use often, lower `ci_host_runner_memory_max` (6G) or
`ci_previews_max` (2). `systemd-cgtop` and `docker stats` show who uses what.

CPU: the runners get both cores at most (`ci_host_runner_cpu_quota`) and
half the weight of everything else, so under contention builds and previews
get two thirds. Each preview has its own `ci_previews_cpus`.

### Turn it on

In Coolify, in Cloudflare and in `local.yml`, in this order. The API token
needs nothing new: it already edits DNS on both zones and the tunnels of the
account. Each run is the whole playbook, `ansible-playbook site.yml -K`
(the preview host's play first, then `ci-1`'s); `--limit ci` alone refuses
to move a wildcard before the preview host has the records of its own names.

1. **A key for `ci-1` in Coolify:** *Keys & Tokens → Private Keys → + Add →
   Generate new ED25519 SSH Key*, name `ci-1`, *Continue*. Copy its public
   key.
2. **Settings** in `group_vars/all/local.yml`:

   ```yaml
   ci_previews_enabled: true
   ci_previews_zones: ["{{ domain }}", example.dev]
   production_hostnames: [main.example.dev]   # what the preview host keeps there
   ci_previews_coolify_address: 100.x.y.z     # `tailscale ip -4` on the preview host
   ci_previews_coolify_key: ssh-ed25519 AAAA...
   ci_previews_networks: [myapp, site]        # one per app
   ```

   A [tailnet-only zone](#tailnet-only-zones) among `ci_previews_zones` needs
   `ci-1`'s own Cloudflare token in `ci_previews_dns_token_file` on `ci-1`
   first; the run stops without it.
3. **Run the playbook.** The preview host gets the records and routes of
   `production_hostnames` and drops the wildcards; `ci-1` gets Coolify's
   login, the networks `previews-myapp` and `previews-site`, its tunnel (Zero
   Trust → Networks → Tunnels shows `ci-1-previews` *Healthy*) and the
   wildcards. Old previews on the preview host stop answering then.
   `Coolify has no server named 'ci-1' yet` is expected.
4. **`ci-1` in Coolify:** *Servers → + Add*: name `ci-1`
   (`ci_previews_coolify_server`), IP address: `ci-1`'s **Tailscale IP**
   (`tailscale ip -4` there; Coolify resolves names inside its container,
   without MagicDNS), port 22, user `root`, key `ci-1`. *Continue*, then
   *Validate Server*: Docker is already there. Leave *Sentinel* (metrics)
   off: it would have `ci-1` call the preview host.
5. **A destination per network:** *Servers → ci-1 → Destinations → + Add*,
   name and network `previews-myapp`; the same for each of
   `ci_previews_networks`. Coolify keeps the networks the playbook made.
6. **Run the playbook again.** It sets `ci-1`'s proxy (127.0.0.1 only,
   `preview-access`, noindex) and starts it, and checks that
   `https://preview-infra-check.<domain>/` answers 403 from `ci-1`.
7. **The applications on `ci-1`:** for each app, *Projects → the project →
   + Add → Resource → Private Repository (with GitHub App)*, server `ci-1`, destination
   `previews-<network>`, the repository and `main`, and the values the run
   printed under *Coolify settings for the preview apps*. Name it as its
   `preview_apps` entry (`myapp-previews`). Don't press *Deploy*.
8. **`preview_apps`** in `local.yml` as [above](#previews-on-ci-1):
   `previews: false` on the app that stays, the entry with `server` for the
   one on `ci-1`, and its `preview_env` moved there. **Run the playbook a
   third time:** it sets the limits and *Auto Deploy* off on `ci-1`'s apps,
   previews off on the others, and the variables.
9. **Open a test PR** (below).

### Check

| Where | Command or URL | Expected |
|---|---|---|
| the PR | the GitHub App's comment | a link `http://pr-<n>.example.dev` (or `pr-<n>-preview.<domain>`) |
| Coolify | the app on `ci-1` → *Preview Deployments* | the PR, *Running*, deployed on `ci-1` |
| browser | `https://pr-<n>.example.dev` | the Access login, then the app |
| browser | `https://pr-<n>-preview.example.com` | the site, no login |
| anywhere | `curl -sI https://pr-<n>-preview.example.com \| grep -i x-robots-tag` | `x-robots-tag: noindex, nofollow` |
| `ci-1` | `docker ps --filter label=coolify.pullRequestId=<n>` | the preview's container |
| `ci-1` | `docker inspect -f '{{json .NetworkSettings.Networks}}' <container>` | `previews-<app>` only |
| `ci-1` | `curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: main.example.dev' http://127.0.0.1/` | `403` |
| `ci-1` | `docker run --rm --network previews-<app> busybox wget -T 5 -qO /dev/null http://example.com && echo out` | `out`: the internet |
| `ci-1` | `docker run --rm --network previews-<app> busybox nslookup example.com` | an address: DNS works |
| `ci-1` | `docker run --rm --network previews-<app> busybox wget -T 5 -qO- http://100.100.100.100/` | `download timed out`: no tailnet |
| `ci-1` | `docker run --rm --network previews-<app> busybox wget -T 5 -qO- http://169.254.169.254/opc/v2/instance/` | `download timed out`: no instance metadata |
| `ci-1` | `systemctl list-timers preview-limit.timer coolify-prune-images.timer` | both scheduled |
| preview host | `docker ps --filter label=coolify.pullRequestId=<n>` | nothing |
| browser | `https://main.example.dev` | production, unchanged |

Close the PR: within a minute the preview's container and volume are gone
from `ci-1` (`docker ps -a --filter label=coolify.pullRequestId=<n>` and
`docker volume ls --filter name=-pr-<n>` list nothing).

### Rollback

1. Set `ci_previews_enabled: false` in `local.yml` (keep the rest), turn
   *Preview Deployments* back on for the apps on the preview host (or
   `previews: true` on their entries), and run the playbook. The preview
   host takes the wildcards back (route and DNS), and `ci-1` stops its
   cloudflared, `preview-access`, `preview-limit` and Coolify's root login.
2. In Coolify, delete the applications on `ci-1` (their previews go with
   them), stop its proxy (*Servers → ci-1 → Proxy → Stop*), then delete the
   server `ci-1`.
3. What stays, to remove by hand when you are done: the tunnel
   `ci-1-previews` (Zero Trust → Networks → Tunnels → Delete), the
   `previews-*` networks on `ci-1` (`docker network rm`), and the key `ci-1`
   in Coolify.

## Retiring a runner

A runner whose repository no longer sends it jobs still waits for one, with
its own registration on GitHub. To retire it:

1. Check that no workflow asks for it: the repository's `.github/workflows/`
   has no `runs-on` with its labels (`preview-host` for the preview host).
2. Take the repository out of `github_runner_repos` (or `ci_host_runner_repos`)
   in `local.yml`, and run the playbook. It stops `github-runner@<owner>-<repo>-<n>`,
   and the runner deletes its registration on GitHub as it stops.
3. Check on GitHub: the repository's *Settings → Actions → Runners* lists no
   `preview-host-<owner>-<repo>-…`. From a shell:
   `gh api repos/<owner>/<repo>/actions/runners --jq '.runners[] | [.id, .name, .status] | @tsv'`.
   A leftover one (the host was off when it stopped) goes with *Remove
   runner* there, or `gh api -X DELETE repos/<owner>/<repo>/actions/runners/<id>`.
4. If no host serves the repository anymore, take it out of the token's
   *Repository access* (GitHub → Settings → Developer settings → Fine-grained
   tokens → the runner token). The token is shared by both hosts.

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
- **Dashboard says realtime is not connected.** Open it at
  `https://<coolify_dashboard_hostname>`, the instance domain the playbook
  sets, not at another name. Check `tailscale serve status`: port 443 should
  be a TCP forward to `127.0.0.1:443`, with no HTTPS routes.
- **The dashboard or a tailnet-only preview has a certificate error.** Traefik
  had no certificate yet. `docker logs coolify-proxy 2>&1 | grep -i acme`
  shows why; usually the token (wrong zone, missing permission). Fix the
  file and restart the proxy (`docker restart coolify-proxy`).
- **"Permission denied (publickey)" on the first deploy.** The application was
  created as *Private Repository (with Deploy Key)* and picked the GitHub App's
  key, which GitHub does not accept for SSH. Create it as *Private Repository
  (with GitHub App)* instead; that is also what makes PR previews and comments
  work.
- **Changed a domain and Traefik still routes the old one.** Coolify stores the
  container labels with the application. After changing a domain, use
  *Container labels → Reset To Defaults* (or save from the General page) and
  redeploy.
- **Deploy fails in `git clone` with `curl 92 HTTP/2 stream ... CANCEL`**
  (then `early EOF`, `invalid index-pack output`). The host's link to GitHub
  is failing, not the build: Coolify clones before building and does not
  retry. If the self-hosted runner's checkouts fail with the same error at the
  same time, it is the network. Check it from the host with
  `time git ls-remote https://github.com/<owner>/<repo>.git`, then redeploy
  once that answers in a second or two. A repository whose tip carries large
  binaries (screenshots, for one) needs a faster link: even a
  `--depth=1` clone downloads all of them.

## Why `http://` in Coolify

Cloudflare already serves HTTPS and the tunnel delivers plain HTTP to Traefik
on port 80. With `https://` domains, Traefik would redirect that HTTP request
to HTTPS. The redirect comes back through the tunnel as HTTP again, so the
browser loops. Using `http://` domains plus *Always Use HTTPS* at Cloudflare
avoids the loop. The link in the PR comment shows `http://`, and Cloudflare
redirects it to HTTPS.

## Layout

```
site.yml                         playbook: coolify, tailscale_dashboard, cloudflare_tunnel, preview_access, preview_cleanup, coolify_apps,
                                 github_runner, ci_previews (coolify.yml); then ci_host, github_runner and ci_previews on the
                                 remote host
inventory/hosts.yml              which machine is the preview host, and the remote host (ci)
group_vars/all/defaults.yml      every setting and its default
group_vars/all/local.yml         your settings (not in git; template: local.example.yml)
roles/coolify/                   runs Coolify's installer once, binds its ports to loopback, limits concurrent builds,
                                 Traefik's certificates through DNS-01 (tasks/proxy_dns01.yml)
roles/tailscale_dashboard/       dashboard on the tailnet: Tailscale Serve forwards TCP 443 to Traefik
roles/cloudflare_tunnel/         tunnel + routes + DNS via API, cloudflared in Docker,
                                 Cloudflare Access for the other zones (tasks/access.yml)
roles/preview_access/            Traefik checks the Access token of the other zones itself (preview-access,
                                 a forward-auth service on Coolify's network)
roles/preview_cleanup/           daily removal of unused Coolify images, Coolify cleanup mode
roles/coolify_apps/              the applications in coolify_apps, through Coolify's API,
                                 and the environment variables of every app (tasks/env.yml)
roles/github_runner/             optional GitHub Actions runners for private repositories, one container per job
roles/ci_host/                   remote host base: ufw (tailnet only, DOCKER-USER), sshd, security updates, swap,
                                 memory-floor (never idle for Oracle), the previews' network isolation (nftables), Docker
roles/ci_previews/               PR previews on the remote host: Coolify's login, networks, its own tunnel, preview-access,
                                 preview-limit; and the remote server's settings in the preview host's Coolify (coolify.yml)
tests/tunnel_routes.yml          offline check of the tunnel routes and DNS names
tests/coolify_env.py             the coolify_apps role against a fake Coolify API
tests/coolify_build_limit.yml    offline check of the build limit task
tests/github_runner_settings.yml offline check of the runner instance names and defaults, and of ci-1's
                                 runner settings in site.yml
tests/github_runner_job.py       github-runner-job against a fake GitHub API
tests/ci_host_settings.yml       offline check of the remote host's sshd, ufw, memory-floor and update files
tests/memory_floor.py            how much memory memory-floor holds, and its ntfy warning, on a fake host
tests/preview_access.py          which requests preview-access lets through, with tokens signed by openssl
tests/ci_previews_settings.yml   offline check of the remote host's preview routes, hosts, kept names and Traefik file
tests/ci_previews_coolify.yml    the remote server's Coolify settings against a stand-in for docker
tests/preview_limit.py           which previews preview-limit stops, against a stand-in for docker
tests/proxy_dns01.yml            Traefik's certificates through DNS-01 in Coolify's proxy configuration, against a stand-in for docker
.github/workflows/checks.yml     the syntax check and the tests above, on every pull request
```
