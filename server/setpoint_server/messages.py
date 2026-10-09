"""Reads the board's evsim/cp messages and writes the evsim/setpoint ones.

Pure functions, no network. See
docs/superpowers/specs/2026-09-30-setpoint-server-design.md for the contract.
"""

import json
import math
from datetime import timezone

from .config import MAX_SETPOINT_W


def parse_cp(payload):
    """Return the `setpoint_w` of an evsim/cp payload as a float, or None.

    None means the message is unusable: not JSON, not an object, no numeric
    `setpoint_w`, or a value that is not finite, is negative, or is above
    MAX_SETPOINT_W. Such a value is dropped, never clamped. It never raises.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("setpoint_w")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or not 0 <= value <= MAX_SETPOINT_W:
            return None
        return float(value)
    except OverflowError:  # an integer too large to convert to a float
        return None


def _dump(fields):
    return json.dumps(fields, separators=(",", ":"))


def format_setpoint(count, mean, window_s, now_utc):
    """The evsim/setpoint message for one tick.

    `ok` with the rounded mean when `count` > 0, otherwise `no_data` and 0 W.
    `now_utc` must be a timezone-aware datetime.
    """
    if count > 0:
        status, setpoint_w = "ok", math.floor(mean + 0.5)  # half rounds up
    else:
        status, setpoint_w = "no_data", 0
    return _dump({
        "setpoint_w": setpoint_w,
        "samples": count,
        "window_s": window_s,
        "status": status,
        "ts": now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_ms": int(now_utc.timestamp() * 1000),       # the same instant in epoch milliseconds
    })


def offline_payload(window_s):
    """The `server_offline` message, used as the last-will and on a clean stop.

    It has no `ts`: the last-will is fixed when the client connects.
    """
    return _dump({
        "setpoint_w": 0,
        "samples": 0,
        "window_s": window_s,
        "status": "server_offline",
    })
