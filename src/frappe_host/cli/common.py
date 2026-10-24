"""Shared CLI helpers: exit codes, consent prompts and plan execution."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from frappe_host.application import registry, sites, steps
from frappe_host.application.state import Store
from frappe_host.cli import output as out
from frappe_host.domain.validation import ValidationError
from frappe_host.policies.confirm import require_typed_confirmation

EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_BLOCKED, EXIT_REFUSED, EXIT_NOT_FOUND = 0, 1, 2, 3, 4, 5


class Refused(Exception):
    pass


def confirm_proceed(args: argparse.Namespace, question: str = "Proceed?") -> None:
    """Consent for non-destructive changes: --yes, or an interactive y/N."""
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise Refused("not running interactively; review the plan with --dry-run and re-run with --yes")
    if input(f"{question} [y/N] ").strip().lower() not in ("y", "yes"):
        raise Refused("cancelled; nothing was changed")


def resolve_bench(store: Store, value: str | None) -> Path:
    """A registered Bench: the given path, or the only registered one."""
    benches = sites.registered_bench_paths(store)
    if value:
        p = Path(os.path.expanduser(value)).resolve()
        if p not in benches:
            raise ValidationError(f"{p} is not registered; run: frappe-host bench register {p}")
        return p
    if len(benches) == 1:
        return benches[0]
    raise ValidationError("pass --bench PATH" if benches else "no Benches registered; run `frappe-host bench discover`")


def run_plan(args: argparse.Namespace, store: Store, plan: steps.Plan, kind: str, target: str | None,
             confirm_name: str | None = None, confirm_what: str = "") -> int:
    """Print, gate (blockers / dry-run / consent) and execute a Plan."""
    steps.print_plan(plan, out.info)
    if plan.blockers:
        out.info("Nothing was changed. Resolve the blockers above and run again.")
        return EXIT_INVALID
    runnable = [s for s in plan.steps if not s.manual]
    manual = [s for s in plan.steps if s.manual]
    if not runnable:
        out.info("Nothing to change." + (" Complete the MANUAL step(s) above, then run this command again." if manual else ""))
        return EXIT_OK
    if getattr(args, "dry_run", False):
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    if confirm_name is not None:
        require_typed_confirmation(confirm_name, confirm_what or kind, getattr(args, "confirm", None))
    else:
        confirm_proceed(args)
    op_id = steps.execute(store, kind, target, plan, on_progress=lambda m: out.info(f"==> {m}"))
    out.info(f"Done (operation {op_id}).")
    if manual:
        out.info("Remaining MANUAL step(s):")
        for s in manual:
            out.info(f"  - {s.title}")
    return EXIT_OK
