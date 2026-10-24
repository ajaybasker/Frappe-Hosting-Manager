import io
from pathlib import Path

import pytest

from frappe_host.adapters import process
from frappe_host.domain import validation as v
from frappe_host.policies.confirm import ConfirmationRefused, require_typed_confirmation
from frappe_host.policies.redaction import REDACTED, redact_obj, redact_text


@pytest.mark.parametrize("name", ["school.local", "erp.example.com", "a", "site-1.test", "SCHOOL.Local"])
def test_valid_site_names(name):
    assert v.site_name(name) == name.lower()


@pytest.mark.parametrize("name", [
    "", "x;rm -rf /", "$(id)", "`id`", "a b", "../etc", "a/b", "-rf", "a..b", ".hidden",
    "site.", "-site.com", "site-.com", "a\x00b", "a" * 64 + ".com", "x|y", "x&&y", "名前.com",
])
def test_invalid_site_names_rejected(name):
    with pytest.raises(v.ValidationError):
        v.site_name(name)


@pytest.mark.parametrize("bid", ["../x", "bk;id", "", "bk/1", "a", "-bk-1"])
def test_invalid_backup_ids(bid):
    with pytest.raises(v.ValidationError):
        v.backup_id(bid)


def test_branch_and_app_validation():
    assert v.branch_name("version-16") == "version-16"
    for bad in ("../x", "-b", "a b", "x;y"):
        with pytest.raises(v.ValidationError):
            v.branch_name(bad)
    assert v.app_name("erpnext") == "erpnext"
    with pytest.raises(v.ValidationError):
        v.app_name("erp next")


def test_contained_path_rejects_traversal_and_symlink_escape(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "ok").write_text("x")
    assert v.contained_path(base, base / "ok") == (base / "ok").resolve()
    with pytest.raises(v.ValidationError):
        v.contained_path(base, base / ".." / "other")
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (base / "link").symlink_to(outside)
    with pytest.raises(v.ValidationError):
        v.contained_path(base, base / "link")


def test_existing_directory(tmp_path):
    assert v.existing_directory(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(v.ValidationError):
        v.existing_directory(str(tmp_path / "missing"))
    with pytest.raises(v.ValidationError):
        v.existing_directory("")


# --- redaction -------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ('"db_password": "hunter2"', "hunter2"),
    ("admin_password=hunter2 other=1", "hunter2"),
    ("--db-root-password hunter2", "hunter2"),
    ("--admin-password=hunter2", "hunter2"),
    ("redis://default:hunter2@127.0.0.1:6379", "hunter2"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("encryption_key: 'k3y=='", "k3y=="),
    ("api_secret=xyz", "xyz"),
])
def test_redact_text(text, secret):
    out = redact_text(text)
    assert secret not in out
    assert REDACTED in out


def test_redact_text_keeps_ordinary_text():
    assert redact_text("site a.local migrated in 3s") == "site a.local migrated in 3s"


def test_redact_obj_nested():
    data = {"db_name": "x", "db_password": "p", "nested": [{"api_key": "k", "n": 1}], "encryption_key": "e", "empty_password": ""}
    out = redact_obj(data)
    assert out["db_name"] == "x"
    assert out["db_password"] == REDACTED
    assert out["nested"][0]["api_key"] == REDACTED
    assert out["nested"][0]["n"] == 1
    assert out["encryption_key"] == REDACTED
    assert out["empty_password"] == ""


# --- subprocess safety -----------------------------------------------------

def test_shell_metacharacters_are_passed_literally():
    r = process.run(["/bin/echo", "a; echo pwned", "$(id)"])
    assert r.ok
    assert r.stdout.strip() == "a; echo pwned $(id)"


@pytest.mark.parametrize("argv", [[], ["echo", "a\x00b"], ["echo", 1], "echo hi"])
def test_bad_argv_rejected(argv):
    with pytest.raises(process.CommandError):
        process.run(argv)


def test_missing_executable_and_timeout():
    assert process.run(["/nonexistent/binary"]).returncode == 127
    assert process.run(["sleep", "5"], timeout=0.2).returncode == 124


def test_display_redacts():
    assert "hunter2" not in process.display(["bench", "new-site", "x", "--admin-password", "hunter2"])


# --- confirmation ----------------------------------------------------------

class _NoTTY(io.StringIO):
    def isatty(self):
        return False


def test_confirmation_flag_must_match_exactly():
    require_typed_confirmation("a.local", "restore", "a.local")
    with pytest.raises(ConfirmationRefused):
        require_typed_confirmation("a.local", "restore", "A.local")
    with pytest.raises(ConfirmationRefused):
        require_typed_confirmation("a.local", "restore", "yes")


def test_confirmation_refused_without_tty():
    with pytest.raises(ConfirmationRefused):
        require_typed_confirmation("a.local", "restore", None, stdin=_NoTTY())


def test_confirmation_prompt():
    require_typed_confirmation("a.local", "restore", None, prompt=lambda _: "a.local")
    with pytest.raises(ConfirmationRefused):
        require_typed_confirmation("a.local", "restore", None, prompt=lambda _: "y")
