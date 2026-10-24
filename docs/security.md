# Security model

To report a vulnerability, see [../SECURITY.md](../SECURITY.md).

## Controls implemented in 0.1.0

| Control | Implementation | Tested by |
|---|---|---|
| Least privilege | No command uses sudo. Lifecycle commands refuse Benches owned by another user. | `test_cli_workflows` (ownership is a preflight check) |
| No shell injection | Subprocesses take argv lists only, never `shell=True`. Non-list, non-string and NUL-containing argv are rejected. Site names, app names, branch names and backup IDs are checked against allowlists before use. | `test_validation_security` (injection strings, shell metacharacters passed literally) |
| Path traversal | `logs --file` accepts a bare file name that must exist in the site or Bench log directories, and the path must resolve inside the Bench. Discovery doesn't follow symlinks. Backup archives with absolute or `..` member names, or links escaping the archive, fail verification. | `test_contained_path_*`, `test_logs_are_redacted_and_confined`, `test_verify_rejects_unsafe_tar_and_bad_gzip` |
| Secrets never in argv | `site create` and `restore` let Bench prompt for passwords on the terminal | `test_site_create_dry_run_and_real` asserts no password appears in argv |
| Redaction | Password, secret, token, API key and encryption-key values, `--*password*` flags, URL credentials and `Authorization` headers are redacted in logs, operation records, JSON output and `logs` output | `test_redact_*`, `test_operation_success_and_log` |
| Restrictive state | State directory 0700, database and logs 0600, no secrets stored | `test_store_created_with_restrictive_permissions`, `test_registry_never_stores_site_secrets` |
| Backup file permissions | Backups created by frappe-host are set to 0600 (they contain data and `site_config` secrets). Copy destinations are created 0700. | `test_backup_create_records_checksums_and_permissions`, `test_backup_copy_to_separate_dir` |
| Backups never web-served | `--copy-to` inside the Bench's `sites/` directory is refused (it could land in `public/files`) | `test_copy_to_inside_sites_is_refused` |
| No ambiguous backup records | If more than one backup set appears during a run (e.g. Frappe's scheduler ran at the same moment), nothing is recorded and the operation fails | `test_concurrent_backup_set_is_not_recorded` |
| Untrusted binaries | `doctor` only runs a Procfile-named `node` binary if it is owned by the current user or root | code review |
| Destructive confirmation | `restore` needs the exact site name, typed or passed as `--confirm SITE`. `--yes` is not accepted. Non-interactive runs without `--confirm` are refused. | `test_restore_refuses_without_matching_confirmation` |
| Audit log | Every mutating operation gets an ID, a status, a redacted plan and a log. Interrupted operations are detected. | `test_state_operations` |
| Exposure detection | `doctor` fails if database or Redis ports listen beyond loopback | `test_exposure_classification` |
| No telemetry | No network calls except local probes to 127.0.0.1 | code review |

## Threat model (from the design document §10.2)

| Threat | Current mitigation | Gap / planned |
|---|---|---|
| Public scanning/exploitation | No public exposure features exist yet | Phase 6 security checklist and gate |
| Exposed database/Redis port | `doctor` detects it and fails | — |
| Malicious app code | Inspection is read-only and never imports Bench code | Warnings before installing unknown apps (Phase 3+) |
| Credential leakage | Bench prompts directly, redaction, 0600 files | — |
| Disk exhaustion | Disk thresholds in `doctor`; preflight in create and backup | Monitoring and alerts (Phase 2 of the product plan) |
| Bad restore or migration | Verification, version gate, pre-restore backup, typed confirmation | Maintenance-mode and staged update workflow |
| Manager overwrites config | The tool currently writes no system or Bench config | Atomic writes, diff preview and rollback in Phase 5 |
| Backups only on the same disk | Warned on every backup; `--copy-to` with checksum verification | Encryption, retention |

## Known limitations

- `logs` redaction is pattern-based. Unusual secret formats in custom app logs may not be caught, so review logs before sharing them.
- Backups are not encrypted by frappe-host. Protect `--copy-to` destinations.
- Firewall state is read from the config file only, not the live rules.
