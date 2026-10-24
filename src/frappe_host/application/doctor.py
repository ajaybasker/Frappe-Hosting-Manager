"""Host readiness diagnostics. Strictly read-only."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from frappe_host.adapters import bench_fs
from frappe_host.domain import support
from frappe_host.domain.models import BenchInfo, Check, Status
from frappe_host.platform.linux import host

GIB = 1024**3

# Well-known ports of services that must never be reachable from outside the host.
INTERNAL_PORTS = {3306: "MariaDB/MySQL", 5432: "PostgreSQL", 6379: "Redis"}


def _gib(n: int) -> str:
    return f"{n / GIB:.1f} GiB"


def check_platform(osr: dict[str, str] | None = None, arch: str | None = None) -> list[Check]:
    osr = osr if osr is not None else host.os_release()
    arch = arch or host.architecture()
    out = []
    key = (osr.get("ID", ""), osr.get("VERSION_ID", ""))
    pretty = osr.get("PRETTY_NAME", "unknown")
    if key in support.OS_SUPPORT:
        out.append(Check("platform", "os", Status.PASS, f"{pretty} ({support.OS_SUPPORT[key]})", {"id": key[0], "version": key[1]}))
    elif key[0] in ("ubuntu", "debian") or "debian" in osr.get("ID_LIKE", ""):
        out.append(Check("platform", "os", Status.WARN, f"{pretty} is not in the support matrix", {"id": key[0], "version": key[1]},
                         "Use a version listed in docs/support-matrix.md, or proceed knowing it is untested"))
    else:
        out.append(Check("platform", "os", Status.FAIL, f"{pretty} is not supported", {"id": key[0], "version": key[1]},
                         "Frappe Hosting Manager currently targets Ubuntu/Debian only"))
    if arch in support.SUPPORTED_ARCH:
        out.append(Check("platform", "architecture", Status.PASS, arch))
    else:
        out.append(Check("platform", "architecture", Status.WARN, f"{arch} has not been tested"))
    return out


def check_resources(paths: list[Path], mem: dict[str, int] | None = None, cpus: int | None = None) -> list[Check]:
    mem = mem if mem is not None else host.meminfo()
    cpus = cpus if cpus is not None else host.cpu_count()
    out = []
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    ev = {"total": total, "available": avail}
    if total < support.MIN_RAM_GIB * GIB * 0.95:
        out.append(Check("resources", "memory", Status.FAIL, f"{_gib(total)} RAM (minimum {support.MIN_RAM_GIB} GiB)", ev))
    elif total < support.RECOMMENDED_RAM_GIB * GIB * 0.95:
        out.append(Check("resources", "memory", Status.WARN, f"{_gib(total)} RAM; {support.RECOMMENDED_RAM_GIB} GiB+ recommended", ev))
    else:
        out.append(Check("resources", "memory", Status.PASS, f"{_gib(total)} RAM, {_gib(avail)} available", ev))
    if total and avail / total < 0.10:
        out.append(Check("resources", "memory-pressure", Status.WARN, f"only {_gib(avail)} memory available right now", ev,
                         "Stop unused Benches or processes before heavy operations (builds, migrations)"))
    swap = mem.get("SwapTotal", 0)
    out.append(Check("resources", "swap", Status.PASS if swap else Status.WARN,
                     f"{_gib(swap)} swap" if swap else "no swap configured", {"swap_total": swap},
                     None if swap else "Asset builds can be OOM-killed without swap; consider adding some"))
    out.append(Check("resources", "cpu", Status.PASS if cpus >= 2 else Status.WARN, f"{cpus} CPU(s)", {"cpus": cpus}))

    seen_dev: set[int] = set()
    for p in paths:
        dev = host.device_id(p)
        if dev is None or dev in seen_dev:
            continue
        seen_dev.add(dev)
        du = host.disk_usage(p)
        ev = {"path": du.path, "total": du.total, "free": du.free, "used_percent": round(du.used_percent, 1)}
        if du.free < support.MIN_FREE_DISK_GIB * GIB:
            st, nxt = Status.FAIL, "Free disk space before creating sites, backups or running updates"
        elif du.free < support.RECOMMENDED_FREE_DISK_GIB * GIB or du.used_percent >= support.DISK_USED_WARN_PERCENT:
            st, nxt = Status.WARN, "Databases, backups and logs grow; plan for more free space"
        else:
            st, nxt = Status.PASS, None
        out.append(Check("resources", f"disk {du.path}", st, f"{_gib(du.free)} free of {_gib(du.total)} ({du.used_percent:.0f}% used)", ev, nxt))
    return out


def check_toolchain() -> list[Check]:
    out = []
    tools = [
        ("bench", ("--version",), "Install bench: pipx install frappe-bench (see docs/install.md)"),
        ("git", ("--version",), "sudo apt install git"),
        ("python3", ("--version",), None),
        ("node", ("--version",), "Install the Node.js version required by your Frappe branch"),
        ("yarn", ("--version",), "Install yarn (required by bench for asset builds)"),
        ("redis-server", ("--version",), "sudo apt install redis-server"),
        ("mariadb", ("--version",), "Install MariaDB server/client (or PostgreSQL if your sites use it)"),
        ("wkhtmltopdf", ("--version",), "Needed for PDF printing on Frappe v16 and older"),
    ]
    for name, args, hint in tools:
        exe, ver = host.tool_version(name, args)
        if exe:
            out.append(Check("toolchain", name, Status.PASS, (ver or "found")[:80], {"path": exe}))
        else:
            st = Status.FAIL if name in ("bench", "git", "python3") else Status.WARN
            out.append(Check("toolchain", name, st, "not found on PATH", {}, hint))
    return out


def check_services(benches: list[BenchInfo]) -> list[Check]:
    out = []
    modes = {b.process_mode for b in benches}
    for unit, needed_when in (("mariadb", "always"), ("redis-server", "production"), ("nginx", "production"), ("supervisor", "supervisor")):
        state = host.systemd_state(unit)
        needed = needed_when == "always" or (needed_when == "production" and modes & {"supervisor", "systemd"}) or (needed_when in modes)
        if state == "active":
            out.append(Check("services", unit, Status.PASS, "active"))
        elif needed:
            out.append(Check("services", unit, Status.FAIL if unit == "mariadb" else Status.WARN, state, {},
                             f"Check with: systemctl status {unit}"))
        else:
            out.append(Check("services", unit, Status.INFO, f"{state} (not required by detected process modes)"))
    out.append(Check("services", "ufw", Status.INFO, f"host firewall: {host.ufw_state()} (read from config; live rules need sudo)"))
    return out


def _node_for_bench(info: BenchInfo) -> str | None:
    """Prefer an absolute node path from the Procfile (e.g. nvm) over the PATH node."""
    for cmd in bench_fs.parse_procfile(Path(info.path)).values():
        m = re.match(r"^(/\S*/node)\s", cmd)
        if m and Path(m.group(1)).is_file():
            # Only execute a binary named in the Procfile if it belongs to us or root.
            if Path(m.group(1)).stat().st_uid not in (0, os.getuid()):
                return None
            return host.tool_version(m.group(1))[1]  # shutil.which accepts absolute paths
    return host.tool_version("node")[1]


def check_bench(info: BenchInfo, mariadb_version: tuple[int, ...] | None = None, node_version: str | None = None) -> list[Check]:
    sec = f"bench {info.path}"
    out = [Check(sec, "frappe", Status.PASS if info.frappe_version else Status.WARN,
                 f"Frappe {info.frappe_version or 'unknown'} on {info.frappe_branch or 'unknown branch'}; "
                 f"{len(info.apps)} apps, {len(info.sites)} sites; process mode: {info.process_mode}")]
    for w in info.warnings:
        out.append(Check(sec, "layout", Status.WARN, w))
    branch = support.branch_from_version(info.frappe_version)
    req = support.BRANCHES.get(branch or "")
    if req is None:
        out.append(Check(sec, "support-matrix", Status.WARN, f"branch {branch or 'unknown'} is not in the support matrix",
                         {}, "Version checks were skipped; see docs/support-matrix.md"))
        return out
    py = host.parse_version(info.python)
    if py is None:
        out.append(Check(sec, "python", Status.UNKNOWN, "could not read env/pyvenv.cfg"))
    elif py[:2] < req.python_min or (req.python_max and py[:2] > req.python_max):
        out.append(Check(sec, "python", Status.FAIL, f"env Python {info.python} does not meet {branch} requirement "
                         f"(>= {'.'.join(map(str, req.python_min))})"))
    else:
        out.append(Check(sec, "python", Status.PASS, f"env Python {info.python}"))
    nv = host.parse_version(node_version)
    if nv is None:
        out.append(Check(sec, "node", Status.UNKNOWN, "Node.js version unknown"))
    else:
        out.append(Check(sec, "node", Status.PASS if nv[0] >= req.node_major_min else Status.FAIL,
                         f"Node {node_version} (requires {req.node_major_min}+)"))
    if mariadb_version is not None:
        ok = mariadb_version >= req.mariadb_min
        out.append(Check(sec, "mariadb", Status.PASS if ok else Status.WARN,
                         f"MariaDB client {'.'.join(map(str, mariadb_version))} (requires {'.'.join(map(str, req.mariadb_min))}+); "
                         "server version is not checked without credentials"))
    if info.process_mode == "development":
        out.append(Check(sec, "process-mode", Status.INFO, "development mode (bench start / bench serve); not a production setup",
                         {}, "Production service management arrives in a later phase; see docs/architecture.md"))
    return out


def _redis_ports(cfg: dict) -> dict[int, str]:
    ports = {}
    for key in ("redis_cache", "redis_queue", "redis_socketio"):
        url = cfg.get(key)
        if isinstance(url, str):
            try:
                p = urlparse(url).port
            except ValueError:
                p = None
            if p:
                ports[p] = f"Redis ({key})" if p not in ports else ports[p].rstrip(")") + f", {key})"
    return ports


def check_exposure(benches: list[BenchInfo], listeners: list[host.Listener] | None = None) -> list[Check]:
    """Flag internal services listening beyond loopback. Based on local socket
    state only: says nothing about whether a firewall/router blocks them."""
    listeners = listeners if listeners is not None else host.tcp_listeners()
    internal = dict(INTERNAL_PORTS)
    # Browser-facing ports: web, and Socket.IO (browsers connect to it directly in development mode;
    # in production NGINX proxies it, so it should then be loopback-only).
    web_ports: dict[int, str] = {}
    for b in benches:
        cfg = bench_fs.common_config(Path(b.path))
        internal.update(_redis_ports(cfg))
        if b.webserver_port:
            web_ports[b.webserver_port] = f"web port {b.webserver_port}"
        if isinstance(cfg.get("socketio_port"), int):
            web_ports[cfg["socketio_port"]] = f"Socket.IO port {cfg['socketio_port']}"
    out = []
    for port, label in sorted(internal.items()):
        ls = host.listeners_on(port, listeners)
        exposed = [l for l in ls if l.scope != "loopback"]
        if exposed:
            out.append(Check("network", f"port {port}", Status.FAIL,
                             f"{label} listens on {', '.join(l.address for l in exposed)}; internal services must bind to loopback",
                             {"listeners": [l.address for l in ls]},
                             "Bind it to 127.0.0.1 and make sure no firewall/router rule forwards this port"))
        elif ls:
            out.append(Check("network", f"port {port}", Status.PASS, f"{label} bound to loopback only"))
    for port, label in sorted(web_ports.items()):
        ls = host.listeners_on(port, listeners)
        if not ls:
            out.append(Check("network", f"port {port}", Status.INFO, f"{label} not listening (Bench not running?)"))
        elif any(l.scope != "loopback" for l in ls):
            out.append(Check("network", f"port {port}", Status.WARN,
                             f"{label} listens on all/LAN interfaces; other devices on your network may reach it "
                             "(not verified from another device)", {"listeners": [l.address for l in ls]},
                             "If LAN access is not intended, bind to 127.0.0.1 or restrict with the host firewall"))
        else:
            out.append(Check("network", f"port {port}", Status.PASS, f"{label} bound to loopback (local only)"))
    out.append(Check("network", "public", Status.UNKNOWN, "public internet reachability: not verified",
                     {}, "Public hosting checks are planned (Phase 6); a local response never proves public access"))
    return out


def run(benches: list[BenchInfo], extra_paths: list[Path] | None = None) -> list[Check]:
    checks = check_platform()
    checks.append(Check("platform", "deployment-note", Status.INFO,
                        "Frappe's production guide favours container images; this tool manages Bench (bare-metal) "
                        "installs and documents its tested scope in docs/support-matrix.md"))
    paths = [Path.home(), *[Path(b.path) for b in benches], *(extra_paths or [])]
    checks += check_resources(paths)
    checks += check_toolchain()
    checks += check_services(benches)
    mariadb_v = host.parse_version(host.tool_version("mariadb")[1])
    for b in benches:
        checks += check_bench(b, mariadb_v, _node_for_bench(b))
    checks += check_exposure(benches)
    return checks
