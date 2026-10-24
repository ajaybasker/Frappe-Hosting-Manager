# Support matrix

Keep this file in sync with `src/frappe_host/domain/support.py`.

**Status meanings**

- **tested**: this project has recorded test evidence (CI or a manual test log below).
- **candidate**: documented as supported by Frappe, and frappe-host's read-only commands have run on it, but the full workflow has not been tested.
- **unsupported**: known not to work, or out of scope.

## Host operating systems

| OS | Arch | Status | Evidence |
|---|---|---|---|
| Ubuntu 24.04 LTS | x86_64 | candidate | Read-only commands run against a real version-16 Bench (2026-10-24, see below) |
| Ubuntu 22.04 LTS | x86_64 | candidate | none yet |
| Debian 12 / 13 | x86_64 | candidate | none yet |
| any of the above | aarch64 | candidate | none yet |
| Other Linux distributions | — | unsupported (doctor warns) | — |
| Windows (native), macOS | — | unsupported | Out of scope for the initial release |

Frappe recommends Debian 13+ or Ubuntu 24.04+, since older releases "may have issues with some of the packages".

## Frappe branches

Requirements are from the official [installation guide](https://docs.frappe.io/framework/user/en/installation), checked 2026-10-24. `doctor` checks the Bench's own virtualenv Python, the Node.js its Procfile uses (falling back to `node` on PATH) and the MariaDB **client** version. The server version needs credentials and is not checked.

| Branch | Python | Node.js | MariaDB | Redis | Status |
|---|---|---|---|---|---|
| version-15 | 3.10 – 3.13 | 18+ | 10.6.6+ | 6+ | candidate |
| version-16 | 3.14 | 24 | 11.8+ | 6+ | candidate |
| version-14 and older, develop | — | — | — | — | not in matrix (doctor warns) |

## Deployment modes

| Mode | Detection | Status |
|---|---|---|
| development (`bench start`, Procfile with `bench serve`) | Procfile | Inspected; this is the only mode exercised so far |
| supervisor | `config/supervisor.conf` symlinked into `/etc/supervisor/conf.d` | Managed by `production enable/disable` (ADR 0004). Tested with fake system tools only. |
| systemd (`bench setup systemd`) | `config/systemd/` | Detected only; `production enable` refuses it |
| Docker / frappe_docker | — | unsupported. Frappe's production guide favours containers; this tool deliberately targets Bench installs. See ADR 0003. |

## frappe-host runtime

Python ≥ 3.10 is declared. The test suite has been run on **3.12.3 and 3.14**. 3.10 and 3.11 have not been run locally (a network error blocked the interpreter download); CI covers them.

## Test evidence log

| Date | Host | What was run | Result |
|---|---|---|---|
| 2026-10-24 | Ubuntu 24.04.4, x86_64, Bench 5.29.1, Frappe 16.18.2 (development, 21 sites) | `doctor`, `bench discover/register/list`, `site list/status`, `logs`, `backup list`, `status`, `state export`. Bench config checksums compared before and after. | Pass. Bench unchanged. No backup, create or restore was run on this host (only 3 GiB free disk). |
| 2026-10-24 | Same | 0.2.0 dry-run plans: `init --production`, `production enable`, `publish`, `domain check`, `check`, `diagnostics --preview`, `dashboard` (token, Host and CSP checks via curl) | Plans correct. Production correctly blocked while `bench start` was running; publish correctly blocked (developer mode, no DNS, production off). Nothing changed. |
| 2026-10-24 | Same | Automated suite (168 tests: fixture Benches, fake `bench`, fake sudo/nginx/supervisorctl/systemctl/certbot/crontab/apt, /etc sysroot) on Python 3.12 and 3.14 | 168 passed |
| 2026-10-24 | Same | Built a wheel, installed it into a clean virtualenv, ran `frappe-host --version` and `python -m frappe_host` | Pass |
| — | Clean Ubuntu 24.04 / Debian 12 VM | Full checklist in README §13 (`init` → `bench create` → site → backup/restore → production → schedule) | **Pending** |
