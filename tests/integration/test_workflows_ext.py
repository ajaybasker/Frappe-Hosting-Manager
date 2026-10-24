"""Integration tests for setup, lifecycle, production, publishing, scheduling and tooling.

All system tools are fakes (tests/fixtures/fake_tool.py) and every /etc path
points into a per-test sysroot; nothing touches the real system.
"""

import hashlib
import http.server
import io
import json
import os
import stat
import tarfile
import threading
from pathlib import Path

import pytest

from frappe_host.application import production
from frappe_host.cli.main import main
from frappe_host.platform.linux import host
from tests.conftest import add_site, bench_calls, make_bench, tree_digest


def run(capsys, *argv):
    code = main(list(argv))
    o, e = capsys.readouterr()
    return code, o, e


@pytest.fixture
def registered(state_home, bench, capsys):
    assert run(capsys, "bench", "register", str(bench), "--yes")[0] == 0
    return bench.resolve()


@pytest.fixture
def quiet_network(monkeypatch):
    """No listeners, no port-80 waiting, deterministic DNS."""
    monkeypatch.setattr(host, "tcp_listeners", lambda *a, **k: [])
    monkeypatch.setattr(host, "systemd_state", lambda unit: "active")
    monkeypatch.setattr(production, "POST_CHECK_SECONDS", 0)
    from frappe_host.adapters import network

    monkeypatch.setattr(network, "resolve", lambda name: ["203.0.113.10"] if name.endswith("example.com") else [])
    monkeypatch.setattr(network, "public_ip", lambda *a, **k: pytest.fail("public_ip must be opt-in"))


# ---------------------------------------------------------------- steps executor

def test_executor_rolls_back_completed_steps_in_reverse(store):
    from frappe_host.application.operations import OperationFailed
    from frappe_host.application.steps import Plan, Step, execute

    log = []

    def act(name, fail=False):
        def f(op):
            log.append(name)
            if fail:
                raise OperationFailed(f"{name} broke")
        return f

    plan = Plan("t", [
        Step("a", func=act("a"), undo=Step("undo a", func=act("undo-a"))),
        Step("manual", manual=True),
        Step("b", func=act("b"), undo=Step("undo b", func=act("undo-b"))),
        Step("c", func=act("c", fail=True)),
    ])
    with pytest.raises(OperationFailed, match="rolled back 2"):
        execute(store, "test", None, plan)
    assert log == ["a", "b", "c", "undo-b", "undo-a"]


def test_executor_refuses_plans_with_blockers(store):
    from frappe_host.application.operations import OperationFailed
    from frappe_host.application.steps import Plan, Step, execute

    with pytest.raises(OperationFailed, match="blockers"):
        execute(store, "t", None, Plan("t", [Step("x", func=lambda op: None)], blockers=["no"]))


# ---------------------------------------------------------------- init

def test_init_plan_installs_only_missing_and_respects_mariadb_version(state_home, fake_system, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DPKG_INSTALLED", "git,curl")
    monkeypatch.setenv("FAKE_APT_MARIADB", "1:10.11.13-0ubuntu1")  # too old for version-16
    monkeypatch.setattr(host, "os_release", lambda *a: {"ID": "ubuntu", "VERSION_ID": "24.04"})
    monkeypatch.setattr(host, "tool_version", lambda name, args=("--version",): (None, None))
    code, out, _ = run(capsys, "init", "--branch", "version-16", "--dry-run")
    assert code == 0
    assert "MANUAL: apt offers MariaDB 10.11.13 but version-16 needs 11.8" in out
    install = next(l for l in out.splitlines() if "apt-get install" in l)
    assert "mariadb-server" not in install and "git " not in install and "redis-server" in install
    assert "install Node.js 24" in out
    assert fake_system.calls("apt-get") == []  # dry run


def test_init_runs_steps_through_sudo(state_home, fake_system, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DPKG_INSTALLED", ",".join(
        ["git", "curl", "build-essential", "python3-dev", "pkg-config", "pipx", "cron", "redis-server", "xvfb",
         "libfontconfig1", "mariadb-server", "mariadb-client", "libmariadb-dev"]))
    monkeypatch.setenv("FAKE_UV_PYTHON", "/opt/py/bin/python3.14")
    monkeypatch.setattr(host, "os_release", lambda *a: {"ID": "ubuntu", "VERSION_ID": "24.04"})
    monkeypatch.setattr(host, "systemd_state", lambda u: "active")
    versions = {"mariadb": "mariadb from 11.8.3-MariaDB, client 15.2", "node": "v24.1.0"}
    monkeypatch.setattr(host, "tool_version", lambda name, args=("--version",): ("/x/" + name, versions.get(name)) if name in versions else (None, None))
    code, out, err = run(capsys, "init", "--yes")
    assert code == 0, err
    assert fake_system.calls("apt-get") == [["update"], ["install", "-y", "wkhtmltopdf"]]
    assert ["apt-get", "update"] in fake_system.calls("sudo")
    assert ["install", "frappe-bench"] == fake_system.calls("uv")[-1][1:] or ["tool", "install", "frappe-bench"] in fake_system.calls("uv")


def test_init_refuses_root(state_home, capsys, monkeypatch):
    monkeypatch.setattr(host, "os_release", lambda *a: {"ID": "ubuntu"})
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    code, out, _ = run(capsys, "init", "--dry-run")
    assert code == 2 and "normal user" in out


def _serve_dir(directory: Path):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(directory), **k)
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _node_tarball(tmp_path: Path, arch: str) -> tuple[Path, str]:
    name = f"node-v24.9.9-linux-{arch}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for f in ("node", "npm", "npx"):
            data = b"#!/bin/sh\necho v24.9.9\n"
            info = tarfile.TarInfo(f"{name}/bin/{f}")
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    d = tmp_path / "dist" / "latest-v24.x"
    d.mkdir(parents=True)
    (d / f"{name}.tar.xz").write_bytes(buf.getvalue())
    return d, f"{name}.tar.xz"


def test_install_node_verifies_checksum_and_links(tmp_path, state_home):
    from frappe_host.application import setup
    from frappe_host.application.operations import OperationFailed

    d, fname = _node_tarball(tmp_path, setup._node_arch())
    good = hashlib.sha256((d / fname).read_bytes()).hexdigest()
    srv = _serve_dir(tmp_path / "dist")
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        (d / "SHASUMS256.txt").write_text(f"{'0' * 64}  {fname}\n")
        with pytest.raises(OperationFailed, match="checksum mismatch"):
            setup.install_node(24, base_url=base)
        assert not (Path.home() / ".local" / "bin" / "node").exists()
        (d / "SHASUMS256.txt").write_text(f"{good}  {fname}\n")
        target = setup.install_node(24, base_url=base)
        link = Path.home() / ".local" / "bin" / "node"
        assert link.is_symlink() and Path(os.readlink(link)) == target / "bin" / "node"
        # A foreign ~/.local/bin/npm is never overwritten.
        (Path.home() / ".local" / "bin" / "npm").unlink()
        (Path.home() / ".local" / "bin" / "npm").write_text("mine")
        with pytest.raises(OperationFailed, match="not created by frappe-host"):
            setup.link_local_bins(target / "bin", ["npm"])
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- bench create / update

def test_bench_create_and_register(state_home, fake_bench_on_path, fake_system, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_UV_PYTHON", "/opt/py/bin/python3.14")
    monkeypatch.setattr("frappe_host.application.setup.node_major", lambda: 24)
    (tmp_path / "bin" / "yarn").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "bin" / "yarn").chmod(0o755)
    parent = tmp_path / "work"
    parent.mkdir()
    code, out, err = run(capsys, "bench", "create", "my-bench", "--dir", str(parent), "--yes")
    assert code == 0, out + err
    call = bench_calls(fake_bench_on_path)[0]["argv"]
    assert call == ["init", "my-bench", "--frappe-branch", "version-16", "--python", "/opt/py/bin/python3.14"]
    code, out, _ = run(capsys, "bench", "list")
    assert str(parent / "my-bench") in out


@pytest.mark.parametrize("name", ["../x", "My Bench", "a;b", ""])
def test_bench_create_rejects_bad_names(state_home, capsys, name, tmp_path):
    assert run(capsys, "bench", "create", name, "--dir", str(tmp_path), "--dry-run")[0] == 2


def test_bench_update_backs_up_every_site_first(registered, fake_bench_on_path, capsys):
    add_site(registered, "b.local")
    code, out, err = run(capsys, "bench", "update", "--yes")
    assert code == 0, err
    argvs = [c["argv"] for c in bench_calls(fake_bench_on_path)]
    assert argvs[-1] == ["update"]
    assert sum(1 for a in argvs if "backup" in a) == 2


# ---------------------------------------------------------------- site lifecycle

def test_site_delete_requires_typed_confirmation(registered, fake_bench_on_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run(capsys, "site", "delete", "a.local")[0] == 4
    assert run(capsys, "site", "delete", "a.local", "--confirm", "b.local")[0] == 4
    assert bench_calls(fake_bench_on_path) == []
    code, out, err = run(capsys, "site", "delete", "a.local", "--confirm", "a.local")
    assert code == 0, err
    argvs = [c["argv"] for c in bench_calls(fake_bench_on_path)]
    assert argvs[0][:3] == ["--site", "a.local", "backup"]
    assert argvs[-1] == ["drop-site", "a.local", "--no-backup"]
    assert (registered / "archived_sites" / "a.local").is_dir()
    assert run(capsys, "site", "status", "a.local")[0] == 5


def test_site_delete_without_our_backup_keeps_frappes(registered, fake_bench_on_path, capsys):
    assert run(capsys, "site", "delete", "a.local", "--no-backup", "--confirm", "a.local")[0] == 0
    assert bench_calls(fake_bench_on_path)[-1]["argv"] == ["drop-site", "a.local"]


def test_migrate_stops_when_backup_fails(registered, fake_bench_on_path, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BENCH_FAIL", "backup")
    code, _, err = run(capsys, "site", "migrate", "a.local", "--yes")
    assert code == 1
    assert not any("migrate" in c["argv"] for c in bench_calls(fake_bench_on_path))


def test_maintenance_mode(registered, fake_bench_on_path, capsys):
    assert run(capsys, "site", "maintenance", "a.local", "on", "--yes")[0] == 0
    assert json.loads((registered / "sites/a.local/site_config.json").read_text())["maintenance_mode"] == 1


def test_site_hosts_is_idempotent(state_home, fake_system, capsys):
    hosts = fake_system.sysroot / "etc" / "hosts"
    assert run(capsys, "site", "hosts", "school.local", "--yes")[0] == 0
    assert run(capsys, "site", "hosts", "school.local", "--yes")[0] == 0
    assert hosts.read_text().count("school.local") == 1
    assert "localhost" in hosts.read_text()


# ---------------------------------------------------------------- apps

@pytest.mark.parametrize("source", ["http://x.com/a.git", "erp next", "$(id)", "https://x.com/../../etc", "--upload-pack=x"])
def test_app_get_rejects_unsafe_sources(registered, fake_bench_on_path, capsys, source):
    assert run(capsys, "app", "get", "--yes", "--", source)[0] == 2
    assert bench_calls(fake_bench_on_path) == []


def test_app_get_and_install(registered, fake_bench_on_path, capsys):
    code, out, err = run(capsys, "app", "get", "https://github.com/frappe/hrms", "--branch", "version-16", "--yes")
    assert code == 0, err
    assert bench_calls(fake_bench_on_path)[0]["argv"] == ["get-app", "--branch", "version-16", "https://github.com/frappe/hrms"]
    assert "only install apps from sources you trust" in out
    code, _, err = run(capsys, "app", "install", "a.local", "hrms", "--yes")
    assert code == 0, err
    assert bench_calls(fake_bench_on_path)[-1]["argv"] == ["--site", "a.local", "install-app", "hrms"]
    assert run(capsys, "app", "install", "a.local", "notthere", "--yes")[0] == 2


# ---------------------------------------------------------------- production

def test_production_enable_links_validates_and_disables(registered, fake_bench_on_path, fake_system, quiet_network, capsys):
    code, out, err = run(capsys, "production", "enable", "--yes")
    assert code == 0, out + err
    links = production.links_for(registered)
    assert links.nginx.is_symlink() and Path(os.readlink(links.nginx)) == registered / "config" / "nginx.conf"
    assert links.supervisor.is_symlink()
    assert json.loads((registered / "sites/common_site_config.json").read_text())["dns_multitenant"] is True
    sudo = fake_system.calls("sudo")
    assert ["nginx", "-t"] in sudo and ["supervisorctl", "update"] in sudo and ["systemctl", "reload", "nginx"] in sudo
    assert sudo.index(["nginx", "-t"]) < sudo.index(["systemctl", "reload", "nginx"])
    assert [c["argv"] for c in bench_calls(fake_bench_on_path)][:3] == [
        ["config", "dns_multitenant", "on"], ["setup", "supervisor", "--yes", "--skip-supervisord"], ["setup", "nginx", "--yes"]]
    # Idempotent second run keeps the links.
    assert run(capsys, "production", "enable", "--yes")[0] == 0
    assert run(capsys, "production", "disable", "--yes")[0] == 0
    assert not links.nginx.exists() and not links.supervisor.exists()


def test_production_rolls_back_links_when_nginx_test_fails(registered, fake_bench_on_path, fake_system, quiet_network, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "nginx")
    code, _, err = run(capsys, "production", "enable", "--yes")
    assert code == 1 and "rolled back" in err
    links = production.links_for(registered)
    assert not links.nginx.exists() and not links.supervisor.exists()
    assert ["systemctl", "reload", "nginx"] not in fake_system.calls("sudo")


def test_production_refuses_foreign_config(registered, fake_bench_on_path, fake_system, quiet_network, capsys):
    other = production.links_for(registered).nginx
    other.write_text("server { listen 80; }  # someone else's file")
    code, out, _ = run(capsys, "production", "enable", "--yes")
    assert code == 2 and "does not belong to this Bench" in out
    assert other.read_text().startswith("server")


def test_production_blocks_while_dev_server_runs(registered, fake_bench_on_path, fake_system, capsys, monkeypatch):
    monkeypatch.setattr(host, "tcp_listeners", lambda *a, **k: [host.Listener("0.0.0.0", 8000)])
    code, out, _ = run(capsys, "production", "enable", "--yes")
    assert code == 2 and "bench start" in out


def test_production_never_touches_default_nginx_site(registered, fake_bench_on_path, fake_system, quiet_network, capsys):
    default = fake_system.sysroot / "etc/nginx/sites-enabled/default"
    default.parent.mkdir(parents=True)
    default.write_text("server { listen 80 default_server; }")
    assert run(capsys, "production", "enable", "--yes")[0] == 0
    assert default.exists()


# ---------------------------------------------------------------- domains / publish

@pytest.fixture
def in_production(registered, fake_bench_on_path, fake_system, quiet_network, capsys, monkeypatch):
    assert run(capsys, "production", "enable", "--yes")[0] == 0
    monkeypatch.setenv("FAKE_DPKG_INSTALLED", "python3-certbot-nginx")
    return registered


def test_publish_requires_domain_confirmation(in_production, fake_system, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, _, err = run(capsys, "publish", "a.local", "--domain", "erp.example.com", "--email", "ops@example.com")
    assert code == 4
    assert fake_system.calls("certbot") == []


def test_publish_with_tls(in_production, fake_system, fake_bench_on_path, capsys):
    code, out, err = run(capsys, "publish", "a.local", "--domain", "erp.example.com", "--email", "ops@example.com",
                         "--confirm", "erp.example.com")
    assert code == 0, out + err
    cert = fake_system.calls("certbot")[0]
    assert cert[:4] == ["certonly", "--nginx", "-d", "erp.example.com"] and "ops@example.com" in cert
    cfg = json.loads((in_production / "sites/a.local/site_config.json").read_text())
    dom = cfg["domains"][0]
    assert dom["domain"] == "erp.example.com" and dom["ssl_certificate"].endswith("erp.example.com/fullchain.pem")
    assert cfg["host_name"] == "https://erp.example.com"
    assert "NOT verified" in out


def test_publish_rolls_back_when_certificate_fails(in_production, fake_system, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "certbot")
    code, _, err = run(capsys, "publish", "a.local", "--domain", "erp.example.com", "--confirm", "erp.example.com")
    assert code == 1 and "rolled back" in err
    cfg = json.loads((in_production / "sites/a.local/site_config.json").read_text())
    assert cfg.get("domains") == []
    assert "erp.example.com" not in (in_production / "config/nginx.conf").read_text()


@pytest.mark.parametrize("problem,needle", [
    ("dev", "developer_mode is on"),
    ("dns", "does not resolve"),
    ("exposed", "internal services"),
])
def test_publish_blockers(in_production, capsys, monkeypatch, problem, needle):
    domain = "erp.example.com"
    if problem == "dev":
        add_site(in_production, "a.local", developer_mode=1)
    elif problem == "dns":
        domain = "erp.unresolvable.org"
    else:
        monkeypatch.setattr(host, "tcp_listeners", lambda *a, **k: [host.Listener("0.0.0.0", 3306)])
    code, out, _ = run(capsys, "publish", "a.local", "--domain", domain, "--confirm", domain)
    assert code == 2 and needle in out


def test_publish_needs_production(registered, fake_system, quiet_network, capsys):
    code, out, _ = run(capsys, "publish", "a.local", "--domain", "erp.example.com", "--dry-run")
    assert code == 2 and "production mode is not enabled" in out


def test_unpublish(in_production, fake_system, capsys):
    assert run(capsys, "publish", "a.local", "--domain", "erp.example.com", "--no-tls", "--confirm", "erp.example.com")[0] == 0
    assert run(capsys, "unpublish", "a.local", "--domain", "erp.example.com", "--yes")[0] == 0
    assert json.loads((in_production / "sites/a.local/site_config.json").read_text())["domains"] == []


def test_domain_check_detects_cgnat_and_never_claims_public(state_home, capsys, monkeypatch):
    from frappe_host.adapters import network

    monkeypatch.setattr(network, "resolve", lambda n: ["192.168.1.20"])
    monkeypatch.setattr(host, "interfaces", lambda: [host.Interface("wan0", "100.70.1.2", 10)])
    monkeypatch.setattr(host, "tcp_listeners", lambda *a, **k: [])
    monkeypatch.setattr(network, "public_ip", lambda *a, **k: pytest.fail("must be opt-in"))
    code, out, _ = run(capsys, "domain", "check", "erp.example.com")
    assert code == 0
    assert "carrier-grade NAT" in out and "LAN-only" in out and "NOT verified" in out


def test_domain_check_public_ip_mismatch(state_home, capsys, monkeypatch):
    from frappe_host.adapters import network

    monkeypatch.setattr(network, "resolve", lambda n: ["203.0.113.10"])
    monkeypatch.setattr(network, "public_ip", lambda *a, **k: "198.51.100.7")
    monkeypatch.setattr(host, "tcp_listeners", lambda *a, **k: [])
    code, out, _ = run(capsys, "domain", "check", "erp.example.com", "--detect-public-ip")
    assert "public IP is 198.51.100.7" in out


@pytest.mark.parametrize("bad", ["exa mple.com", "localhost", "-x.com", "a..com", "x.com;id"])
def test_domain_validation(state_home, capsys, bad):
    assert run(capsys, "domain", "check", "--", bad)[0] == 2


def test_probe_reports_lan_only_for_private_answers(capsys):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"message": "pong"}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, out, _ = run(capsys, "probe", f"http://localhost:{srv.server_address[1]}")
    finally:
        srv.shutdown()
    assert "pong" in out and "only proves LAN access" in out


# ---------------------------------------------------------------- backups: prune / import / schedule

def _backup(capsys):
    code, out, err = run(capsys, "backup", "create", "a.local", "--yes", "--json")
    assert code == 0, err
    return json.loads(out)


def test_prune_keeps_newest_and_never_touches_unmanaged(registered, fake_bench_on_path, capsys, monkeypatch):
    recs = [_backup(capsys) for _ in range(3)]
    unmanaged = registered / "sites/a.local/private/backups/20200101_000000-a_local-database.sql.gz"
    unmanaged.write_bytes(b"frappe scheduled backup")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run(capsys, "backup", "prune", "a.local", "--keep", "1")[0] == 4
    code, out, err = run(capsys, "backup", "prune", "a.local", "--keep", "1", "--confirm", "a.local")
    assert code == 0, err
    left = json.loads(run(capsys, "backup", "list", "--json")[1])["backups"]
    assert [r["id"] for r in left] == [recs[-1]["id"]]
    for r in recs[:-1]:
        assert not any(Path(f["path"]).exists() for f in r["files"])
    assert unmanaged.exists()


def test_import_unmanaged_sets(registered, capsys):
    import gzip

    d = registered / "sites/a.local/private/backups"
    with gzip.open(d / "20261001_020000-a_local-database.sql.gz", "wb") as g:
        g.write(b"CREATE TABLE x (y int);")
    (d / "20261001_020000-a_local-site_config_backup.json").write_text("{}")
    (d / "20261002_020000-a_local-files.tar").write_bytes(b"orphan without database")
    code, out, err = run(capsys, "backup", "import", "a.local", "--yes")
    assert code == 0, err
    recs = json.loads(run(capsys, "backup", "list", "--json")[1])["backups"]
    assert len(recs) == 1 and recs[0]["origin"] == "imported" and recs[0]["created_at"].startswith("2026-10-01T02:00")
    assert run(capsys, "backup", "verify", recs[0]["id"])[0] == 0


def test_schedule_preserves_other_crontab_lines(registered, fake_system, capsys, tmp_path):
    fake_system.crontab.write_text("MAILTO=me\n5 * * * * /usr/bin/true # mine\n")
    dest = tmp_path / "offsite"
    code, out, err = run(capsys, "backup", "schedule", "a.local", "--at", "02:30", "--copy-to", str(dest), "--keep", "7", "--yes")
    assert code == 0, err
    assert run(capsys, "backup", "schedule", "a.local", "--at", "03:15", "--yes")[0] == 0  # replaces, not duplicates
    text = fake_system.crontab.read_text()
    assert text.startswith("MAILTO=me\n5 * * * * /usr/bin/true # mine\n")
    assert text.count("backup create a.local") == 1 and "15 3 * * *" in text
    code, out, _ = run(capsys, "backup", "schedules")
    assert "a.local" in out and "03:15" in out
    assert run(capsys, "backup", "unschedule", "a.local")[0] == 0
    assert fake_system.crontab.read_text() == "MAILTO=me\n5 * * * * /usr/bin/true # mine\n"


@pytest.mark.parametrize("at", ["25:00", "2:3", "noon", "02:30;rm"])
def test_schedule_rejects_bad_times(registered, fake_system, capsys, at):
    assert run(capsys, "backup", "schedule", "a.local", "--at", at, "--yes")[0] == 2


# ---------------------------------------------------------------- tooling

def test_diagnostics_bundle_is_private_and_redacted(registered, capsys, tmp_path):
    (registered / "logs" / "web.error.log").write_text("connect failed db_password=hunter2\n")
    out_file = tmp_path / "diag.tar.gz"
    code, _, err = run(capsys, "diagnostics", "--output", str(out_file))
    assert code == 0, err
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    with tarfile.open(out_file) as tf:
        content = b"".join(tf.extractfile(m).read() for m in tf if m.isfile())
    assert b"hunter2" not in content and b"s3cret-db-pass" not in content
    assert b"doctor.json" not in content  # names are members, content is JSON
    assert run(capsys, "diagnostics", "--output", str(out_file))[0] == 2  # never overwrites


def test_check_flags_stale_backups(registered, fake_bench_on_path, capsys, store, monkeypatch):
    from frappe_host.application import doctor

    monkeypatch.setattr(doctor, "check_resources", lambda *a, **k: [])
    monkeypatch.setattr(host, "tcp_listeners", lambda *a, **k: [])
    rec = _backup(capsys)
    assert run(capsys, "check")[0] == 0
    with store.transaction() as db:
        db.execute("UPDATE backups SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (rec["id"],))
    code, out, _ = run(capsys, "check")
    assert code == 1 and "backup a.local" in out


def test_dashboard_is_token_protected_and_rejects_foreign_hosts(registered):
    import urllib.error
    import urllib.request

    from frappe_host.cli import dashboard

    srv, token = dashboard.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    assert srv.server_address[0] == "127.0.0.1"

    def get(path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                                  {"redirect_request": lambda *a, **k: None}))
        try:
            r = opener.open(req)
            return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, "", dict(e.headers)

    try:
        assert get("/")[0] == 403
        assert get(f"/?token={token}", {"Host": "evil.example"})[0] == 421
        code, _, headers = get(f"/?token={token}")
        assert code == 303 and "HttpOnly" in headers["Set-Cookie"]
        cookie = headers["Set-Cookie"].split(";")[0]
        code, body, headers = get("/", {"Cookie": cookie})
        assert code == 200 and "a.local" in body and "s3cret" not in body
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        code, body, _ = get("/api/summary", {"Cookie": cookie})
        assert json.loads(body)["sites"][0]["name"] == "a.local"
    finally:
        srv.shutdown()
        srv.server_close()
