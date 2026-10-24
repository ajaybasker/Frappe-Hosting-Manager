# Frappe Hosting Manager

**Manage and host your Frappe projects on your own Linux computer, using your own storage, without renting a cloud server or VPS.**

`frappe-host` is a command-line tool that prepares a Ubuntu/Debian computer for Frappe, creates and manages Benches and sites, backs them up and restores them, switches to production mode (Supervisor + NGINX), and, if your network allows it, publishes a site on a domain with HTTPS.

It is a management layer around the official tools (Bench, MariaDB, Redis, NGINX, Supervisor, certbot) and does not replace them. Every command:

- **inspects first.** Read-only commands never change anything.
- **shows a plan** before changing anything. Add `--dry-run` to see only the plan.
- **asks for consent.** Use `--yes` for normal changes. Destructive or internet-facing actions make you type the site or domain name.
- **records what it did**, with an operation ID and a redacted log (`frappe-host history`).
- **is honest about networking.** A site answering on this computer is never reported as "publicly reachable".

> **Version 0.2.0, alpha.** Every command below is implemented and covered by automated tests that use simulated Benches and system tools. Real-machine verification is still in progress: see [What has been tested](#what-has-been-tested).

---

## Contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Path A: brand-new computer → running Frappe site](#3-path-a-brand-new-computer--running-frappe-site)
4. [Path B: you already have a Bench](#4-path-b-you-already-have-a-bench)
5. [Everyday use](#5-everyday-use)
6. [Backups and restore](#6-backups-and-restore)
7. [Production mode (Supervisor + NGINX)](#7-production-mode-supervisor--nginx)
8. [Putting a site on the internet](#8-putting-a-site-on-the-internet)
9. [Dashboard, health checks and diagnostics](#9-dashboard-health-checks-and-diagnostics)
10. [Command reference](#10-command-reference)
11. [Safety model](#11-safety-model)
12. [Files, state and uninstall](#12-files-state-and-uninstall)
13. [Testing checklist for a new machine](#13-testing-checklist-for-a-new-machine)
14. [What has been tested](#what-has-been-tested)
15. [Development](#15-development)

---

## 1. Requirements

| | |
|---|---|
| OS | **Ubuntu 22.04 / 24.04** or **Debian 12 / 13**, x86_64 or arm64. Windows and macOS hosts are not supported. |
| User | A normal user with `sudo` rights. **Do not run as root.** Bench must run as a normal user. |
| Python | 3.10+ to run frappe-host itself. Ubuntu/Debian already ship it. |
| Hardware | 8 GB RAM recommended (4 GB minimum), 20 GB+ free disk. |
| Frappe | `version-15` or `version-16` |

Frappe's version requirements, from the official installation guide:

| Branch | Python | Node.js | MariaDB |
|---|---|---|---|
| version-15 | 3.10–3.13 | 18+ | 10.6.6+ |
| version-16 | 3.14 | 24 | **11.8+** |

> **Ubuntu 24.04 + version-16:** Ubuntu's own MariaDB is 10.11, which is too old for v16. `init` detects this and tells you to add the official MariaDB 11.8 repository first ([see 3.2](#32-only-if-init-printed-a-manual-mariadb-step)). Debian 13 ships 11.8. On Ubuntu, `version-15` works with the stock MariaDB.

## 2. Install

```bash
sudo apt update && sudo apt install -y git pipx
pipx ensurepath                 # then open a new terminal

git clone https://github.com/ajaybasker/Frappe-Hosting-Manager.git
cd Frappe-Hosting-Manager
pipx install .                  # or: uv tool install .

frappe-host --version           # frappe-host 0.2.0
frappe-host --help
```

frappe-host has no third-party dependencies and installs into its own isolated environment. To upgrade later, run `git pull && pipx install --force .`.

## 3. Path A: brand-new computer → running Frappe site

### 3.1 Check and prepare the computer

```bash
frappe-host doctor                                # read-only report: what's ready, what's missing
frappe-host init --branch version-16 --dry-run    # see exactly what would be installed
frappe-host init --branch version-16              # do it (asks before starting; sudo asks for your password)
```

`init` installs only what is missing. Run it again at any time and it only does what's still needed:

- system packages via apt: git, build tools, Redis, MariaDB, cron, wkhtmltopdf and friends;
- MariaDB and Redis started and enabled at boot;
- `mariadb-secure-installation`, **interactive**, on a fresh MariaDB. Set a root password and remember it, because Bench asks for it when creating sites;
- [uv](https://docs.astral.sh/uv/) and the Python version the branch needs, in your home directory;
- Node.js from nodejs.org, SHA-256 verified and linked into `~/.local/bin`. If nvm already has a suitable version, init tells you to make it the default instead;
- yarn and the `bench` CLI.

Add `--production` to also install NGINX, Supervisor and certbot. Add `--secure-mariadb` to run `mariadb-secure-installation` again.

**Open a new terminal after `init`** so `~/.local/bin` is on your PATH.

### 3.2 Only if `init` printed a MANUAL MariaDB step

The tool never pipes a downloaded script into `sudo`, so on Ubuntu + v16 you add the MariaDB repository yourself:

```bash
# Official instructions: https://mariadb.org/download/?t=repo-config
# (choose your Ubuntu version and MariaDB 11.8, then follow the commands shown there), then:
sudo apt-get update
sudo apt-get install -y mariadb-server mariadb-client libmariadb-dev
frappe-host init --branch version-16 --secure-mariadb
```

### 3.3 Create a Bench and a site

```bash
frappe-host bench create frappe-bench --frappe-branch version-16    # ~5–15 min; registers it automatically
frappe-host site create school.local --bench ~/frappe-bench         # Bench asks for the MariaDB root + Administrator passwords
frappe-host site hosts school.local                                 # makes school.local resolve to this computer
```

Optionally add ERPNext or another app:

```bash
frappe-host app get erpnext --branch version-16
frappe-host app install school.local erpnext                        # takes a verified backup first
```

### 3.4 Run it

```bash
cd ~/frappe-bench && bench start        # development mode; leave this terminal open
```

Open **http://school.local:8000** and log in as `Administrator` with the password you set. In another terminal:

```bash
frappe-host site status school.local    # local HTTP check, backups, maintenance mode, ...
```

To keep it running in the background and survive reboots, see [production mode](#7-production-mode-supervisor--nginx).

## 4. Path B: you already have a Bench

```bash
frappe-host doctor                          # finds Benches in your home folder (read-only)
frappe-host bench discover                  # list candidates (read-only)
frappe-host bench register ~/frappe-bench   # records it; the Bench itself is not modified
frappe-host bench list                      # also reports "drift" if things changed outside frappe-host
frappe-host site list
frappe-host status
```

Registration only writes to frappe-host's own state. If you add sites or update apps with `bench` directly, `bench list` shows the drift. Accept the new state with `frappe-host bench refresh PATH`.

## 5. Everyday use

```bash
frappe-host status                              # all Benches, sites, recent operations
frappe-host site status SITE                    # health of one site (separate dimensions, with evidence)
frappe-host logs SITE                           # list log files
frappe-host logs SITE --file frappe.log --lines 200    # passwords/tokens are redacted
frappe-host site apps SITE                      # installed apps (asks Frappe)
frappe-host site maintenance SITE on|off
frappe-host site migrate SITE                   # verified backup first, then bench migrate
frappe-host bench update ~/frappe-bench         # verified backup of every site, then bench update
frappe-host site delete SITE                    # verified backup, then drop; you must type SITE to confirm
frappe-host history                             # audit log of everything frappe-host changed
```

When a site exists in several registered Benches, add `--bench PATH`. With only one registered Bench, `--bench` is optional everywhere.

## 6. Backups and restore

```bash
frappe-host backup SITE                               # database + public + private files, via bench
frappe-host backup SITE --copy-to /media/usb/frappe   # also copy to another disk and verify the copy
frappe-host backup list SITE
frappe-host backup verify BACKUP_ID                   # checksums, gzip integrity, tar safety
frappe-host restore SITE BACKUP_ID --dry-run
frappe-host restore SITE BACKUP_ID                    # type SITE to confirm
```

How backups and restores are protected:

- **Backups are checked as they're made.** Each backup file gets a SHA-256 checksum and owner-only permissions (0600). A backup that produced no database dump counts as failed.
- **Restore checks first.** It re-verifies the backup and refuses one taken on a newer Frappe major version.
- **Restore keeps your current data.** It takes a safety backup of the current site before restoring, and if the restore fails, the error tells you which backup to roll back to.
- **Backups never land where they could be downloaded.** `--copy-to` is refused inside the Bench's `sites/` folder, which may be web-served.
- **You're warned when backups share a disk with the live site.** A disk failure would lose both. Use `--copy-to` with another disk.

Retention, importing and scheduling:

```bash
frappe-host backup prune SITE --keep 7                 # delete older frappe-host backups (type SITE to confirm)
frappe-host backup import SITE                         # adopt Frappe's own scheduled backups (adds checksums)
frappe-host backup schedule SITE --at 02:30 --copy-to /media/usb/frappe --keep 14   # daily, via your crontab
frappe-host backup schedules                           # list
frappe-host backup unschedule SITE
```

Scheduling only edits a clearly marked block in **your user's** crontab; other crontab lines are preserved. Output goes to `~/.local/share/frappe-host/scheduled-backups.log`.

**Do a restore test** now and then. A backup that can't be restored isn't a backup:

```bash
frappe-host site create restore-test.local
frappe-host restore restore-test.local BACKUP_ID
frappe-host site status restore-test.local
frappe-host site delete restore-test.local --no-backup
```

## 7. Production mode (Supervisor + NGINX)

Production mode keeps the Bench running in the background and restarts it after crashes and reboots. Sites are served by NGINX on port 80, by hostname.

```bash
# stop `bench start` first (Ctrl+C in its terminal)
frappe-host init --production                       # installs nginx, supervisor, certbot if missing
frappe-host production enable --dry-run             # read the plan
frappe-host production enable
frappe-host production status
```

What `production enable` does, unlike `bench setup production`:

- turns on `dns_multitenant`, so sites are served by hostname;
- generates `config/supervisor.conf` and `config/nginx.conf` with Bench, running as you;
- links them into `/etc/supervisor/conf.d/` and `/etc/nginx/conf.d/`. It refuses if a file with that name belongs to something else;
- runs **`nginx -t` before reloading**. If validation fails, it removes its own links again and nothing is reloaded;
- **never deletes the default NGINX site**, never installs Ansible/fail2ban, never edits `supervisord.conf`;
- may add the "execute" permission bit to your home directory (`chmod o+x`) so NGINX can serve the site's static files. Your files stay unreadable to others, and the plan shows this step.

Open `http://school.local` (port 80) afterwards. After creating a new site in production mode, run `frappe-host production enable` again (it's idempotent) so NGINX picks it up. `frappe-host production disable` reverses the links and returns you to `bench start`.

## 8. Putting a site on the internet

**Read this first.** Local and LAN hosting need nothing extra. Public hosting from home also needs **all** of the following:

1. A domain with a DNS **A record** pointing to your public IP.
2. Your internet provider must allow incoming connections. **CGNAT** (very common on mobile and fibre connections) usually makes direct hosting impossible.
3. Your router must forward **only ports 80 and 443** to this computer. Never forward database or Redis ports.
4. This computer must stay on and connected.

```bash
frappe-host domain check erp.example.com --site school.local                       # DNS, NAT/CGNAT, ports, TLS, binding
frappe-host domain check erp.example.com --site school.local --detect-public-ip    # opt-in: asks api.ipify.org for your public IP
frappe-host publish school.local --domain erp.example.com --email you@example.com --dry-run
frappe-host publish school.local --domain erp.example.com --email you@example.com  # type the domain to confirm
```

`publish` runs a **security checklist** first and refuses to continue if:

- production mode is off;
- database or Redis ports listen beyond localhost;
- `developer_mode` is on for the site;
- the domain doesn't resolve;
- certbot or its NGINX plugin is missing.

It then:

1. adds the domain to the site;
2. regenerates the NGINX config, validates it and reloads;
3. sets `host_name` so links in emails use the domain;
4. gets a Let's Encrypt certificate with `certbot --nginx`, which needs no NGINX downtime;
5. switches the domain to HTTPS and validates and reloads again.

If any step fails, the domain is removed again (rollback). The Ubuntu certbot package renews certificates automatically through `certbot.timer`.

**Verify from outside.** frappe-host never claims "public" from your own computer. On a phone hotspot or another network, run:

```bash
frappe-host probe https://erp.example.com      # or simply open the URL in a browser on mobile data
```

To stop serving the site at a domain: `frappe-host unpublish school.local --domain erp.example.com`.

## 9. Dashboard, health checks and diagnostics

```bash
frappe-host dashboard                   # read-only web page; prints a one-time URL for http://127.0.0.1:8765
frappe-host check                       # quick health check; exit code 1 if something is wrong
frappe-host diagnostics --preview       # show what a support bundle would contain
frappe-host diagnostics                 # write a redacted .tar.gz (0600) - never uploaded anywhere
```

The dashboard listens on **127.0.0.1 only**. It needs the per-run token in the printed URL, rejects other `Host` headers (a DNS-rebinding defence) and has no buttons that change anything.

Get alerted when something breaks (via cron's email or your own wrapper):

```bash
( crontab -l 2>/dev/null; echo "*/30 * * * * $(command -v frappe-host) check --quiet" ) | crontab -
```

`check` fails on:

- low disk space;
- exposed database or Redis ports;
- backups older than 26 hours (for sites you back up with frappe-host; change it with `--max-backup-age HOURS`);
- certificates expiring within 14 days;
- sites not answering in production mode.

## 10. Command reference

Global options: `--json` (machine-readable output on stdout), `--help`, `--version`. Mutating commands accept `--dry-run` and `--yes`. Destructive or internet-facing commands accept `--confirm NAME` instead of `--yes`.

| Area | Command | What it does |
|---|---|---|
| Host | `doctor` | Read-only readiness report (OS, RAM, disk, tools, services, version requirements, exposed ports) |
| | `init [--branch B] [--production] [--secure-mariadb]` | Install what Frappe needs (see §3) |
| Benches | `bench discover [--root DIR]` | Find Benches (read-only) |
| | `bench register PATH` / `unregister PATH` | Start/stop managing an existing Bench (files untouched) |
| | `bench list` / `bench refresh PATH` | List, with drift detection / accept current state |
| | `bench create NAME [--frappe-branch B] [--dir PARENT]` | `bench init` and register |
| | `bench update [PATH] [--no-backup]` | Verified backups, then `bench update` |
| Sites | `site list` / `site status SITE` | Inventory / health |
| | `site create SITE [--install-app APP]… [--db-type mariadb\|postgres]` | `bench new-site` (Bench prompts for passwords) |
| | `site delete SITE [--no-backup]` | Backup, then `bench drop-site` (**type SITE**) |
| | `site migrate SITE` · `site maintenance SITE on\|off` | Lifecycle |
| | `site hosts SITE` | Add `127.0.0.1 SITE` to /etc/hosts (once) |
| | `site apps SITE` | Installed apps |
| | `logs SITE [--file NAME] [--lines N]` | Redacted logs |
| Apps | `app get NAME\|https-URL [--branch B]` | `bench get-app` |
| | `app install SITE APP [--no-backup]` | Backup, then `install-app` |
| Backups | `backup SITE` (= `backup create SITE`) `[--copy-to DIR]` | Create + checksum (+ verified copy) |
| | `backup list [SITE]` · `backup verify ID` | Inventory · integrity |
| | `backup prune SITE --keep N` | Retention (**type SITE**) |
| | `backup import SITE` | Adopt existing Frappe backups |
| | `backup schedule SITE --at HH:MM [--copy-to DIR] [--keep N]` · `schedules` · `unschedule SITE` | Daily cron backups |
| | `restore SITE BACKUP_ID [--no-pre-restore-backup]` | Verified restore (**type SITE**) |
| Production | `production status\|enable\|disable [BENCH]` | Supervisor + NGINX |
| Internet | `domain check DOMAIN [--site SITE] [--detect-public-ip]` | DNS / NAT / ports / TLS diagnosis |
| | `publish SITE --domain D [--email E] [--no-tls]` | Domain + HTTPS (**type DOMAIN**) |
| | `unpublish SITE --domain D` | Remove a domain |
| | `probe URL` | Reachability test, run **from another network** |
| Tools | `status` · `history` | Overview · audit log |
| | `check [--max-backup-age H] [--quiet]` | Health check for cron |
| | `dashboard [--port P]` | Local read-only web dashboard |
| | `diagnostics [--preview] [--output FILE]` | Redacted support bundle |
| | `state path` · `state export` | Where state lives · dump it as JSON |

**Exit codes:** `0` ok · `1` operation failed (the log path is printed) · `2` invalid input or blocked preflight (nothing changed) · `3` `doctor` found blockers · `4` confirmation missing or wrong (nothing changed) · `5` not found.

## 11. Safety model

- **Root access is narrow.** frappe-host runs as you. Only specific steps use `sudo` (apt, systemctl, links in /etc, `nginx -t`, certbot), each shown in the plan with the reason.
- **Passwords never pass through frappe-host.** Bench, MariaDB and sudo prompt you directly. Nothing secret goes into command arguments, logs or the frappe-host database.
- **No shell injection.** Every command runs as an argument list, never as a shell string. Site, app, domain, branch and time inputs are checked against strict patterns.
- **Changes are reversible.** Multi-step changes roll back the earlier steps when a later one fails (for example, NGINX links are removed if `nginx -t` fails).
- **It never:**
  - disables your firewall, changes SSH, or changes database bind addresses;
  - deletes the default NGINX site;
  - overwrites configuration that belongs to something else;
  - uploads anything. `--detect-public-ip` is the only outbound request, and it's opt-in.
- **Public access is never assumed.** It is reported as *not verified* until `probe` succeeds from another network.

Details: [docs/security.md](docs/security.md).

## 12. Files, state and uninstall

| What | Where |
|---|---|
| Registry, audit log, backup inventory | `~/.local/share/frappe-host/state.db` (0600) |
| Operation logs | `~/.local/share/frappe-host/operations/*.log` (0600, redacted) |
| Node.js installed by `init` | `~/.local/share/frappe-host/node/`, linked into `~/.local/bin` |
| Backups | `BENCH/sites/SITE/private/backups/` (+ your `--copy-to` folder) |
| Production config links | `/etc/nginx/conf.d/BENCH.conf`, `/etc/supervisor/conf.d/BENCH.conf` |

Uninstall: run `frappe-host production disable` and `backup unschedule` where used, then `pipx uninstall frappe-hosting-manager`. Optionally `rm -r ~/.local/share/frappe-host`. Your Benches, sites and backups are never removed by uninstalling.

## 13. Testing checklist for a new machine

A suggested end-to-end test on a **spare** Ubuntu 24.04 or Debian 12/13 machine or VM (4 vCPU, 8 GB RAM, 40 GB disk). Each step lists what you should see.

| # | Run | Expect |
|---|---|---|
| 1 | `frappe-host doctor` | Report, with missing tools as FAIL/WARN; exit code 3 if blockers |
| 2 | `frappe-host init --branch version-15 --production --dry-run` | Plan listing only missing pieces; nothing installed |
| 3 | `frappe-host init --branch version-15 --production` | Installs; prompts for sudo and the MariaDB root password. Re-running says "Nothing to change". |
| 4 | New terminal → `frappe-host doctor` | Toolchain PASS |
| 5 | `frappe-host bench create frappe-bench --frappe-branch version-15` | Bench created and listed in `bench list` |
| 6 | `frappe-host site create test.local` → `site hosts test.local` | Site created; `/etc/hosts` gets one line, even if run twice |
| 7 | `cd ~/frappe-bench && bench start`, then `frappe-host site status test.local` | `http-local` PASS (pong); open http://test.local:8000 |
| 8 | `frappe-host backup test.local` → `backup verify ID` | Backup recorded, "archives verified" |
| 9 | `frappe-host restore test.local ID` and type a **wrong** name | Refused (exit 4), nothing changed |
| 10 | `frappe-host restore test.local ID` and type `test.local` | Safety backup, then restore; site works |
| 11 | Stop `bench start`; `frappe-host production enable` | Programs RUNNING in `production status`; http://test.local works on port 80 |
| 12 | `ls /etc/nginx/sites-enabled/` | `default` is still there |
| 13 | `frappe-host backup schedule test.local --at 03:00 --keep 7` → `crontab -l` | One marked block; your other lines untouched |
| 14 | `frappe-host dashboard` | Page opens via the printed URL; same URL without the token → 403 |
| 15 | `frappe-host publish test.local --domain x.example.com --dry-run` | Checklist; blockers listed (e.g. developer mode, DNS) |
| 16 | `frappe-host history` | Every change above listed with status |
| 17 | `frappe-host production disable` | Links removed; `bench start` works again |

For v16 on Ubuntu 24.04, do [3.2](#32-only-if-init-printed-a-manual-mariadb-step) first. Please report failures with `frappe-host diagnostics --preview` output, and the operation log path printed by the failing command.

## What has been tested

| Scope | Evidence |
|---|---|
| Automated suite: 168 tests (unit + integration with fake Bench, sudo, nginx, supervisor, certbot, crontab, apt) | Passing on Python 3.12 and 3.14 |
| Read-only commands and dry-run plans (`doctor`, `discover`, `register`, `site status`, `logs`, `init`/`production`/`publish --dry-run`, `domain check`, `diagnostics`, `dashboard`) on a real Ubuntu 24.04 machine with a Frappe 16 Bench | Pass, and Bench config files unchanged |
| `init`, `bench create`, `site create`, backup/restore, `production enable`, `publish` and scheduling **on a real machine** | **Not yet.** This is what the checklist above is for. |

See [docs/support-matrix.md](docs/support-matrix.md) for the full matrix.

## 15. Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest
```

Tests never touch the real system. They run with a temporary HOME, a fake `bench`, fake system tools, and an `/etc` sysroot under the test's temporary folder; real `sudo` is blocked. See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md).

More docs: [install](docs/install.md) · [networking](docs/networking.md) · [security](docs/security.md) · [backup & restore](docs/backup-restore.md) · [troubleshooting](docs/troubleshooting.md) · [recovery runbooks](docs/recovery-runbooks.md) · [support matrix](docs/support-matrix.md) · [decisions](docs/adr/) · [changelog](CHANGELOG.md)

License: MIT.
