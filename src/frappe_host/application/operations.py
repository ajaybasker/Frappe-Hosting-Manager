"""Operation tracking and audit log.

Every mutating action runs inside `operation(...)`: it gets a unique ID, a
redacted log file and a final status recorded in the state registry, even
when it fails. Operations left 'running' by a crashed process are reported
as interrupted.
"""

from __future__ import annotations

import os
import secrets
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from frappe_host.application.state import Store
from frappe_host.policies.redaction import redact_obj, redact_text


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


@dataclass
class Operation:
    id: str
    kind: str
    target: str | None
    log_path: Path
    steps: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        line = f"{now_iso()} {redact_text(message)}\n"
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)


class OperationFailed(RuntimeError):
    def __init__(self, message: str, op_id: str | None = None):
        super().__init__(message)
        self.op_id = op_id


@contextmanager
def operation(store: Store, kind: str, target: str | None, plan: list[str]) -> Iterator[Operation]:
    store.db  # ensure dirs/schema exist before the first log write
    op_id = new_id("op")
    op = Operation(op_id, kind, target, store.ops_dir / f"{op_id}.log")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO operations(id, kind, target, started_at, status, plan, log_path, pid) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (op.id, kind, target, now_iso(), "running", store.dumps(redact_obj(plan)), str(op.log_path), os.getpid()),
        )
    op.log(f"START {kind} target={target}")
    for step in plan:
        op.log(f"PLAN {step}")
    try:
        yield op
    except BaseException as e:
        status = "cancelled" if isinstance(e, KeyboardInterrupt) else "failed"
        op.log(f"{status.upper()} {type(e).__name__}: {e}")
        if not isinstance(e, (OperationFailed, KeyboardInterrupt)):
            op.log(redact_text(traceback.format_exc()))
        _finish(store, op.id, status, f"{type(e).__name__}: {e}")
        if isinstance(e, OperationFailed) and e.op_id is None:
            e.op_id = op.id
        raise
    else:
        op.log("SUCCEEDED")
        _finish(store, op.id, "succeeded", None)


def _finish(store: Store, op_id: str, status: str, error: str | None) -> None:
    with store.transaction() as db:
        db.execute(
            "UPDATE operations SET status=?, finished_at=?, error=? WHERE id=?",
            (status, now_iso(), redact_text(error) if error else None, op_id),
        )


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def history(store: Store, limit: int = 20) -> list[dict[str, Any]]:
    if not store.exists():
        return []
    rows = store.db.execute("SELECT * FROM operations ORDER BY rowid DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["plan"] = store.loads(d["plan"])
        if d["status"] == "running" and not _pid_alive(d.pop("pid")):
            d["status"] = "interrupted"
        else:
            d.pop("pid", None)
        out.append(d)
    return out
