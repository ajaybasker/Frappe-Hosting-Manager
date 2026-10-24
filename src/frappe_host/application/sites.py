"""Site inventory, status, creation and logs for registered Benches."""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from frappe_host.adapters import bench_fs, network, process
from frappe_host.application import registry
from frappe_host.application.operations import OperationFailed, operation
from frappe_host.application.state import Store
from frappe_host.domain import support
from frappe_host.domain.models import Check, SiteInfo, Status
from frappe_host.domain.validation import ValidationError, app_name, contained_path, site_name
from frappe_host.platform.linux import host
from frappe_host.policies.redaction import redact_text


class SiteNotFound(LookupError):
    pass


def registered_bench_paths(store: Store) -> list[Path]:
    return [Path(b["path"]) for b in registry.list_benches(store)]


def resolve_site(store: Store, name: str, bench: str | None = None) -> tuple[Path, str]:
    """Find which registered Bench holds a site. Ambiguity is an error, never a guess."""
    name = site_name(name)
    benches = registered_bench_paths(store)
    if bench is not None:
        b = Path(os.path.expanduser(bench)).resolve()
        if b not in benches:
            raise SiteNotFound(f"Bench {b} is not registered; run: frappe-host bench register {b}")
        benches = [b]
    if not benches:
        raise SiteNotFound("no Benches registered; run: frappe-host bench discover, then bench register PATH")
    hits = [b for b in benches if name in bench_fs.list_sites(b)]
    if not hits:
        raise SiteNotFound(f"site {name!r} not found in registered Benches")
    if len(hits) > 1:
        raise SiteNotFound(f"site {name!r} exists in several Benches ({', '.join(map(str, hits))}); pass --bench")
    return hits[0], name


def list_sites(store: Store) -> list[SiteInfo]:
    out = []
    for b in registered_bench_paths(store):
        if not b.is_dir():
            continue
        for s in bench_fs.list_sites(b):
            out.append(bench_fs.inspect_site(b, s))
    return out


@dataclass
class BackupFile:
    path: Path
    size: int
    mtime: float


def latest_backup_files(bench: Path, site: str) -> list[BackupFile]:
    """Files from the most recent backup set in the site's backup directory."""
    d = bench_fs.backups_dir(bench, site)
    try:
        entries = [Path(e.path) for e in os.scandir(d) if e.is_file(follow_symlinks=False)]
    except OSError:
        return []
    if not entries:
        return []
    prefix = max(p.name.split("-", 1)[0] for p in entries)
    return [BackupFile(p, p.stat().st_size, p.stat().st_mtime) for p in entries if p.name.startswith(prefix + "-")]


def status(store: Store, bench: Path, site: str, *, probe: bool = True) -> list[Check]:
    """Distinct health dimensions; each states what it observed and nothing more."""
    import time

    info = bench_fs.inspect_site(bench, site)
    binfo = bench_fs.inspect_bench(bench)
    sec = site
    checks = [Check(sec, "bench", Status.INFO, f"{bench} (Frappe {binfo.frappe_version or 'unknown'}, mode {binfo.process_mode})")]
    checks.append(Check(sec, "config", Status.PASS if info.db_name else Status.FAIL,
                        f"site_config.json readable; database type {info.db_type}" if info.db_name
                        else "site_config.json has no db_name"))
    if info.maintenance_mode:
        checks.append(Check(sec, "maintenance", Status.WARN, "maintenance mode is ON", {},
                            f"bench --site {site} set-maintenance-mode off"))
    if info.scheduler_paused:
        checks.append(Check(sec, "scheduler", Status.WARN, "scheduler is paused or disabled for this site"))
    if info.developer_mode:
        checks.append(Check(sec, "developer-mode", Status.INFO, "developer_mode is enabled (not recommended for production)"))

    if probe:
        port = binfo.webserver_port
        if port is None:
            checks.append(Check(sec, "http-local", Status.UNKNOWN, "webserver_port not set in common_site_config.json"))
        else:
            r = network.probe_frappe("127.0.0.1", port, site)
            checks.append(Check(sec, "http-local", Status.PASS if r.ok else Status.FAIL,
                                f"GET http://127.0.0.1:{port}/api/method/ping (Host: {site}) -> {r.detail}"
                                + (" pong" if r.frappe_pong else ""), {"status": r.status},
                                None if r.ok else "Is the Bench running? Development mode: `bench start`"))
            ls = host.listeners_on(port)
            if any(l.scope != "loopback" for l in ls):
                checks.append(Check(sec, "lan", Status.UNKNOWN,
                                    f"port {port} listens beyond loopback, so LAN access is possible; not verified from another device"))
            elif ls:
                checks.append(Check(sec, "lan", Status.INFO, f"port {port} is bound to loopback: not reachable from the LAN"))
    checks.append(Check(sec, "public", Status.UNKNOWN, "public reachability: not verified"))

    files = latest_backup_files(bench, site)
    if not files:
        checks.append(Check(sec, "backup", Status.WARN, "no backups found", {}, f"frappe-host backup {site}"))
    else:
        age_h = (time.time() - max(f.mtime for f in files)) / 3600
        st = Status.PASS if age_h <= 24 else Status.WARN
        checks.append(Check(sec, "backup", st, f"latest backup {age_h:.0f}h old ({files[0].path.name.split('-', 1)[0]})"))
        rows = store.db.execute(
            "SELECT verify_result FROM backups WHERE site=? AND bench_path=? AND verify_result IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (site, str(bench)),
        ).fetchone() if store.exists() else None
        checks.append(Check(sec, "backup-verified", Status.PASS if rows and rows[0] == "ok" else Status.INFO,
                            f"latest recorded verification: {rows[0]}" if rows else "no recorded backup verification"))
    return checks


def plan_create(store: Store, bench: Path, name: str, apps: list[str], db_type: str) -> tuple[list[str], list[str]]:
    """Return (argv, plan) or raise ValidationError. No changes are made."""
    name = site_name(name)
    if bench not in registered_bench_paths(store):
        raise ValidationError(f"Bench {bench} is not registered")
    if db_type not in ("mariadb", "postgres"):
        raise ValidationError("db type must be mariadb or postgres")
    if (bench / "sites" / name).exists():
        raise ValidationError(f"site {name!r} already exists in {bench}")
    available = {a.name for a in bench_fs.list_apps(bench)}
    for a in apps:
        app_name(a)
        if a not in available:
            raise ValidationError(f"app {a!r} is not in this Bench (available: {', '.join(sorted(available))})")
    if bench.stat().st_uid != os.getuid():
        raise ValidationError("this Bench is owned by another user; run frappe-host as that user")
    free = host.disk_usage(bench).free
    if free < support.MIN_FREE_DISK_GIB * 1024**3:
        raise ValidationError(f"only {free / 1024**3:.1f} GiB free; need at least {support.MIN_FREE_DISK_GIB} GiB")
    bench_exe = process.which("bench")
    if not bench_exe:
        raise ValidationError("bench is not on PATH")
    argv = [bench_exe, "new-site", name, "--db-type", db_type]
    for a in apps:
        argv += ["--install-app", a]
    plan = [
        f"run in {bench}: {process.display(argv)}",
        "bench will prompt for the database root password and the Administrator password "
        "(frappe-host never sees or stores them)",
        f"creates sites/{name}/ and a new database",
        "refresh the registry snapshot for this Bench",
    ]
    return argv, plan


def create(store: Store, bench: Path, name: str, argv: list[str], plan: list[str]) -> str:
    with operation(store, "site.create", f"{bench}:{name}", plan) as op:
        op.log("running bench new-site with the user's terminal attached")
        rc = process.run_interactive(argv, cwd=bench)
        op.log(f"bench new-site exited {rc}")
        if rc != 0:
            raise OperationFailed(f"bench new-site failed with exit code {rc}; see the output above")
        registry.refresh(store, str(bench))
    return op.id


def log_files(bench: Path, site: str) -> list[Path]:
    dirs = [bench / "sites" / site / "logs", bench / "logs"]
    out = []
    for d in dirs:
        try:
            out += sorted(Path(e.path) for e in os.scandir(d) if e.is_file(follow_symlinks=False) and e.name.endswith(".log"))
        except OSError:
            continue
    return out


def tail(bench: Path, site: str, filename: str, lines: int = 100) -> list[str]:
    """Last N lines of a named log file, redacted. The name must be a file in the site/bench log dirs."""
    if "/" in filename or filename.startswith("."):
        raise ValidationError("give a log file name, not a path (see: frappe-host logs SITE --list)")
    for f in log_files(bench, site):
        if f.name == filename:
            contained_path(bench, f)
            with open(f, encoding="utf-8", errors="replace") as fh:
                return [redact_text(l.rstrip("\n")) for l in deque(fh, maxlen=lines)]
    raise ValidationError(f"no log named {filename!r} for {site}")
