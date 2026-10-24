"""End-to-end CLI workflows against disposable fixture Benches and a fake `bench`."""

import gzip
import json
import os
import stat
import tarfile
import io
from pathlib import Path

import pytest

from frappe_host.cli.main import main
from tests.conftest import add_site, bench_calls, make_bench, tree_digest


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys, *argv):
    code, out, err = run(capsys, *argv, "--json")
    return code, json.loads(out) if out.strip() else None


@pytest.fixture
def registered(state_home, bench, capsys):
    code, _, _ = run(capsys, "bench", "register", str(bench), "--yes")
    assert code == 0
    return bench.resolve()


def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert "frappe-host" in capsys.readouterr().out
    assert main([]) == 2


def test_register_requires_consent_without_tty(state_home, bench, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, out, err = run(capsys, "bench", "register", str(bench))
    assert code == 4
    assert not (state_home / "state.db").exists() or "registered" not in out


def test_register_dry_run_changes_nothing(state_home, bench, capsys):
    before = tree_digest(bench)
    code, out, _ = run(capsys, "bench", "register", str(bench), "--dry-run")
    assert code == 0 and "Dry run" in out
    assert not state_home.exists()
    assert tree_digest(bench) == before


def test_register_rejects_non_bench(state_home, tmp_path, capsys):
    (tmp_path / "x").mkdir()
    code, _, err = run(capsys, "bench", "register", str(tmp_path / "x"), "--yes")
    assert code == 2 and "does not look like a Frappe Bench" in err


def test_global_and_local_json_flag(registered, capsys):
    code, data = run_json(capsys, "bench", "list")
    assert code == 0 and data[0]["path"] == str(registered)
    code = main(["--json", "bench", "list"])
    assert json.loads(capsys.readouterr().out)[0]["path"] == str(registered)


def test_site_list_and_json_redaction(registered, capsys):
    code, out, _ = run(capsys, "site", "list")
    assert code == 0 and "a.local" in out
    code, out, _ = run(capsys, "--json", "state", "export")
    assert "s3cret-db-pass" not in out


def test_site_status_offline_bench(registered, capsys):
    code, data = run_json(capsys, "site", "status", "a.local")
    assert code == 0
    names = {c["name"]: c for c in data["checks"]}
    assert names["http-local"]["status"] == "fail"  # nothing listening on 8000 in tests (or wrong service)
    assert names["public"]["status"] == "unknown"
    assert names["backup"]["status"] == "warn"


def test_unknown_and_ambiguous_site(registered, tmp_path, capsys):
    assert run(capsys, "site", "status", "nope.local")[0] == 5
    other = make_bench(tmp_path / "other", sites=("a.local",))
    assert run(capsys, "bench", "register", str(other), "--yes")[0] == 0
    code, _, err = run(capsys, "site", "status", "a.local")
    assert code == 5 and "--bench" in err
    assert run(capsys, "site", "status", "a.local", "--bench", str(other), "--no-probe")[0] == 0


def test_logs_are_redacted_and_confined(registered, capsys):
    log = registered / "sites" / "a.local" / "logs" / "frappe.log"
    log.write_text("line1\nconnect db_password=hunter2 ok\nlast\n")
    code, out, _ = run(capsys, "logs", "a.local", "--file", "frappe.log", "--lines", "2")
    assert code == 0
    assert "hunter2" not in out and "last" in out and "line1" not in out
    assert run(capsys, "logs", "a.local", "--file", "../../../etc/passwd")[0] == 2
    assert run(capsys, "logs", "a.local", "--file", "missing.log")[0] == 2
    code, out, _ = run(capsys, "logs", "a.local")
    assert "frappe.log" in out


# --- site create -----------------------------------------------------------

def test_site_create_dry_run_and_real(registered, fake_bench_on_path, capsys):
    code, out, _ = run(capsys, "site", "create", "new.local", "--dry-run")
    assert code == 0 and "bench will prompt" in out
    assert bench_calls(fake_bench_on_path) == []
    code, out, err = run(capsys, "site", "create", "new.local", "--yes")
    assert code == 0, err
    call = bench_calls(fake_bench_on_path)[0]
    assert call["argv"][:2] == ["new-site", "new.local"]
    assert not any("password" in a for a in call["argv"])  # secrets never in argv
    assert call["cwd"] == str(registered)
    code, data = run_json(capsys, "bench", "list")
    assert data[0]["drift"]["changes"] == []  # snapshot refreshed after create


@pytest.mark.parametrize("args,expect", [
    (["site", "create", "a.local", "--yes"], "already exists"),
    (["site", "create", "x.local", "--install-app", "notinstalled", "--yes"], "not in this Bench"),
    (["site", "create", "bad;name", "--yes"], "invalid site name"),
])
def test_site_create_preflight_failures(registered, fake_bench_on_path, capsys, args, expect):
    code, _, err = run(capsys, *args)
    assert code == 2 and expect in err
    assert bench_calls(fake_bench_on_path) == []


def test_site_create_bench_failure_is_recorded(registered, fake_bench_on_path, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BENCH_FAIL", "new-site")
    code, _, err = run(capsys, "site", "create", "new.local", "--yes")
    assert code == 1 and "Operation log" in err
    code, data = run_json(capsys, "history")
    assert data[0]["status"] == "failed" and data[0]["kind"] == "site.create"


# --- backups ---------------------------------------------------------------

def _backup(capsys, *extra):
    code, data = run_json(capsys, "backup", "create", "a.local", "--yes", *extra)
    assert code == 0
    return data


def test_backup_create_records_checksums_and_permissions(registered, fake_bench_on_path, capsys):
    rec = _backup(capsys)
    kinds = {f["kind"] for f in rec["files"]}
    assert {"database", "public-files", "private-files", "site-config"} <= kinds
    for f in rec["files"]:
        assert len(f["sha256"]) == 64
        assert stat.S_IMODE(os.stat(f["path"]).st_mode) == 0o600
    assert bench_calls(fake_bench_on_path)[0]["argv"] == ["--site", "a.local", "backup", "--with-files"]
    code, out, _ = run(capsys, "backup", "list", "a.local")
    assert rec["id"] in out


def test_backup_shorthand(registered, fake_bench_on_path, capsys):
    code, out, err = run(capsys, "backup", "a.local", "--yes")
    assert code == 0, err
    assert "Backup bk-" in out
    assert "same disk" in out  # same-disk warning shown in the plan


def test_backup_copy_to_separate_dir(registered, fake_bench_on_path, capsys, tmp_path):
    dest = tmp_path / "offdisk"
    rec = _backup(capsys, "--copy-to", str(dest))
    for f in rec["files"]:
        assert Path(f["copy"]).read_bytes() == Path(f["path"]).read_bytes()
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o700
    # Original lost -> restore planning falls back to the verified copy.
    db = next(f for f in rec["files"] if f["kind"] == "database")
    Path(db["path"]).unlink()
    code, out, _ = run(capsys, "restore", "a.local", rec["id"], "--dry-run")
    assert code == 0 and str(dest) in out


def test_backup_verify_detects_tampering(registered, fake_bench_on_path, capsys):
    rec = _backup(capsys)
    assert run(capsys, "backup", "verify", rec["id"])[0] == 0
    db = next(f for f in rec["files"] if f["kind"] == "database")
    with open(db["path"], "ab") as fh:
        fh.write(b"garbage")
    code, out, _ = run(capsys, "backup", "verify", rec["id"])
    assert code == 1 and "VERIFICATION FAILED" in out


def test_verify_rejects_unsafe_tar_and_bad_gzip(tmp_path):
    from frappe_host.application import backups

    bad_tar = tmp_path / "x-files.tar"
    with tarfile.open(bad_tar, "w") as tf:
        info = tarfile.TarInfo("../../etc/evil")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    rec = {"size": bad_tar.stat().st_size, "sha256": backups.sha256(bad_tar), "kind": "public-files"}
    assert "unsafe member" in backups.verify_file(rec, bad_tar)

    link_tar = tmp_path / "y-private-files.tar"
    with tarfile.open(link_tar, "w") as tf:
        info = tarfile.TarInfo("site/private/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/shadow"
        tf.addfile(info)
    rec = {"size": link_tar.stat().st_size, "sha256": backups.sha256(link_tar), "kind": "private-files"}
    assert "unsafe link" in backups.verify_file(rec, link_tar)

    gz = tmp_path / "z-database.sql.gz"
    gz.write_bytes(gzip.compress(b"CREATE TABLE x;")[:-6])  # truncated
    rec = {"size": gz.stat().st_size, "sha256": backups.sha256(gz), "kind": "database"}
    assert "corrupt" in backups.verify_file(rec, gz)


def test_backup_failure_is_recorded(registered, fake_bench_on_path, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BENCH_FAIL", "backup")
    code, _, _ = run(capsys, "backup", "create", "a.local", "--yes")
    assert code == 1
    assert run_json(capsys, "backup", "list")[1]["backups"] == []
    assert run_json(capsys, "history")[1][0]["status"] == "failed"


# --- restore ---------------------------------------------------------------

def test_restore_refuses_without_matching_confirmation(registered, fake_bench_on_path, capsys, monkeypatch):
    rec = _backup(capsys)
    calls_before = len(bench_calls(fake_bench_on_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run(capsys, "restore", "a.local", rec["id"])[0] == 4
    assert run(capsys, "restore", "a.local", rec["id"], "--confirm", "other.local")[0] == 4
    assert len(bench_calls(fake_bench_on_path)) == calls_before
    assert not (registered / "sites" / "a.local" / "restored.json").exists()


def test_restore_happy_path_with_pre_restore_backup(registered, fake_bench_on_path, capsys):
    rec = _backup(capsys)
    code, out, err = run(capsys, "restore", "a.local", rec["id"], "--confirm", "a.local")
    assert code == 0, err
    calls = [c["argv"] for c in bench_calls(fake_bench_on_path)]
    assert calls[1][:3] == ["--site", "a.local", "backup"]  # pre-restore backup first
    assert calls[2][:3] == ["--site", "a.local", "restore"]
    assert "--with-public-files" in calls[2] and "--with-private-files" in calls[2]
    assert "Pre-restore backup: bk-" in out
    assert len(run_json(capsys, "backup", "list")[1]["backups"]) == 2


def test_restore_refuses_tampered_backup(registered, fake_bench_on_path, capsys):
    rec = _backup(capsys)
    tar = next(f for f in rec["files"] if f["kind"] == "public-files")
    Path(tar["path"]).write_bytes(b"tampered")
    code, _, err = run(capsys, "restore", "a.local", rec["id"], "--confirm", "a.local")
    assert code == 2 and "no intact copy" in err
    assert not (registered / "sites" / "a.local" / "restored.json").exists()


def test_restore_refuses_newer_frappe_backup(registered, fake_bench_on_path, capsys, store):
    rec = _backup(capsys)
    with store.transaction() as db:
        db.execute("UPDATE backups SET frappe_version='17.0.0' WHERE id=?", (rec["id"],))
    code, _, err = run(capsys, "restore", "a.local", rec["id"], "--confirm", "a.local")
    assert code == 2 and "older Frappe" in err


def test_restore_into_other_site_warns(registered, fake_bench_on_path, capsys):
    add_site(registered, "test.local")
    rec = _backup(capsys)
    code, out, _ = run(capsys, "restore", "test.local", rec["id"], "--dry-run")
    assert code == 0 and "encryption_key" in out


def test_restore_failure_points_to_pre_restore_backup(registered, fake_bench_on_path, capsys, monkeypatch):
    rec = _backup(capsys)
    monkeypatch.setenv("FAKE_BENCH_FAIL", "restore")
    code, _, err = run(capsys, "restore", "a.local", rec["id"], "--confirm", "a.local")
    assert code == 1 and "pre-restore backup" in err


def test_status_and_history(registered, fake_bench_on_path, capsys):
    _backup(capsys)
    code, out, _ = run(capsys, "status")
    assert code == 0 and "1 with a frappe-host backup" in out and "not verified" in out
    code, data = run_json(capsys, "history")
    assert [d["kind"] for d in data] == ["backup.create", "bench.register"]


def test_unregister_keeps_files(registered, capsys):
    before = tree_digest(registered)
    assert run(capsys, "bench", "unregister", str(registered), "--yes")[0] == 0
    assert tree_digest(registered) == before
    assert run(capsys, "bench", "unregister", str(registered), "--yes")[0] == 5


def test_copy_to_inside_sites_is_refused(registered, fake_bench_on_path, capsys):
    dest = registered / "sites" / "a.local" / "public" / "files" / "bk"
    code, _, err = run(capsys, "backup", "create", "a.local", "--copy-to", str(dest), "--yes")
    assert code == 2 and "sites/" in err
    assert bench_calls(fake_bench_on_path) == []


def test_concurrent_backup_set_is_not_recorded(registered, fake_bench_on_path, capsys, monkeypatch):
    from frappe_host.adapters import process

    real = process.run_interactive

    def with_scheduler_backup(argv, cwd=None):
        rc = real(argv, cwd=cwd)
        (registered / "sites" / "a.local" / "private" / "backups" / "19990101_000000-a_local-database.sql.gz").write_bytes(b"x")
        return rc

    monkeypatch.setattr(process, "run_interactive", with_scheduler_backup)
    code, _, err = run(capsys, "backup", "create", "a.local", "--yes")
    assert code == 1 and "ambiguous" in err
    assert run_json(capsys, "backup", "list")[1]["backups"] == []
