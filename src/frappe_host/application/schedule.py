"""Scheduled backups through the user's own crontab (no root needed).

frappe-host owns only the lines between its marker comments; every other
crontab line is preserved byte-for-byte.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from frappe_host.adapters import process
from frappe_host.application.operations import OperationFailed, operation
from frappe_host.application.state import Store, state_home
from frappe_host.domain.validation import ValidationError

BEGIN = "# >>> frappe-host managed backups (do not edit inside this block) >>>"
END = "# <<< frappe-host managed backups <<<"
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass
class Entry:
    site: str
    bench: str
    at: str
    copy_to: str | None
    keep: int | None
    line: str


def read_crontab() -> str:
    if not process.which("crontab"):
        raise OperationFailed("crontab is not installed; run `frappe-host init` (installs cron)")
    r = process.run(["crontab", "-l"], timeout=15)
    if r.returncode != 0:
        if "no crontab" in (r.stderr + r.stdout).lower():
            return ""
        raise OperationFailed(f"could not read crontab: {r.stderr.strip()}")
    return r.stdout


def split_block(text: str) -> tuple[list[str], list[str], list[str]]:
    lines = text.splitlines()
    if BEGIN in lines and END in lines:
        b, e = lines.index(BEGIN), lines.index(END)
        return lines[:b], lines[b + 1:e], lines[e + 1:]
    return lines, [], []


def parse_entries(block: list[str]) -> list[Entry]:
    out = []
    for i, line in enumerate(block):
        m = re.match(r"^# site=(\S+) bench=(\S+) at=(\S+) copy_to=(\S+) keep=(\S+)$", line)
        if m and i + 1 < len(block):
            site, bench, at, copy_to, keep = m.groups()
            out.append(Entry(site, shlex.split(bench)[0], at, None if copy_to == "-" else shlex.split(copy_to)[0],
                             None if keep == "-" else int(keep), block[i + 1]))
    return out


def render_entry(site: str, bench: Path, at: str, copy_to: Path | None, keep: int | None) -> list[str]:
    m = _TIME_RE.match(at)
    if not m:
        raise ValidationError("--at must be HH:MM (24h)")
    hour, minute = int(m.group(1)), int(m.group(2))
    me = [sys.executable, "-m", "frappe_host"]
    log = state_home() / "scheduled-backups.log"
    create = me + ["backup", "create", site, "--bench", str(bench), "--yes"] + (["--copy-to", str(copy_to)] if copy_to else [])
    cmd = " ".join(shlex.quote(a) for a in create)
    if keep:
        prune = me + ["backup", "prune", site, "--bench", str(bench), "--keep", str(keep), "--confirm", site]
        cmd += " && " + " ".join(shlex.quote(a) for a in prune)
    path = shlex.quote(process.search_path())
    header = (f"# site={site} bench={shlex.quote(str(bench))} at={at} "
              f"copy_to={shlex.quote(str(copy_to)) if copy_to else '-'} keep={keep or '-'}")
    return [header, f"{minute} {hour} * * * PATH={path} {cmd} >> {shlex.quote(str(log))} 2>&1"]


def list_entries() -> list[Entry]:
    _, block, _ = split_block(read_crontab())
    return parse_entries(block)


def _write(store: Store, kind: str, target: str, new_block: list[str], plan: list[str]) -> str:
    before, _, after = split_block(read_crontab())
    lines = list(before)
    if new_block:
        lines += [BEGIN, *new_block, END]
    lines += after
    text = "\n".join(lines).strip("\n") + "\n" if any(l.strip() for l in lines) else ""
    with operation(store, kind, target, plan) as op:
        r = process.run(["crontab", "-"], input=text, timeout=15)
        if not r.ok:
            raise OperationFailed(f"crontab rejected the new table: {r.stderr.strip()}")
        op.log("crontab updated")
    return op.id


def _without(block: list[str], site: str, bench: Path) -> list[str]:
    out, skip = [], False
    for line in block:
        if skip:
            skip = False
            continue
        if line.startswith(f"# site={site} bench={shlex.quote(str(bench))} "):
            skip = True
            continue
        out.append(line)
    return out


def set_schedule(store: Store, site: str, bench: Path, at: str, copy_to: Path | None, keep: int | None) -> tuple[list[str], str]:
    _, block, _ = split_block(read_crontab())
    entry = render_entry(site, bench, at, copy_to, keep)
    new = _without(block, site, bench) + entry
    op = _write(store, "backup.schedule", site, new, [f"crontab: {entry[1]}"])
    return entry, op


def remove_schedule(store: Store, site: str, bench: Path) -> str | None:
    _, block, _ = split_block(read_crontab())
    new = _without(block, site, bench)
    if new == block:
        return None
    return _write(store, "backup.unschedule", site, new, [f"remove the scheduled backup of {site}"])
