"""Host preparation (`init`) and Bench creation/update.

`init` follows the official installation guide
(https://docs.frappe.io/framework/user/en/installation) with these deliberate
differences, all documented in docs/install.md:
  * Node.js is installed from the official nodejs.org binary tarball after a
    SHA-256 check, into ~/.local (no `curl | bash` nvm installer, no root).
  * uv is installed with pipx (no `curl | sh`).
  * If apt only offers an older MariaDB than the branch requires, adding the
    MariaDB repository is a MANUAL step; frappe-host never pipes a remote
    script into sudo.
Every step is detected first, so re-running init only does what is missing.
"""

from __future__ import annotations

import hashlib
import os
import re
import tarfile
import urllib.request
from pathlib import Path

from frappe_host.adapters import bench_fs, process
from frappe_host.application import backups, registry
from frappe_host.application.operations import Operation, OperationFailed
from frappe_host.application.state import Store, state_home
from frappe_host.application.steps import Plan, Step
from frappe_host.domain import support
from frappe_host.domain.validation import ValidationError
from frappe_host.platform.linux import host

APT_COMMON = ["git", "curl", "build-essential", "python3-dev", "pkg-config", "pipx", "cron", "redis-server",
              "xvfb", "libfontconfig1"]
APT_MARIADB = ["mariadb-server", "mariadb-client", "libmariadb-dev"]
APT_PRODUCTION = ["nginx", "supervisor", "certbot", "python3-certbot-nginx"]
PYTHON_FOR_BRANCH = {"version-15": "3.12", "version-16": "3.14"}
NODE_DIST = "https://nodejs.org/dist"
_BENCH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def dpkg_installed(pkg: str) -> bool:
    r = process.run(["dpkg-query", "-W", "-f=${Status}", pkg], timeout=15)
    return r.ok and "install ok installed" in r.stdout


def apt_candidate_version(pkg: str) -> tuple[int, ...] | None:
    r = process.run(["apt-cache", "policy", pkg], timeout=30)
    m = re.search(r"Candidate:\s*(?:\d+:)?(\d+\.\d+(?:\.\d+)?)", r.stdout) if r.ok else None
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def installed_mariadb_version() -> tuple[int, ...] | None:
    for name in ("mariadb", "mysql"):
        exe, ver = host.tool_version(name)
        if exe and ver:
            m = re.search(r"(\d+\.\d+\.\d+)-MariaDB", ver) or re.search(r"Distrib (\d+\.\d+\.\d+)", ver)
            if m:
                return tuple(int(x) for x in m.group(1).split("."))
    return None


def node_major() -> int | None:
    v = host.parse_version(host.tool_version("node")[1])
    return v[0] if v else None


def nvm_node(major_min: int) -> Path | None:
    """Newest nvm-managed Node.js with at least `major_min`, if any."""
    root = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")) / "versions" / "node"
    found = []
    try:
        for d in root.iterdir():
            v = host.parse_version(d.name)
            if v and v[0] >= major_min and (d / "bin" / "node").exists():
                found.append((v, d))
    except OSError:
        return None
    return max(found)[1] if found else None


def uv_python(version: str) -> str | None:
    if not process.which("uv"):
        return None
    r = process.run(["uv", "python", "find", version], timeout=60)
    return r.stdout.strip() if r.ok and r.stdout.strip() else None


def _vstr(v: tuple[int, ...]) -> str:
    return ".".join(map(str, v))


# ---------------------------------------------------------------- Node.js

def _node_arch() -> str:
    arch = host.architecture()
    return {"x86_64": "x64", "aarch64": "arm64"}.get(arch) or arch


def node_install_dir() -> Path:
    return state_home() / "node"


def install_node(major: int, op: Operation | None = None, base_url: str = NODE_DIST) -> Path:
    """Download the latest official Node.js `major` tarball, verify SHA-256, unpack
    into frappe-host's data dir and link node/npm/npx/corepack into ~/.local/bin."""
    log = op.log if op else (lambda m: None)
    index = f"{base_url}/latest-v{major}.x"
    with urllib.request.urlopen(f"{index}/SHASUMS256.txt", timeout=60) as r:
        sums = r.read().decode()
    wanted = re.compile(rf"^([0-9a-f]{{64}})\s+(node-v{major}\.\d+\.\d+-linux-{re.escape(_node_arch())}\.tar\.xz)$", re.M)
    m = wanted.search(sums)
    if not m:
        raise OperationFailed(f"no Node.js {major} build for linux-{_node_arch()} in {index}/SHASUMS256.txt")
    digest, fname = m.groups()
    dest_root = node_install_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = dest_root / f".{fname}.partial"
    h = hashlib.sha256()
    log(f"downloading {index}/{fname}")
    with urllib.request.urlopen(f"{index}/{fname}", timeout=600) as r, open(tmp, "wb") as fh:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            fh.write(chunk)
    if h.hexdigest() != digest:
        tmp.unlink()
        raise OperationFailed(f"checksum mismatch for {fname}; download discarded")
    log(f"sha256 ok {digest}")
    target = dest_root / fname.removesuffix(".tar.xz")
    if not target.exists():
        with tarfile.open(tmp, "r:xz") as tf:
            for mem in tf.getmembers():
                if mem.name.startswith("/") or ".." in Path(mem.name).parts:
                    raise OperationFailed(f"unsafe path in Node.js archive: {mem.name}")
            try:
                tf.extractall(dest_root, filter="data")
            except TypeError:  # Python without extraction filters; members were checked above
                tf.extractall(dest_root)
    tmp.unlink(missing_ok=True)
    link_local_bins(target / "bin", ["node", "npm", "npx", "corepack"])
    log(f"node installed at {target}")
    return target


def link_local_bins(src_dir: Path, names: list[str]) -> None:
    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    for n in names:
        src = src_dir / n
        if not src.exists():
            continue
        link = local_bin / n
        if link.is_symlink() and node_install_dir() in Path(os.readlink(link)).parents:
            link.unlink()  # one of ours (older version): replace
        elif link.exists() or link.is_symlink():
            raise OperationFailed(f"{link} already exists and was not created by frappe-host; not overwriting")
        link.symlink_to(src)


# ---------------------------------------------------------------- init plan

def plan_init(branch: str, production: bool, secure_mariadb: bool) -> Plan:
    if branch not in support.BRANCHES:
        raise ValidationError(f"branch must be one of: {', '.join(support.BRANCHES)}")
    req = support.BRANCHES[branch]
    plan = Plan(f"prepare this computer for Frappe {branch}" + (" (production)" if production else ""))

    osr = host.os_release()
    if osr.get("ID") not in ("ubuntu", "debian") and "debian" not in osr.get("ID_LIKE", ""):
        plan.blockers.append(f"{osr.get('PRETTY_NAME', 'this OS')} is not Debian/Ubuntu; init only supports apt-based hosts")
        return plan
    if os.geteuid() == 0:
        plan.blockers.append("run init as your normal user (it uses sudo only for system packages); Bench must not run as root")
        return plan
    if not process.which("sudo"):
        plan.blockers.append("sudo is not installed")
        return plan

    # MariaDB version: apt may only offer an older release than the branch needs.
    packages = list(APT_COMMON)
    installed_db = installed_mariadb_version()
    mariadb_fresh = False
    if installed_db is not None:
        if installed_db >= req.mariadb_min:
            plan.satisfied.append(f"MariaDB {_vstr(installed_db)} (needs {_vstr(req.mariadb_min)}+)")
        else:
            plan.warnings.append(f"installed MariaDB {_vstr(installed_db)} is older than {branch} requires ({_vstr(req.mariadb_min)}+)")
            plan.steps.append(Step(
                f"upgrade MariaDB to {_vstr(req.mariadb_min[:2])}+ using the official MariaDB repository "
                "(https://mariadb.org/download/?t=repo-config), back up your databases first, then re-run "
                "`frappe-host init`", manual=True))
        packages += [p for p in APT_MARIADB if p != "mariadb-server" and p != "mariadb-client"]
    else:
        cand = apt_candidate_version("mariadb-server")
        if cand is not None and cand < req.mariadb_min:
            plan.steps.append(Step(
                f"apt offers MariaDB {_vstr(cand)} but {branch} needs {_vstr(req.mariadb_min)}+: add the official "
                f"MariaDB {_vstr(req.mariadb_min[:2])} repository (https://mariadb.org/download/?t=repo-config), "
                "install mariadb-server, mariadb-client and libmariadb-dev from it, then re-run `frappe-host init`",
                manual=True))
        else:
            packages += APT_MARIADB
            mariadb_fresh = True
    if req.branch in ("version-15", "version-16"):
        packages.append("wkhtmltopdf")
    if production:
        packages += APT_PRODUCTION

    missing = [p for p in packages if not dpkg_installed(p)]
    present = [p for p in packages if p not in missing]
    if present:
        plan.satisfied.append(f"apt packages already installed: {', '.join(present)}")
    if missing:
        plan.steps.append(Step("refresh the apt package index", ["apt-get", "update"], sudo=True,
                               why="installing system packages"))
        plan.steps.append(Step(f"install {len(missing)} system package(s): {', '.join(missing)}",
                               ["apt-get", "install", "-y", *missing], sudo=True, why="installing system packages"))

    services = ["redis-server"] + (["mariadb"] if installed_db or mariadb_fresh else [])
    if production:
        services += ["nginx", "supervisor"]
    for svc in services:
        if host.systemd_state(svc) == "active":
            plan.satisfied.append(f"service {svc} is running")
        else:
            plan.steps.append(Step(f"start {svc} and enable it at boot", ["systemctl", "enable", "--now", svc],
                                   sudo=True, why=f"managing the {svc} system service"))
    if mariadb_fresh or secure_mariadb:
        plan.steps.append(Step(
            "set the MariaDB root password (interactive). bench new-site logs in as the database root user; "
            "answer the prompts and remember the password", ["mariadb-secure-installation"], sudo=True,
            why="configuring the MariaDB server"))
    else:
        plan.warnings.append("if you never set a MariaDB root password, re-run with --secure-mariadb")

    # uv + Python
    if process.which("uv"):
        plan.satisfied.append("uv is installed")
    else:
        plan.steps.append(Step("install uv (Python installer) for your user", ["pipx", "install", "uv"]))
    pyver = PYTHON_FOR_BRANCH[branch]
    if uv_python(pyver):
        plan.satisfied.append(f"Python {pyver} is available through uv")
    else:
        plan.steps.append(Step(f"install Python {pyver} for your user (managed by uv)", ["uv", "python", "install", pyver]))

    # Node.js + yarn
    nm = node_major()
    nvm = nvm_node(req.node_major_min)
    if nm is not None and nm >= req.node_major_min:
        plan.satisfied.append(f"Node.js {nm} on PATH (needs {req.node_major_min}+)")
    elif nvm is not None:
        plan.steps.append(Step(
            f"Node.js {nvm.name} is installed with nvm but is not your default: run `nvm alias default "
            f"{req.node_major_min}` and open a new terminal (frappe-host does not change nvm settings)", manual=True))
    else:
        if nm is not None:
            plan.warnings.append(f"Node.js {nm} on PATH is too old for {branch}; a newer one will be linked into ~/.local/bin")
        plan.steps.append(Step(
            f"install Node.js {req.node_major_min} for your user: official nodejs.org build, SHA-256 verified, "
            f"unpacked into {node_install_dir()} and linked into ~/.local/bin",
            func=lambda op, m=req.node_major_min: install_node(m, op)))
    if process.which("yarn"):
        plan.satisfied.append("yarn is installed")
    else:
        plan.steps.append(Step("install yarn for your user", ["npm", "install", "-g", "--prefix", str(Path.home() / ".local"), "yarn"]))

    # bench
    if process.which("bench"):
        plan.satisfied.append("bench CLI is installed")
    else:
        plan.steps.append(Step("install the bench CLI for your user", ["uv", "tool", "install", "frappe-bench"]))

    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
        plan.warnings.append(f"{local_bin} is not on your PATH yet; open a new terminal (or log out and in) after init")
    free = host.disk_usage(Path.home()).free
    if free < support.MIN_FREE_DISK_GIB * 1024**3:
        plan.warnings.append(f"only {free / 1024**3:.1f} GiB free in your home directory; a Bench needs several GiB")
    return plan


# ---------------------------------------------------------------- bench create / update

def plan_create_bench(store: Store, name: str, branch: str, parent: Path, python_version: str | None) -> tuple[Plan, Path]:
    if not _BENCH_NAME_RE.match(name or ""):
        raise ValidationError("bench name: lowercase letters, digits, '-' or '_' (e.g. frappe-bench)")
    if branch not in support.BRANCHES:
        raise ValidationError(f"--frappe-branch must be one of: {', '.join(support.BRANCHES)}")
    req = support.BRANCHES[branch]
    if not parent.is_dir():
        raise ValidationError(f"{parent} is not a directory")
    target = parent / name
    if target.exists():
        raise ValidationError(f"{target} already exists")
    plan = Plan(f"create Bench {target} on {branch}")
    if os.geteuid() == 0:
        plan.blockers.append("do not create a Bench as root")
    bench_exe = process.which("bench")
    if not bench_exe:
        plan.blockers.append("bench CLI not found; run `frappe-host init` first")
    pyver = python_version or PYTHON_FOR_BRANCH[branch]
    py = uv_python(pyver)
    if not py:
        found = process.which(f"python{pyver}")
        py = found
    if not py:
        plan.blockers.append(f"Python {pyver} not found; run `frappe-host init --branch {branch}`")
    nm = node_major()
    if nm is None or nm < req.node_major_min:
        plan.blockers.append(f"Node.js {req.node_major_min}+ is required (found {nm or 'none'}); run `frappe-host init --branch {branch}`")
    if not process.which("yarn"):
        plan.blockers.append("yarn not found; run `frappe-host init`")
    free = host.disk_usage(parent).free
    if free < support.MIN_FREE_DISK_GIB * 1024**3:
        plan.blockers.append(f"only {free / 1024**3:.1f} GiB free in {parent}; need {support.MIN_FREE_DISK_GIB} GiB+")
    if plan.blockers:
        return plan, target
    plan.steps.append(Step(
        f"bench init: clone Frappe {branch}, create the virtualenv, install Node packages and build assets "
        "(takes several minutes)",
        [bench_exe, "init", name, "--frappe-branch", branch, "--python", py], cwd=parent))
    plan.steps.append(Step(f"register {target} with frappe-host", func=lambda op: registry.register(store, target)))
    return plan, target


def app_changes(bench: Path) -> dict[str, list[str]]:
    """Apps with uncommitted local changes (git status --porcelain), read-only."""
    out = {}
    for app in bench_fs.list_apps(bench):
        d = bench / "apps" / app.name
        if (d / ".git").exists() and process.which("git"):
            r = process.run(["git", "-C", str(d), "status", "--porcelain"], timeout=60)
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            if lines:
                out[app.name] = lines[:5]
    return out


def plan_update_bench(store: Store, bench: Path, backup_sites: bool) -> Plan:
    plan = Plan(f"update Bench {bench} (git pull, requirements, migrate, build)")
    bench_exe = process.which("bench")
    if not bench_exe:
        plan.blockers.append("bench CLI not found")
        return plan
    if bench.stat().st_uid != os.getuid():
        plan.blockers.append("Bench is owned by another user")
    for app, lines in app_changes(bench).items():
        plan.warnings.append(f"app {app} has local changes; `bench update` may refuse or they may conflict: {lines[0]}")
    sites = bench_fs.list_sites(bench)
    if backup_sites:
        for s in sites:
            plan.steps.append(Step(f"pre-update backup of {s} (verified)",
                                   func=lambda op, s=s: _verified_backup(store, bench, s, op)))
    else:
        plan.warnings.append("pre-update backups SKIPPED at your request")
    plan.steps.append(Step("run bench update (stops at the first failing stage)", [bench_exe, "update"], cwd=bench))
    plan.steps.append(Step("refresh the registry snapshot", func=lambda op: registry.refresh(store, str(bench))))
    return plan


def _verified_backup(store: Store, bench: Path, site: str, op: Operation) -> None:
    cp = backups.plan_create(bench, site, None)
    rec = backups.create(store, bench, site, cp, None)
    ok, problems = backups.verify(store, rec["id"])
    op.log(f"backup {rec['id']} of {site}: {'verified' if ok else 'FAILED VERIFICATION'}")
    if not ok:
        raise OperationFailed(f"backup of {site} failed verification: {'; '.join(problems[:2])}")
