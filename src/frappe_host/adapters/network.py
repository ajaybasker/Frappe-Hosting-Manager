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


@dataclass
class TlsResult:
    ok: bool
    detail: str
    not_after: str | None = None
    days_left: int | None = None
    issuer: str | None = None


def tls_check(connect_host: str, port: int, server_name: str, timeout: float = 8.0) -> TlsResult:
    """TLS handshake with full verification (system CA store + hostname check).
    connect_host may differ from server_name (e.g. 127.0.0.1 with SNI=domain)."""
    import ssl
    import time as _time

    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((connect_host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=server_name) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return TlsResult(False, f"certificate not trusted for {server_name}: {e.verify_message or e}")
    except (OSError, ssl.SSLError) as e:
        return TlsResult(False, f"{type(e).__name__}: {e}")
    not_after = cert.get("notAfter")
    days = None
    if not_after:
        days = int((ssl.cert_time_to_seconds(not_after) - _time.time()) // 86400)
    issuer = ", ".join("=".join(x) for rdn in cert.get("issuer", ()) for x in rdn if x[0] in ("organizationName", "commonName"))
    return TlsResult(True, f"valid certificate, expires in {days} days", not_after, days, issuer or None)


def http_get(url: str, timeout: float = 10.0) -> HttpProbe:
    """GET a URL with normal certificate verification (used by `probe`)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "frappe-host/probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096)
            status = resp.status
    except urllib.error.HTTPError as e:
        return HttpProbe(False, e.code, f"HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return HttpProbe(False, None, f"{type(reason).__name__}: {reason}")
    pong = False
    try:
        pong = json.loads(body).get("message") == "pong"
    except (ValueError, AttributeError):
        pass
    return HttpProbe(status == 200 and pong, status, f"HTTP {status}", pong)


DEFAULT_PUBLIC_IP_SERVICE = "https://api.ipify.org"


def public_ip(service: str = DEFAULT_PUBLIC_IP_SERVICE, timeout: float = 8.0) -> str | None:
    """Ask an external service for this network's public IPv4. Opt-in only:
    callers must have the user's explicit consent (--detect-public-ip)."""
    import ipaddress
    import urllib.request

    try:
        with urllib.request.urlopen(urllib.request.Request(service, headers={"User-Agent": "frappe-host"}), timeout=timeout) as r:
            text = r.read(64).decode().strip()
        return str(ipaddress.ip_address(text))
    except (OSError, ValueError):
        return None
