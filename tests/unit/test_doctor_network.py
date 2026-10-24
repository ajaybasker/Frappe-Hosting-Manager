from pathlib import Path

from frappe_host.adapters import bench_fs
from frappe_host.application import doctor
from frappe_host.domain.models import Check, Status, overall
from frappe_host.platform.linux import host
from frappe_host.platform.linux.host import Listener

GIB = 1024**3


def by_name(checks, name):
    return next(c for c in checks if c.name == name)


def test_platform_supported_unsupported_and_unknown():
    assert by_name(doctor.check_platform({"ID": "ubuntu", "VERSION_ID": "24.04", "PRETTY_NAME": "U"}, "x86_64"), "os").status == Status.PASS
    assert by_name(doctor.check_platform({"ID": "ubuntu", "VERSION_ID": "18.04"}, "x86_64"), "os").status == Status.WARN
    assert by_name(doctor.check_platform({"ID": "fedora", "VERSION_ID": "40"}, "x86_64"), "os").status == Status.FAIL
    assert by_name(doctor.check_platform({"ID": "ubuntu", "VERSION_ID": "24.04"}, "riscv64"), "architecture").status == Status.WARN


def test_memory_thresholds(tmp_path):
    low = doctor.check_resources([], {"MemTotal": 2 * GIB, "MemAvailable": GIB, "SwapTotal": 0}, 1)
    assert by_name(low, "memory").status == Status.FAIL
    assert by_name(low, "swap").status == Status.WARN
    assert by_name(low, "cpu").status == Status.WARN
    ok = doctor.check_resources([], {"MemTotal": 16 * GIB, "MemAvailable": 8 * GIB, "SwapTotal": GIB}, 4)
    assert by_name(ok, "memory").status == Status.PASS
    pressure = doctor.check_resources([], {"MemTotal": 16 * GIB, "MemAvailable": GIB, "SwapTotal": GIB}, 4)
    assert by_name(pressure, "memory-pressure").status == Status.WARN


def test_disk_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "disk_usage", lambda p: host.DiskUsage(str(p), 100 * GIB, 2 * GIB))
    checks = doctor.check_resources([tmp_path], {"MemTotal": 16 * GIB, "MemAvailable": 8 * GIB, "SwapTotal": GIB}, 4)
    assert [c for c in checks if c.name.startswith("disk")][0].status == Status.FAIL


def test_branch_requirements(bench):
    info = bench_fs.inspect_bench(bench)
    checks = doctor.check_bench(info, (11, 8, 2), "v24.1.0")
    assert by_name(checks, "python").status == Status.PASS
    assert by_name(checks, "node").status == Status.PASS
    assert by_name(checks, "mariadb").status == Status.PASS
    info.python = "3.12.3"
    checks = doctor.check_bench(info, (10, 11, 0), "v20.0.0")
    assert by_name(checks, "python").status == Status.FAIL
    assert by_name(checks, "node").status == Status.FAIL
    assert by_name(checks, "mariadb").status == Status.WARN


def test_unknown_branch_is_flagged(tmp_path):
    from tests.conftest import make_bench

    info = bench_fs.inspect_bench(make_bench(tmp_path / "b", frappe_version="13.0.0"))
    assert by_name(doctor.check_bench(info), "support-matrix").status == Status.WARN


def test_exposure_classification(bench):
    info = bench_fs.inspect_bench(bench)
    listeners = [
        Listener("0.0.0.0", 6379),       # system redis exposed -> FAIL
        Listener("127.0.0.1", 3306),     # mariadb loopback -> PASS
        Listener("127.0.0.1", 13000),    # bench redis -> PASS
        Listener("::", 8000),            # web on all -> WARN
        Listener("127.0.0.1", 9000),     # socketio loopback -> PASS
    ]
    checks = doctor.check_exposure([info], listeners)
    assert by_name(checks, "port 6379").status == Status.FAIL
    assert by_name(checks, "port 3306").status == Status.PASS
    assert by_name(checks, "port 13000").status == Status.PASS
    assert by_name(checks, "port 8000").status == Status.WARN
    assert by_name(checks, "port 9000").status == Status.PASS
    public = by_name(checks, "public")
    assert public.status == Status.UNKNOWN and "not verified" in public.summary


def test_proc_net_tcp_parsing(tmp_path):
    (tmp_path / "tcp").write_text(
        "  sl  local_address rem_address   st\n"
        "   0: 0100007F:0CEA 00000000:0000 0A 00000000\n"   # 127.0.0.1:3306 LISTEN
        "   1: 00000000:1F45 00000000:0000 0A 00000000\n"   # 0.0.0.0:8005 LISTEN
        "   2: 0100007F:1F45 0100007F:9C40 01 00000000\n"   # established, ignored
    )
    (tmp_path / "tcp6").write_text(
        "  sl  local_address rem_address st\n"
        "   0: 00000000000000000000000000000000:2329 00000000000000000000000000000000:0000 0A 0\n"  # [::]:9001
        "   1: 00000000000000000000000001000000:18EB 00000000000000000000000000000000:0000 0A 0\n"  # [::1]:6379
    )
    ls = host.tcp_listeners(tmp_path)
    assert Listener("127.0.0.1", 3306) in ls
    assert Listener("0.0.0.0", 8005) in ls
    assert Listener("::", 9001) in ls
    assert Listener("::1", 6379) in ls
    assert len(ls) == 4
    assert Listener("::1", 6379).scope == "loopback"
    assert Listener("::", 9001).scope == "all-interfaces"


def test_classify_address():
    assert host.classify_address("100.72.1.5") == "cgnat"
    assert host.classify_address("192.168.1.10") == "private"
    assert host.classify_address("127.0.0.1") == "loopback"
    assert host.classify_address("8.8.8.8") == "public"


def test_overall_verdict():
    assert overall([Check("s", "a", Status.PASS, "")]) == "READY"
    assert overall([Check("s", "a", Status.PASS, ""), Check("s", "b", Status.WARN, "")]) == "NEEDS ATTENTION"
    assert overall([Check("s", "a", Status.FAIL, "")]) == "BLOCKED"


def test_frappe_ping_probe_against_local_server():
    """Local probe distinguishes a Frappe pong from any other HTTP 200."""
    import http.server
    import threading

    from frappe_host.adapters import network

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"message": "pong"}' if self.headers["Host"] == "a.local" else b"<html>other</html>"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        assert network.probe_frappe("127.0.0.1", port, "a.local").ok
        r = network.probe_frappe("127.0.0.1", port, "b.local")
        assert r.status == 200 and not r.ok
    finally:
        srv.shutdown()
    assert not network.probe_frappe("127.0.0.1", port, "a.local", timeout=1).ok
