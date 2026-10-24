# Changelog

All notable changes are documented here. This project follows semantic versioning; 0.x releases may change behaviour.

## [0.2.0] - 2026-10-24

Completes the roadmap through phase 7 (production, public hosting, dashboard).

### Added
- `init`: detect-then-install host preparation (apt packages, MariaDB/Redis services, `mariadb-secure-installation`, uv + Python, Node.js from nodejs.org with SHA-256 verification, yarn, bench); `--production` adds NGINX, Supervisor and certbot. If apt's MariaDB is too old for the branch, adding the MariaDB repository is a printed MANUAL step.
- `bench create` (`bench init` + register) and `bench update` (verified backups of every site first).
- `site delete` (typed confirmation, verified backup), `site migrate`, `site maintenance`, `site hosts` (idempotent /etc/hosts entry), `site apps`.
- `app get` (name or https URL, untrusted-code warning) and `app install` (backup first).
- `production enable|disable|status`: Bench-generated Supervisor/NGINX config, linked into /etc with `nginx -t` before any reload and rollback on failure. Never deletes the default NGINX site.
- `domain check` (DNS, NAT/CGNAT evidence, opt-in public IP check, ports, firewall, site binding, TLS, renewal timer), `probe URL` (reachability from the network it runs on), `publish` (security checklist, domain, NGINX, Let's Encrypt via `certbot --nginx`, rollback) and `unpublish`.
- `backup prune` (typed confirmation; frappe-host-managed backups only), `backup import`, `backup schedule|schedules|unschedule` (marked block in the user's crontab).
- `check` (health check for cron), `diagnostics` (redacted 0600 bundle, `--preview`), `dashboard` (127.0.0.1-only, token, Host-header check, CSP, read-only).
- Shared plan/step executor with sudo annotations and reverse-order rollback.

### Changed
- Child processes get `~/.local/bin` prepended to PATH so tools installed by `init` work immediately.
- Tests run with an isolated HOME, PATH without the developer's home directories, fake system tools and an /etc sysroot; real sudo is blocked.

## [0.1.0] - 2026-10-24

First development release (phases 0–4 of the roadmap).

### Added
- `doctor`: read-only host readiness checks (OS, architecture, RAM, swap, CPU, disk, toolchain, systemd services, per-Bench Python/Node/MariaDB checks against the branch, exposed internal ports, UFW config state). Exit code 3 on blockers.
- `bench discover`, `register`, `unregister`, `list` (with drift detection), and `refresh`.
- `site list`, `site status` (separate health dimensions, local `/api/method/ping` probe, LAN binding, backup freshness), and `site create` through `bench new-site`.
- `logs`: list and tail site and Bench logs, with secrets redacted.
- `backup create` (and the `backup SITE` shorthand), `backup list` (managed and unmanaged files), `backup verify` (SHA-256, gzip CRC, tar safety); `--copy-to` with verified copies.
- `restore`, with verification, a Frappe major-version gate, a pre-restore backup and typed confirmation.
- `status`, `history` (operation audit log, with interrupted-operation detection), `state path`, and `state export`.
- `--json` on all commands and `--dry-run` on all mutating commands.
- SQLite state store with schema version 1 and the migration framework.

### Not included (added in 0.2.0)
- Dependency installation, production service management, NGINX/TLS, domain and public reachability checks, scheduled backups and retention.
