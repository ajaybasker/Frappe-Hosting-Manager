"""Human and machine-readable output."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Iterable

from frappe_host.domain.models import Check, Status
from frappe_host.policies.redaction import redact_obj

_COLORS = {Status.PASS: "32", Status.WARN: "33", Status.FAIL: "31", Status.INFO: "36", Status.UNKNOWN: "35"}


def use_color(stream=sys.stdout) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


def tag(status: Status, color: bool) -> str:
    label = f"[{status.value.upper():>7}]"
    return f"\033[{_COLORS[status]}m{label}\033[0m" if color else label


def print_checks(checks: Iterable[Check], color: bool | None = None) -> None:
    color = use_color() if color is None else color
    section = None
    for c in checks:
        if c.section != section:
            section = c.section
            print(f"\n{section}")
        print(f"  {tag(c.status, color)} {c.name}: {c.summary}")
        if c.next_step and c.status in (Status.FAIL, Status.WARN, Status.UNKNOWN):
            print(f"            -> {c.next_step}")


def print_json(data: Any) -> None:
    json.dump(redact_obj(data), sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    if not rows:
        return
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*[str(x) for x in r]))


# Human-oriented messages (plans, notices). Redirected to stderr in --json mode.
info_stream = None


def info(message: str = "") -> None:
    print(message, file=info_stream or sys.stdout)


def print_plan(title: str, plan: list[str], warnings: list[str] | None = None) -> None:
    info(f"Plan: {title}")
    for i, step in enumerate(plan, 1):
        info(f"  {i}. {step}")
    for w in warnings or []:
        info(f"  WARNING: {w}")


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)
