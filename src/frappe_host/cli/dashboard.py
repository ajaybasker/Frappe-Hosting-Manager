"""Local, read-only web dashboard.

Security: binds to 127.0.0.1 only (no option to change it), requires a random
per-run token (given once in the URL, then kept in an HttpOnly cookie),
rejects requests whose Host header is not the loopback address (DNS
rebinding), sends a strict Content-Security-Policy and performs no actions.
"""

from __future__ import annotations

import html
import json
import secrets
from dataclasses import asdict
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from frappe_host import __version__
from frappe_host.adapters import bench_fs
from frappe_host.application import backups, doctor, operations, registry
from frappe_host.application.state import Store
from frappe_host.domain.models import overall
from frappe_host.policies.redaction import redact_obj

CSS = """
:root{--bg:#f7f7f8;--fg:#1d1d1f;--muted:#666;--card:#fff;--line:#e3e3e6;--pass:#1a7f37;--warn:#9a6700;--fail:#cf222e;--info:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#111214;--fg:#e8e8ea;--muted:#9a9aa0;--card:#1b1c1f;--line:#2c2d31;--pass:#3fb950;--warn:#d29922;--fail:#f85149;--info:#58a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase}tr:last-child td{border-bottom:0}
.s{font-weight:600;font-size:12px;text-transform:uppercase}.pass{color:var(--pass)}.warn{color:var(--warn)}.fail{color:var(--fail)}.info,.unknown{color:var(--info)}
code{font-size:12px}
"""


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _table(headers, rows) -> str:
    if not rows:
        return '<div class="card"><table><tr><td class="muted">none</td></tr></table></div>'
    h = "".join(f"<th>{_esc(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="card"><table><tr>{h}</tr>{body}</table></div>'


def summary(store: Store) -> dict:
    benches = registry.list_benches(store)
    infos = [bench_fs.inspect_bench(Path(b["path"])) for b in benches if Path(b["path"]).is_dir()]
    checks = doctor.run(infos)
    sites = [asdict(bench_fs.inspect_site(Path(i.path), s)) for i in infos for s in i.sites]
    return redact_obj({
        "version": __version__,
        "overall": overall(checks),
        "checks": [c.to_dict() for c in checks],
        "benches": [{**b, "drift": asdict(registry.drift(b))} for b in benches],
        "sites": sites,
        "backups": backups.list_backups(store)[:50],
        "operations": operations.history(store, 30),
    })


def render(data: dict) -> str:
    st = lambda s: f'<span class="s {_esc(s)}">{_esc(s)}</span>'
    checks = _table(["status", "section", "check", "finding"],
                    [[st(c["status"]), _esc(c["section"]), _esc(c["name"]), _esc(c["summary"])] for c in data["checks"]])
    benches = _table(["path", "frappe", "mode", "state"],
                     [[f"<code>{_esc(b['path'])}</code>", _esc(b["frappe_version"]), _esc(b["process_mode"]),
                       st("warn") + " drift" if b["drift"]["changes"] or not b["drift"]["exists"] else st("pass")] for b in data["benches"]])
    sites = _table(["site", "bench", "maintenance", "scheduler", "developer mode"],
                   [[_esc(s["name"]), f"<code>{_esc(s['bench_path'])}</code>", "on" if s["maintenance_mode"] else "off",
                     "paused" if s["scheduler_paused"] else "on", "on" if s["developer_mode"] else "off"] for s in data["sites"]])
    bks = _table(["id", "site", "created", "size", "verified"],
                 [[f"<code>{_esc(b['id'])}</code>", _esc(b["site"]), _esc(b["created_at"]), f"{b['total_size'] / 1048576:.1f} MiB",
                   _esc(b["verify_result"] or "never")] for b in data["backups"]])
    ops = _table(["started", "status", "operation", "target"],
                 [[_esc(o["started_at"]), st({"succeeded": "pass", "failed": "fail"}.get(o["status"], "warn")) + " " + _esc(o["status"]),
                   _esc(o["kind"]), _esc(o["target"])] for o in data["operations"]])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Frappe Hosting Manager</title><style>{CSS}</style></head><body><main>
<h1>Frappe Hosting Manager</h1><div class="muted">v{_esc(data['version'])} · read-only local dashboard · overall: <b>{_esc(data['overall'])}</b>
· public reachability is never verified from this computer · <a href="/">refresh</a></div>
<h2>Host readiness</h2>{checks}<h2>Benches</h2>{benches}<h2>Sites</h2>{sites}<h2>Backups</h2>{bks}<h2>Recent operations</h2>{ops}
</main></body></html>"""


def make_handler(store_factory, token: str, port_ref: list[int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "frappe-host"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str, ctype: str, extra: dict | None = None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            allowed = {f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}"}
            if self.headers.get("Host") not in allowed:
                return self._send(421, "misdirected request", "text/plain")
            url = urlparse(self.path)
            given = parse_qs(url.query).get("token", [None])[0]
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            have = jar["fh_token"].value if "fh_token" in jar else None
            if given is not None and secrets.compare_digest(given, token):
                # Move the token into a cookie and drop it from the address bar.
                return self._send(303, "", "text/plain", {"Location": url.path or "/",
                                  "Set-Cookie": f"fh_token={token}; HttpOnly; SameSite=Strict; Path=/"})
            if have is None or not secrets.compare_digest(have, token):
                return self._send(403, "forbidden: open the URL printed by `frappe-host dashboard`", "text/plain")
            store = store_factory()
            try:
                data = summary(store)
            finally:
                store.close()
            if url.path == "/api/summary":
                return self._send(200, json.dumps(data, default=str), "application/json")
            if url.path == "/":
                return self._send(200, render(data), "text/html; charset=utf-8")
            return self._send(404, "not found", "text/plain")

    return Handler


def serve(port: int, store_factory=Store, token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    """Create (not start) the server, always bound to 127.0.0.1. Returns (server, token)."""
    token = token or secrets.token_urlsafe(24)
    port_ref = [port]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store_factory, token, port_ref))
    port_ref[0] = srv.server_address[1]
    return srv, token
