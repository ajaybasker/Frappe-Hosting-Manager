# Architecture

This describes the code as it exists in 0.2.0. The product specification is
[Frappe_Hosting_Manager_Master_Design_Document.docx](Frappe_Hosting_Manager_Master_Design_Document.docx);
decisions that the specification left open are recorded in [adr/](adr/).

## Layers

```
src/frappe_host/
├── cli/            argument parsing, prompts, human/JSON output, exit codes
│   ├── main.py         core commands + parser
│   ├── workflows.py    setup/lifecycle/production/publish/schedule/tooling commands
│   ├── common.py       exit codes, consent, run_plan()
│   ├── dashboard.py    127.0.0.1-only read-only web dashboard
│   └── output.py
├── application/    use cases
│   ├── doctor.py       HostDoctor: read-only readiness checks
│   ├── registry.py     BenchRegistry: discover / register / drift
│   ├── sites.py        SiteManager: list / status / create / logs
│   ├── backups.py      BackupManager: create / list / verify / restore
│   ├── setup.py        init (host preparation), bench create/update, Node.js installer
│   ├── lifecycle.py    site delete/migrate/maintenance/hosts, app get/install
│   ├── production.py   Supervisor + NGINX production mode (ADR 0004)
│   ├── domains.py      domain check, probe, publish/unpublish + security checklist
│   ├── schedule.py     crontab-managed scheduled backups
│   ├── diagnostics.py  redacted support bundle, `check`
│   ├── steps.py        Plan/Step executor: sudo annotation, manual steps, rollback
│   ├── operations.py   operation IDs, audit log, interrupted-op detection
│   └── state.py        SQLite state store, schema migrations
├── domain/         entities, validation allowlists, support matrix
├── adapters/       bench_fs (read-only Bench parsing), network probes, safe subprocess
├── policies/       redaction, destructive-operation confirmation
└── platform/linux/ OS release, /proc parsing, systemd state, listeners, interfaces,
                    and paths.py (the /etc locations; FRAPPE_HOST_SYSROOT prefixes them in tests)
```

Dependencies point inward: `cli → application → (domain, adapters, policies, platform)`.
The runtime has **no third-party dependencies** (Python ≥ 3.10 standard library only).

## Sources of truth

| Concern | Authoritative source | frappe-host's role |
|---|---|---|
| Bench layout, apps, versions | Files in the Bench (`sites/apps.txt`, `apps/*/__init__.py`, `.git/HEAD`) | Parsed read-only; never imports or executes Bench code during inspection |
| Sites and their settings | `sites/*/site_config.json`, `common_site_config.json` | Parsed read-only; secrets are never copied into state |
| Services | systemd, `/proc/net/tcp*` | Queried read-only |
| What frappe-host manages and has done | `state.db` (registry) | Recorded; **reconciled** against reality (`bench list` reports drift and never rewrites the Bench silently) |

## Execution model

- **Read-only commands** (`doctor`, `discover`, `list`, `status`, `logs`) do not create the state directory if it doesn't exist yet.
- **Mutating commands** follow plan → consent → execute → record:
  1. Validate input against allowlists (`domain/validation.py`).
  2. Preflight checks (ownership, disk space, existence, version compatibility).
  3. Print the plan. `--dry-run` stops here.
  4. Consent: `--yes` or an interactive `y` for non-destructive operations. Destructive operations (restore) require the **exact site name**, typed or passed as `--confirm SITE`. `--yes` is not accepted for them.
  5. Run inside `operation(...)`, which allocates an ID, writes a 0600 redacted log and records success, failure or cancellation. Operations whose process died show up as `interrupted`.
- **Multi-step workflows** (init, bench create/update, site delete/migrate, app get/install, production, publish) build a `Plan` of `Step`s from detected state. Each step is a command (optionally `sudo`, with a stated reason), an in-process action, or a MANUAL instruction that is printed but not run. Steps can register an `undo`. On failure, completed steps are undone in reverse order, and the operation is recorded as failed with "rolled back N change(s)".
- **Bench integration** uses Bench itself (`bench new-site`, `bench backup`, `bench restore`), invoked with argv lists and never through a shell. Commands that need passwords run with the terminal attached, so **Bench prompts the user directly**. frappe-host never sees those secrets.

## State

`$FRAPPE_HOST_HOME` or `$XDG_DATA_HOME/frappe-host` or `~/.local/share/frappe-host`:

```
frappe-host/            0700
├── state.db            0600  SQLite; PRAGMA user_version = schema version
├── state.db.pre-vN.bak       copy taken before migrating to schema vN
└── operations/         0700
    └── op-<ts>-<hex>.log  0600 redacted operation logs
```

Tables: `benches` (path, versions, snapshot for drift detection), `operations` (audit log), and `backups` (files, sizes, SHA-256, verification results). Migrations run in a transaction and roll back on failure. A state file newer than the tool is refused, never downgraded.

## Health model

`site status` reports separate dimensions rather than one badge: config, maintenance, scheduler, developer mode, local HTTP (`/api/method/ping` with the site's `Host` header, requiring `pong`), LAN binding, public reachability (always `unknown` until Phase 6), backup freshness and recorded verification.

## Not implemented

- systemd- or container-based production modes;
- automatic firewall and router changes (by design);
- remote or multi-user dashboard access;
- backup encryption and cloud backup destinations;
- scheduled restore drills.
