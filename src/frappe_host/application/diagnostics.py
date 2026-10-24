"""User-initiated diagnostics bundle and a compact health `check`.

The bundle is written locally (mode 0600) after the user has seen its file
list; nothing is ever uploaded. All text passes through redaction.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from frappe_host import __version__
from frappe_host.adapters import bench_fs, network
from frappe_host.application import backups, doctor, operations, registry
from frappe_host.application.state import Store
from frappe_host.domain.models import Check, Status
from frappe_host.platform.linux import host
from frappe_host.policies.redaction import redact_obj, redact_text

LOG_TAIL_LINES = 200
BENCH_LOGS = ("web.error.log", "worker.error.log", "schedule.error.log", "frappe.log")


def collect(store: Store) -> dict[str, str]:
    """Return {archive path: text} for the bundle. Everything is redacted."""
    benches = registry.list_benches(store)
    infos = [bench_fs.inspect_bench(Path(b["path"])) for b in benches if Path(b["path"]).is_dir()]
    files: dict[str, str] = {}
    files["manifest.json"] = json.dumps({"frappe_host": __version__, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                         "os": host.os_release().get("PRETTY_NAME")}, indent=2)
    files["doctor.json"] = json.dumps([c.to_dict() for c in doctor.run(infos)], indent=2, default=str)
    files["benches.json"] = json.dumps([{**b, "drift": asdict(registry.drift(b))} for b in benches], indent=2, default=str)
    files["history.json"] = json.dumps(operations.history(store, 100), indent=2, default=str)
    files["backups.json"] = json.dumps(backups.list_backups(store), indent=2, default=str)
    for info in infos:
        bench = Path(info.path)
        key = bench.name
        files[f"{key}/common_site_config.json"] = json.dumps(bench_fs.common_config(bench), indent=2)
        for name in BENCH_LOGS:
            p = bench / "logs" / name
            if p.is_file():
                with open(p, encoding="utf-8", errors="replace") as fh:
                    files[f"{key}/logs/{name}"] = "".join(deque(fh, maxlen=LOG_TAIL_LINES))
    return {k: redact_text(json.dumps(redact_obj(json.loads(v)), indent=2) if k.endswith(".json") else v)
            for k, v in files.items()}


def write_bundle(files: dict[str, str], out: Path) -> Path:
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tf:
        for name, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(f"frappe-host-diagnostics/{name}")
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
            tf.addfile(info, io.BytesIO(data))
    return out


def check(store: Store, max_backup_age_hours: int = 26, cert_days: int = 14) -> list[Check]:
    """Fast health check intended for cron/monitoring: problems only matter."""
    out: list[Check] = []
    benches = [b for b in registry.list_benches(store)]
    infos = []
    for b in benches:
        d = registry.drift(b)
        if not d.exists:
            out.append(Check("check", f"bench {b['path']}", Status.FAIL, "registered Bench directory is missing"))
            continue
        infos.append(bench_fs.inspect_bench(Path(b["path"])))
    paths = [Path.home(), *[Path(i.path) for i in infos]]
    out += [c for c in doctor.check_resources(paths) if c.name.startswith("disk") or c.name == "memory-pressure"]
    out += [c for c in doctor.check_exposure(infos) if c.status == Status.FAIL]
    now = time.time()
    for info in infos:
        bench = Path(info.path)
        for site in info.sites:
            recs = [r for r in backups.list_backups(store, site) if r["bench_path"] == info.path]
            if not recs:
                continue  # only sites the user backs up through frappe-host are monitored
            from datetime import datetime

            age_h = (now - datetime.fromisoformat(recs[0]["created_at"]).timestamp()) / 3600
            out.append(Check("check", f"backup {site}", Status.PASS if age_h <= max_backup_age_hours else Status.FAIL,
                             f"latest frappe-host backup {age_h:.0f}h old"))
            for d in bench_fs.site_config(bench, site).get("domains", []):
                if isinstance(d, dict) and d.get("ssl_certificate"):
                    t = network.tls_check("127.0.0.1", 443, d["domain"])
                    st = Status.PASS if t.ok and (t.days_left or 0) > cert_days else Status.FAIL
                    out.append(Check("check", f"tls {d['domain']}", st, t.detail))
        if bench_fs.detect_process_mode(bench) == "supervisor":
            for site in info.sites:
                p = network.probe_frappe("127.0.0.1", 80, site, timeout=5)
                out.append(Check("check", f"http {site}", Status.PASS if p.ok else Status.FAIL, p.detail))
    return out
