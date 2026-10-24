"""Support matrix. Keep in sync with docs/support-matrix.md.

Requirements come from the official installation guide
(https://docs.frappe.io/framework/user/en/installation, checked 2026-10-24).
"tested" means this project has recorded test evidence on that combination;
"candidate" means officially documented by Frappe but not yet tested by us.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BranchRequirements:
    branch: str
    python_min: tuple[int, int]
    python_max: tuple[int, int] | None
    node_major_min: int
    mariadb_min: tuple[int, ...]
    redis_major_min: int
    status: str


BRANCHES: dict[str, BranchRequirements] = {
    "version-15": BranchRequirements("version-15", (3, 10), (3, 13), 18, (10, 6, 6), 6, "candidate"),
    "version-16": BranchRequirements("version-16", (3, 14), None, 24, (11, 8), 6, "candidate"),
}

# OS support: (ID, VERSION_ID) -> status
OS_SUPPORT: dict[tuple[str, str], str] = {
    ("ubuntu", "24.04"): "candidate",
    ("ubuntu", "22.04"): "candidate",
    ("debian", "12"): "candidate",
    ("debian", "13"): "candidate",
}

SUPPORTED_ARCH = {"x86_64", "aarch64"}

# Capacity thresholds (documented as heuristics, not guarantees).
MIN_RAM_GIB = 4
RECOMMENDED_RAM_GIB = 8
MIN_FREE_DISK_GIB = 5
RECOMMENDED_FREE_DISK_GIB = 20
DISK_USED_WARN_PERCENT = 90


def branch_from_version(version: str | None) -> str | None:
    """Map a Frappe __version__ like '16.18.2' to its release branch."""
    if not version:
        return None
    major = version.split(".", 1)[0]
    return f"version-{major}" if major.isdigit() else None
