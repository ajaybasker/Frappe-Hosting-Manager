# Security model

To report a vulnerability, see [../SECURITY.md](../SECURITY.md).

## Controls implemented in 0.2.0

| Control | Implementation | Tested by |
|---|---|---|
| Least privilege | frappe-host runs as the user. Only listed steps use `sudo` (apt, systemctl, links in /etc, `nginx -t`, supervisorctl, certbot, /etc/hosts), each shown in the plan with its reason. `init` refuses to run as root. Lifecycle commands refuse Benches owned by another user. | `test_init_runs_steps_through_sudo`, `test_init_refuses_root` |
| No silent system changes | Production mode never deletes the default NGINX site, never replaces another file in conf.d, validates with `nginx -t` before reloading, and rolls back its links on failure | `test_production_*` |
| Internet exposure gate | `publish` refuses unless production mode is on, internal ports are loopback-only, developer_mode is off, DNS resolves and certbot is present. It requires the domain typed to confirm and rolls back on failure. | `test_publish_*` |
| Honest reachability | Public access is reported as *not verified* everywhere except `probe`, which also flags answers from private addresses as LAN-only | `test_domain_check_*`, `test_probe_reports_lan_only_for_private_answers` |
| Opt-in outbound requests | The only outbound request is the public-IP lookup with `--detect-public-ip`, plus downloading Node.js during `init` (shown in the plan, SHA-256 verified) | `test_domain_check_detects_cgnat_and_never_claims_public`, `test_install_node_verifies_checksum_and_links` |
| Local dashboard | Bound to 127.0.0.1 with no option to change it. Random per-run token moved into an HttpOnly SameSite=Strict cookie. Requests with any `Host` other than loopback get 421 (DNS-rebinding defence). CSP `default-src 'none'`. Read-only. | `test_dashboard_is_token_protected_and_rejects_foreign_hosts` |
| Crontab safety | Only the marked frappe-host block is rewritten; other lines are preserved byte-for-byte | `test_schedule_preserves_other_crontab_lines` |
| App sources | `app get` accepts app names or `https://` URLs only (no options, `..` or shell metacharacters) and warns that app code runs with the user's permissions | `test_app_get_rejects_unsafe_sources` |
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
| No telemetry | No network calls except local probes, plus the explicit ones listed above. The diagnostics bundle is written locally (0600) and never uploaded. | `test_diagnostics_bundle_is_private_and_redacted` |

## Threat model (from the design document §10.2)

| Threat | Current mitigation | Gap / planned |
|---|---|---|
| Public scanning/exploitation | `publish` checklist gate, HTTPS by default, only 80/443 recommended | Rate limiting and fail2ban are left to the user |
| Exposed database/Redis port | `doctor` detects it and fails | — |
| Malicious app code | Inspection is read-only and never imports Bench code | Warnings before installing unknown apps (Phase 3+) |
| Credential leakage | Bench prompts directly, redaction, 0600 files | — |
| Disk exhaustion | Disk thresholds in `doctor`; preflight in create and backup | Monitoring and alerts (Phase 2 of the product plan) |
| Bad restore or migration | Verification, version gate, pre-restore backup, typed confirmation | Maintenance-mode and staged update workflow |
| Manager overwrites config | Only its own symlinks in /etc and its own crontab block. Bench config files are written by Bench. `nginx -t` runs before any reload, with rollback on failure. | — |
| Backups only on the same disk | Warned on every backup; `--copy-to` with checksum verification | Encryption, retention |

## Known limitations

- `logs` redaction is pattern-based. Unusual secret formats in custom app logs may not be caught, so review logs before sharing them.
- Backups are not encrypted by frappe-host. Protect `--copy-to` destinations.
- Firewall state is read from the config file only, not the live rules. frappe-host never changes firewall or router rules.
- The Node.js download is checked against nodejs.org's published SHA-256 list over HTTPS; the GPG signature is not checked.
- `production enable` may add `o+x` to the home directory so NGINX can reach static files. Contents stay unreadable, but other local users can then traverse into paths whose names they know.
