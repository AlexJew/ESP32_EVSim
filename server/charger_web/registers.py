"""The AMTRON's Modbus register map, loaded from amtron_registers.json."""

import json
from pathlib import Path

from . import config

WRITABLE_ADDRESS = 1000        # the HEMS current limit; the operator limit (134) must never be written
DEFAULT_MAP = Path(__file__).with_name("amtron_registers.json")
REQUIRED = (
    "set_limit_a", "operator_limit_a", "installation_limit_a", "signalled_current_a",
    "vehicle_state", "charger_status", "meter_current_l1_a", "meter_current_l2_a", "meter_current_l3_a",
)


def load_map(path=None):
    """Read the register map and check that every register the page needs is in it."""
    try:
        with open(path or config.modbus_register_map() or DEFAULT_MAP, encoding="utf-8") as handle:
            register_map = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the register map: {exc}") from exc
    missing = [name for name in REQUIRED if name not in register_map.get("registers", {})]
    if missing:
        raise ValueError("the register map is missing: " + ", ".join(missing))
    if register_map["registers"]["set_limit_a"].get("address") != WRITABLE_ADDRESS:
        raise ValueError(f"set_limit_a must be register {WRITABLE_ADDRESS}: it is the only register this page writes")
    return register_map
