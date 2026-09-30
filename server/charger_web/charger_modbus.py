"""Modbus TCP backend for the charger page.

Same interface as ChargerClient (status, set_operator_limit, reconnect), but it
never uses the charger's web/REST login, so it does not compete with the AMTRON
web page for its single session.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import config
from .charger import ChargerClient, ChargerError, LimitValidationError
from .modbus import ModbusError, ModbusTcp

log = logging.getLogger("charger_web.modbus")
DEFAULT_MAP = Path(__file__).with_name("amtron_registers.json")
KEYS = ("operator_limit_a", "installation_limit_a", "signaled_current_a", "meter_current_a")
SPEC = {
    "operator_limit_a": "operator_limit",
    "installation_limit_a": "installation_limit",
    "signaled_current_a": "signalled_current",
    "meter_current_a": "meter_current",
}


def load_map(path=None):
    with open(path or config.modbus_register_map() or DEFAULT_MAP, encoding="utf-8") as handle:
        return json.load(handle)


class ModbusChargerClient:
    configured = True

    def __init__(self, host=None, port=None, register_map=None, clock=time.monotonic):
        self.map = register_map if register_map is not None else load_map()
        self._clock = clock
        self._lock = threading.RLock()
        self._modbus = ModbusTcp(
            host or config.charger_host(),
            port or config.MODBUS_PORT,
            int(self.map.get("unit", 255)),
            config.HTTP_TIMEOUT_S,
        )
        self._values = dict.fromkeys(KEYS)
        self._sampled_at = dict.fromkeys(KEYS)
        self._attempt_at = None
        self._error = None

    def _mapped(self, spec):
        return bool(spec) and (spec.get("address") is not None or any(v is not None for v in spec.get("addresses", {}).values()))

    @property
    def any_mapped(self):
        return any(self._mapped(self.map.get(name)) for name in SPEC.values())

    def _read_spec(self, key):
        spec = self.map.get(SPEC[key])
        if not self._mapped(spec):
            return None
        scale = spec.get("scale", 1)
        table = spec.get("table", "holding")
        if "addresses" in spec:
            phases = {}
            for phase, address in spec["addresses"].items():
                if address is not None:
                    phases[phase] = round(self._modbus.read(address, 1, table)[0] * scale, 3)
            return phases or None
        raw = self._modbus.read(spec["address"], 1, table)[0]
        value = raw * scale
        return int(value) if key.endswith("limit_a") and float(value).is_integer() else value

    def _refresh(self, force=False):
        now = self._clock()
        if not force and self._attempt_at is not None and now - self._attempt_at < config.DASHBOARD_CACHE_S:
            return
        self._attempt_at = now
        self._error = None
        for key in KEYS:
            try:
                value = self._read_spec(key)
            except ModbusError as exc:
                self._error = str(exc)
                break  # the connection is suspect; keep older values as stale
            self._values[key] = value
            self._sampled_at[key] = self._clock() if value is not None else None

    def _reading(self, key):
        value, at = self._values[key], self._sampled_at[key]
        status = "unavailable" if value is None or at is None else "ok"
        if status == "ok" and (self._error or self._clock() - at >= config.DASHBOARD_CACHE_S * 1.5):
            status = "stale"
        return {"value": value, "status": status, "updated_at": ChargerClient._timestamp(at)}

    def status(self):
        with self._lock:
            if not self.any_mapped:
                unavailable = {"value": None, "status": "unavailable", "updated_at": None}
                return {
                    "status": "not_configured",
                    "error": "Modbus register map is empty; fill in amtron_registers.json (see tools/amtron_modbus_probe.py).",
                    **{key: dict(unavailable) for key in KEYS},
                }
            self._refresh()
            readings = {key: self._reading(key) for key in KEYS}
            if self._error:
                overall = "stale" if any(r["value"] is not None for r in readings.values()) else "unavailable"
            elif any(r["status"] == "stale" for r in readings.values()):
                overall = "stale"
            else:
                overall = "ok"
            return {"status": overall, "error": self._error, **readings}

    def reconnect(self):
        with self._lock:
            self._modbus.close()
            self._attempt_at = None
            return self.status()

    def set_operator_limit(self, value):
        with self._lock:
            spec = self.map.get("operator_limit")
            if not self._mapped(spec):
                raise ChargerError("The operator-limit Modbus register is not configured; no write was made.")
            try:
                self._refresh(force=True)
                installation = self._values["installation_limit_a"]
                ChargerClient._validate_limit(value, None if installation is None else int(installation))
                self._modbus.write_single(spec["address"], int(round(value / spec.get("scale", 1))))
                self._refresh(force=True)
            except ModbusError as exc:
                self._modbus.close()
                raise ChargerError(f"Modbus write was not confirmed ({exc}); check the setting before retrying.") from exc
            confirmed = self._values["operator_limit_a"]
            if confirmed != value:
                raise ChargerError(f"Write was not confirmed; charger reports {confirmed!r} A.")
            return {"success": True, "operator_limit_a": confirmed, "updated_at": ChargerClient._timestamp(self._sampled_at["operator_limit_a"])}
