"""The experiment: step the AMTRON's maximum current through a list of values, log each write, restore the original.

Time zero of every delay is `limit_writes.t_write_ns`, the wall clock taken by the charger service just before the
Modbus write request. The recorder writes only register 1000 and only through ChargerService.set_limit, so the page's
safety rules apply: 0 or a whole number from 6 to 16 and not above the installation limit. The charger's poll (its
heartbeat) runs for the whole experiment; the original limit is written back on every way out unless `leave` is set.
"""

import logging
import re
import threading
import time

from charger_web import config as charger_config
from charger_web.service import ChargerError

log = logging.getLogger("evsim_recorder.experiment")


class PlanError(ValueError):
    """The plan cannot be run: nothing has been written."""


def parse_steps(text):
    """'6,7,12' -> [6, 7, 12]. Only plain whole numbers separated by commas."""
    parts = [part.strip() for part in str(text).split(",")]
    if not all(re.fullmatch(r"[0-9]+", part) for part in parts):
        raise PlanError(f"The steps must be whole numbers separated by commas, for example 6,7,12 (got {text!r}).")
    return [int(part) for part in parts]


def validate_steps(steps, installation_limit):
    """Every step must be 0 or from 6 A to the installation limit (16 A at most; 16 A if it is not known)."""
    if not steps:
        raise PlanError("There are no steps to run.")
    ceiling = min(charger_config.MAX_LIMIT_A, installation_limit if installation_limit is not None else charger_config.MAX_LIMIT_A)
    for value in steps:
        if value != 0 and not charger_config.MIN_LIMIT_A <= value <= ceiling:
            raise PlanError(f"{value} A is not allowed: use 0 A or a whole value from {charger_config.MIN_LIMIT_A} A to {ceiling} A.")


def describe(steps, dwell_s, repeat, baseline_s, current_limit, charger_host, leave=False):
    writes = len(steps) * repeat
    total = baseline_s + writes * dwell_s
    lines = [
        f"Charger: {charger_host}; its maximum current is now {current_limit} A.",
        f"Steps: {', '.join(str(s) for s in steps)} A, held {dwell_s:g} s each"
        + (f", the list run {repeat} times" if repeat > 1 else "") + f": {writes} writes, about {total:g} s"
        + (f" (after {baseline_s:g} s of baseline)." if baseline_s else "."),
    ]
    if 0 in steps:
        lines.append("A step of 0 A pauses charging; the inverter loses its input while it lasts and restarts slowly.")
    lines.append("The last step will stay on the charger." if leave
                 else f"At the end, or on an interrupt, the limit is written back to {current_limit} A (restore).")
    return "\n".join(lines)


class Experiment:
    def __init__(self, service, store, steps, dwell_s, repeat=1, baseline_s=0, leave=False, stop=None, clock_ns=time.time_ns):
        self._service = service
        self._store = store
        self._steps = list(steps)
        self._dwell = dwell_s
        self._repeat = repeat
        self._baseline = baseline_s
        self._leave = leave
        self._stop = stop if stop is not None else threading.Event()
        self._clock_ns = clock_ns

    def request_stop(self):
        self._stop.set()

    def _write(self, run, step, from_a, to_a, label):
        """One limit write, logged. True if it was confirmed."""
        before = self._clock_ns()
        try:
            result = self._service.set_limit(to_a)
        except ChargerError as exc:
            log.warning("writing %s A failed: %s", to_a, exc)
            self._store.add_limit_write(run, step, before, None, from_a, to_a, None, label, str(exc))
            return False
        self._store.add_limit_write(run, step, result["write_sent_ns"], result["write_acked_ns"], from_a, to_a,
                                    result["set_limit_a"], label, None)
        return True

    def _restore(self, original, current, force=False):
        """Write the first limit back. `force` after a failed write: the charger may hold the new value, so restore even
        if the last confirmed value was the original, and even with --leave."""
        if not force and (self._leave or current == original):
            return
        if original is None:
            self._store.add_event("restore_skipped", "the limit set at the start could not be read")
            return
        self._write(0, -1, current, original, "restore")

    def run(self):
        """Run the plan. True if every step was written and held; False if it was stopped or a write failed."""
        original = self._service.status()["set_limit_a"]["value"]
        current = original
        uncertain = False
        try:
            if self._stop.wait(self._baseline):
                return False
            for run in range(1, self._repeat + 1):
                for step, to_a in enumerate(self._steps):
                    if self._stop.is_set():
                        return False
                    if not self._write(run, step, current, to_a, f"{current} -> {to_a} A"):
                        uncertain = True
                        return False
                    current = to_a
                    if self._stop.wait(self._dwell):
                        return False
            return True
        finally:
            self._restore(original, current, force=uncertain)
