"""Read-only inspection of a Bench directory.

Nothing here executes code from the Bench or imports Frappe; versions and
branches are parsed from files so that discovery can never mutate a Bench.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from frappe_host.domain.models import AppInfo, BenchInfo, SiteInfo

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_PYPROJECT_VERSION_RE = re.compile(r"""^version\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_PYVENV_VERSION_RE = re.compile(r"^version(?:_info)?\s*=\s*([0-9][0-9.]*)", re.MULTILINE)

MAX_CONFIG_BYTES = 1_000_000


def is_bench(path: Path) -> bool:
    """Heuristic Bench layout check: sites/ and apps/frappe, plus apps.txt or a Procfile."""
    try:
        return (
            (path / "sites").is_dir()
            and (path / "apps" / "frappe").is_dir()
            and ((path / "sites" / "apps.txt").is_file() or (path / "Procfile").is_file())
        )
    except OSError:
        return False


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON config file defensively. Returns {} on any problem."""
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_text(path: Path, limit: int = MAX_CONFIG_BYTES) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


def git_head(repo: Path) -> tuple[str | None, str | None]:
    """Return (branch, commit) by reading .git directly (supports .git files for worktrees)."""
    git = repo / ".git"
    if git.is_file():
        txt = _read_text(git) or ""
        if txt.startswith("gitdir:"):
            git = (repo / txt.split(":", 1)[1].strip()).resolve()
    head = _read_text(git / "HEAD")
    if not head:
        return None, None
    head = head.strip()
    if not head.startswith("ref:"):
        return None, head[:40] or None  # detached HEAD
    ref = head.split(":", 1)[1].strip()
    branch = ref.removeprefix("refs/heads/")
    commit = (_read_text(git / ref) or "").strip() or None
    if commit is None:
        for line in (_read_text(git / "packed-refs") or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                commit = parts[0]
                break
    return branch, commit


def app_version(app_dir: Path, name: str) -> str | None:
    txt = _read_text(app_dir / name / "__init__.py", 200_000)
    if txt and (m := _VERSION_RE.search(txt)):
        return m.group(1)
    txt = _read_text(app_dir / "pyproject.toml", 200_000)
    if txt and (m := _PYPROJECT_VERSION_RE.search(txt)):
        return m.group(1)
    return None


def list_apps(bench: Path) -> list[AppInfo]:
    names: list[str] = []
    txt = _read_text(bench / "sites" / "apps.txt")
    if txt:
        names = [n.strip() for n in txt.splitlines() if n.strip()]
    apps = []
    for n in names:
        d = bench / "apps" / n
        branch, commit = git_head(d) if d.is_dir() else (None, None)
        apps.append(AppInfo(n, app_version(d, n) if d.is_dir() else None, branch, commit))
    return apps


def list_sites(bench: Path) -> list[str]:
    sites_dir = bench / "sites"
    out = []
    try:
        for entry in sorted(os.scandir(sites_dir), key=lambda e: e.name):
            if entry.is_dir(follow_symlinks=False) and (Path(entry.path) / "site_config.json").is_file():
                out.append(entry.name)
    except OSError:
        pass
    return out


def common_config(bench: Path) -> dict[str, Any]:
    return read_json(bench / "sites" / "common_site_config.json")


def site_config(bench: Path, site: str) -> dict[str, Any]:
    return read_json(bench / "sites" / site / "site_config.json")


def parse_procfile(bench: Path) -> dict[str, str]:
    procs: dict[str, str] = {}
    for line in (_read_text(bench / "Procfile") or "").splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            if k.strip():
                procs[k.strip()] = v.strip()
    return procs


def detect_process_mode(bench: Path, supervisor_conf_dir: Path = Path("/etc/supervisor/conf.d")) -> str:
    """Classify how this Bench's processes are meant to run.

    supervisor : bench's generated supervisor.conf is linked into the system supervisor
    systemd    : bench generated systemd units (config/systemd)
    development: Procfile with `bench serve` (run via `bench start`)
    unknown    : none of the above
    """
    conf = bench / "config" / "supervisor.conf"
    if conf.is_file():
        try:
            for entry in supervisor_conf_dir.iterdir():
                if entry.is_symlink() and entry.resolve() == conf.resolve():
                    return "supervisor"
        except OSError:
            pass
    if (bench / "config" / "systemd").is_dir():
        return "systemd"
    procs = parse_procfile(bench)
    if any("bench serve" in v for v in procs.values()):
        return "development"
    return "unknown"


def venv_python(bench: Path) -> str | None:
    txt = _read_text(bench / "env" / "pyvenv.cfg")
    if txt and (m := _PYVENV_VERSION_RE.search(txt)):
        return m.group(1)
    return None


def inspect_bench(bench: Path) -> BenchInfo:
    apps = list_apps(bench)
    frappe = next((a for a in apps if a.name == "frappe"), None)
    if frappe is None and (bench / "apps" / "frappe").is_dir():
        b, c = git_head(bench / "apps" / "frappe")
        frappe = AppInfo("frappe", app_version(bench / "apps" / "frappe", "frappe"), b, c)
    cfg = common_config(bench)
    warnings = []
    if not (bench / "sites" / "common_site_config.json").is_file():
        warnings.append("sites/common_site_config.json is missing or unreadable")
    if not (bench / "env").is_dir():
        warnings.append("no Python virtualenv at env/")
    port = cfg.get("webserver_port")
    return BenchInfo(
        path=str(bench),
        frappe_version=frappe.version if frappe else None,
        frappe_branch=frappe.branch if frappe else None,
        apps=apps,
        sites=list_sites(bench),
        process_mode=detect_process_mode(bench),
        webserver_port=port if isinstance(port, int) else None,
        python=venv_python(bench),
        warnings=warnings,
    )


def inspect_site(bench: Path, site: str) -> SiteInfo:
    common = common_config(bench)
    cfg = {**common, **site_config(bench, site)}
    return SiteInfo(
        name=site,
        bench_path=str(bench),
        db_type=cfg.get("db_type", "mariadb"),
        db_name=cfg.get("db_name"),
        host_name=cfg.get("host_name"),
        maintenance_mode=bool(cfg.get("maintenance_mode")),
        scheduler_paused=bool(cfg.get("pause_scheduler")) or cfg.get("enable_scheduler") == 0,
        developer_mode=bool(cfg.get("developer_mode")),
    )


def backups_dir(bench: Path, site: str) -> Path:
    return bench / "sites" / site / "private" / "backups"
