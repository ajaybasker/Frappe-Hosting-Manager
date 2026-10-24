"""Backup creation, inventory, verification and restore.

Backups are produced by `bench --site SITE backup --with-files` (the supported
Frappe mechanism); this module records, checksums, verifies, copies and
restores them. A verified archive is still not proof of recoverability: only
a restore test is (see docs/backup-restore.md).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frappe_host.adapters import bench_fs, process
from frappe_host.application.operations import OperationFailed, new_id, now_iso, operation
from frappe_host.application.state import Store
from frappe_host.domain import support
from frappe_host.domain.validation import ValidationError, backup_id as validate_backup_id
from frappe_host.platform.linux import host

CHUNK = 1024 * 1024


def classify(name: str) -> str:
    if name.endswith("-database.sql.gz") or name.endswith("-database.sql"):
        return "database"
    if name.endswith("-private-files.tar") or name.endswith("-private-files.tgz") or name.endswith("-private-files.tar.gz"):
        return "private-files"
    if name.endswith("-files.tar") or name.endswith("-files.tgz") or name.endswith("-files.tar.gz"):
        return "public-files"
    if name.endswith("-site_config_backup.json"):
        return "site-config"
    return "other"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_dir(d: Path) -> set[str]:
    try:
        return {e.name for e in os.scandir(d) if e.is_file(follow_symlinks=False)}
    except OSError:
        return set()


@dataclass
class CreatePlan:
    argv: list[str]
    plan: list[str]
    warnings: list[str]


def plan_create(bench: Path, site: str, copy_to: Path | None) -> CreatePlan:
    if bench.stat().st_uid != os.getuid():
        raise ValidationError("this Bench is owned by another user; run frappe-host as that user")
    bench_exe = process.which("bench")
    if not bench_exe:
        raise ValidationError("bench is not on PATH")
    free = host.disk_usage(bench).free
    if free < 1024**3:
        raise ValidationError(f"only {free / 1024**3:.1f} GiB free on the Bench disk; refusing to start a backup")
    argv = [bench_exe, "--site", site, "backup", "--with-files"]
    warnings = []
    if free < support.MIN_FREE_DISK_GIB * 1024**3:
        warnings.append(f"low disk space ({free / 1024**3:.1f} GiB free); the backup may fail part-way")
    plan = [
        f"run in {bench}: {process.display(argv)}",
        f"record new files in {bench_fs.backups_dir(bench, site)} with size and SHA-256",
        "restrict new backup files to owner-only permissions (0600); they contain data and secrets",
    ]
    if copy_to:
        if copy_to == bench / "sites" or (bench / "sites") in copy_to.parents:
            # Anything under sites/ may be web-served (public/files) and would leak the dump and secrets.
            raise ValidationError("--copy-to must not be inside the Bench's sites/ directory")
        plan.append(f"copy the backup set to {copy_to} and verify the copied checksums")
        if host.device_id(copy_to) == host.device_id(bench):
            warnings.append(f"{copy_to} is on the same filesystem as the Bench; this does not protect against disk failure")
    else:
        warnings.append("backups will only exist on the same disk as the live site; use --copy-to DIR on a separate disk")
    return CreatePlan(argv, plan, warnings)


def _copy_verified(src: Path, dest_dir: Path, digest: str) -> Path:
    old = os.umask(0o077)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old)
    final = dest_dir / src.name
    if final.exists():
        raise OperationFailed(f"{final} already exists; refusing to overwrite")
    tmp = dest_dir / f".{src.name}.partial"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out, CHUNK)
        out.flush()
        os.fsync(out.fileno())
    if sha256(tmp) != digest:
        tmp.unlink()
        raise OperationFailed(f"checksum mismatch after copying {src.name}")
    os.replace(tmp, final)
    return final


def create(store: Store, bench: Path, site: str, plan: CreatePlan, copy_to: Path | None) -> dict[str, Any]:
    bdir = bench_fs.backups_dir(bench, site)
    with operation(store, "backup.create", f"{bench}:{site}", plan.plan) as op:
        before = _snapshot_dir(bdir)
        rc = process.run_interactive(plan.argv, cwd=bench)
        op.log(f"bench backup exited {rc}")
        if rc != 0:
            raise OperationFailed(f"bench backup failed with exit code {rc}")
        new = sorted(_snapshot_dir(bdir) - before)
        if not new:
            raise OperationFailed(f"bench reported success but no new files appeared in {bdir}")
        prefixes = {n.split("-", 1)[0] for n in new}
        if len(prefixes) > 1:
            # e.g. Frappe's scheduler wrote a backup at the same time; never guess which set is ours.
            raise OperationFailed(f"several backup sets appeared during this run ({', '.join(sorted(prefixes))}); "
                                  "not recording an ambiguous backup, please run it again")
        files = []
        for name in new:
            p = bdir / name
            os.chmod(p, 0o600)
            files.append({"name": name, "path": str(p), "kind": classify(name), "size": p.stat().st_size, "sha256": sha256(p)})
            op.log(f"recorded {name} ({files[-1]['size']} bytes)")
        kinds = {f["kind"] for f in files}
        missing = {"database", "public-files", "private-files"} - kinds
        if "database" in missing:
            raise OperationFailed("no database dump was produced")
        if copy_to:
            for f in files:
                f["copy"] = str(_copy_verified(Path(f["path"]), copy_to, f["sha256"]))
                op.log(f"copied {f['name']} -> {f['copy']}")
        record = {
            "id": new_id("bk"),
            "site": site,
            "bench_path": str(bench),
            "created_at": now_iso(),
            "frappe_version": bench_fs.inspect_bench(bench).frappe_version,
            "files": files,
            "total_size": sum(f["size"] for f in files),
            "operation_id": op.id,
            "warnings": [f"expected {k} archive was not produced" for k in sorted(missing)],
        }
        with store.transaction() as db:
            db.execute(
                "INSERT INTO backups(id, site, bench_path, created_at, frappe_version, files, total_size, operation_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (record["id"], site, str(bench), record["created_at"], record["frappe_version"],
                 store.dumps(files), record["total_size"], op.id),
            )
    return record


def _row(store: Store, r: Any) -> dict[str, Any]:
    d = dict(r)
    d["files"] = store.loads(d["files"])
    return d


def list_backups(store: Store, site: str | None = None) -> list[dict[str, Any]]:
    if not store.exists():
        return []
    if site:
        rows = store.db.execute("SELECT * FROM backups WHERE site=? ORDER BY created_at DESC", (site,))
    else:
        rows = store.db.execute("SELECT * FROM backups ORDER BY created_at DESC")
    return [_row(store, r) for r in rows]


def get(store: Store, bid: str) -> dict[str, Any]:
    bid = validate_backup_id(bid)
    r = store.db.execute("SELECT * FROM backups WHERE id=?", (bid,)).fetchone()
    if r is None:
        raise ValidationError(f"no backup with id {bid!r} (see: frappe-host backup list)")
    return _row(store, r)


def unmanaged_backups(bench: Path, site: str, known: set[str]) -> list[str]:
    """Backup files present on disk but not recorded by frappe-host (e.g. Frappe's scheduled backups)."""
    return sorted(_snapshot_dir(bench_fs.backups_dir(bench, site)) - known)


def _check_tar(path: Path) -> str | None:
    """Read every member; reject unsafe member names. Returns an error or None."""
    try:
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                name = m.name
                if name.startswith("/") or ".." in Path(name).parts:
                    return f"unsafe member path {name!r}"
                if m.issym() or m.islnk():
                    target = m.linkname
                    if target.startswith("/") or ".." in Path(target).parts:
                        return f"unsafe link {name!r} -> {target!r}"
                if m.isfile():
                    fh = tf.extractfile(m)
                    if fh is not None:
                        while fh.read(CHUNK):
                            pass
    except (tarfile.TarError, OSError, EOFError) as e:
        return f"unreadable archive: {e}"
    return None


def _check_sql_gz(path: Path) -> str | None:
    try:
        with gzip.open(path, "rb") as fh:
            head = fh.read(1 << 20)
            while fh.read(CHUNK):
                pass  # forces full decompression and CRC check
    except (OSError, EOFError) as e:
        return f"corrupt gzip stream: {e}"
    if b"CREATE TABLE" not in head and b"INSERT INTO" not in head and b"-- " not in head:
        return "does not look like an SQL dump"
    return None


def verify_file(f: dict[str, Any], path: Path) -> str | None:
    if not path.is_file():
        return f"missing: {path}"
    if path.stat().st_size != f["size"]:
        return f"size changed: {path} ({path.stat().st_size} != {f['size']})"
    if sha256(path) != f["sha256"]:
        return f"checksum mismatch: {path}"
    if path.name.endswith(".sql.gz"):
        return _check_sql_gz(path)
    if f["kind"] in ("public-files", "private-files"):
        return _check_tar(path)
    if f["kind"] == "site-config":
        try:
            json.loads(path.read_text())
        except (OSError, ValueError) as e:
            return f"invalid JSON: {e}"
    return None


def verify(store: Store, bid: str) -> tuple[bool, list[str]]:
    rec = get(store, bid)
    problems, notes = [], []
    for f in rec["files"]:
        err = verify_file(f, Path(f["path"]))
        if err:
            problems.append(f"{f['name']}: {err}")
        else:
            notes.append(f"{f['name']}: ok")
        if f.get("copy"):
            err = verify_file(f, Path(f["copy"]))
            (problems if err else notes).append(f"{f['name']} (copy): {err or 'ok'}")
    ok = not problems
    with store.transaction() as db:
        db.execute("UPDATE backups SET verified_at=?, verify_result=? WHERE id=?",
                   (now_iso(), "ok" if ok else "failed: " + "; ".join(problems)[:500], rec["id"]))
    return ok, problems + notes


def usable_path(f: dict[str, Any]) -> Path:
    """Prefer the original file; fall back to a verified copy."""
    for candidate in (f.get("path"), f.get("copy")):
        if candidate and Path(candidate).is_file() and verify_file(f, Path(candidate)) is None:
            return Path(candidate)
    raise ValidationError(f"no intact copy of {f['name']} is available")


@dataclass
class RestorePlan:
    argv: list[str]
    plan: list[str]
    warnings: list[str]


def plan_restore(store: Store, bench: Path, target_site: str, rec: dict[str, Any]) -> RestorePlan:
    if bench.stat().st_uid != os.getuid():
        raise ValidationError("this Bench is owned by another user; run frappe-host as that user")
    warnings = []
    current = bench_fs.inspect_bench(bench).frappe_version
    b_major = support.branch_from_version(rec.get("frappe_version"))
    c_major = support.branch_from_version(current)
    if b_major and c_major:
        if int(b_major.split("-")[1]) > int(c_major.split("-")[1]):
            raise ValidationError(f"backup is from {b_major} but this Bench runs {c_major}; restoring into an older Frappe is unsupported")
        if b_major != c_major:
            warnings.append(f"backup is from {b_major}, Bench runs {c_major}: run `bench --site {target_site} migrate` afterwards")
    else:
        warnings.append("could not compare Frappe versions of the backup and the Bench")
    if rec["site"] != target_site:
        warnings.append(f"backup was taken from site {rec['site']!r} but will be restored INTO {target_site!r}; "
                        "encrypted fields only decrypt if the target uses the same encryption_key")
    by_kind = {f["kind"]: f for f in rec["files"]}
    if "database" not in by_kind:
        raise ValidationError("backup record has no database dump")
    bench_exe = process.which("bench")
    if not bench_exe:
        raise ValidationError("bench is not on PATH")
    argv = [bench_exe, "--site", target_site, "restore", str(usable_path(by_kind["database"]))]
    if "public-files" in by_kind:
        argv += ["--with-public-files", str(usable_path(by_kind["public-files"]))]
    else:
        warnings.append("no public files archive in this backup; public files will not be restored")
    if "private-files" in by_kind:
        argv += ["--with-private-files", str(usable_path(by_kind["private-files"]))]
    else:
        warnings.append("no private files archive in this backup; private files will not be restored")
    plan = [
        f"verify checksums and archive integrity of backup {rec['id']}",
        f"take a pre-restore backup of {target_site}",
        f"run in {bench}: {process.display(argv)}",
        f"REPLACES the database and files of {target_site}; bench will prompt for the database root password",
    ]
    return RestorePlan(argv, plan, warnings)


def restore(store: Store, bench: Path, target_site: str, rec: dict[str, Any], rplan: RestorePlan,
            pre_backup: CreatePlan | None) -> dict[str, Any]:
    ok, problems = verify(store, rec["id"])
    if not ok:
        raise OperationFailed("backup failed verification; refusing to restore: " + "; ".join(problems[:3]))
    pre = None
    if pre_backup is not None:
        pre = create(store, bench, target_site, pre_backup, None)
    with operation(store, "site.restore", f"{bench}:{target_site}", rplan.plan + [f"pre-restore backup: {pre['id'] if pre else 'skipped by user'}"]) as op:
        rc = process.run_interactive(rplan.argv, cwd=bench)
        op.log(f"bench restore exited {rc}")
        if rc != 0:
            msg = f"bench restore failed with exit code {rc}"
            if pre:
                msg += f"; the pre-restore backup {pre['id']} can be restored with: frappe-host restore {target_site} {pre['id']}"
            raise OperationFailed(msg)
    return {"operation_id": op.id, "pre_restore_backup": pre["id"] if pre else None}
