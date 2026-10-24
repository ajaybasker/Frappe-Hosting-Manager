"""Production mode: Supervisor + NGINX via Bench-generated configuration.

Unlike `bench setup production`, this never deletes the default NGINX site,
never installs Ansible/fail2ban, and never edits supervisord.conf. It
generates config/supervisor.conf and config/nginx.conf with Bench (as the
user), links them into the system include directories, validates with
`nginx -t` before reloading, and rolls back its own links on failure.
"""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from frappe_host.adapters import bench_fs, network, process
from frappe_host.application import registry
from frappe_host.application.operations import Operation
from frappe_host.application.state import Store
from frappe_host.application.steps import Plan, Step
from frappe_host.domain.models import Check, Status
from frappe_host.platform.linux import host, paths


def bench_name(bench: Path) -> str:
    return bench.name


@dataclass
class Links:
    supervisor: Path
    nginx: Path


def links_for(bench: Path) -> Links:
    return Links(paths.supervisor_conf_d() / f"{bench_name(bench)}.conf", paths.nginx_conf_d() / f"{bench_name(bench)}.conf")


def _link_state(link: Path, target: Path) -> str:
    """ours | absent | foreign"""
    if link.is_symlink():
        return "ours" if Path(os.readlink(link)) == target else "foreign"
    return "foreign" if link.exists() else "absent"


def untraversable_dirs(path: Path) -> list[Path]:
    """Ancestors that the nginx worker (another user) cannot traverse (missing o+x)."""
    out = []
    for d in [path, *path.parents]:
        try:
            if not os.stat(d).st_mode & stat.S_IXOTH:
                out.append(d)
        except OSError:
            continue
    return list(reversed(out))


def plan_enable(store: Store, bench: Path) -> Plan:
    plan = Plan(f"enable production mode for {bench} (Supervisor + NGINX)")
    exe = process.which("bench")
    if not exe:
        plan.blockers.append("bench CLI not found")
        return plan
    if bench.stat().st_uid != os.getuid() or os.geteuid() == 0:
        plan.blockers.append("run as the (non-root) user that owns the Bench")
    for tool, pkg in (("nginx", "nginx"), ("supervisorctl", "supervisor")):
        if not process.which(tool) and not Path(f"/usr/sbin/{tool}").exists():
            plan.blockers.append(f"{pkg} is not installed; run `frappe-host init --production`")
    cfg = bench_fs.common_config(bench)
    links = links_for(bench)
    sup_target = bench / "config" / "supervisor.conf"
    ngx_target = bench / "config" / "nginx.conf"
    for link, target in ((links.supervisor, sup_target), (links.nginx, ngx_target)):
        st = _link_state(link, target)
        if st == "foreign":
            plan.blockers.append(f"{link} already exists and does not belong to this Bench; refusing to replace it")
        elif st == "ours":
            plan.satisfied.append(f"{link} -> {target}")
    if cfg.get("restart_systemd_on_update"):
        plan.blockers.append("this Bench is configured for systemd (restart_systemd_on_update); not supported")

    # The development server must not be running alongside supervisor.
    mode = bench_fs.detect_process_mode(bench, paths.supervisor_conf_d())
    port = cfg.get("webserver_port")
    if mode != "supervisor" and isinstance(port, int) and host.listeners_on(port):
        plan.blockers.append(f"something is listening on the Bench web port {port} (is `bench start` running?); stop it first")
    listeners_80 = host.listeners_on(80)
    if listeners_80 and host.systemd_state("nginx") != "active":
        plan.blockers.append("port 80 is used by a program other than NGINX; free it first")
    if not (bench / "sites" / "assets").exists():
        plan.warnings.append("sites/assets is missing; run `bench build` or pages will load without styles")

    sites = bench_fs.list_sites(bench)
    if not cfg.get("dns_multitenant"):
        plan.steps.append(Step("serve sites by hostname on port 80 (bench config dns_multitenant on)",
                               [exe, "config", "dns_multitenant", "on"], cwd=bench))
    plan.warnings.append("sites are served by their hostname: each site name (or domain) must resolve to this "
                         "computer. For local names use `frappe-host site hosts SITE`.")

    for d in untraversable_dirs(bench / "sites"):
        mine = os.stat(d).st_uid == os.getuid()
        plan.steps.append(Step(
            f"allow NGINX to traverse {d} (adds only the 'execute' bit for others; contents stay unreadable)",
            ["chmod", "o+x", str(d)], sudo=not mine, why=f"{d} is owned by another user"))

    plan.steps.append(Step("generate config/supervisor.conf (web, workers, scheduler, socket.io, redis)",
                           [exe, "setup", "supervisor", "--yes", "--skip-supervisord"], cwd=bench))
    plan.steps.append(Step("generate config/nginx.conf", [exe, "setup", "nginx", "--yes"], cwd=bench))
    if _link_state(links.supervisor, sup_target) == "absent":
        plan.steps.append(Step(f"link {links.supervisor} -> {sup_target}", ["ln", "-s", str(sup_target), str(links.supervisor)],
                               sudo=True, why="supervisor includes live in /etc",
                               undo=Step(f"remove {links.supervisor}", ["rm", "-f", str(links.supervisor)], sudo=True, why="rollback")))
    if _link_state(links.nginx, ngx_target) == "absent":
        plan.steps.append(Step(f"link {links.nginx} -> {ngx_target}", ["ln", "-s", str(ngx_target), str(links.nginx)],
                               sudo=True, why="NGINX includes live in /etc",
                               undo=Step(f"remove {links.nginx}", ["rm", "-f", str(links.nginx)], sudo=True, why="rollback")))
    plan.steps.append(Step("validate the complete NGINX configuration (nothing is reloaded if this fails)",
                           ["nginx", "-t"], sudo=True, why="nginx -t reads root-only files"))
    plan.steps.append(Step("load the Bench programs into Supervisor", ["supervisorctl", "reread"], sudo=True,
                           why="controlling the system Supervisor"))
    plan.steps.append(Step("start the Bench programs", ["supervisorctl", "update"], sudo=True, why="controlling the system Supervisor"))
    plan.steps.append(Step("reload NGINX", ["systemctl", "reload", "nginx"], sudo=True, why="reloading a system service"))
    plan.steps.append(Step("check that each site answers through NGINX", func=lambda op: _post_check(bench, sites, op)))
    plan.steps.append(Step("refresh the registry snapshot", func=lambda op: registry.refresh(store, str(bench))))
    return plan


POST_CHECK_SECONDS = 30


def _post_check(bench: Path, sites: list[str], op: Operation) -> None:
    deadline = time.time() + POST_CHECK_SECONDS
    pending = list(sites)
    while pending and time.time() < deadline:
        pending = [s for s in pending if not network.probe_frappe("127.0.0.1", 80, s, timeout=3).ok]
        if pending:
            time.sleep(2)
    for s in sites:
        op.log(f"post-check {s}: {'ok' if s not in pending else 'NOT answering on port 80 yet'}")


def plan_disable(store: Store, bench: Path) -> Plan:
    plan = Plan(f"disable production mode for {bench}")
    links = links_for(bench)
    removed = False
    for link, target in ((links.supervisor, bench / "config" / "supervisor.conf"), (links.nginx, bench / "config" / "nginx.conf")):
        st = _link_state(link, target)
        if st == "ours":
            plan.steps.append(Step(f"remove {link}", ["rm", "-f", str(link)], sudo=True, why="removing a system include"))
            removed = True
        elif st == "foreign":
            plan.warnings.append(f"{link} does not point to this Bench; leaving it alone")
    if not removed:
        plan.satisfied.append("no production links for this Bench")
        return plan
    plan.steps.append(Step("stop and unload the Bench programs", ["supervisorctl", "reread"], sudo=True, why="controlling Supervisor"))
    plan.steps.append(Step("apply Supervisor changes", ["supervisorctl", "update"], sudo=True, why="controlling Supervisor"))
    plan.steps.append(Step("validate NGINX configuration", ["nginx", "-t"], sudo=True, why="nginx -t reads root-only files"))
    plan.steps.append(Step("reload NGINX", ["systemctl", "reload", "nginx"], sudo=True, why="reloading a system service"))
    plan.steps.append(Step("refresh the registry snapshot", func=lambda op: registry.refresh(store, str(bench))))
    plan.warnings.append("afterwards run the Bench in development mode with `bench start`, or re-enable production")
    return plan


def status(bench: Path) -> list[Check]:
    sec = f"production {bench}"
    out = []
    links = links_for(bench)
    mode = bench_fs.detect_process_mode(bench, paths.supervisor_conf_d())
    out.append(Check(sec, "mode", Status.PASS if mode == "supervisor" else Status.INFO, f"process mode: {mode}"))
    for name, link, target in (("supervisor-link", links.supervisor, bench / "config" / "supervisor.conf"),
                               ("nginx-link", links.nginx, bench / "config" / "nginx.conf")):
        st = _link_state(link, target)
        out.append(Check(sec, name, {"ours": Status.PASS, "absent": Status.INFO, "foreign": Status.WARN}[st], f"{link}: {st}"))
    for svc in ("nginx", "supervisor"):
        s = host.systemd_state(svc)
        out.append(Check(sec, svc, Status.PASS if s == "active" else Status.WARN if mode == "supervisor" else Status.INFO, s))
    r = process.run(process.sudo(["-n", "supervisorctl", "status"]) if os.geteuid() != 0 else ["supervisorctl", "status"], timeout=20)
    prefix = f"{bench_name(bench)}-"
    progs = [l for l in r.stdout.splitlines() if l.startswith(prefix) or l.startswith(f"{bench_name(bench)}-web:")]
    if r.returncode in (0, 3) and progs:
        bad = [l.split()[0] for l in progs if "RUNNING" not in l]
        out.append(Check(sec, "programs", Status.FAIL if bad else Status.PASS,
                         f"{len(progs) - len(bad)}/{len(progs)} Supervisor programs running" + (f"; not running: {', '.join(bad)}" if bad else "")))
    elif mode == "supervisor":
        out.append(Check(sec, "programs", Status.UNKNOWN, "could not read Supervisor status without a sudo password",
                         {}, "run: sudo supervisorctl status"))
    if mode == "supervisor":
        for s in bench_fs.list_sites(bench):
            p = network.probe_frappe("127.0.0.1", 80, s, timeout=5)
            out.append(Check(sec, f"http {s}", Status.PASS if p.ok else Status.FAIL, f"port 80 Host {s}: {p.detail}"))
    return out


def nginx_conf_has_domain(bench: Path, domain: str) -> bool:
    try:
        text = (bench / "config" / "nginx.conf").read_text()
    except OSError:
        return False
    return re.search(rf"server_name[^;]*\b{re.escape(domain)}\b", text) is not None
