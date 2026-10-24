# ADR 0001: Python, standard library only, installed with pipx/uv

Status: accepted (2026-10-24). The design document leaves this open (§20.1).

## Context

The repository was empty. The tool must run on Ubuntu/Debian hosts that already run Frappe. It handles secrets and eventually privileged operations, so a small dependency surface is valuable.

## Decision

- Python ≥ 3.10, with no third-party runtime dependencies (`argparse`, `sqlite3`, `subprocess`, `http.client`, `tarfile`, `gzip`).
- Packaged with `pyproject.toml` and installed as an isolated tool with `pipx install .` or `uv tool install .`. pytest is a dev-only dependency.
- The tool runs in its own environment, **not** inside a Bench's virtualenv, so it keeps working when a Bench is broken and never imports Frappe.

## Consequences

- Frappe developers can read and contribute to it, since Frappe and Bench are themselves Python.
- There are no rich TUI libraries, so output is plain text and JSON.
- Ubuntu 22.04 ships Python 3.10, 24.04 ships 3.12 and v16 Benches use 3.14. CI tests 3.10–3.14.
