# Changelog

All notable changes are documented here. This project follows semantic versioning; 0.x releases may change behaviour.

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

### Not included
- Dependency installation (`init`), production service management, NGINX/TLS, domain and public reachability checks, and scheduled backups and retention.
