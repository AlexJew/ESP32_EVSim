"""The AMTRON's Modbus register map, loaded from amtron_registers.json."""

import json
from pathlib import Path

from . import config

DEFAULT_MAP = Path(__file__).with_name("amtron_registers.json")
REQUIRED = (
    "set_limit_a", "operator_limit_a", "installation_limit_a", "signalled_current_a",
    "vehicle_state", "charger_status", "meter_current_l1_a", "meter_current_l2_a", "meter_current_l3_a",
)


def load_map(path=None):
    """Read the register map and check that every register the page needs is in it."""
    with open(path or config.modbus_register_map() or DEFAULT_MAP, encoding="utf-8") as handle:
        register_map = json.load(handle)
    missing = [name for name in REQUIRED if name not in register_map.get("registers", {})]
    if missing:
        raise ValueError("the register map is missing: " + ", ".join(missing))
    return register_map
