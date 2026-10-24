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
| `init`: "MANUAL: apt offers MariaDB 10.11 but version-16 needs 11.8" | Ubuntu's MariaDB is too old for v16 | Add the MariaDB 11.8 repository (README §3.2), or use `--branch version-15` |
| `init`: "~/.local/bin/node already exists and was not created by frappe-host" | You installed node there yourself | Remove or rename it, or make sure it is new enough, then re-run |
| `bench`/`node` "not found" right after init | `~/.local/bin` is not on PATH in this shell | Open a new terminal or log out and back in |
| `production enable`: "something is listening on the Bench web port" | `bench start` is still running | Stop it (Ctrl+C), then re-run |
| `production enable`: "… does not belong to this Bench" | Another Bench with the same directory name is in production, or a hand-made file exists | Rename the Bench directory or remove the other file after checking it |
| Production: site gives 404 or the NGINX default page | The hostname doesn't reach the site | `frappe-host site hosts SITE` (local), or check the DNS record and `server_name` in `config/nginx.conf` |
| Production: CSS/JS missing (403) | NGINX can't traverse your home directory | Re-run `production enable`; it adds `o+x` to the directories that need it |
| `publish`: certbot fails | Let's Encrypt can't reach http://DOMAIN from the internet | `frappe-host domain check DOMAIN --detect-public-ip`: check the DNS A record, router forwarding of port 80, and CGNAT. The publish was rolled back. |
| `probe` from a phone fails but works at home | Port forwarding, CGNAT or the ISP is blocking inbound traffic | See docs/networking.md |
| Scheduled backups don't run | cron isn't running, or the machine was off | `systemctl status cron`; check `~/.local/share/frappe-host/scheduled-backups.log` |
| `backup verify` fails with a checksum mismatch | The file changed or was corrupted after the backup | Use the `--copy-to` copy if you have one; otherwise take a new backup |

## Never do this

- `chmod -R 777` on a Bench. It exposes site secrets and private files.
- Disabling the firewall to "test" public access.
- Deleting files in `sites/SITE/` to "reset" a site. Use `bench drop-site`.
