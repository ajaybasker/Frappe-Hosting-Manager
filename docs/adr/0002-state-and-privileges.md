# ADR 0002: Local state in SQLite; no privileged operations in the MVP

Status: accepted (2026-10-24). The design document leaves this open (§20.5, §20.6).

## State

- SQLite at `$XDG_DATA_HOME/frappe-host/state.db` (overridable with `FRAPPE_HOST_HOME`), with directory mode 0700 and file mode 0600.
- Versioned with `PRAGMA user_version`. Migrations are ordered functions applied in a transaction. Before migrating an existing database, a copy is saved as `state.db.pre-vN.bak`. A database newer than the tool is refused.
- Chosen over JSON for transactional writes, concurrent-safe appends to the operation log, and queryable backup history.
- No secrets are stored. Site credentials stay in the Bench's `site_config.json`.
- `frappe-host state export` dumps the state as JSON for inspection or recovery.

## Privileges

- Every 0.1.0 command runs as the invoking user, with no sudo. Lifecycle commands refuse to run if the Bench is owned by a different user, rather than escalating.
- Phase 5 (Supervisor/NGINX) will need root. The plan is a narrow set of explicitly listed commands, run through `sudo` with a displayed plan, never a general root shell. This will be decided in a separate ADR before Phase 5.
