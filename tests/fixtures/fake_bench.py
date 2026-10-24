"""A stand-in for the `bench` CLI used by integration tests.

Supports the subset of commands frappe-host calls, producing realistic files
without needing Frappe, a database or Redis. Every call is appended to
$FAKE_BENCH_LOG. Set FAKE_BENCH_FAIL=<subcommand> to make that call fail.
"""

import gzip
import io
import json
import os
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
if os.environ.get("FAKE_BENCH_FAIL") == cmd:
    print(f"fake bench: {cmd} failed", file=sys.stderr)
    sys.exit(1)


def tar_with(path: Path, members: dict) -> None:
    with tarfile.open(path, "w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


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
else:
    print(f"fake bench: unsupported {argv}", file=sys.stderr)
    sys.exit(2)
