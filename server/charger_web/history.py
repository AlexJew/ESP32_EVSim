"""A 1 Hz sampler: one frame per second into a ring of the last 10 minutes, in memory only.

A frame holds the wall-clock second and every measurement. A value is recorded only if its reading is `ok`, so a
stale or missing source is a gap (None), never a flat line. The raw board setpoint is the mean of the samples
received since the previous tick (about five), so it can be compared fairly with the 1 s average. The charger's
current and voltage are the mean of the phases that read. The charger is read from its cache (`status()`): the
sampler never talks to the charger.
"""

import logging
import threading
import time
from collections import deque

from . import config

log = logging.getLogger("charger_web.history")


def _ok(source):
    return round(source["value"], 1) if source["status"] == "ok" else None


def _phase_mean(source):
    """The mean of the phases of an `ok` reading whose value is {"L1": x, ...}; None if there is none."""
    values = [v for v in (source["value"] or {}).values() if isinstance(v, (int, float))] if source["status"] == "ok" else []
    return round(sum(values) / len(values), 1) if values else None


class History:
    def __init__(self, telemetry, charger=None, clock=time.monotonic, wall=time.time, period_s=None):
        self._telemetry = telemetry
        self._charger = charger
        self._clock = clock
        self._wall = wall
        self._period = config.HISTORY_PERIOD_S if period_s is None else period_s
        self._frames = deque(maxlen=config.HISTORY_WINDOW_S // config.HISTORY_PERIOD_S)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def tick(self):
        """Take one frame now."""
        samples = self._telemetry.drain_cp_samples()
        snap = self._telemetry.snapshot()
        inverter = snap["inverter"]
        frame = {
            "t": int(self._wall()),
            "cp_w": round(sum(samples) / len(samples), 1) if samples else None,
            "setpoint_w": _ok(snap["setpoint"]["setpoint_w"]),
            "target_dc_w": _ok(snap["server"]["target_dc_w"]),
            "dc_w": _ok(inverter["dc_w"]),
            "dc_a": _ok(inverter["dc_a"]),
            "dc_v": _ok(inverter["dc_v"]),
            "ac_w": _ok(inverter["ac_w"]),
            "ac_a": _ok(inverter["ac_a"]),
            "ac_v": _ok(inverter["ac_v"]),
            "charger_w": None, "charger_a": None, "charger_v": None, "sig_a": None,
        }
        if self._charger is not None:
            status = self._charger.status()
            frame["charger_w"] = _ok(status["meter_power_w"])
            frame["charger_a"] = _phase_mean(status["meter_current_a"])
            frame["charger_v"] = _phase_mean(status["meter_voltage_v"])
            frame["sig_a"] = _ok(status["signalled_current_a"])
        with self._lock:
            self._frames.append(frame)

    def snapshot(self):
        with self._lock:
            frames = list(self._frames)
        return {"interval_s": config.HISTORY_PERIOD_S, "window_s": config.HISTORY_WINDOW_S, "frames": frames}

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="history-sampler", daemon=True)
        self._thread.start()

    def _run(self):
        next_tick = self._clock() + self._period
        while not self._stop.wait(max(0.0, next_tick - self._clock())):
            try:
                self.tick()
            except Exception:                   # one bad frame must never stop the sampler
                log.exception("history tick failed")
            next_tick += self._period           # scheduled, not slept, so a slow tick adds no drift
            now = self._clock()
            if next_tick <= now:                # fell behind: skip the missed ticks, do not burst
                next_tick = now + self._period

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
