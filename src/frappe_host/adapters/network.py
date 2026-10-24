"""Network probes. These never claim more than they observed: a request made
from this host proves local reachability only."""

from __future__ import annotations

import http.client
import json
import socket
from dataclasses import dataclass


@dataclass
class HttpProbe:
    ok: bool
    status: int | None
    detail: str
    frappe_pong: bool = False


def probe_frappe(host: str, port: int, site: str, timeout: float = 5.0) -> HttpProbe:
    """GET /api/method/ping with the site as Host header (Frappe resolves sites by Host)."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/api/method/ping", headers={"Host": site, "User-Agent": "frappe-host/doctor"})
        resp = conn.getresponse()
        body = resp.read(4096)
    except (OSError, http.client.HTTPException) as e:
        return HttpProbe(False, None, f"{type(e).__name__}: {e}")
    finally:
        conn.close()
    pong = False
    try:
        pong = json.loads(body).get("message") == "pong"
    except (ValueError, AttributeError):
        pass
    return HttpProbe(resp.status == 200 and pong, resp.status, f"HTTP {resp.status}", pong)


def tcp_connect(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    return sorted({i[4][0] for i in infos})
