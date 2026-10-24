"""Core entities shared across the application layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"
    UNKNOWN = "unknown"


# Order used to compute an overall verdict from many checks.
_SEVERITY = {Status.PASS: 0, Status.INFO: 0, Status.UNKNOWN: 1, Status.WARN: 2, Status.FAIL: 3}


@dataclass
class Check:
    """One diagnostic finding with the evidence that produced it."""

    section: str
    name: str
    status: Status
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    next_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def overall(checks: list[Check]) -> str:
    worst = max((_SEVERITY[c.status] for c in checks), default=0)
    if worst >= 3:
        return "BLOCKED"
    if worst >= 1:
        return "NEEDS ATTENTION"
    return "READY"


@dataclass
class AppInfo:
    name: str
    version: str | None
    branch: str | None
    commit: str | None


@dataclass
class SiteInfo:
    name: str
    bench_path: str
    db_type: str | None
    db_name: str | None
    host_name: str | None
    maintenance_mode: bool
    scheduler_paused: bool
    developer_mode: bool
    installed_apps_known: bool = False


@dataclass
class BenchInfo:
    path: str
    frappe_version: str | None
    frappe_branch: str | None
    apps: list[AppInfo]
    sites: list[str]
    process_mode: str
    webserver_port: int | None
    python: str | None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackupRecord:
    id: str
    site: str
    bench_path: str
    created_at: str
    frappe_version: str | None
    files: list[dict[str, Any]]
    total_size: int
    verified_at: str | None = None
    verify_result: str | None = None
    operation_id: str | None = None
