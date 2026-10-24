"""Input validation. Everything that later reaches a subprocess argv or a
filesystem path is checked here first against explicit allowlists."""

from __future__ import annotations

import os
import re
from pathlib import Path


class ValidationError(ValueError):
    pass


# Frappe site names are hostnames (bench new-site rejects most other forms).
# Labels: letters, digits, hyphens; no leading/trailing hyphen; dots separate labels.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_SITE_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
_BACKUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,63}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_APP_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def site_name(value: str) -> str:
    v = (value or "").strip().lower()
    if not v or len(v) > 253 or not _SITE_RE.match(v):
        raise ValidationError(
            f"invalid site name {value!r}: use a hostname such as 'school.local' "
            "(letters, digits, hyphens and dots only)"
        )
    return v


def backup_id(value: str) -> str:
    v = (value or "").strip().lower()
    if not _BACKUP_ID_RE.match(v):
        raise ValidationError(f"invalid backup id {value!r}")
    return v


def branch_name(value: str) -> str:
    if not _BRANCH_RE.match(value or "") or ".." in value:
        raise ValidationError(f"invalid branch name {value!r}")
    return value


def app_name(value: str) -> str:
    if not _APP_RE.match(value or ""):
        raise ValidationError(f"invalid app name {value!r}")
    return value


def existing_directory(value: str) -> Path:
    """Resolve a user-supplied directory path (symlinks resolved) and require it to exist."""
    if not value or "\x00" in value:
        raise ValidationError("path must not be empty")
    p = Path(os.path.expanduser(value)).resolve()
    if not p.is_dir():
        raise ValidationError(f"not a directory: {p}")
    return p


def contained_path(base: Path, candidate: Path) -> Path:
    """Return candidate resolved, refusing anything that escapes base (path traversal / symlinks)."""
    base_r = base.resolve()
    cand_r = candidate.resolve()
    if cand_r != base_r and base_r not in cand_r.parents:
        raise ValidationError(f"path {candidate} escapes {base_r}")
    return cand_r
