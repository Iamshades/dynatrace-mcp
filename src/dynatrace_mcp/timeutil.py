"""Time helpers. Dynatrace takes ``now-2h``, ISO-8601 and epoch milliseconds; we pass
user input through unchanged and only compute absolute windows for the RCA tools."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

_REL = re.compile(r"^now(?:-(\d+)([smhdwMy]))?$")
_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000,
            "M": 2_592_000_000, "y": 31_536_000_000}


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | float | None) -> str | None:
    if ms is None or ms in (-1, 0):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_ms(value: str | int | None, now: int | None = None) -> int | None:
    """Best-effort conversion of a Dynatrace timeframe value to epoch ms."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    value = value.strip()
    now = now_ms() if now is None else now
    if value.isdigit():
        return int(value)
    m = _REL.match(value)
    if m:
        if not m.group(1):
            return now
        return now - int(m.group(1)) * _UNIT_MS[m.group(2)]
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def resolution_for(window_ms: int, points: int = 60) -> str:
    """Pick a metric resolution that yields roughly ``points`` data points."""
    minutes = max(1, window_ms // 60_000 // points)
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 360, 720, 1440):
        if minutes <= step:
            return f"{step}m" if step < 60 else f"{step // 60}h"
    return "1d"


def duration_text(ms: int | None) -> str | None:
    if ms is None or ms < 0:
        return None
    minutes, _ = divmod(ms // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
