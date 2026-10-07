"""Background Modbus poller and limit keeper for the AMTRON charger page.

One thread owns one Modbus connection and reads the charger every POLL_PERIOD_S. That is also the
heartbeat the charger needs: after FALLBACK_AFTER_S without Modbus traffic it drops to its safe current
(0 A on this unit), and the next request restores the limit last written. HTTP handlers read a cache and
never talk to the charger on the read path; writes (from the HTTP threads) take the same I/O lock.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import config
from .modbus import ModbusError, ModbusTcp, ModbusTransportError
from .registers import load_map

log = logging.getLogger("charger_web.service")

READ_ORDER = (
    "set_limit_a", "operator_limit_a", "installation_limit_a", "signalled_current_a", "vehicle_state",
    "charger_status", "meter_current_l1_a", "meter_current_l2_a", "meter_current_l3_a",
    "meter_power_w", "meter_voltage_l1_v", "meter_voltage_l2_v", "meter_voltage_l3_v",
)
PHASES = (("L1", "meter_current_l1_a"), ("L2", "meter_current_l2_a"), ("L3", "meter_current_l3_a"))
VOLT_PHASES = (("L1", "meter_voltage_l1_v"), ("L2", "meter_voltage_l2_v"), ("L3", "meter_voltage_l3_v"))
VEHICLE_STATES = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}
CHARGER_STATUS = {
    0: "Available", 1: "Occupied", 2: "Reserved", 3: "Unavailable", 4: "Faulted", 5: "Preparing",
    6: "Charging", 7: "Suspended by the charger", 8: "Suspended by the vehicle", 9: "Finishing",
}
NO_METER = 0xFFFFFFFF


class ChargerError(RuntimeError):
    """A safe-to-display charger or communication error."""


class LimitValidationError(ChargerError):
    """The requested current limit is outside the accepted range."""


class ChargerService:
    def __init__(self, host=None, port=None, register_map=None, clock=time.monotonic, period_s=None):
        self.map = register_map if register_map is not None else load_map()
        self._registers = self.map["registers"]
        self._clock = clock
        self._period = config.POLL_PERIOD_S if period_s is None else period_s
        self._modbus = ModbusTcp(
            host or config.charger_host(), port or config.MODBUS_PORT,
            int(self.map.get("unit", 255)), config.MODBUS_TIMEOUT_S,
        )
        self._io_lock = threading.Lock()       # one conversation with the charger at a time
        self._cache_lock = threading.Lock()    # held only to copy or update the cache, never during I/O
        self._values = dict.fromkeys(READ_ORDER)
        self._at = dict.fromkeys(READ_ORDER)
        self._last_ok = None
        self._error = None
        self._desired = None
        self._restored_at = None
        self._restore_attempt_at = None
        self._stop = threading.Event()
        self._thread = None

    # -- reading -----------------------------------------------------------------------------

    def _read_register(self, name):
        spec = self._registers[name]
        size = spec.get("size", 1)
        words = self._modbus.read(spec["address"], size)
        raw = words[0] if size == 1 else (words[0] << 16) | words[1]
        if size == 2 and raw == NO_METER:
            return None
        scale = spec.get("scale", 1)
        return raw if scale == 1 else round(raw * scale, 3)

    def poll_once(self):
        """Read every register once and update the cache."""
        with self._io_lock:
            values, error = {}, None
            for name in READ_ORDER:
                try:
                    values[name] = self._read_register(name)
                except ModbusTransportError as exc:
                    error = str(exc)
                    break                     # the connection is suspect; the older values simply go stale
                except ModbusError:
                    values[name] = None       # this register is refused; the others still read
            now = self._clock()
            with self._cache_lock:
                self._error = error
                if error is None:
                    self._last_ok = now
                for name, value in values.items():
                    self._values[name] = value
                    self._at[name] = None if value is None else now
            if error is None:
                self._restore_if_needed(now)

    def _link(self, now):
        if self._last_ok is None:
            return "unavailable"
        age = now - self._last_ok
        if age < config.STALE_AFTER_S:
            return "ok"
        return "stale" if age < config.FALLBACK_AFTER_S else "fallback_expected"

    def _timestamp(self, at):
        if at is None:
            return None
        age = max(0.0, self._clock() - at)
        return datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()

    def _reading(self, value, at, link, now):
        if value is None or at is None:
            status = "unavailable"
        elif link != "ok" or now - at >= config.STALE_AFTER_S:
            status = "stale"
        else:
            status = "ok"
        return {"value": value, "status": status, "updated_at": self._timestamp(at)}

    def status(self):
        with self._cache_lock:
            now = self._clock()
            link = self._link(now)
            values, at = dict(self._values), dict(self._at)
            desired, restored_at, error = self._desired, self._restored_at, self._error

            def reading(name, value=None):
                return self._reading(values[name] if value is None else value, at[name], link, now)

            phases = {phase: values[name] for phase, name in PHASES if values[name] is not None}
            meter_at = min((at[name] for phase, name in PHASES if values[name] is not None), default=None)
            volts = {phase: values[name] for phase, name in VOLT_PHASES if values[name] is not None}
            volts_at = min((at[name] for phase, name in VOLT_PHASES if values[name] is not None), default=None)
            state_raw, status_raw = values["vehicle_state"], values["charger_status"]
            state_text = None if state_raw is None else VEHICLE_STATES.get(state_raw, f"state {state_raw}")
            status_text = None if status_raw is None else CHARGER_STATUS.get(status_raw, f"status {status_raw}")
            set_limit, operator, signalled = values["set_limit_a"], values["operator_limit_a"], values["signalled_current_a"]
            return {
                "status": link,
                "link": link,
                "error": error,
                "set_limit_a": reading("set_limit_a"),
                "operator_limit_a": reading("operator_limit_a"),
                "installation_limit_a": reading("installation_limit_a"),
                "signalled_current_a": reading("signalled_current_a"),
                "meter_current_a": self._reading(phases or None, meter_at, link, now),
                "meter_voltage_v": self._reading(volts or None, volts_at, link, now),
                "meter_power_w": reading("meter_power_w"),
                "vehicle_state": self._reading(state_text, at["vehicle_state"], link, now),
                "charger_status": self._reading(status_text, at["charger_status"], link, now),
                "paused": bool(set_limit and signalled == 0),
                "operator_below_set": bool(set_limit and operator is not None and operator < set_limit),
                "desired_limit_a": desired,
                "restored_at": self._timestamp(restored_at),
            }

    # -- keeping and setting the limit ------------------------------------------------------

    def _restore_if_needed(self, now):
        """Put the desired limit back if the charger lost it. Never runs before a confirmed Apply."""
        with self._cache_lock:
            desired, current = self._desired, self._values["set_limit_a"]
        if desired is None or current is None or current == desired or self._stop.is_set():
            return
        if self._restore_attempt_at is not None and now - self._restore_attempt_at < config.RESTORE_MIN_INTERVAL_S:
            return
        self._restore_attempt_at = now
        log.warning("the charger reports a limit of %s A; restoring the desired %s A", current, desired)
        try:
            self._modbus.write_single(self._registers["set_limit_a"]["address"], desired)
        except ModbusError as exc:
            with self._cache_lock:
                self._error = str(exc)
            return
        with self._cache_lock:
            self._restored_at = now

    @staticmethod
    def _validate_limit(value, installation_limit):
        if type(value) is not int:
            raise LimitValidationError("Current limit must be a whole number of amps.")
        if value != 0 and not config.MIN_LIMIT_A <= value <= config.MAX_LIMIT_A:
            raise LimitValidationError("Current limit must be 0 A or a whole value from 6 A to 16 A.")
        ceiling = min(config.MAX_LIMIT_A, installation_limit)
        if value > ceiling:
            raise LimitValidationError(f"Current limit cannot exceed the installation limit of {ceiling} A.")

    def _forget_desired(self):
        with self._cache_lock:
            self._desired = None

    def set_limit(self, value):
        """Write register 1000, read it back, and only then remember it as the desired limit."""
        with self._cache_lock:
            installation = self._values["installation_limit_a"]
        self._validate_limit(value, config.MAX_LIMIT_A if installation is None else installation)
        with self._io_lock:
            sent = False
            try:
                self._modbus.write_single(self._registers["set_limit_a"]["address"], value)
                sent = True
                confirmed = self._read_register("set_limit_a")
            except ModbusError as exc:
                self._modbus.close()
                if sent or isinstance(exc, ModbusTransportError):
                    self._forget_desired()        # the charger may hold the new value: do not revert it behind the user's back
                raise ChargerError(f"Modbus write was not confirmed ({exc}); check the setting before retrying.") from exc
            if confirmed != value:
                self._forget_desired()
                raise ChargerError(f"Write was not confirmed; charger reports {confirmed!r} A.")
            now = self._clock()
            with self._cache_lock:
                self._values["set_limit_a"], self._at["set_limit_a"] = confirmed, now
                # the signalled current (706) follows the limit with a delay: do not show a stale 0 as "paused"
                self._values["signalled_current_a"], self._at["signalled_current_a"] = None, None
                self._desired = value
        return {"success": True, "set_limit_a": confirmed, "updated_at": self._timestamp(now)}

    # -- the poller thread -------------------------------------------------------------------

    def start(self):
        """Poll in the background: this is also the heartbeat that keeps the charger from falling back."""
        self._thread = threading.Thread(target=self._run, name="charger-poller", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:             # one bad poll must never stop the heartbeat
                log.exception("charger poll failed")
            self._stop.wait(self._period)

    def stop(self):
        """Stop the poller (if it is running) and close the connection."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * config.MODBUS_TIMEOUT_S + 2)
        if self._io_lock.acquire(timeout=2 * config.MODBUS_TIMEOUT_S + 2):
            try:
                self._modbus.close()
            finally:
                self._io_lock.release()
