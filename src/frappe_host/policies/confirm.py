"""Destructive-operation confirmation policy.

A destructive operation proceeds only when the user types the exact resource
name. `--yes` is deliberately NOT accepted for destructive operations; non-
interactive automation must pass `--confirm <exact-name>` instead, so a script
can never destroy a resource it did not name.
"""

from __future__ import annotations

import sys
from typing import Callable, TextIO


class ConfirmationRefused(Exception):
    pass


def require_typed_confirmation(
    resource: str,
    action: str,
    provided: str | None = None,
    *,
    stdin: TextIO | None = None,
    prompt: Callable[[str], str] | None = None,
) -> None:
    """Raise ConfirmationRefused unless the exact resource name is confirmed."""
    if provided is not None:
        if provided != resource:
            raise ConfirmationRefused(f"--confirm value {provided!r} does not match {resource!r}")
        return
    stdin = stdin or sys.stdin
    if not stdin.isatty() and prompt is None:
        raise ConfirmationRefused(
            f"{action} requires confirmation; re-run interactively or pass --confirm {resource}"
        )
    ask = prompt or input
    answer = ask(f"Type the site name '{resource}' to confirm {action}: ").strip()
    if answer != resource:
        raise ConfirmationRefused("confirmation did not match; nothing was changed")
