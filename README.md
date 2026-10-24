# Frappe Hosting Manager

Manage and host your Frappe projects on your own compatible Linux computer, with your own primary storage, without renting a cloud server or VPS.

`frappe-host` is a Frappe-specific management layer around the tools you already use — Bench, MariaDB, Redis and your OS. It does not replace Frappe or Bench. It inspects before it changes anything, shows a plan, records every operation, and tells you honestly what it could and could not verify.

> **Status: alpha (0.1.0).** The read-only diagnostics, Bench registry, site inventory and backup/restore workflows are implemented and tested against fixture Benches. Production service management and public hosting are **not implemented yet** — see [Roadmap](#roadmap).

## What works today

| Command | What it does | Changes your system? |
|---|---|---|
| `frappe-host doctor` | Host readiness: OS, CPU/RAM/disk, toolchain, services, branch requirements, exposed internal ports | No |
| `frappe-host bench discover` | Find Bench directories under your home (or `--root DIR`) | No |
| `frappe-host bench register PATH` | Record an existing Bench in the frappe-host registry | Only frappe-host's own state |
| `frappe-host bench list` / `refresh` / `unregister` | Registered Benches, drift detection, accept current state, forget a Bench | Only frappe-host's own state |
| `frappe-host site list` | Sites across registered Benches | No |
| `frappe-host site status SITE` | Config, maintenance/scheduler flags, local HTTP ping, LAN binding, backup freshness | No |
| `frappe-host site create SITE` | `bench new-site` with preflight checks; Bench prompts for passwords itself | Yes (new site + database) |
| `frappe-host logs SITE [--file NAME]` | List or tail site/Bench logs, with secrets redacted | No |
| `frappe-host backup SITE [--copy-to DIR]` | `bench backup --with-files`, then SHA-256 checksums, 0600 permissions and an optional verified copy | Yes (backup files) |
| `frappe-host backup list [SITE]` / `backup verify ID` | Inventory, plus checksum and archive integrity checks | No (records the verification result) |
| `frappe-host restore SITE BACKUP_ID` | Verified, version-checked restore with a pre-restore backup and typed confirmation | **Yes — replaces site data** |
| `frappe-host status` / `history` | Summary and operation audit log | No |

Every mutating command supports `--dry-run`. Every command supports `--json` (stdout is pure JSON; human messages go to stderr).

## Quick start

```bash
git clone <this repository> frappe-hosting-manager
cd frappe-hosting-manager
pipx install .                       # or: uv tool install .

frappe-host doctor                   # read-only readiness report
frappe-host bench discover           # read-only scan
frappe-host bench register ~/frappe-bench --dry-run
frappe-host bench register ~/frappe-bench
frappe-host site status mysite.local
frappe-host backup mysite.local --copy-to /media/backup-disk/frappe
frappe-host backup verify <BACKUP_ID>
```

See [docs/install.md](docs/install.md) for details.

## What it will not do

- It will not disable your firewall, replace your NGINX configuration, change SSH, or change database bind addresses.
- It will not put passwords in command-line arguments or logs. Bench prompts you directly.
- It will not claim a site is publicly reachable because it answered on `localhost`.
- It will not delete or restore anything without you typing the exact site name.
- It will not upload telemetry or diagnostics.

## Honest limits of self-hosting

Local and LAN hosting need no public IP or domain. **Public internet hosting is conditional.** It depends on your ISP (CGNAT can make inbound connections impossible), your router, DNS, firewall, TLS, and on your computer staying powered on and connected. A personal computer is not a data centre, so you are responsible for power, updates, credentials, backups and availability. See [docs/networking.md](docs/networking.md).

## Supported platforms

Ubuntu/Debian on x86_64 or aarch64, with Frappe `version-15` and `version-16` Benches. See [docs/support-matrix.md](docs/support-matrix.md) for exactly what has been tested. Windows and macOS hosts are not supported.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 0 | Specification, repository audit, ADRs, support matrix | Done |
| 1 | Doctor and inventory | **Implemented** |
| 2 | Registry, operation log, dry-run | **Implemented** |
| 3 | Local site management (list/status/logs/create) | **Implemented** (not yet E2E-tested on a clean VM with real Bench) |
| 4 | Backup and restore | **Implemented** (restore not yet exercised against a real database) |
| 5 | Production operations (Supervisor + NGINX via Bench) | Planned |
| 6 | Public hosting: domain/DNS/TLS/firewall/CGNAT diagnostics, external reachability | Planned |
| 7 | Local-only dashboard, alerts, packaging | Planned |

`init` (installing OS dependencies), `domain check` and `publish` are **not implemented**.

## Documentation

[Architecture](docs/architecture.md) · [Support matrix](docs/support-matrix.md) · [Install](docs/install.md) · [Networking](docs/networking.md) · [Security](docs/security.md) · [Backup & restore](docs/backup-restore.md) · [Troubleshooting](docs/troubleshooting.md) · [Recovery runbooks](docs/recovery-runbooks.md) · [Decisions](docs/adr/) · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)
