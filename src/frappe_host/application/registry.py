"""Bench discovery, registration and drift detection.

Discovery is a read-only scan. Registration is metadata-only: it writes to
the manager's own state and never to the Bench directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from frappe_host.adapters import bench_fs
from frappe_host.application.operations import now_iso, operation
from frappe_host.application.state import Store
from frappe_host.domain.models import BenchInfo
from frappe_host.domain.validation import ValidationError, existing_directory

SKIP_DIRS = {"node_modules", "env", "apps", "sites", "__pycache__", "snap", "site-packages", "proc", "sys"}
DEFAULT_MAX_DEPTH = 4


@dataclass
class Discovered:
    path: str
    frappe_version: str | None
    frappe_branch: str | None
    site_count: int
    process_mode: str
    registered: bool


def discover(store: Store | None, roots: list[Path], max_depth: int = DEFAULT_MAX_DEPTH) -> list[Discovered]:
    """Walk roots (no symlink following, hidden dirs skipped) and report Bench candidates."""
    registered = {b["path"] for b in list_benches(store)}
    found: list[Discovered] = []
    seen: set[str] = set()

    def walk(d: Path, depth: int) -> None:
        if bench_fs.is_bench(d):
            p = str(d.resolve())
            if p not in seen:
                seen.add(p)
                info = bench_fs.inspect_bench(d)
                found.append(
                    Discovered(p, info.frappe_version, info.frappe_branch, len(info.sites), info.process_mode, p in registered)
                )
            return  # never descend into a Bench
        if depth >= max_depth:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    walk(Path(e.path), depth + 1)
            except OSError:
                continue

    for root in roots:
        if root.is_dir():
            walk(root, 0)
    return sorted(found, key=lambda x: x.path)


def snapshot(info: BenchInfo) -> dict[str, Any]:
    return {
        "frappe_version": info.frappe_version,
        "frappe_branch": info.frappe_branch,
        "apps": {a.name: a.version for a in info.apps},
        "sites": info.sites,
        "process_mode": info.process_mode,
    }


def resolve_bench_path(value: str) -> Path:
    path = existing_directory(value)
    if not bench_fs.is_bench(path):
        raise ValidationError(
            f"{path} does not look like a Frappe Bench (expected sites/, apps/frappe and sites/apps.txt or Procfile)"
        )
    return path


def plan_register(path: Path) -> tuple[BenchInfo, list[str], list[str]]:
    """Return (info, plan steps, warnings) for registering a Bench."""
    info = bench_fs.inspect_bench(path)
    warnings = list(info.warnings)
    try:
        if path.stat().st_uid != os.getuid():
            warnings.append(
                "Bench is owned by a different user; lifecycle commands must be run as that user"
            )
    except OSError:
        pass
    if info.frappe_version is None:
        warnings.append("could not determine the Frappe version")
    plan = [
        f"record Bench {path} in the frappe-host registry (metadata only)",
        f"snapshot: Frappe {info.frappe_version or 'unknown'} ({info.frappe_branch or 'unknown branch'}), "
        f"{len(info.apps)} apps, {len(info.sites)} sites, process mode '{info.process_mode}'",
        "no files inside the Bench will be created, modified or deleted",
    ]
    return info, plan, warnings


def register(store: Store, path: Path) -> tuple[str, str | None]:
    """Register a Bench. Idempotent: returns ('already-registered', None) on repeat."""
    if get_bench(store, path) is not None:
        return "already-registered", None
    info, plan, _ = plan_register(path)
    with operation(store, "bench.register", str(path), plan) as op:
        with store.transaction() as db:
            db.execute(
                "INSERT INTO benches(path, registered_at, frappe_version, frappe_branch, process_mode, snapshot, last_checked_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (str(path), now_iso(), info.frappe_version, info.frappe_branch, info.process_mode,
                 store.dumps(snapshot(info)), now_iso()),
            )
        op.log(f"registered {path}")
    return "registered", op.id


def unregister(store: Store, path: str) -> tuple[str, str | None]:
    if get_bench(store, Path(path)) is None:
        return "not-registered", None
    plan = [f"remove {path} from the frappe-host registry", "the Bench directory and its sites are not touched"]
    with operation(store, "bench.unregister", path, plan) as op:
        with store.transaction() as db:
            db.execute("DELETE FROM benches WHERE path=?", (path,))
        op.log(f"unregistered {path}")
    return "unregistered", op.id


def list_benches(store: Store | None) -> list[dict[str, Any]]:
    if store is None or not store.exists():
        return []  # read-only commands must not create state
    rows = store.db.execute("SELECT * FROM benches ORDER BY path").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snapshot"] = store.loads(d["snapshot"])
        out.append(d)
    return out


def get_bench(store: Store, path: Path) -> dict[str, Any] | None:
    key = str(path)
    for b in list_benches(store):
        if b["path"] == key:
            return b
    return None


@dataclass
class Drift:
    path: str
    exists: bool
    changes: list[str] = field(default_factory=list)

    @property
    def drifted(self) -> bool:
        return not self.exists or bool(self.changes)


def drift(entry: dict[str, Any]) -> Drift:
    """Compare the registered snapshot with the Bench as it is now."""
    path = Path(entry["path"])
    if not path.is_dir():
        return Drift(entry["path"], False, ["Bench directory no longer exists"])
    if not bench_fs.is_bench(path):
        return Drift(entry["path"], True, ["directory no longer has a Bench layout"])
    now = snapshot(bench_fs.inspect_bench(path))
    then = entry["snapshot"] or {}
    changes = []
    for key in ("frappe_version", "frappe_branch", "process_mode"):
        if then.get(key) != now.get(key):
            changes.append(f"{key}: {then.get(key)} -> {now.get(key)}")
    added = sorted(set(now["sites"]) - set(then.get("sites", [])))
    removed = sorted(set(then.get("sites", [])) - set(now["sites"]))
    if added:
        changes.append(f"sites added outside frappe-host: {', '.join(added)}")
    if removed:
        changes.append(f"sites removed: {', '.join(removed)}")
    old_apps, new_apps = then.get("apps", {}), now["apps"]
    for name in sorted(set(old_apps) | set(new_apps)):
        if old_apps.get(name) != new_apps.get(name):
            changes.append(f"app {name}: {old_apps.get(name)} -> {new_apps.get(name)}")
    return Drift(entry["path"], True, changes)


def refresh(store: Store, path: str) -> None:
    """Accept the current Bench state as the new snapshot (user-initiated)."""
    p = Path(path)
    info = bench_fs.inspect_bench(p)
    with operation(store, "bench.refresh", path, [f"update registry snapshot for {path}"]):
        with store.transaction() as db:
            db.execute(
                "UPDATE benches SET frappe_version=?, frappe_branch=?, process_mode=?, snapshot=?, last_checked_at=? WHERE path=?",
                (info.frappe_version, info.frappe_branch, info.process_mode, store.dumps(snapshot(info)), now_iso(), path),
            )
