# Contributing

## Development setup

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest
```

## Rules

1. **Frappe-only scope.** Features must relate to Frappe, Bench, sites or the services they need.
2. **Inspect before mutating.** New mutating commands must provide a plan, `--dry-run`, consent (`--yes` for non-destructive, typed `--confirm SITE` for destructive), and run inside `operations.operation(...)`.
3. **No shell strings.** Use `adapters.process.run` / `run_interactive` with argv lists, and validate every user-supplied value in `domain/validation.py` first.
4. **No secrets in argv, logs or state.** Let Bench prompt for passwords.
5. **Read-only inspection never executes Bench code.** Parse files instead.
6. **Tests with every change.** Unit tests for logic; integration tests use fixture Benches (`tests/conftest.py`) and the fake `bench` (`tests/fixtures/fake_bench.py`). **Never** point tests at a real Bench, real database or production site.
7. **Docs match behaviour.** Update the README command table, `docs/support-matrix.md` and `CHANGELOG.md`. Label anything not implemented as *planned*.
8. **Never claim something was tested if it wasn't.** Add an entry to the evidence log in the support matrix when you test on a real host.

## Runtime dependencies

The runtime uses the standard library only (ADR 0001). Adding a dependency needs an ADR.

## State schema changes

Bump `SCHEMA_VERSION`, append a migration to `MIGRATIONS`, add a test that migrates from the previous version, and note it in the changelog.
