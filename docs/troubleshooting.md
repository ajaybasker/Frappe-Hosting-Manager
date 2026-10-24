# Troubleshooting

Start with `frappe-host doctor`, then `frappe-host site status SITE`. Every failed operation prints its log path, and `frappe-host history` lists recent operations.

| Symptom | Likely cause | What to do |
|---|---|---|
| `doctor` exits 3, `disk ... FAIL` | Less than 5 GiB free | Free space. Check `sites/*/private/backups`, `logs/`, and old Benches. Never delete database files by hand. |
| `port 3306/6379 ... FAIL` | Database or Redis bound to `0.0.0.0` | Set `bind-address = 127.0.0.1` (MariaDB) or `bind 127.0.0.1` (Redis), restart, re-run `doctor`. Make sure your router doesn't forward these ports. |
| `python ... does not meet version-16 requirement` | Bench virtualenv built with the wrong Python | Recreate the Bench with the required Python (see the support matrix). Don't edit `env/` by hand. |
| `node ... FAIL` | Node.js older than the branch needs | Install the required version (e.g. with nvm). `doctor` uses the node path in the Procfile when there is one. |
| `http-local: ConnectionRefusedError` | Bench not running | Development mode: `bench start` in the Bench. Production: `sudo supervisorctl status`. |
| `http-local: HTTP 404` or no `pong` | Site not served on that port, or wrong `Host` routing | Check `webserver_port` in `common_site_config.json`, and that the site directory name matches the hostname. |
| `site ... exists in several Benches; pass --bench` | Same site name in two registered Benches | Add `--bench PATH`. frappe-host never guesses. |
| `Bench is owned by a different user` | Running as the wrong user | Run frappe-host as the Bench owner (for example with `sudo -iu frappe`). Do not chmod the Bench. |
| `refused: not running interactively` | Script or CI without consent | Review the plan with `--dry-run`, then pass `--yes` (non-destructive) or `--confirm SITE` (restore). |
| `bench list` shows DRIFT | Sites, apps or versions changed outside frappe-host | Review the listed changes, then `frappe-host bench refresh PATH` to accept them |
| `state schema vN is newer` | State written by a newer frappe-host | Upgrade frappe-host. Do not downgrade the state. |
| Operation shows `interrupted` | The process was killed mid-operation | Read its log in `~/.local/share/frappe-host/operations/`, check the site with `site status`, and see the recovery runbooks |
| `backup verify` fails with a checksum mismatch | The file changed or was corrupted after the backup | Use the `--copy-to` copy if you have one; otherwise take a new backup |

## Never do this

- `chmod -R 777` on a Bench. It exposes site secrets and private files.
- Disabling the firewall to "test" public access.
- Deleting files in `sites/SITE/` to "reset" a site. Use `bench drop-site`.
