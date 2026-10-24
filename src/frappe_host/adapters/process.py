"""Safe subprocess execution.

Rules: argv lists only (never shell=True), every element must be a str without
NUL bytes, explicit timeouts, and redacted command echo for logs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from frappe_host.policies.redaction import redact_text


class CommandError(RuntimeError):
    pass


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _check_argv(argv: list[str]) -> list[str]:
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise CommandError("argv must be a non-empty list of strings")
    if any("\x00" in a for a in argv):
        raise CommandError("argv contains NUL byte")
    return list(argv)


def display(argv: list[str]) -> str:
    """Human-readable, redacted rendering of a command for plans and logs."""
    import shlex

    return redact_text(" ".join(shlex.quote(a) for a in argv))


def search_path() -> str:
    """PATH with ~/.local/bin first, so tools that `init` installs there (bench, uv,
    node, yarn) are found before the user has opened a new login shell."""
    local_bin = str(Path.home() / ".local" / "bin")
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if local_bin not in parts:
        parts.insert(0, local_bin)
    return os.pathsep.join(parts)


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = search_path()
    return env


def which(name: str) -> str | None:
    return shutil.which(name, path=search_path())


def run(argv: list[str], *, cwd: Path | None = None, timeout: float = 30.0, input: str | None = None) -> Result:
    """Run a command capturing output. Missing executables and timeouts become Results, not crashes."""
    argv = _check_argv(argv)
    try:
        cp = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
            input=input, stdin=None if input is not None else subprocess.DEVNULL, env=child_env(),
        )
    except FileNotFoundError:
        return Result(argv, 127, "", f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return Result(argv, 124, "", f"timed out after {timeout:.0f}s")
    except OSError as e:
        return Result(argv, 126, "", str(e))
    return Result(argv, cp.returncode, cp.stdout, cp.stderr)


# When the CLI emits JSON, child output is sent to stderr so stdout stays parseable.
interactive_stdout = None


def run_interactive(argv: list[str], *, cwd: Path | None = None, input: str | None = None) -> int:
    """Run with the user's terminal attached, so tools like bench and sudo can prompt
    for passwords themselves. Secrets therefore never pass through our argv or logs.
    With `input`, that text is fed to stdin (sudo still prompts on the terminal)."""
    argv = _check_argv(argv)
    try:
        return subprocess.run(
            argv, cwd=cwd, check=False, env=child_env(), text=True, input=input,
            stdout=subprocess.DEVNULL if input is not None else interactive_stdout,
        ).returncode
    except FileNotFoundError:
        return 127


def sudo(argv: list[str]) -> list[str]:
    """Prefix a command with sudo unless we already are root."""
    return list(argv) if os.geteuid() == 0 else ["sudo", *argv]
