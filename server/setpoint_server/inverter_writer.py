"""The inverter writer: a thread that turns the latest target into Modbus writes.

The server hands it one target per second (DC watts, or None for no data) through a one-slot
mailbox. Each step it asks the Controller what to write, performs that through fronius.Inverter,
reads the inverter's power, and publishes a status message. All Modbus I/O happens on this thread,
so a slow or dead inverter never delays the server's MQTT publish. Any error closes the connection
and resets the controller, so the next successful connection re-asserts the cap.
"""

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone

from . import config
from .fronius import Inverter, ModbusTcp, ac_cap_for_dc
from .inverter_control import Controller

log = logging.getLogger("setpoint_server")

MPPT = 4                  # SunSpec state 4: the inverter is running


def _utcnow():
    return datetime.now(timezone.utc)


def _round(value):
    return None if value is None else round(value, 1)


class InverterWriter:
    def __init__(self, host, port=config.INVERTER_PORT, unit=config.INVERTER_UNIT,
                 timeout_s=config.INVERTER_TIMEOUT_S, publish=None, controller=None,
                 clock=time.monotonic, utcnow=_utcnow, period_s=config.WRITER_PERIOD_S):
        self._connect_factory = lambda: Inverter(ModbusTcp(host, port, unit, timeout_s))
        self._publish = publish
        self._controller = controller or Controller(config.DEADBAND_W, config.REFRESH_S, config.STANDBY_AFTER_S)
        self._clock = clock
        self._utcnow = utcnow
        self._period = period_s
        self._lock = threading.Lock()
        self._target = None
        self._target_time = None
        self._inverter = None
        self._starting = False
        self._cap_ac_w = None
        self._last_error = None
        self._retry_at = 0.0
        self._backoff = config.BACKOFF_MIN_S
        self._stop = threading.Event()
        self._stopped = False
        self._thread = None

    # -- called from the server's thread ---------------------------------------------------

    def set_target(self, watts):
        """The latest DC target in watts, or None for no data. Never touches the network."""
        if watts is not None and not (math.isfinite(watts) and watts >= 0):
            log.warning("ignoring the invalid target %r", watts)
            watts = None
        with self._lock:
            self._target, self._target_time = watts, self._clock()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="inverter-writer", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the thread and leave the inverter at a 0 % cap. Safe to call more than once."""
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2 * config.INVERTER_TIMEOUT_S + 2)
        else:
            self._final_zero()

    # -- the writer's own thread -----------------------------------------------------------

    def _run(self):
        while True:
            self.step_once()
            if self._stop.wait(self._period):
                break
        self._final_zero()

    def step_once(self):
        now = self._clock()
        target = self._current_target(now)
        try:
            if self._inverter is None:
                if now < self._retry_at:
                    self._publish_status(None)
                    return
                self._connect()
            inv = self._inverter
            for action in self._controller.step(now, target, force=self._starting):
                self._perform(inv, action)
            power = inv.power()
            if self._starting and power["state"] == MPPT:
                self._starting = False
            self._last_error = None
            self._backoff = config.BACKOFF_MIN_S
            self._publish_status(power)
        except Exception as exc:          # keep the thread alive whatever goes wrong
            self._fail(now, exc)

    def _current_target(self, now):
        with self._lock:
            if self._target_time is None or now - self._target_time > config.MAILBOX_STALE_S:
                return None
            return self._target

    def _connect(self):
        inv = self._connect_factory()
        try:
            controls = inv.controls()
        except Exception:
            inv.close()
            raise
        self._controller.reset(standby=(controls["conn"] == 0))
        self._inverter = inv
        log.info("connected to the inverter")

    def _perform(self, inv, action):
        if action.kind == "cap":
            inv.set_dc_target(action.dc_w)
            self._cap_ac_w = ac_cap_for_dc(action.dc_w)
        elif action.kind == "standby":
            inv.set_conn(0)
        elif action.kind == "resume":
            inv.set_conn(1)
            self._starting = True

    def _fail(self, now, exc):
        text = str(exc) or type(exc).__name__
        if text != self._last_error:
            log.warning("inverter: %s", text)
        self._last_error = text
        self._close_inverter()
        self._starting = False
        self._controller.reset()
        self._retry_at = now + self._backoff
        self._backoff = min(self._backoff * 2, config.BACKOFF_MAX_S)
        self._publish_status(None)

    def _close_inverter(self):
        if self._inverter is not None:
            try:
                self._inverter.close()
            except OSError:
                pass
            self._inverter = None

    def _final_zero(self):
        """Write the 0 % cap on the way out, so a stopped server never leaves the last cap in force."""
        inv = self._inverter
        try:
            if inv is None:
                inv = self._connect_factory()
            inv.set_ac_cap(0)
            log.info("capped the inverter at 0 percent on shutdown")
        except Exception as exc:
            log.warning("could not cap the inverter at 0 percent on shutdown: %s", exc)
        finally:
            if inv is not None:
                try:
                    inv.close()
                except OSError:
                    pass
            self._inverter = None

    def _publish_status(self, power):
        if self._publish is None:
            return
        if self._inverter is None:
            mode = "unreachable"
        elif self._starting:
            mode = "starting"
        else:
            mode = self._controller.mode
        mppt = None
        if power is not None:
            inputs = [m for m in power["mppt"] if m["dc_w"] is not None and m["name"].startswith("MPPT")]
            mppt = next((m for m in inputs if m["name"] == "MPPT 1"), inputs[0] if inputs else None)
        payload = json.dumps({
            "mode": mode,
            "target_dc_w": _round(self._controller.last_target_w),
            "cap_ac_w": _round(self._cap_ac_w),
            "dc_w": _round(mppt["dc_w"]) if mppt else None,
            "ac_w": _round(power["ac_w"]) if power else None,
            "inverter_state": power["state"] if power else None,
            "error": self._last_error,
            "ts": self._utcnow().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, separators=(",", ":"))
        try:
            self._publish(payload)
        except Exception as exc:
            log.warning("could not publish the inverter status: %s", exc)
