"""Site and app lifecycle workflows built on Bench commands."""

from __future__ import annotations

import os
import re
from pathlib import Path

from frappe_host.adapters import bench_fs, process
from frappe_host.application import registry
from frappe_host.application.setup import _verified_backup
from frappe_host.application.state import Store
from frappe_host.application.steps import Plan, Step
from frappe_host.domain.validation import ValidationError, app_name, branch_name, site_name
from frappe_host.platform.linux import paths

_APP_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9._~/-]+$")


def _bench_exe(plan: Plan) -> str:
    exe = process.which("bench")
    if not exe:
        plan.blockers.append("bench CLI not found; run `frappe-host init`")
        return "bench"
    return exe


def _owner_check(plan: Plan, bench: Path) -> None:
    if bench.stat().st_uid != os.getuid():
        plan.blockers.append("this Bench is owned by another user; run frappe-host as that user")


def _pre_backup(plan: Plan, store: Store, bench: Path, site: str, enabled: bool, purpose: str) -> None:
    if enabled:
        plan.steps.append(Step(f"verified backup of {site} before {purpose}",
                               func=lambda op: _verified_backup(store, bench, site, op)))
    else:
        plan.warnings.append(f"backup before {purpose} SKIPPED at your request")


# ---------------------------------------------------------------- sites

def plan_delete(store: Store, bench: Path, site: str, backup: bool) -> Plan:
    plan = Plan(f"DELETE site {site} from {bench}")
    exe = _bench_exe(plan)
    _owner_check(plan, bench)
    info = bench_fs.inspect_site(bench, site)
    plan.warnings.append(f"this drops database {info.db_name or '?'} and moves sites/{site} to archived_sites/")
    _pre_backup(plan, store, bench, site, backup, "deletion")
    # Our verified backup replaces Frappe's own pre-drop backup; without ours, keep Frappe's.
    argv = [exe, "drop-site", site] + (["--no-backup"] if backup else [])
    plan.steps.append(Step("drop the site (bench prompts for the database root password)", argv, cwd=bench))
    plan.steps.append(Step("refresh the registry snapshot", func=lambda op: registry.refresh(store, str(bench))))
    return plan


def plan_migrate(store: Store, bench: Path, site: str, backup: bool) -> Plan:
    plan = Plan(f"migrate {site} (apply patches and schema changes)")
    exe = _bench_exe(plan)
    _owner_check(plan, bench)
    _pre_backup(plan, store, bench, site, backup, "migration")
    plan.steps.append(Step("run bench migrate", [exe, "--site", site, "migrate"], cwd=bench))
    return plan


def plan_maintenance(bench: Path, site: str, state: str) -> Plan:
    if state not in ("on", "off"):
        raise ValidationError("state must be on or off")
    plan = Plan(f"turn maintenance mode {state} for {site}")
    exe = _bench_exe(plan)
    plan.steps.append(Step(f"set maintenance mode {state}", [exe, "--site", site, "set-maintenance-mode", state], cwd=bench))
    return plan


def hosts_has(site: str, hosts: Path | None = None) -> bool:
    hosts = hosts or paths.hosts_file()
    try:
        for line in hosts.read_text().splitlines():
            fields = line.split("#", 1)[0].split()
            if len(fields) >= 2 and site in fields[1:]:
                return True
    except OSError:
        pass
    return False


def plan_hosts(site: str) -> Plan:
    plan = Plan(f"make {site} resolve to this computer")
    if hosts_has(site):
        plan.satisfied.append(f"{paths.hosts_file()} already has an entry for {site}")
        return plan
    plan.steps.append(Step(f"append '127.0.0.1 {site}' to {paths.hosts_file()}",
                           ["tee", "-a", str(paths.hosts_file())], sudo=True, input=f"127.0.0.1\t{site}\n",
                           why="/etc/hosts is owned by root"))
    plan.warnings.append("this only affects this computer; other devices need DNS or their own hosts entry")
    return plan


# ---------------------------------------------------------------- apps

def validate_app_source(source: str) -> str:
    if _APP_URL_RE.match(source or "") and ".." not in source:
        return source
    try:
        return app_name(source)
    except ValidationError:
        raise ValidationError("app must be an https:// git URL or an app name such as erpnext") from None


def plan_get_app(store: Store, bench: Path, source: str, branch: str | None) -> Plan:
    source = validate_app_source(source)
    plan = Plan(f"get app {source} into {bench}")
    exe = _bench_exe(plan)
    _owner_check(plan, bench)
    argv = [exe, "get-app"]
    if branch:
        argv += ["--branch", branch_name(branch)]
    argv.append(source)
    plan.warnings.append("app code runs with your user's permissions; only install apps from sources you trust")
    plan.steps.append(Step("bench get-app (clone, install Python/JS dependencies, build assets)", argv, cwd=bench))
    plan.steps.append(Step("refresh the registry snapshot", func=lambda op: registry.refresh(store, str(bench))))
    return plan


def plan_install_app(store: Store, bench: Path, site: str, app: str, backup: bool) -> Plan:
    app = app_name(app)
    available = {a.name for a in bench_fs.list_apps(bench)}
    if app not in available:
        raise ValidationError(f"app {app!r} is not in this Bench; first run: frappe-host app get {app} --bench {bench}")
    plan = Plan(f"install {app} on {site}")
    exe = _bench_exe(plan)
    _owner_check(plan, bench)
    _pre_backup(plan, store, bench, site, backup, f"installing {app}")
    plan.steps.append(Step(f"bench install-app {app}", [exe, "--site", site, "install-app", app], cwd=bench))
    return plan


def list_site_apps(bench: Path, site: str) -> tuple[bool, str]:
    """Installed apps as reported by Frappe (connects to the site database; read-only)."""
    exe = process.which("bench")
    if not exe:
        return False, "bench CLI not found"
    r = process.run([exe, "--site", site, "list-apps"], cwd=bench, timeout=120)
    return r.ok, (r.stdout if r.ok else r.stderr or r.stdout).strip()


def validate_site(value: str) -> str:
    return site_name(value)
