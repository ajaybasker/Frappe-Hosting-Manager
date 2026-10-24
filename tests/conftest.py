"""Shared fixtures: disposable fake Benches and a fake `bench` executable.

Nothing in the test-suite touches a real Bench, database or system service.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

FAKE_BENCH = Path(__file__).parent / "fixtures" / "fake_bench.py"


def make_bench(root: Path, *, frappe_version: str = "16.4.0", branch: str = "version-16",
               sites: tuple[str, ...] = ("a.local",), apps: tuple[str, ...] = ("frappe",),
               procfile: str = "web: bench serve --port 8000\nworker: bench worker\n",
               python: str = "3.14.0") -> Path:
    """Create a minimal on-disk Bench layout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "sites").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "env").mkdir(exist_ok=True)
    (root / "env" / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {python}\n")
    (root / "Procfile").write_text(procfile)
    (root / "sites" / "apps.txt").write_text("\n".join(apps) + "\n")
    (root / "sites" / "common_site_config.json").write_text(json.dumps({
        "webserver_port": 8000,
        "socketio_port": 9000,
        "redis_cache": "redis://127.0.0.1:13000",
        "redis_queue": "redis://127.0.0.1:11000",
    }))
    for app in apps:
        pkg = root / "apps" / app / app
        pkg.mkdir(parents=True, exist_ok=True)
        version = frappe_version if app == "frappe" else "1.0.0"
        (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n')
        git = root / "apps" / app / ".git"
        (git / "refs" / "heads" / branch).parent.mkdir(parents=True, exist_ok=True)
        (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (git / "refs" / "heads" / branch).write_text("a" * 40 + "\n")
    for s in sites:
        add_site(root, s)
    return root


def add_site(bench: Path, name: str, **extra) -> None:
    d = bench / "sites" / name
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "private" / "backups").mkdir(parents=True, exist_ok=True)
    cfg = {"db_name": "_" + name.replace(".", "_"), "db_password": "s3cret-db-pass", "db_type": "mariadb", **extra}
    (d / "site_config.json").write_text(json.dumps(cfg))


def tree_digest(root: Path) -> dict[str, tuple[int, float]]:
    """Map of every path under root to (size, mtime) for no-mutation assertions."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.lstat()
            out[str(p)] = (st.st_size, st.st_mtime)
    return out


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets a throwaway HOME (so ~/.local/bin, ~/.nvm and state are isolated)
    and a `sudo` that refuses to run unless a test installs the fake system tools."""
    real_home = os.path.expanduser("~")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Drop PATH entries under the real home (~/.local/bin, nvm, ...) so a developer's
    # own bench/node/uv can never be picked up by a test.
    clean = [p for p in os.environ["PATH"].split(os.pathsep) if p and not p.startswith(real_home + os.sep)]
    monkeypatch.setenv("PATH", os.pathsep.join(clean))
    monkeypatch.delenv("NVM_DIR", raising=False)
    guard = tmp_path / "guard-bin"
    guard.mkdir()
    stub = guard / "sudo"
    stub.write_text("#!/bin/sh\necho 'real sudo is blocked in tests' >&2\nexit 97\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{guard}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FRAPPE_HOST_SYSROOT", str(tmp_path / "sysroot"))
    return home


FAKE_TOOLS = ("sudo", "nginx", "supervisorctl", "systemctl", "certbot", "crontab", "apt-get", "apt-cache",
              "dpkg-query", "uv", "pipx", "npm", "mariadb-secure-installation")


@pytest.fixture
def fake_system(tmp_path, monkeypatch):
    """Fake system tools first on PATH plus a sysroot with /etc directories.
    Returns a helper with .calls() and .sysroot."""
    bindir = tmp_path / "sysbin"
    bindir.mkdir()
    tool = Path(__file__).parent / "fixtures" / "fake_tool.py"
    for name in FAKE_TOOLS:
        exe = bindir / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tool}" {name} "$@"\n')
        exe.chmod(0o755)
    root = tmp_path / "sysroot"
    for d in ("etc/nginx/conf.d", "etc/supervisor/conf.d", "etc/letsencrypt/live"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "etc" / "hosts").write_text("127.0.0.1\tlocalhost\n")
    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("FAKE_TOOL_LOG", str(log))
    monkeypatch.setenv("FAKE_CRONTAB", str(tmp_path / "crontab.txt"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    class FakeSystem:
        sysroot = root
        crontab = tmp_path / "crontab.txt"

        @staticmethod
        def calls(tool_name: str | None = None) -> list[list[str]]:
            if not log.exists():
                return []
            rows = [json.loads(l) for l in log.read_text().splitlines()]
            return [r["argv"] for r in rows if tool_name is None or r["tool"] == tool_name]

    return FakeSystem


@pytest.fixture
def state_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("FRAPPE_HOST_HOME", str(home))
    return home


@pytest.fixture
def store(state_home):
    from frappe_host.application.state import Store

    s = Store(state_home)
    yield s
    s.close()


@pytest.fixture
def bench(tmp_path) -> Path:
    return make_bench(tmp_path / "benches" / "frappe-bench")


@pytest.fixture
def fake_bench_on_path(tmp_path, monkeypatch) -> Path:
    """Put a fake `bench` executable first on PATH; it logs each invocation to calls.jsonl."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "bench"
    # A /bin/sh wrapper: shebang lines cannot contain spaces, and this repo's path may.
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_BENCH}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_BENCH_LOG", str(tmp_path / "calls.jsonl"))
    return tmp_path / "calls.jsonl"


def bench_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text().splitlines()]
