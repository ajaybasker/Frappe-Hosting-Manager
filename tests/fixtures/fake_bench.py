"""A stand-in for the `bench` CLI used by integration tests.

Supports the subset of commands frappe-host calls, producing realistic files
without needing Frappe, a database or Redis. Every call is appended to
$FAKE_BENCH_LOG. Set FAKE_BENCH_FAIL=<subcommand> to make that call fail.
"""

import gzip
import io
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

argv = sys.argv[1:]
cwd = Path.cwd()
with open(os.environ["FAKE_BENCH_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": str(cwd)}) + "\n")

site = None
if argv[:1] == ["--site"]:
    site, argv = argv[1], argv[2:]
cmd = argv[0] if argv else ""
sub = argv[1] if len(argv) > 1 else ""
if os.environ.get("FAKE_BENCH_FAIL") in (cmd, f"{cmd} {sub}".strip()):
    print(f"fake bench: {cmd} failed", file=sys.stderr)
    sys.exit(1)


def tar_with(path: Path, members: dict) -> None:
    with tarfile.open(path, "w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def site_cfg(name: str) -> Path:
    return cwd / "sites" / name / "site_config.json"


def update_json(path: Path, **changes) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update(changes)
    path.write_text(json.dumps(data))


def write_nginx() -> None:
    names = []
    for d in sorted((cwd / "sites").iterdir()):
        if (d / "site_config.json").exists():
            names.append(d.name)
            for dom in json.loads((d / "site_config.json").read_text()).get("domains", []):
                names.append(dom["domain"] if isinstance(dom, dict) else dom)
    (cwd / "config" / "nginx.conf").write_text("server {\n\tserver_name " + " ".join(names) + ";\n}\n")


if cmd == "backup":
    d = cwd / "sites" / site / "private" / "backups"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"{time.time_ns() % 1000000:06d}"
    prefix = f"{stamp}-{site.replace('.', '_')}"
    with gzip.open(d / f"{prefix}-database.sql.gz", "wb") as g:
        g.write(b"-- MariaDB dump\nCREATE TABLE `tabUser` (name varchar(140));\nINSERT INTO `tabUser` VALUES ('Administrator');\n")
    if "--with-files" in argv:
        tar_with(d / f"{prefix}-files.tar", {f"{site}/public/files/logo.png": b"PNG"})
        tar_with(d / f"{prefix}-private-files.tar", {f"{site}/private/files/doc.pdf": b"PDF"})
    (d / f"{prefix}-site_config_backup.json").write_text(json.dumps({"db_name": "x", "db_password": "secret"}))
    print("Backup Summary for", site)
elif cmd == "new-site":
    name = argv[1]
    s = cwd / "sites" / name
    s.mkdir(parents=True)
    (s / "site_config.json").write_text(json.dumps({"db_name": "_" + name.replace(".", "_"), "db_password": "generated"}))
elif cmd == "restore":
    (cwd / "sites" / site / "restored.json").write_text(json.dumps(argv))
elif cmd == "drop-site":
    name = argv[1]
    (cwd / "archived_sites").mkdir(exist_ok=True)
    shutil.move(str(cwd / "sites" / name), str(cwd / "archived_sites" / name))
elif cmd in ("migrate", "list-apps", "update"):
    print("ok")
elif cmd == "set-maintenance-mode":
    update_json(site_cfg(site), maintenance_mode=1 if argv[1] == "on" else 0)
elif cmd == "set-config":
    update_json(site_cfg(site), **{argv[1]: argv[2]})
elif cmd == "install-app":
    (cwd / "sites" / site / "installed_apps.txt").open("a").write(argv[1] + "\n")
elif cmd == "get-app":
    name = argv[-1].rstrip("/").split("/")[-1].removesuffix(".git").replace("-", "_")
    pkg = cwd / "apps" / name / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('__version__ = "1.0.0"\n')
    with open(cwd / "sites" / "apps.txt", "a") as fh:
        fh.write(name + "\n")
elif cmd == "config" and sub == "dns_multitenant":
    update_json(cwd / "sites" / "common_site_config.json", dns_multitenant=argv[2] == "on")
elif cmd == "setup" and sub == "supervisor":
    (cwd / "config" / "supervisor.conf").write_text("[group:fake-web]\n")
    update_json(cwd / "sites" / "common_site_config.json", restart_supervisor_on_update=True)
elif cmd == "setup" and sub == "nginx":
    write_nginx()
elif cmd == "setup" and sub == "add-domain":
    domain = argv[2]
    s = argv[argv.index("--site") + 1]
    cfg = json.loads(site_cfg(s).read_text())
    domains = [d for d in cfg.get("domains", []) if (d["domain"] if isinstance(d, dict) else d) != domain]
    if "--ssl-certificate" in argv:
        domains.append({"domain": domain, "ssl_certificate": argv[argv.index("--ssl-certificate") + 1],
                        "ssl_certificate_key": argv[argv.index("--ssl-certificate-key") + 1]})
    else:
        domains.append(domain)
    update_json(site_cfg(s), domains=domains)
elif cmd == "setup" and sub == "remove-domain":
    domain = argv[2]
    s = argv[argv.index("--site") + 1]
    cfg = json.loads(site_cfg(s).read_text())
    update_json(site_cfg(s), domains=[d for d in cfg.get("domains", []) if (d["domain"] if isinstance(d, dict) else d) != domain])
elif cmd == "init":
    name = argv[1]
    root = cwd / name
    for d in ("sites", "logs", "config", "env", "apps/frappe/frappe"):
        (root / d).mkdir(parents=True)
    (root / "apps/frappe/frappe/__init__.py").write_text('__version__ = "16.0.0"\n')
    (root / "sites/apps.txt").write_text("frappe\n")
    (root / "sites/common_site_config.json").write_text(json.dumps({"webserver_port": 8000}))
    (root / "Procfile").write_text("web: bench serve --port 8000\n")
    (root / "env/pyvenv.cfg").write_text("version = 3.14.0\n")
else:
    print(f"fake bench: unsupported {argv}", file=sys.stderr)
    sys.exit(2)
