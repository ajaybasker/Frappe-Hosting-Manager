# Installation

## Prerequisites

- Ubuntu or Debian (see [support-matrix.md](support-matrix.md)), as a **non-root** user.
- Python ≥ 3.10 and `pipx` (`sudo apt install pipx`) or [uv](https://docs.astral.sh/uv/).
- Nothing else: `frappe-host init` installs Frappe's dependencies, or you can register a Bench you already have.

`frappe-host init` can install everything Frappe needs (see the README §3). It follows the official [installation guide](https://docs.frappe.io/framework/user/en/installation), with these differences:

| Official guide | frappe-host init | Why |
|---|---|---|
| `curl … nvm install.sh \| bash`, then `nvm install 24` | Downloads the official nodejs.org tarball, checks SHA-256, unpacks it into `~/.local/share/frappe-host/node`, and links it into `~/.local/bin`. If nvm already has a suitable version, it prints a MANUAL step instead. | No remote script execution |
| `curl … uv/install.sh \| sh` | `pipx install uv` | No remote script execution |
| `uv python install 3.14 --default` | `uv python install 3.14` (no `--default`) | Doesn't change your global `python` |
| `uv tool install frappe-bench` | same | — |
| apt MariaDB | apt MariaDB if it's new enough; otherwise a MANUAL step to add the MariaDB repository | Never pipes a script into sudo |

## Install

```bash
git clone https://github.com/ajaybasker/Frappe-Hosting-Manager.git
cd Frappe-Hosting-Manager
pipx install .            # or: uv tool install .
frappe-host --version
```

The tool installs into its own isolated environment. It does not touch your Bench's virtualenv.

## First run

```bash
frappe-host doctor                          # read-only; exit 3 means blockers were found
frappe-host bench discover                  # read-only scan of your home directory
frappe-host bench register ~/frappe-bench --dry-run
frappe-host bench register ~/frappe-bench   # records metadata; the Bench is not modified
frappe-host status
```

Run frappe-host as the **same user that owns the Bench**. It refuses lifecycle operations on Benches owned by another user, rather than using sudo.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`doctor`: ready or needs attention) |
| 1 | operation failed (the operation log path is printed) |
| 2 | invalid input or failed preflight; nothing was changed |
| 3 | `doctor` found blockers |
| 4 | refused: consent or confirmation missing or wrong; nothing was changed |
| 5 | Bench or site not found |

## Upgrade

`pipx upgrade frappe-hosting-manager`, or reinstall from a newer checkout. State schema migrations run automatically, and the old database is copied to `state.db.pre-vN.bak` first.

## Uninstall

```bash
pipx uninstall frappe-hosting-manager
frappe-host state path      # run before uninstalling to see where the state lives
rm -r ~/.local/share/frappe-host   # optional: removes the registry, audit log and backup inventory
```

Uninstalling never touches your Benches, sites or backup files. Backups created through frappe-host remain in `sites/<site>/private/backups/` and in any `--copy-to` directory.
