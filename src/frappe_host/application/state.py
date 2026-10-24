"""Local state registry (SQLite, versioned schema).

The registry records what the manager manages and what it has done. It is
never the source of truth: Bench directories, site configs and OS services
are, and callers must reconcile against them (see registry.drift()).

Location: $FRAPPE_HOST_HOME, else $XDG_DATA_HOME/frappe-host, else
~/.local/share/frappe-host. Directory mode 0700, files 0600. No secrets are
ever stored here.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class StateError(RuntimeError):
    pass


def state_home() -> Path:
    if env := os.environ.get("FRAPPE_HOST_HOME"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "frappe-host"


def _execute_script(db: sqlite3.Connection, script: str) -> None:
    # executescript() would COMMIT implicitly and break the migration transaction.
    for stmt in script.split(";"):
        if stmt.strip():
            db.execute(stmt)


def _migrate_to_1(db: sqlite3.Connection) -> None:
    _execute_script(
        db,
        """
        CREATE TABLE benches (
            path TEXT PRIMARY KEY,
            registered_at TEXT NOT NULL,
            frappe_version TEXT,
            frappe_branch TEXT,
            process_mode TEXT,
            snapshot TEXT NOT NULL,
            last_checked_at TEXT
        );
        CREATE TABLE operations (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            target TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            plan TEXT,
            error TEXT,
            log_path TEXT,
            pid INTEGER
        );
        CREATE INDEX operations_started ON operations(started_at);
        CREATE TABLE backups (
            id TEXT PRIMARY KEY,
            site TEXT NOT NULL,
            bench_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            frappe_version TEXT,
            files TEXT NOT NULL,
            total_size INTEGER NOT NULL,
            verified_at TEXT,
            verify_result TEXT,
            operation_id TEXT,
            origin TEXT NOT NULL DEFAULT 'manager'
        );
        CREATE INDEX backups_site ON backups(site, created_at);
        """
    )


# Index i holds the migration that upgrades schema i -> i+1.
MIGRATIONS = [_migrate_to_1]
assert len(MIGRATIONS) == SCHEMA_VERSION


class Store:
    def __init__(self, home: Path | None = None):
        self.home = home or state_home()
        self.db_path = self.home / "state.db"
        self.ops_dir = self.home / "operations"
        self._db: sqlite3.Connection | None = None

    # -- lifecycle -------------------------------------------------------
    def _ensure_dirs(self) -> None:
        old = os.umask(0o077)
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self.ops_dir.mkdir(exist_ok=True)
        finally:
            os.umask(old)
        os.chmod(self.home, 0o700)
        os.chmod(self.ops_dir, 0o700)

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._ensure_dirs()
            old = os.umask(0o077)
            try:
                db = sqlite3.connect(self.db_path, isolation_level=None)
            finally:
                os.umask(old)
            os.chmod(self.db_path, 0o600)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            self._db = db
            self._migrate()
        return self._db

    def exists(self) -> bool:
        return self.db_path.exists()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def schema_version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        db = self._db
        assert db is not None
        current = db.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise StateError(
                f"state schema v{current} is newer than this frappe-host supports (v{SCHEMA_VERSION}); "
                "upgrade frappe-host instead of downgrading"
            )
        if current == SCHEMA_VERSION:
            return
        if current > 0:
            backup = self.db_path.with_name(f"state.db.pre-v{current + 1}.bak")
            db.execute("PRAGMA wal_checkpoint(FULL)")
            shutil.copy2(self.db_path, backup)
            os.chmod(backup, 0o600)
        for version in range(current, SCHEMA_VERSION):
            db.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[version](db)
                db.execute(f"PRAGMA user_version = {version + 1}")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def dumps(obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, default=str)

    @staticmethod
    def loads(text: str | None) -> Any:
        return json.loads(text) if text else None

    def export(self) -> dict[str, Any]:
        """Export all state as JSON-serialisable data (no secrets are stored)."""
        out: dict[str, Any] = {"schema_version": self.schema_version()}
        for table in ("benches", "operations", "backups"):
            out[table] = [dict(r) for r in self.db.execute(f"SELECT * FROM {table}")]
        return out
