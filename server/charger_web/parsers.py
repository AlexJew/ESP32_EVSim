"""Parsers for the MQTT payloads the dashboard reads.

Pure functions that never raise: anything unusable gives None. A power outside 0 to MAX_SETPOINT_W is
dropped, never clamped (the setpoint server does the same); only a measured DC power is bounded more loosely, and a bad one becomes None without dropping its message. Text fields are short strings or None; the
page shows them with textContent, so their content cannot inject markup.
"""

import json
import math

from . import config

MAX_TEXT = 32


def _object(payload):
    try:
        data = json.loads(payload)
    except (ValueError, TypeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _watts(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or not 0 <= value <= config.MAX_SETPOINT_W:
            return None
        return float(value)
    except OverflowError:           # an integer too large to convert to a float
        return None


def _dc_watts(value):
    """A measured DC power: finite, from 0 to MAX_DC_W (it may exceed the setpoint cap), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or not 0 <= value <= config.MAX_DC_W:
            return None
        return float(value)
    except OverflowError:
        return None


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= MAX_TEXT else None


def _state(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        return None
    return value


def parse_cp(payload):
    """An evsim/cp sample: the held setpoint and the pilot state the board presents."""
    data = _object(payload)
    if data is None:
        return None
    watts = _watts(data.get("setpoint_w"))
    if watts is None:
        return None
    return {"setpoint_w": watts, "state": _text(data.get("state")), "status": _text(data.get("status"))}


def parse_setpoint(payload):
    """The setpoint topic's message. This is the one function to change when the car emulation's differs."""
    data = _object(payload)
    if data is None:
        return None
    watts = _watts(data.get("setpoint_w"))
    if watts is None:
        return None
    return {"setpoint_w": watts, "status": _text(data.get("status"))}


def parse_inverter(payload):
    """The setpoint server's evsim/inverter status: its DC target, the inverter's measured DC power, the mode and the state."""
    data = _object(payload)
    if data is None:
        return None
    target = data.get("target_dc_w")
    if target is not None:
        target = _watts(target)
        if target is None:
            return None
    return {"target_dc_w": target, "dc_w": _dc_watts(data.get("dc_w")), "mode": _text(data.get("mode")),
            "inverter_state": _state(data.get("inverter_state"))}
