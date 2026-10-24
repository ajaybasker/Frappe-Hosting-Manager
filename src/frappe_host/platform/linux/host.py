"""Read-only Linux host probes."""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from frappe_host.adapters import process


def os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def architecture() -> str:
    return platform.machine()


def cpu_count() -> int:
    return os.cpu_count() or 0


def meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    """Return memory figures in bytes (MemTotal, MemAvailable, SwapTotal, SwapFree)."""
    out: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
            if m:
                out[m.group(1)] = int(m.group(2)) * 1024
    except OSError:
        pass
    return out


@dataclass
class DiskUsage:
    path: str
    total: int
    free: int

    @property
    def used_percent(self) -> float:
        return 100.0 * (self.total - self.free) / self.total if self.total else 0.0


def disk_usage(path: Path) -> DiskUsage:
    u = shutil.disk_usage(path)
    return DiskUsage(str(path), u.total, u.free)


def device_id(path: Path) -> int | None:
    """Filesystem device id; used to tell whether backups share a disk with live data."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def tool_version(name: str, args: tuple[str, ...] = ("--version",)) -> tuple[str | None, str | None]:
    """Return (path, first version line) for an executable, or (None, None)."""
    exe = process.which(name)
    if not exe:
        return None, None
    r = process.run([exe, *args], timeout=10)
    text = (r.stdout or r.stderr).strip().splitlines()
    return exe, (text[0] if text else None)


def parse_version(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def systemd_state(unit: str) -> str:
    """'active', 'inactive', 'failed', 'not-found' or 'unknown' (no systemd)."""
    if not process.which("systemctl"):
        return "unknown"
    r = process.run(["systemctl", "show", "--property=LoadState,ActiveState", "--value", unit], timeout=10)
    if not r.ok:
        return "unknown"
    lines = r.stdout.split()
    if len(lines) >= 2:
        load, active = lines[0], lines[1]
        return "not-found" if load == "not-found" else active
    return "unknown"


@dataclass(frozen=True)
class Listener:
    address: str
    port: int

    @property
    def scope(self) -> str:
        """loopback | all-interfaces | specific"""
        ip = ipaddress.ip_address(self.address)
        if ip.is_loopback:
            return "loopback"
        if ip.is_unspecified:
            return "all-interfaces"
        return "specific"


def _decode_addr(hex_addr: str, v6: bool) -> str:
    raw = bytes.fromhex(hex_addr)
    if not v6:
        return str(ipaddress.IPv4Address(raw[::-1]))
    # IPv6 is stored as four little-endian 32-bit words.
    words = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
    ip = ipaddress.IPv6Address(words)
    return str(ip.ipv4_mapped) if ip.ipv4_mapped else str(ip)


def tcp_listeners(proc_net: Path = Path("/proc/net")) -> list[Listener]:
    """Parse /proc/net/tcp{,6} for sockets in LISTEN state (st == 0A)."""
    out: set[Listener] = set()
    for name, v6 in (("tcp", False), ("tcp6", True)):
        try:
            lines = (proc_net / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            addr, port = parts[1].split(":")
            try:
                out.add(Listener(_decode_addr(addr, v6), int(port, 16)))
            except ValueError:
                continue
    return sorted(out, key=lambda l: (l.port, l.address))


def listeners_on(port: int, listeners: list[Listener] | None = None) -> list[Listener]:
    return [l for l in (listeners if listeners is not None else tcp_listeners()) if l.port == port]


@dataclass(frozen=True)
class Interface:
    name: str
    address: str
    prefixlen: int


def interfaces() -> list[Interface]:
    """IPv4/IPv6 addresses from `ip -j addr` (iproute2)."""
    r = process.run(["ip", "-j", "addr", "show"], timeout=10)
    if not r.ok:
        return []
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return []
    out = []
    for link in data:
        for a in link.get("addr_info", []):
            if "local" in a:
                out.append(Interface(link.get("ifname", "?"), a["local"], int(a.get("prefixlen", 0))))
    return out


CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def classify_address(addr: str) -> str:
    """loopback | link-local | cgnat | private | public"""
    ip = ipaddress.ip_address(addr)
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.version == 4 and ip in CGNAT_NET:
        return "cgnat"
    if ip.is_private:
        return "private"
    return "public"


def ufw_state() -> str:
    """UFW status without sudo: we can only read the config file, not live rules."""
    conf = Path("/etc/ufw/ufw.conf")
    if not conf.is_file():
        return "not-installed"
    try:
        m = re.search(r"^ENABLED\s*=\s*(\w+)", conf.read_text(), re.MULTILINE)
    except OSError:
        return "unknown"
    if not m:
        return "unknown"
    return "enabled" if m.group(1).lower() == "yes" else "disabled"
