# ADR 0004: Production mode and publishing without `bench setup production`

Status: accepted (2026-10-24).

## Context

`bench setup production` (Bench 5.x) installs Ansible and fail2ban, edits `/etc/supervisor/supervisord.conf`, and **deletes** `/etc/nginx/sites-enabled/default` and `/etc/nginx/conf.d/default.conf`. Its Let's Encrypt helper stops NGINX during issuance and edits root's crontab. The design document forbids silent replacement of NGINX configuration and requires validation before reload.

## Decision

- Generate configuration with Bench, running as the user: `bench setup supervisor --yes --skip-supervisord` and `bench setup nginx --yes`.
- frappe-host creates only two symlinks, `/etc/{supervisor,nginx}/conf.d/<bench>.conf`. It refuses if either path exists and isn't its own link to this Bench.
- Run `sudo nginx -t` before `supervisorctl reread/update` and `systemctl reload nginx`. If any step fails, the links created in that run are removed.
- Turn on `dns_multitenant`, so sites are served by hostname on port 80. This coexists with the stock default site, which only answers unknown hostnames.
- When NGINX must serve static files from a home directory that has no "execute" bit for others, add only `o+x`, as a visible plan step.
- Get certificates with `certbot certonly --nginx --deploy-hook "systemctl reload nginx"`. This needs no downtime, and renewal uses the distribution's `certbot.timer`. Attach them with `bench setup add-domain --ssl-certificate/--ssl-certificate-key`, then regenerate, validate and reload.
- `publish` is gated by a checklist (production mode, loopback-only internal ports, developer mode off, DNS resolves, certbot and its NGINX plugin present) and needs the domain typed to confirm. Rollback removes the domain and regenerates the NGINX config.

## Consequences

- fail2ban, firewall rules and router configuration stay the user's responsibility. They're shown as checklist items, never changed automatically.
- Only Supervisor is supported for production. Benches configured for systemd are refused.
