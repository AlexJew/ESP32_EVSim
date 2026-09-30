"""What to write to the inverter, and when. Pure logic: no I/O and no clock of its own.

The state machine of docs/superpowers/specs/2026-09-30-inverter-writer-design.md. Each step gets the
current time and the target (DC watts, or None when there is no data) and returns the actions to
perform, in order. The caller performs them and calls reset() if any of them fails, so the next step
re-asserts what was meant.
"""

from collections import namedtuple

Action = namedtuple("Action", "kind dc_w")   # kind: "cap", "standby" or "resume"; dc_w only for "cap"


class Controller:
    def __init__(self, deadband_w, refresh_s, standby_after_s):
        self._deadband = deadband_w
        self._refresh = refresh_s
        self._standby_after = standby_after_s
        self._outage_since = None
        self.reset()

    def reset(self, standby=False):
        """Forget what was written (after a reconnect or an error) so the next step re-asserts it.

        `standby=True` when the inverter is known to be in standby (Conn = 0). The outage clock is
        kept: a transient error must not postpone the standby fallback.
        """
        self.mode = "standby" if standby else "unknown"
        self.last_target_w = None
        self._last_write = None

    def step(self, now, target_w, force=False):
        if target_w is None:
            return self._step_no_data(now, force)
        self._outage_since = None
        actions = []
        if self.mode == "standby":
            actions.append(Action("resume", None))
            self.last_target_w = None            # the cap must be written again after the restart
        if self._must_write(now, target_w, force):
            actions.append(Action("cap", target_w))
            self._wrote(now, target_w)
        self.mode = "capped"
        return actions

    def _step_no_data(self, now, force):
        if self._outage_since is None:
            self._outage_since = now
        if self.mode == "standby":
            return []
        actions = []
        if self.mode != "zero" or force or now - self._last_write >= self._refresh:
            actions.append(Action("cap", 0.0))
            self._wrote(now, 0.0)
        self.mode = "zero"
        if now - self._outage_since >= self._standby_after:
            actions.append(Action("standby", None))
            self.mode = "standby"
        return actions

    def _must_write(self, now, target_w, force):
        last = self.last_target_w
        if force or last is None:
            return True
        if (target_w == 0) != (last == 0):
            return True
        if abs(target_w - last) >= self._deadband:
            return True
        return now - self._last_write >= self._refresh

    def _wrote(self, now, target_w):
        self.last_target_w = target_w
        self._last_write = now
