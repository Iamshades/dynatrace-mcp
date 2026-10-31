"""Mask secrets in data before it leaves the server.

Log lines and event properties often carry credentials. Masking is on by
default (DT_REDACT=true). The configured tokens are always masked, even when
DT_REDACT=false.
"""

from __future__ import annotations

import json
import re
from typing import Any

MASK = "***"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Dynatrace tokens: dt0c01.<public>.<secret>, dt0s16..., etc.
    (re.compile(r"\bdt0[a-z]\d{2}\.[A-Za-z0-9]{8,}(?:\.[A-Za-z0-9]{8,})?"), "dt0***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "-----PRIVATE KEY " + MASK + "-----"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:\"|')?[^\s\"',;]+(?:\s+[^\s\"',;]+)?"),
     r"\1" + MASK),
    (re.compile(r"(?i)\b(bearer|api-token|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), "jwt" + MASK),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "aws-key" + MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "gh" + MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "slack" + MASK),
    # user:password@host in URLs and JDBC strings
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@"), r"\1" + MASK + "@"),
    # key=value / key: value / "key": "value" for secret-looking keys
    (re.compile(
        r"(?i)([\"']?\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|credential)s?[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"'}]+)"),
     r"\1" + MASK),
]


class Redactor:
    def __init__(self, secrets: list[str] | None = None, enabled: bool = True):
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 6]
        self.enabled = enabled

    def text(self, value: str) -> str:
        for secret in self.secrets:
            if secret in value:
                value = value.replace(secret, MASK)
        if not self.enabled:
            return value
        for pattern, repl in _PATTERNS:
            value = pattern.sub(repl, value)
        return value

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.data(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(v) for v in value]
        return value


def truncate_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"... [truncated {len(value) - limit} chars]"
        return value
    if isinstance(value, dict):
        return {k: truncate_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [truncate_strings(v, limit) for v in value]
    return value


def fit_output(value: Any, max_chars: int) -> Any:
    """Shrink the largest lists until the JSON form fits in ``max_chars``.

    Cursor and the model handle a bounded, honest summary better than a
    megabyte of JSON. Dropped items are reported in ``_truncated``.
    """
    size = len(json.dumps(value, default=str))
    if size <= max_chars:
        return value
    notes: list[str] = []
    for _ in range(40):
        path, lst = _largest_list(value)
        if lst is None or len(lst) <= 1:
            break
        keep = max(1, len(lst) // 2)
        notes.append(f"{path or 'result'}: kept {keep} of {len(lst)} items")
        del lst[keep:]
        size = len(json.dumps(value, default=str))
        if size <= max_chars:
            break
    if size > max_chars:
        text = json.dumps(value, default=str)
        return {"_truncated": notes + [f"output cut to {max_chars} chars"],
                "partial_json": text[:max_chars]}
    if isinstance(value, dict):
        value = {**value, "_truncated": notes}
    return value


def _largest_list(value: Any, path: str = "") -> tuple[str, list | None]:
    best: tuple[str, list | None, int] = ("", None, 0)

    def walk(node: Any, where: str) -> None:
        nonlocal best
        if isinstance(node, list):
            size = len(json.dumps(node, default=str))
            if size > best[2] and len(node) > 1:
                best = (where, node, size)
            for i, item in enumerate(node[:50]):
                walk(item, f"{where}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{where}.{k}" if where else k)

    walk(value, path)
    return best[0], best[1]
