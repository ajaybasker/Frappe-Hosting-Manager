"""Secret redaction for anything that is printed, logged or exported."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

_SECRET_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|encryption[_-]?key|private[_-]?key|credential|auth)",
    re.IGNORECASE,
)

_TEXT_PATTERNS = [
    # key=value / key: value / "key": "value"
    re.compile(
        r"""(?P<k>["']?[\w-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|encryption[_-]?key)[\w-]*["']?\s*[:=]\s*)(?P<v>"[^"]*"|'[^']*'|[^\s,;&]+)""",
        re.IGNORECASE,
    ),
    # CLI flags such as --admin-password X / --db-root-password=X
    re.compile(r"(?P<k>--[\w-]*password[\w-]*[ =])(?P<v>\S+)", re.IGNORECASE),
    # URLs with embedded credentials: scheme://user:pass@host
    re.compile(r"(?P<k>\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<v>[^\s@/]+)(?=@)", re.IGNORECASE),
    # Authorization headers
    re.compile(r"(?P<k>\bAuthorization:\s*(?:Bearer|Basic|token)\s+)(?P<v>\S+)", re.IGNORECASE),
]


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def redact_text(text: str) -> str:
    for pat in _TEXT_PATTERNS:
        text = pat.sub(lambda m: m.group("k") + REDACTED, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Recursively redact values whose key looks secret, and scrub strings."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if isinstance(k, str) and is_secret_key(k) and v not in (None, "") else redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
