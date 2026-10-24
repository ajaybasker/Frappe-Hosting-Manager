"""Plan/step execution shared by multi-step workflows (init, production, publish, ...).

A workflow is a list of Steps computed up front from the current state, shown
to the user as a plan, and executed in order inside one recorded operation.
Execution stops at the first failure; completed steps that registered an undo
action are rolled back in reverse order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from frappe_host.adapters import process
from frappe_host.application.operations import Operation, OperationFailed, operation
from frappe_host.application.state import Store


@dataclass
class Step:
    title: str
    argv: list[str] | None = None
    sudo: bool = False
    why: str | None = None             # shown for every sudo step
    cwd: Path | None = None
    input: str | None = None           # fed to stdin (e.g. `tee -a /etc/hosts`)
    func: Callable[[Operation], None] | None = None  # in-process action instead of a command
    undo: "Step | None" = None         # rollback if a LATER step fails
    manual: bool = False               # cannot be automated: printed, not executed
    allowed_rc: tuple[int, ...] = (0,)

    def command(self) -> list[str] | None:
        if self.argv is None:
            return None
        return process.sudo(self.argv) if self.sudo else list(self.argv)

    def describe(self) -> str:
        parts = [self.title]
        if self.manual:
            parts[0] = f"MANUAL: {self.title}"
        cmd = self.command()
        if cmd:
            parts.append(f"$ {process.display(cmd)}" + (f"  (cwd {self.cwd})" if self.cwd else ""))
        if self.sudo:
            parts.append(f"needs root: {self.why or 'system change'}")
        return "\n     ".join(parts)


@dataclass
class Plan:
    title: str
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)   # checks that need no action
    blockers: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [s.describe() for s in self.steps]

    @property
    def needs_sudo(self) -> bool:
        return any(s.sudo for s in self.steps if not s.manual)


def print_plan(plan: Plan, info: Callable[[str], None]) -> None:
    info(f"Plan: {plan.title}")
    for s in plan.satisfied:
        info(f"  ok  {s}")
    if not plan.steps:
        info("  (nothing to do)")
    for i, s in enumerate(plan.steps, 1):
        info(f"  {i}. {s.describe()}")
    for w in plan.warnings:
        info(f"  WARNING: {w}")
    for b in plan.blockers:
        info(f"  BLOCKER: {b}")
    if plan.needs_sudo:
        info("  Steps marked 'needs root' run through sudo, which may ask for your password.")


def _run_step(step: Step, op: Operation) -> None:
    op.log(f"STEP {step.title}")
    if step.func is not None:
        step.func(op)
        return
    cmd = step.command()
    if cmd is None:
        return
    op.log(f"RUN {process.display(cmd)}")
    rc = process.run_interactive(cmd, cwd=step.cwd, input=step.input)
    op.log(f"EXIT {rc}")
    if rc not in step.allowed_rc:
        raise OperationFailed(f"step failed ({step.title}): `{process.display(cmd)}` exited {rc}")


def execute(store: Store, kind: str, target: str | None, plan: Plan,
            on_progress: Callable[[str], None] | None = None) -> str:
    """Run every non-manual step; roll back on failure. Returns the operation id."""
    if plan.blockers:
        raise OperationFailed("plan has blockers: " + "; ".join(plan.blockers))
    with operation(store, kind, target, plan.lines()) as op:
        done: list[Step] = []
        for i, step in enumerate(plan.steps, 1):
            if step.manual:
                continue
            if on_progress:
                on_progress(f"[{i}/{len(plan.steps)}] {step.title}")
            try:
                _run_step(step, op)
            except BaseException as e:
                undo = [s.undo for s in reversed(done) if s.undo is not None]
                for u in undo:
                    try:
                        op.log(f"ROLLBACK {u.title}")
                        if on_progress:
                            on_progress(f"rollback: {u.title}")
                        _run_step(u, op)
                    except Exception as re:  # keep rolling back the rest
                        op.log(f"ROLLBACK FAILED {u.title}: {re}")
                if undo and isinstance(e, OperationFailed):
                    e.args = (f"{e.args[0]}; rolled back {len(undo)} earlier change(s)",)
                raise
            done.append(step)
    return op.id
