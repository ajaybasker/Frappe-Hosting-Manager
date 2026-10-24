# Installation

## Prerequisites

- Ubuntu or Debian (see [support-matrix.md](support-matrix.md)), as a **non-root** user.
- Python ≥ 3.10 and `pipx` (`sudo apt install pipx`) or [uv](https://docs.astral.sh/uv/).
- For managing sites: an existing Frappe Bench, and `bench` on your PATH.

frappe-host 0.1.0 does **not** install Frappe, MariaDB, Redis or Node.js. To set up a Bench, follow the official [installation guide](https://docs.frappe.io/framework/user/en/installation), then run `frappe-host doctor` to check the result.

## Install

```bash
git clone <repository-url> frappe-hosting-manager
cd frappe-hosting-manager
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
