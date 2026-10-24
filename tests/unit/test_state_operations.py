import os
import sqlite3
import stat

import pytest

from frappe_host.application import operations
from frappe_host.application.operations import OperationFailed, operation
from frappe_host.application.state import SCHEMA_VERSION, StateError, Store


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_store_created_with_restrictive_permissions(store):
    assert store.schema_version() == SCHEMA_VERSION
    assert mode(store.home) == 0o700
    assert mode(store.db_path) == 0o600
    assert mode(store.ops_dir) == 0o700


def test_store_not_created_by_exists_check(state_home):
    s = Store(state_home)
    assert not s.exists()
    assert not state_home.exists()


def test_newer_schema_is_refused(state_home):
    state_home.mkdir(parents=True)
    db = sqlite3.connect(state_home / "state.db")
    db.execute("PRAGMA user_version = 999")
    db.close()
    with pytest.raises(StateError, match="newer"):
        Store(state_home).db


def test_reopen_is_idempotent(state_home):
    s1 = Store(state_home)
    s1.db
    s1.close()
    s2 = Store(state_home)
    assert s2.schema_version() == SCHEMA_VERSION
    s2.close()


def test_failed_migration_rolls_back(state_home, monkeypatch):
    from frappe_host.application import state

    def broken(db):
        db.execute("CREATE TABLE half_done(x)")
        raise RuntimeError("boom")

    monkeypatch.setattr(state, "MIGRATIONS", [broken])
    with pytest.raises(RuntimeError):
        Store(state_home).db
    db = sqlite3.connect(state_home / "state.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert db.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchone() is None


def test_operation_success_and_log(store):
    with operation(store, "test.op", "target", ["step one", "--admin-password hunter2"]) as op:
        op.log("db_password=hunter2")
    rows = operations.history(store)
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["finished_at"]
    log = op.log_path.read_text()
    assert "hunter2" not in log
    assert "step one" in log
    assert mode(op.log_path) == 0o600
    assert "hunter2" not in str(rows[0]["plan"])


def test_operation_failure_recorded(store):
    with pytest.raises(OperationFailed) as ei:
        with operation(store, "test.op", "t", []):
            raise OperationFailed("bad thing token=abc123")
    assert ei.value.op_id
    row = operations.history(store)[0]
    assert row["status"] == "failed"
    assert "abc123" not in row["error"]


def test_interrupted_operation_detected(store):
    with store.transaction() as db:
        db.execute(
            "INSERT INTO operations(id, kind, target, started_at, status, pid) VALUES ('op-x','k','t','2020-01-01','running', 999999999)"
        )
    assert operations.history(store)[0]["status"] == "interrupted"


def test_export_contains_no_secret_columns(store):
    data = store.export()
    assert set(data) == {"schema_version", "benches", "operations", "backups"}
