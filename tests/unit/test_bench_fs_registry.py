import json
import shutil
from pathlib import Path

from frappe_host.adapters import bench_fs
from frappe_host.application import registry

from tests.conftest import add_site, make_bench, tree_digest


def test_is_bench(bench, tmp_path):
    assert bench_fs.is_bench(bench)
    (tmp_path / "plain").mkdir()
    assert not bench_fs.is_bench(tmp_path / "plain")


def test_inspect_bench(bench):
    info = bench_fs.inspect_bench(bench)
    assert info.frappe_version == "16.4.0"
    assert info.frappe_branch == "version-16"
    assert info.apps[0].commit == "a" * 40
    assert info.sites == ["a.local"]
    assert info.process_mode == "development"
    assert info.webserver_port == 8000
    assert info.python == "3.14.0"


def test_packed_refs_and_detached_head(tmp_path):
    b = make_bench(tmp_path / "b")
    git = b / "apps" / "frappe" / ".git"
    (git / "refs" / "heads" / "version-16").unlink()
    (git / "packed-refs").write_text("# pack-refs\n" + "b" * 40 + " refs/heads/version-16\n")
    assert bench_fs.git_head(b / "apps" / "frappe") == ("version-16", "b" * 40)
    (git / "HEAD").write_text("c" * 40 + "\n")
    assert bench_fs.git_head(b / "apps" / "frappe") == (None, "c" * 40)


def test_process_mode_supervisor(tmp_path):
    b = make_bench(tmp_path / "b")
    (b / "config" / "supervisor.conf").write_text("[program:x]\n")
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    assert bench_fs.detect_process_mode(b, conf_d) == "development"
    (conf_d / "frappe-bench.conf").symlink_to(b / "config" / "supervisor.conf")
    assert bench_fs.detect_process_mode(b, conf_d) == "supervisor"


def test_process_mode_unknown(tmp_path):
    b = make_bench(tmp_path / "b", procfile="")
    assert bench_fs.detect_process_mode(b) == "unknown"


def test_corrupt_config_does_not_crash(bench):
    (bench / "sites" / "common_site_config.json").write_text("{not json")
    (bench / "sites" / "a.local" / "site_config.json").write_text("[1,2]")
    info = bench_fs.inspect_bench(bench)
    assert info.webserver_port is None
    assert bench_fs.inspect_site(bench, "a.local").db_name is None


def test_site_flags(bench):
    add_site(bench, "m.local", maintenance_mode=1, pause_scheduler=1, developer_mode=1)
    s = bench_fs.inspect_site(bench, "m.local")
    assert s.maintenance_mode and s.scheduler_paused and s.developer_mode


def test_discover_is_read_only_and_bounded(tmp_path):
    root = tmp_path / "home"
    b1 = make_bench(root / "work" / "bench1")
    make_bench(root / ".hidden" / "bench2")
    make_bench(root / "a" / "b" / "c" / "d" / "e" / "deep")
    (root / "link").symlink_to(b1, target_is_directory=True)
    # A bench nested inside another bench must not be reported separately.
    make_bench(b1 / "nested")
    before = tree_digest(root)
    found = registry.discover(None, [root], max_depth=4)
    assert [f.path for f in found] == [str(b1.resolve())]
    assert tree_digest(root) == before


def test_register_is_idempotent_and_does_not_touch_bench(store, bench):
    before = tree_digest(bench)
    result, op_id = registry.register(store, bench.resolve())
    assert result == "registered" and op_id
    assert registry.register(store, bench.resolve()) == ("already-registered", None)
    assert len(registry.list_benches(store)) == 1
    assert tree_digest(bench) == before


def test_drift_detection(store, bench):
    registry.register(store, bench.resolve())
    entry = registry.list_benches(store)[0]
    assert not registry.drift(entry).drifted
    add_site(bench, "new.local")
    (bench / "apps" / "frappe" / "frappe" / "__init__.py").write_text('__version__ = "16.5.0"\n')
    d = registry.drift(entry)
    assert d.drifted
    assert any("new.local" in c for c in d.changes)
    assert any("16.4.0 -> 16.5.0" in c for c in d.changes)
    registry.refresh(store, entry["path"])
    assert not registry.drift(registry.list_benches(store)[0]).drifted


def test_drift_when_bench_removed(store, bench):
    registry.register(store, bench.resolve())
    shutil.rmtree(bench)
    d = registry.drift(registry.list_benches(store)[0])
    assert d.drifted and not d.exists


def test_unregister_leaves_files(store, bench):
    registry.register(store, bench.resolve())
    before = tree_digest(bench)
    assert registry.unregister(store, str(bench.resolve()))[0] == "unregistered"
    assert registry.list_benches(store) == []
    assert tree_digest(bench) == before


def test_registry_never_stores_site_secrets(store, bench):
    registry.register(store, bench.resolve())
    assert "s3cret-db-pass" not in json.dumps(store.export())
