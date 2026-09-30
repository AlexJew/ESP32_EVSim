# Inverter Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the setpoint server drive the Fronius GEN24 over Modbus TCP: turn the averaged DC setpoint into a cap on the inverter's AC output, with a 0 % cap on no data and standby after a long outage.

**Architecture:** The Modbus client moves from `tools/` into the server package as the single copy of the register logic (`fronius.py`, with the CLI in `fronius_cli.py`). A pure state machine (`inverter_control.py`) decides what to write; a writer thread (`inverter_writer.py`) performs the writes and reports status; `server.py` hands it one target per second. Everything is tested against a fake inverter on localhost, and finally against the real one.

**Tech Stack:** Python 3 (standard library only for the Modbus code), `paho-mqtt` 2.x (already installed in `server/.venv`), `unittest`, Mosquitto 2.x.

**Spec:** [docs/superpowers/specs/2026-09-30-inverter-writer-design.md](../specs/2026-09-30-inverter-writer-design.md). It builds on [2026-09-30-setpoint-server-design.md](../specs/2026-09-30-setpoint-server-design.md). The bench findings behind it are in `memory/evsim-inverter-fronius.md`.

Work on a new branch `inverter-writer`, not on `master`.

## Global Constraints

Every task's requirements include these, copied from the spec.

- The setpoint is the **DC input power** to take from the charger. The AC cap is `(P_dc - 6.3) / 1.0303 + 3 W`, and the register is `cap / WMax x 10 000` for a scale factor of -2 (WMax 12 000 W, read from the inverter). The constants are `DC_PER_AC = 1.0303`, `DC_OFFSET_W = 6.3`, `AC_SETTLE_W = 3.0` in `config.py`.
- **Every cap change is: write `WMaxLimPct`, then write `WMaxLim_Ena` = 1, then read both back.** The value alone changes nothing.
- The only registers ever written are `WMaxLimPct`, `WMaxLim_Ena` and `Conn` (0 or 1), each range-checked. Scale factors are re-read with every read, never cached.
- **Dead-band 100 W:** a new target is written only if it differs from the last *written* target by at least 100 W, or if one of the two is exactly 0. **Refresh:** re-commit the current cap every 10 s. **Standby:** `Conn` = 0 after **120 s** of no data. A stale target (older than 3 s) counts as no data.
- No data means a **0 % cap plus commit at once** (about 94 W of DC draw remains). A valid target of 0 W caps at 0 % and never escalates to standby. Data returning from standby writes `Conn` = 1, then the cap, and keeps re-committing (forced) until the inverter reports MPPT (state 4).
- Server start with no data asserts the 0 % cap. A clean shutdown writes the 0 % cap, then closes.
- **The server never controls the inverter unless started with `--inverter HOST`.** Modbus I/O runs on its own thread; a slow or dead inverter must never delay the once-a-second MQTT publish.
- Inverter status topic `evsim/inverter`, QoS 0, not retained, once per second, JSON with keys in this order: `mode`, `target_dc_w`, `cap_ac_w`, `dc_w`, `ac_w`, `inverter_state`, `error`, `ts` (UTC, `%Y-%m-%dT%H:%M:%SZ`). `mode` is `capped`, `zero`, `standby`, `starting` or `unreachable`.
- Inverter defaults: port 502, unit ID 1, timeout 3 s, retry backoff 1 to 5 s.

## Review Focus

Inputs and conditions the spec implies but does not spell out, most likely to bite first. Each has a test in the task that owns the code.

1. **The inverter accepts the TCP connection but never answers** (a blackholed or hung unit). Expected: the caller of `set_target` never blocks, the writer reports `unreachable`, and MQTT keeps publishing. Tested in Task 3.
2. **A write sequence that dies half way** (the value is written, the `Ena` commit is refused). Expected: the partial state is re-asserted after reconnecting, never assumed done. Tested in Task 3.
3. **`Conn` = 1 written, then the inverter is STARTING for about 57 s** and may refuse or ignore cap writes. Expected: no crash and no spin; the cap is forced every step until the inverter reports MPPT. Tested in Task 3.
4. **A target that is NaN, infinite or negative** reaching the writer. Expected: treated as no data (0 % cap), never written as a garbage register. Tested in Task 3.
5. **The writer connects to an inverter already in standby** (an earlier run left `Conn` = 0). Expected: it adopts standby and resumes when data arrives. Tested in Task 3.

---

### Task 1: Move the Modbus code into the package

**Files:**
- Modify: `server/setpoint_server/config.py` (append constants)
- Create: `server/setpoint_server/fronius.py`, `server/setpoint_server/fronius_cli.py`
- Create: `server/tests/fake_inverter.py`, `server/tests/test_fronius.py` (moved from `tools/test_fronius_modbus.py`)
- Modify: `tools/fronius_modbus.py` (becomes a thin shim)
- Delete: `tools/test_fronius_modbus.py` (moved)

This is a refactor: the behaviour and the 30 existing tests stay the same, and one test is added for `Inverter.close()`. Also commits the spec and this plan.

**Interfaces:**
- Produces:
  - `setpoint_server.fronius`: `ModbusTcp(host, port, unit, timeout)` with `read`, `write_single`, `close`; `Inverter(modbus)` with `models`, `identity`, `wmax`, `controls`, `power`, `set_ac_cap`, `set_dc_target`, `clear_cap`, `set_conn`, `close`; `pct_register`, `ac_cap_for_dc`, `s16`, `scaled`, `STATES` and the offset constants. `controls()` returns `{"conn", "pct_raw", "pct", "rvrt_s", "ena", "sf"}`; `power()` returns `{"ac_w", "state", "mppt": [{"id", "name", "dc_a", "dc_v", "dc_w"}]}`.
  - `setpoint_server.fronius_cli.main(argv)`: the command line, as before.
  - `server/tests/fake_inverter.py`: `FakeInverter(with_123=True)` with `start()` (returns the port), `stop()`, `regs` (dict address to value), `writes` (list of `(address, value)`), `put(addr, values)`, `refuse_writes`, and the constants `M1, M103, M120, M121, M123, M160`.
  - `config.py`: `DC_PER_AC`, `DC_OFFSET_W`, `AC_SETTLE_W` and the writer constants (`INVERTER_TOPIC`, `INVERTER_PORT`, `INVERTER_UNIT`, `INVERTER_TIMEOUT_S`, `DEADBAND_W`, `REFRESH_S`, `STANDBY_AFTER_S`, `MAILBOX_STALE_S`, `WRITER_PERIOD_S`, `BACKOFF_MIN_S`, `BACKOFF_MAX_S`).

- [ ] **Step 1: Create the branch and commit the spec and this plan**

```bash
git switch -c inverter-writer
git add docs/superpowers/specs/2026-09-30-inverter-writer-design.md docs/superpowers/plans/2026-09-30-inverter-writer.md
git commit -m "$(cat <<'EOF'
Add design spec and plan for the inverter writer

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 2: Move the tests and extract the fake inverter**

```bash
git mv tools/test_fronius_modbus.py server/tests/test_fronius.py
python3 - <<'EOF'
p = "server/tests/test_fronius.py"
src = open(p).read()
start = src.index("# Wire addresses of the real unit's model chain")
end = src.index("class Base(unittest.TestCase):")
fake = src[start:end]
fake = fake.replace("    def start(self):", "    def start(self, port=0):")
fake = fake.replace('socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)',
                    'socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)')
assert "port=0" in fake and '("127.0.0.1", port)' in fake

fake_header = '''"""A fake GEN24 that speaks Modbus TCP (function codes 3 and 6) on localhost.

The SunSpec layout is copied from the real GEN24 12.0 Plus SC read on 2026-09-30. It records every
write. Used by the client, writer and bench tests; nothing here touches the real inverter.
"""

import socketserver
import struct
import threading

'''
open("server/tests/fake_inverter.py", "w").write(fake_header + fake.rstrip() + "\n")

test_header = '''"""Tests for the Fronius Modbus client and command line, against a fake inverter on localhost.

Nothing here touches the real inverter.
"""

import contextlib
import io
import unittest

from fake_inverter import FakeInverter, M1, M103, M123, M160
from setpoint_server import fronius as fm
from setpoint_server import fronius_cli as cli


'''
body = src[end:].replace("fm.main(", "cli.main(")
open(p, "w").write(test_header + body)
EOF
grep -n "^class\|^from\|^import" server/tests/test_fronius.py server/tests/fake_inverter.py
```

Expected: `test_fronius.py` lists the imports above and the classes `Base`, `PureFunctionsTest`, `ModbusTest`, `ReadTest`, `WriteTest`, `CliTest`; `fake_inverter.py` lists `FakeInverter`.

- [ ] **Step 3: Add a test for `Inverter.close()`**

Append to `server/tests/test_fronius.py`, just before the final `if __name__ == "__main__":` block:

```python
class CloseTest(Base):
    def test_close_releases_the_connection(self):
        self.inv.close()
        with self.assertRaises(OSError):
            self.inv.controls()
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_fronius.py' 2>&1 | grep -E "Error"`
Expected: `ImportError: cannot import name 'fronius' from 'setpoint_server'` (the module does not exist yet).

- [ ] **Step 5: Add the constants to `config.py`**

```bash
cat >> server/setpoint_server/config.py <<'CONFIG_END'

# The inverter writer (see docs/superpowers/specs/2026-09-30-inverter-writer-design.md).
INVERTER_TOPIC = "evsim/inverter"    # out: inverter status, QoS 0, not retained
INVERTER_PORT = 502                  # Modbus TCP
INVERTER_UNIT = 1
INVERTER_TIMEOUT_S = 3               # connect and read timeout toward the inverter
DEADBAND_W = 100                     # a new DC target is written only if it moved this much
REFRESH_S = 10                       # re-commit the current cap this often
STANDBY_AFTER_S = 120                # no data for this long: Conn = 0
MAILBOX_STALE_S = 3                  # a target older than this counts as no data
WRITER_PERIOD_S = 1                  # the writer's step
BACKOFF_MIN_S = 1                    # retry delay toward an unreachable inverter
BACKOFF_MAX_S = 5

# Calibration of the inverter, fitted on 2026-09-30 from 8 AC caps between 1 and 7.3 kW: the DC power it
# takes from the charger side is DC = DC_PER_AC * AC + DC_OFFSET_W (worst residual 0.9 % at 2 kW, else
# under 0.25 %). The AC output also settles about AC_SETTLE_W below the cap.
# See memory/evsim-inverter-fronius.md.
DC_PER_AC = 1.0303
DC_OFFSET_W = 6.3
AC_SETTLE_W = 3.0
CONFIG_END
```

- [ ] **Step 6: Split the tool into the two package modules**

```bash
python3 - <<'SPLIT_END'
tool = open("tools/fronius_modbus.py").read()

doc_start = tool.index('"""')
doc_end = tool.index('"""', doc_start + 3) + 3
docstring = tool[doc_start:doc_end]

marker = tool.index("# -- command line")
head = tool[tool.index("DEFAULT_HOST"):marker]
tail = tool[marker:]

# the calibration constants live in config.py now
cal_start = head.index("# Calibration, fitted")
cal_end = head.index("STATES = {")
head = head[:cal_start] + head[cal_end:]

old = "        self._models = None\n\n    def models(self):"
new = "        self._models = None\n\n    def close(self):\n        self._m.close()\n\n    def models(self):"
assert head.count(old) == 1
head = head.replace(old, new)

fronius_doc = '''"""Modbus TCP client for the Fronius GEN24 (SunSpec): reads, and the writes that cap the export.

Standard library only. The only registers ever written are WMaxLimPct, WMaxLim_Ena and Conn of
model 123, each range-checked. A cap is always committed with an Ena write, because the inverter
applies a new WMaxLimPct only then. Scale factors are re-read with every read. Background and
calibration: memory/evsim-inverter-fronius.md.
"""
'''
fronius = (fronius_doc + "\nimport socket\nimport struct\n\nfrom .config import AC_SETTLE_W, DC_OFFSET_W, DC_PER_AC\n\n"
           + head.rstrip() + "\n")
open("server/setpoint_server/fronius.py", "w").write(fronius)

cli = (docstring + "\n\nimport argparse\nimport sys\nimport time\n\n"
       "from .fronius import (\n    DEFAULT_HOST, DEFAULT_PORT, DEFAULT_UNIT, M123_CONN, M123_ENA, M123_PCT, M123_PCT_SF,\n"
       "    M123_RVRT, STATES, Inverter, ModbusTcp, ac_cap_for_dc,\n)\n\n\n" + tail)
open("server/setpoint_server/fronius_cli.py", "w").write(cli)
SPLIT_END
python3 -c "import ast,sys; [ast.parse(open(f).read()) for f in ('server/setpoint_server/fronius.py','server/setpoint_server/fronius_cli.py')]; print('both parse')"
```

Expected: `both parse`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_fronius.py' -v 2>&1 | tail -6`
Expected: `Ran 31 tests` and `OK` (the 30 moved tests plus the new `close` test). Then the whole suite, which takes about a minute because of the bench tests:
`cd server && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
Expected: `Ran 74 tests` and `OK`.

- [ ] **Step 8: Replace the tool with a thin shim and check it**

Overwrite `tools/fronius_modbus.py`:

```python
#!/usr/bin/env python3
"""Read and cap the Fronius GEN24 over Modbus TCP. Run `fronius_modbus.py --help`.

The implementation lives in server/setpoint_server/fronius.py and fronius_cli.py, so the setpoint
server and this tool share one copy of the register logic. Commands: probe, status, watch (read-only),
set, off, conn. Background: memory/evsim-inverter-fronius.md.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from setpoint_server.fronius_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
```

Run: `python3 tools/fronius_modbus.py --help | head -3`
Expected: the usage line listing `probe,status,watch,set,off,conn`.

Run (read-only, needs the lab network; skip if unreachable): `python3 tools/fronius_modbus.py status`
Expected: one line starting `Conn=1  WMaxLimPct=...`. If the network is down you get `error: cannot reach 192.168.21.120:502`, which is also correct behaviour.

- [ ] **Step 9: Commit**

```bash
git add server/setpoint_server/config.py server/setpoint_server/fronius.py server/setpoint_server/fronius_cli.py \
        server/tests/fake_inverter.py server/tests/test_fronius.py tools/fronius_modbus.py
git commit -m "$(cat <<'EOF'
Move the Fronius Modbus code into the server package

The client, its command line and the fake inverter now live under server/,
so the setpoint server and tools/fronius_modbus.py share one copy of the
register logic. The tool is a thin shim. The calibration constants move to
config.py.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The controller (pure state machine)

**Files:**
- Create: `server/setpoint_server/inverter_control.py`
- Test: `server/tests/test_inverter_control.py`

**Interfaces:**
- Consumes: nothing (pure logic).
- Produces:
  - `Action = namedtuple("Action", "kind dc_w")` with `kind` one of `"cap"`, `"standby"`, `"resume"`; `dc_w` is the DC target in watts for `"cap"` and `None` otherwise.
  - `Controller(deadband_w, refresh_s, standby_after_s)` with:
    - `step(now, target_w, force=False) -> list[Action]`: `target_w` is watts or `None` (no data). `force=True` writes the cap regardless of the dead-band and the refresh timer.
    - `reset(standby=False)`: forget what was written, so the next step re-asserts; `standby=True` when the inverter is known to be in standby. It keeps the outage clock.
    - `mode`: `"unknown"`, `"capped"`, `"zero"` or `"standby"`; `last_target_w`: the last written DC target, or `None`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_inverter_control.py`:

```python
import unittest

from setpoint_server.inverter_control import Action, Controller


def cap(watts):
    return Action("cap", watts)


STANDBY = Action("standby", None)
RESUME = Action("resume", None)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.c = Controller(deadband_w=100, refresh_s=10, standby_after_s=120)

    def to_standby(self):
        self.c.step(0, None)
        self.c.step(120, None)
        self.assertEqual(self.c.mode, "standby")

    def test_the_first_valid_target_is_written(self):
        self.assertEqual(self.c.step(0, 4000.0), [cap(4000.0)])
        self.assertEqual((self.c.mode, self.c.last_target_w), ("capped", 4000.0))

    def test_starting_without_data_asserts_a_zero_cap(self):
        self.assertEqual(self.c.step(0, None), [cap(0.0)])
        self.assertEqual(self.c.mode, "zero")

    def test_no_data_after_a_valid_target_caps_at_zero_at_once(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, None), [cap(0.0)])
        self.assertEqual(self.c.mode, "zero")

    def test_inside_the_dead_band_nothing_is_written(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4099.9), [])
        self.assertEqual(self.c.step(2, 3900.1), [])

    def test_at_the_dead_band_a_write_happens(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4100.0), [cap(4100.0)])

    def test_drift_is_measured_from_the_last_written_target(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4060.0), [])
        self.assertEqual(self.c.step(2, 4110.0), [cap(4110.0)])

    def test_zero_versus_non_zero_always_writes(self):
        self.c.step(0, 0.0)
        self.assertEqual(self.c.step(1, 50.0), [cap(50.0)])
        self.assertEqual(self.c.step(2, 0.0), [cap(0.0)])

    def test_the_cap_is_re_committed_every_refresh_period(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(9.9, 4000.0), [])
        self.assertEqual(self.c.step(10.0, 4000.0), [cap(4000.0)])
        self.assertEqual(self.c.step(19.9, 4000.0), [])
        self.assertEqual(self.c.step(20.0, 4000.0), [cap(4000.0)])

    def test_the_zero_cap_is_re_committed_during_an_outage(self):
        self.c.step(0, None)
        self.assertEqual(self.c.step(9.9, None), [])
        self.assertEqual(self.c.step(10.0, None), [cap(0.0)])

    def test_standby_comes_only_after_the_outage_time(self):
        self.c.step(0, None)
        for t in range(1, 120):
            self.assertNotIn(STANDBY, self.c.step(t, None))
        self.assertEqual(self.c.step(120, None)[-1], STANDBY)
        self.assertEqual(self.c.mode, "standby")

    def test_standby_is_silent(self):
        self.to_standby()
        self.assertEqual(self.c.step(121, None), [])
        self.assertEqual(self.c.step(500, None), [])

    def test_data_returning_from_standby_resumes_then_caps(self):
        self.to_standby()
        self.assertEqual(self.c.step(200, 4000.0), [RESUME, cap(4000.0)])
        self.assertEqual(self.c.mode, "capped")

    def test_a_valid_zero_is_not_an_outage(self):
        self.c.step(0, 0.0)
        for t in range(1, 400):
            self.assertNotIn(STANDBY, self.c.step(t, 0.0))
        self.assertEqual(self.c.mode, "capped")

    def test_the_outage_clock_restarts_when_data_returns(self):
        self.c.step(0, None)
        self.c.step(100, None)
        self.c.step(101, 4000.0)
        self.c.step(102, None)                       # a new outage starts here
        self.assertNotIn(STANDBY, self.c.step(200, None))   # only 98 s since 102
        self.assertIn(STANDBY, self.c.step(222, None))

    def test_force_writes_even_inside_the_dead_band(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4000.0, force=True), [cap(4000.0)])

    def test_reset_makes_the_next_step_re_assert(self):
        self.c.step(0, 4000.0)
        self.c.reset()
        self.assertEqual(self.c.mode, "unknown")
        self.assertEqual(self.c.step(1, 4000.0), [cap(4000.0)])

    def test_reset_into_standby_resumes_when_data_arrives(self):
        self.c.reset(standby=True)
        self.assertEqual(self.c.step(0, None), [])
        self.assertEqual(self.c.step(1, 3000.0), [RESUME, cap(3000.0)])

    def test_reset_keeps_the_outage_clock(self):
        self.c.step(0, None)
        self.c.step(60, None)
        self.c.reset()
        self.assertIn(STANDBY, self.c.step(120, None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_inverter_control.py' 2>&1 | grep -E "Error"`
Expected: `ModuleNotFoundError: No module named 'setpoint_server.inverter_control'`.

- [ ] **Step 3: Write the implementation**

Create `server/setpoint_server/inverter_control.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_inverter_control.py' -v 2>&1 | tail -5`
Expected: `Ran 18 tests` and `OK`.

- [ ] **Step 5: Commit**

```bash
git add server/setpoint_server/inverter_control.py server/tests/test_inverter_control.py
git commit -m "$(cat <<'EOF'
Add the inverter controller, a pure state machine

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The writer thread

**Files:**
- Modify: `server/tests/fake_inverter.py` (refusable addresses, closable connections)
- Create: `server/setpoint_server/inverter_writer.py`
- Test: `server/tests/test_inverter_writer.py`
- Modify: `docs/superpowers/specs/2026-09-30-inverter-writer-design.md` (one sentence, see step 7)

**Interfaces:**
- Consumes: `Inverter`, `ModbusTcp`, `ac_cap_for_dc` (Task 1); `Controller`, `Action` (Task 2); the `config` constants (Task 1).
- Produces: `InverterWriter(host, port=config.INVERTER_PORT, unit=config.INVERTER_UNIT, timeout_s=config.INVERTER_TIMEOUT_S, publish=None, controller=None, clock=time.monotonic, utcnow=<UTC now>, period_s=config.WRITER_PERIOD_S)` with:
  - `set_target(watts_or_None)`: hand over the latest DC target, or `None` for no data. Thread-safe, never touches the network. A NaN, infinite or negative value is logged and treated as `None`.
  - `step_once()`: one iteration, synchronously: read the mailbox (older than `MAILBOX_STALE_S` counts as `None`), connect if needed, run the controller and perform its actions, read the inverter's power, and call `publish(json_text)` with the status message. Never raises: any error closes the connection, resets the controller, and is reported as `unreachable`.
  - `start()`: run `step_once()` every `period_s` on a daemon thread.
  - `stop()`: stop the thread, then write the 0 % cap (value, then `Ena` = 1) and close. Safe to call more than once, and without `start()`.
  - `fake_inverter.FakeInverter` gains `refuse` (a set of addresses whose writes get exception 3) and `stop()` now also closes open connections; `start(port)` can rebind a port.

- [ ] **Step 1: Extend the fake inverter**

```bash
python3 - <<'PYEDIT'
p = "server/tests/fake_inverter.py"
s = open(p).read()


def sub(old, new):
    global s
    assert s.count(old) == 1, old
    s = s.replace(old, new)


sub("import socketserver\n", "import socket\nimport socketserver\n")
sub("        self.refuse_writes = False  # answer writes with Modbus exception 3\n",
    "        self.refuse_writes = False  # answer every write with Modbus exception 3\n"
    "        self.refuse = set()         # addresses whose writes get exception 3\n"
    "        self.clients = []           # open connections, closed by stop()\n")
sub("            def handle(self):\n                while True:\n",
    "            def handle(self):\n                fake.clients.append(self.request)\n                while True:\n")
sub("if fake.refuse_writes or addr not in fake.regs:",
    "if fake.refuse_writes or addr in fake.refuse or addr not in fake.regs:")
sub("    def stop(self):\n        self._server.shutdown()\n        self._server.server_close()\n",
    "    def stop(self):\n        for client in list(self.clients):\n            try:\n                client.shutdown(socket.SHUT_RDWR)\n            except OSError:\n                pass\n"
    "        self._server.shutdown()\n        self._server.server_close()\n")
open(p, "w").write(s)
PYEDIT
cd server && .venv/bin/python -m unittest discover -s tests -p 'test_fronius.py' 2>&1 | tail -3
```

Expected: `Ran 31 tests` and `OK` (the existing tests still pass with the extended fake).

- [ ] **Step 2: Write the failing tests**

Create `server/tests/test_inverter_writer.py`:

```python
import json
import logging
import socket
import threading
import time
import unittest
from datetime import datetime, timezone

from fake_inverter import FakeInverter, M103, M123
from setpoint_server import config
from setpoint_server.inverter_writer import InverterWriter

NOW = datetime(2026, 9, 30, 13, 42, 7, tzinfo=timezone.utc)
CONN, PCT, ENA = M123 + 2, M123 + 3, M123 + 7
ST = M103 + 36           # SunSpec state register: 4 = MPPT, 3 = STARTING


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class WriterTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.fake = FakeInverter()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.clock = FakeClock()
        self.status = []
        self.writer = InverterWriter("127.0.0.1", self.port, timeout_s=1, publish=self.status.append,
                                     clock=self.clock, utcnow=lambda: NOW)
        self.addCleanup(self.writer.stop)

    def last(self):
        return json.loads(self.status[-1])

    def test_a_target_is_written_committed_and_reported(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(PCT, 3233), (ENA, 1)])
        s = self.last()
        self.assertEqual((s["mode"], s["target_dc_w"], s["cap_ac_w"]), ("capped", 4000.0, 3879.2))
        self.assertEqual((s["dc_w"], s["ac_w"], s["inverter_state"], s["error"]), (7272, 7047, 4, None))
        self.assertEqual(list(s), ["mode", "target_dc_w", "cap_ac_w", "dc_w", "ac_w", "inverter_state", "error", "ts"])
        self.assertEqual(s["ts"], "2026-09-30T13:42:07Z")

    def test_starting_with_no_data_asserts_a_zero_cap(self):
        self.writer.step_once()            # nothing was ever set
        self.assertEqual(self.fake.writes, [(PCT, 0), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "zero")

    def test_a_target_inside_the_dead_band_is_not_written(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        del self.fake.writes[:]
        self.clock.t += 1
        self.writer.set_target(4050.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [])

    def test_a_stale_target_counts_as_no_data(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        del self.fake.writes[:]
        self.clock.t += config.MAILBOX_STALE_S + 1     # the server stopped refreshing it
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(PCT, 0), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "zero")

    def test_nan_infinite_and_negative_targets_count_as_no_data(self):
        for bad in (float("nan"), float("inf"), -5.0):
            with self.subTest(bad=bad):
                del self.fake.writes[:]
                self.writer.set_target(4000.0)
                self.writer.step_once()
                self.clock.t += 1
                self.writer.set_target(bad)
                self.writer.step_once()
                self.assertEqual(self.fake.writes[-2:], [(PCT, 0), (ENA, 1)])
                self.clock.t += 1

    def test_a_dead_inverter_is_reported_and_the_cap_re_asserted_when_it_returns(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.fake.stop()                                # closes the connection too
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        s = self.last()
        self.assertEqual(s["mode"], "unreachable")
        self.assertIsNotNone(s["error"])
        # it comes back on the same port with its cap gone (as after a reset)
        fake2 = FakeInverter()
        fake2.start(port=self.port)
        self.addCleanup(fake2.stop)
        self.clock.t += 2                               # past the backoff
        self.writer.step_once()
        self.assertEqual(fake2.writes, [(PCT, 3233), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "capped")

    def test_a_failed_commit_is_re_asserted_after_reconnecting(self):
        self.fake.refuse = {ENA}                        # the value is written, the commit refused
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.last()["mode"], "unreachable")
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (3233, 0))
        self.fake.refuse = set()
        self.clock.t += 2
        self.writer.step_once()
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (3233, 1))
        self.assertEqual(self.last()["mode"], "capped")

    def test_an_inverter_found_in_standby_is_resumed_when_data_arrives(self):
        self.fake.regs[CONN] = 0
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(CONN, 1), (PCT, 3233), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "capped")

    def test_a_cap_is_forced_every_step_while_the_inverter_is_starting(self):
        self.fake.regs[CONN] = 0
        self.fake.put(ST, [3])                          # STARTING, as for about 24 s after Conn = 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.last()["mode"], "starting")
        del self.fake.writes[:]
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(PCT, 3233), (ENA, 1)])   # forced, and no second resume
        self.assertEqual(self.last()["mode"], "starting")
        self.fake.put(ST, [4])                          # the inverter is running
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.last()["mode"], "capped")
        del self.fake.writes[:]
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [])          # back to the dead-band rule

    def test_an_outage_ends_in_standby_and_data_resumes(self):
        self.writer.step_once()                         # no data: zero cap
        self.clock.t += config.STANDBY_AFTER_S
        self.writer.step_once()
        self.assertEqual(self.fake.regs[CONN], 0)
        self.assertEqual(self.last()["mode"], "standby")
        self.clock.t += 5
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual((self.fake.regs[CONN], self.fake.regs[PCT]), (1, 3233))

    def test_stop_writes_a_zero_cap(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.writer.stop()
        self.assertEqual(self.fake.writes[-2:], [(PCT, 0), (ENA, 1)])

    def test_a_stalled_inverter_does_not_block_the_caller(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        status = []
        writer = InverterWriter("127.0.0.1", listener.getsockname()[1], timeout_s=1, publish=status.append)
        thread = threading.Thread(target=writer.step_once)   # accepted by the backlog, never answered
        thread.start()
        time.sleep(0.2)
        started = time.monotonic()
        writer.set_target(1000.0)
        self.assertLess(time.monotonic() - started, 0.1)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(json.loads(status[-1])["mode"], "unreachable")

    def test_the_thread_applies_targets_and_stops_with_a_zero_cap(self):
        writer = InverterWriter("127.0.0.1", self.port, timeout_s=1, publish=self.status.append, period_s=0.05)
        writer.set_target(4000.0)
        writer.start()
        deadline = time.monotonic() + 3
        while self.fake.regs[PCT] != 3233 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (3233, 1))
        writer.stop()
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (0, 1))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_inverter_writer.py' 2>&1 | grep -E "Error"`
Expected: `ModuleNotFoundError: No module named 'setpoint_server.inverter_writer'`.

- [ ] **Step 4: Write the implementation**

Create `server/setpoint_server/inverter_writer.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_inverter_writer.py' -v 2>&1 | tail -6`
Expected: `Ran 13 tests` and `OK`. If `test_a_dead_inverter...` fails at the `unreachable` assertion, the fake's `stop()` did not close the open connection: recheck step 1.

- [ ] **Step 6: Run the whole suite**

Run: `cd server && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
Expected: `Ran 105 tests` and `OK` (92 plus 13).

- [ ] **Step 7: Align the spec with what the writer does**

The spec says a read-back mismatch is "retried once". The writer instead closes the connection, reports the error, and re-asserts on the next step after the backoff, which covers a partial write as well. Update the sentence:

```bash
python3 - <<'PYEDIT'
p = "docs/superpowers/specs/2026-09-30-inverter-writer-design.md"
s = open(p).read()
old = "- **Read back every write.** A mismatch is retried once, then reported in the status and the error field."
new = ("- **Read back every write.** A failed write or a read-back mismatch closes the connection, is reported in\n"
       "  the status and the error field, and is re-asserted on reconnecting (after a 1 to 5 s backoff), which\n"
       "  also repairs a write that died half way.")
assert s.count(old) == 1
open(p, "w").write(s.replace(old, new))
PYEDIT
```

- [ ] **Step 8: Commit**

```bash
git add server/tests/fake_inverter.py server/setpoint_server/inverter_writer.py server/tests/test_inverter_writer.py \
        docs/superpowers/specs/2026-09-30-inverter-writer-design.md
git commit -m "$(cat <<'EOF'
Add the inverter writer thread

It takes one DC target per second through a one-slot mailbox, has the
controller decide what to write, performs it over Modbus, and publishes the
inverter's status. Any error closes the connection and re-asserts the cap
on reconnecting; a stopped writer leaves a 0 percent cap.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire the writer into the server

**Files:**
- Modify: `server/setpoint_server/server.py`
- Modify: `server/setpoint_server/__main__.py`
- Test: `server/tests/test_server.py`

**Interfaces:**
- Consumes: `InverterWriter` (Task 3), through `set_target(watts_or_None)` and `stop()` only; `Controller` (Task 2).
- Produces:
  - `SetpointServer(client, clock=time.monotonic, utcnow=<UTC now>, writer=None)`: `tick()` calls `writer.set_target(mean)` (or `None` when the window is empty) whether or not MQTT is connected; `shutdown()` calls `writer.stop()` after the `server_offline` publish and before disconnecting.
  - Command line: `--inverter HOST` (Modbus off unless given), `--inverter-port` (default 502), `--inverter-unit` (default 1), `--standby-after SECONDS` (default 120).

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_server.py`, before the final `if __name__ == "__main__":` block:

```python
class FakeWriter:
    """Stands in for the InverterWriter: records what the server hands it."""

    def __init__(self, client):
        self._client = client
        self.targets = []
        self.published_when_stopped = None
        self.disconnected_when_stopped = None

    def set_target(self, watts):
        self.targets.append(watts)

    def stop(self):
        self.published_when_stopped = list(self._client.published)
        self.disconnected_when_stopped = self._client.disconnected


class WriterIntegrationTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.client = FakeClient()
        self.clock = FakeClock()
        self.writer = FakeWriter(self.client)
        self.server = SetpointServer(self.client, clock=self.clock, utcnow=lambda: NOW, writer=self.writer)

    def feed(self, payload):
        self.client.on_message(self.client, None, message(payload))

    def test_tick_hands_the_mean_to_the_writer(self):
        self.feed(b'{"setpoint_w":1000}')
        self.clock.t = 100.2
        self.feed(b'{"setpoint_w":2000}')
        self.clock.t = 100.4
        self.server.tick()
        self.assertEqual(self.writer.targets, [1500.0])

    def test_no_data_hands_none_to_the_writer(self):
        self.server.tick()
        self.assertEqual(self.writer.targets, [None])

    def test_the_writer_is_fed_even_when_mqtt_is_down(self):
        self.client.connected = False
        self.feed(b'{"setpoint_w":1000}')
        self.server.tick()
        self.assertEqual(self.writer.targets, [1000.0])
        self.assertEqual(self.client.published, [])

    def test_shutdown_stops_the_writer_after_the_offline_message_and_before_disconnecting(self):
        self.server.shutdown()
        self.assertEqual(self.writer.published_when_stopped[-1][1], offline_payload(config.WINDOW_S))
        self.assertFalse(self.writer.disconnected_when_stopped)
        self.assertTrue(self.client.disconnected)

    def test_shutdown_stops_the_writer_even_when_mqtt_is_down(self):
        self.client.connected = False
        self.server.shutdown()
        self.assertEqual(self.writer.published_when_stopped, [])
        self.assertTrue(self.client.disconnected)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_server.py' 2>&1 | grep -E "TypeError|FAILED|Ran"`
Expected: `TypeError: SetpointServer.__init__() got an unexpected keyword argument 'writer'` in the five new tests, and `FAILED`.

- [ ] **Step 3: Update `server.py`**

```bash
python3 - <<'PYEDIT'
p = "server/setpoint_server/server.py"
s = open(p).read()


def sub(old, new):
    global s
    assert s.count(old) == 1, old
    s = s.replace(old, new)


sub("    def __init__(self, client, clock=time.monotonic, utcnow=_utcnow):\n        self._client = client\n",
    "    def __init__(self, client, clock=time.monotonic, utcnow=_utcnow, writer=None):\n        self._client = client\n        self._writer = writer\n")
sub("        payload = format_setpoint(count, mean, config.WINDOW_S, self._utcnow())\n        if not self._client.is_connected():\n",
    "        payload = format_setpoint(count, mean, config.WINDOW_S, self._utcnow())\n"
    "        if self._writer is not None:\n"
    "            # The inverter is driven from the average whether or not MQTT is up.\n"
    "            self._writer.set_target(mean if count > 0 else None)\n"
    "        if not self._client.is_connected():\n")
sub("                log.warning(\"could not publish server_offline: %s\", exc)\n        self._client.disconnect()\n",
    "                log.warning(\"could not publish server_offline: %s\", exc)\n"
    "        if self._writer is not None:\n"
    "            self._writer.stop()          # leaves the inverter at a 0 percent cap\n"
    "        self._client.disconnect()\n")
open(p, "w").write(s)
PYEDIT
cd server && .venv/bin/python -m unittest discover -s tests -p 'test_server.py' 2>&1 | tail -3
```

Expected: `Ran 18 tests` and `OK` (13 existing plus the 5 new).

- [ ] **Step 4: Replace `__main__.py`**

Overwrite `server/setpoint_server/__main__.py`:

```python
"""Entry point: python -m setpoint_server [--host H] [--port P] [--inverter HOST]"""

import argparse
import logging
import signal
import sys

import paho.mqtt.client as mqtt

from . import config
from .inverter_control import Controller
from .inverter_writer import InverterWriter
from .server import SetpointServer


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m setpoint_server",
        description="Average the board's evsim/cp setpoint over 1 s and publish it on evsim/setpoint.",
    )
    parser.add_argument("--host", default=config.BROKER_HOST, help="broker address (default %(default)s)")
    parser.add_argument("--port", type=int, default=config.BROKER_PORT, help="broker port (default %(default)s)")
    parser.add_argument("--inverter", metavar="HOST",
                        help="also drive the Fronius inverter at HOST over Modbus TCP; without this option "
                             "the inverter is never touched")
    parser.add_argument("--inverter-port", type=int, default=config.INVERTER_PORT,
                        help="inverter Modbus port (default %(default)s)")
    parser.add_argument("--inverter-unit", type=int, default=config.INVERTER_UNIT,
                        help="inverter Modbus unit ID (default %(default)s)")
    parser.add_argument("--standby-after", type=float, default=config.STANDBY_AFTER_S,
                        help="seconds without data before the inverter goes to standby (default %(default)s)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.CLIENT_ID)
    client.reconnect_delay_set(min_delay=config.RECONNECT_MIN_S, max_delay=config.RECONNECT_MAX_S)

    writer = None
    if args.inverter:
        def publish_status(payload):
            if client.is_connected():
                client.publish(config.INVERTER_TOPIC, payload, qos=0)

        writer = InverterWriter(
            args.inverter, args.inverter_port, args.inverter_unit, publish=publish_status,
            controller=Controller(config.DEADBAND_W, config.REFRESH_S, args.standby_after),
        )
    server = SetpointServer(client, writer=writer)

    signal.signal(signal.SIGINT, lambda signum, frame: server.request_stop())
    signal.signal(signal.SIGTERM, lambda signum, frame: server.request_stop())

    # connect_async + loop_start: an unreachable broker at startup is retried in
    # the background instead of raising, and the loop reconnects after a drop.
    client.connect_async(args.host, args.port, keepalive=config.KEEPALIVE_S)
    client.loop_start()
    if writer is not None:
        writer.start()
    try:
        server.run()
    finally:
        if writer is not None:
            writer.stop()             # a no-op when run() already stopped it
        client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Smoke-test the flags and run the whole suite**

Run: `cd server && .venv/bin/python -m setpoint_server --help | grep -E "inverter|standby"`
Expected: the `--inverter HOST`, `--inverter-port`, `--inverter-unit` and `--standby-after` lines.

Run: `cd server && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
Expected: `Ran 110 tests` and `OK` (105 plus the 5 new server tests; about a minute).

- [ ] **Step 6: Commit**

```bash
git add server/setpoint_server/server.py server/setpoint_server/__main__.py server/tests/test_server.py
git commit -m "$(cat <<'EOF'
Drive the inverter from the setpoint server behind --inverter

The tick hands the averaged setpoint to the writer whether or not MQTT is
up, and shutdown stops the writer, leaving a 0 percent cap, after the
server_offline message. Without --inverter the inverter is never touched.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: End-to-end bench tests with a fake inverter

**Files:**
- Modify: `server/tests/test_bench.py`

**Interfaces:**
- Consumes: the running server (`python -m setpoint_server --port P --inverter 127.0.0.1 --inverter-port Q [--standby-after S]`), a private Mosquitto, and `fake_inverter.FakeInverter` (Tasks 1 and 3).
- Produces: five bench tests, in the same style as the existing ones.

These run code that already exists, so they are expected to pass first time; a failure is a real finding about the server, and the spec is the arbiter. Do not loosen an assertion to make one pass without saying so.

- [ ] **Step 1: Add the tests**

```bash
python3 - <<'PYEDIT'
p = "server/tests/test_bench.py"
s = open(p).read()


def sub(old, new):
    global s
    assert s.count(old) == 1, old
    s = s.replace(old, new)


sub("import unittest\n\ntry:\n", "import unittest\n\nfrom fake_inverter import FakeInverter, M123\n\ntry:\n")
sub('TOPIC_SETPOINT = "evsim/setpoint"\n',
    'TOPIC_SETPOINT = "evsim/setpoint"\nTOPIC_INVERTER = "evsim/inverter"\nCONN, PCT, ENA = M123 + 2, M123 + 3, M123 + 7\n')
sub('''    def __init__(self, port):
        self._lock = threading.Lock()
        self._messages = []  # (monotonic time, retain, decoded JSON)
        self._client = new_client()
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(TOPIC_SETPOINT, qos=1)''',
    '''    def __init__(self, port, topic=TOPIC_SETPOINT):
        self._lock = threading.Lock()
        self._messages = []  # (monotonic time, retain, decoded JSON)
        self._client = new_client()
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(topic, qos=1)''')
sub('''    def start_server(self):
        log = open(self.logpath, "a")
        self.addCleanup(log.close)
        self.server = subprocess.Popen(
            [sys.executable, "-u", "-m", "setpoint_server", "--port", str(self.port)],''',
    '''    def start_server(self, *extra):
        log = open(self.logpath, "a")
        self.addCleanup(log.close)
        self.server = subprocess.Popen(
            [sys.executable, "-u", "-m", "setpoint_server", "--port", str(self.port)] + list(extra),''')

tests = '''
    # -- driving the inverter (against a fake one) ----------------------------------------

    def start_with_inverter(self, *extra):
        self.inverter = FakeInverter()
        inverter_port = self.inverter.start()
        self.addCleanup(self.inverter.stop)
        self.start_broker()
        self.start_clients()
        self.start_server("--inverter", "127.0.0.1", "--inverter-port", str(inverter_port), *extra)
        self.assertTrue(self.wait_for_log(READY, 10), self.log_text())

    def wait_until(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return predicate()

    def test_a_setpoint_becomes_a_cap_on_the_inverter(self):
        self.start_with_inverter()
        self.feeder.feed(4000, 3.5)
        self.assertTrue(self.wait_until(
            lambda: self.inverter.regs[PCT] == 3233 and self.inverter.regs[ENA] == 1, 4), self.log_text())

    def test_silence_gives_a_zero_cap_then_standby_and_data_resumes(self):
        self.start_with_inverter("--standby-after", "4")
        self.feeder.feed(4000, 2.5)
        self.assertTrue(self.wait_until(lambda: self.inverter.regs[PCT] == 3233, 4), self.log_text())
        self.assertTrue(self.wait_until(lambda: self.inverter.regs[PCT] == 0, 8), self.log_text())
        self.assertTrue(self.wait_until(lambda: self.inverter.regs[CONN] == 0, 12), self.log_text())
        self.feeder.feed(4000, 3.5)
        self.assertTrue(self.wait_until(
            lambda: self.inverter.regs[CONN] == 1 and self.inverter.regs[PCT] == 3233, 8), self.log_text())

    def test_the_inverter_status_is_published_once_a_second(self):
        self.start_with_inverter()
        status = Watcher(self.port, TOPIC_INVERTER)     # created once the broker is up, so it connects at once
        self.addCleanup(status.stop)
        time.sleep(0.5)
        self.feeder.feed(4000, 4.5)
        messages = status.snapshot()
        self.assertGreaterEqual(len(messages), 3, self.log_text())
        self.assertTrue(any(p["mode"] == "capped" and p["target_dc_w"] == 4000 and p["dc_w"] == 7272
                            for _, _, p in messages), messages[-1])

    def test_sigterm_leaves_the_inverter_at_a_zero_cap(self):
        self.start_with_inverter()
        self.feeder.feed(4000, 3.0)
        self.assertTrue(self.wait_until(lambda: self.inverter.regs[PCT] == 3233, 4), self.log_text())
        self.server.terminate()
        self.assertEqual(self.server.wait(timeout=8), 0)
        self.assertEqual((self.inverter.regs[PCT], self.inverter.regs[ENA]), (0, 1))

    def test_without_the_flag_no_inverter_status_appears(self):
        self.up()
        status = Watcher(self.port, TOPIC_INVERTER)
        self.addCleanup(status.stop)
        time.sleep(0.5)
        self.feeder.feed(4000, 3.0)
        self.assertGreater(len(self.watcher.snapshot()), 0)     # the server is running and publishing
        self.assertEqual(status.snapshot(), [])
'''
tail = '\n\nif __name__ == "__main__":\n    unittest.main()\n'
assert s.endswith(tail)
s = s[:-len(tail)] + "\n" + tests + tail
open(p, "w").write(s)
PYEDIT
```

- [ ] **Step 2: Run the new bench tests**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_bench.py' -v 2>&1 | tail -22`
Expected: 15 tests (the 10 existing plus 5 new), all `ok`, in about two minutes. On a failure, read the server log that the assertion prints before changing anything.

- [ ] **Step 3: Mutation-check two of them**

Break the server on purpose and confirm the matching test fails, then restore. Clear the bytecode cache each time (a same-size edit within the same second can leave stale bytecode).

```bash
cd server
sed -i '' 's/            inv.set_ac_cap(0)/            pass/' setpoint_server/inverter_writer.py
rm -rf setpoint_server/__pycache__
.venv/bin/python -B -m unittest discover -s tests -p 'test_bench.py' -k test_sigterm_leaves_the_inverter_at_a_zero_cap 2>&1 | grep -E "^(FAIL|OK|FAILED)"
git checkout -- setpoint_server/inverter_writer.py

sed -i '' 's/self._writer.set_target(mean if count > 0 else None)/pass/' setpoint_server/server.py
rm -rf setpoint_server/__pycache__
.venv/bin/python -B -m unittest discover -s tests -p 'test_bench.py' -k test_a_setpoint_becomes_a_cap_on_the_inverter 2>&1 | grep -E "^(FAIL|OK|FAILED)"
git checkout -- setpoint_server/server.py
rm -rf setpoint_server/__pycache__
git status --short | grep -v '^?? data/'
```

Expected: each mutated run prints `FAILED`, and the final `git status` shows only `server/tests/test_bench.py` as modified.

- [ ] **Step 4: Run the whole suite**

Run: `cd server && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
Expected: `Ran 115 tests` and `OK`.

- [ ] **Step 5: Commit**

```bash
git add server/tests/test_bench.py
git commit -m "$(cat <<'EOF'
Add end-to-end bench tests for driving the inverter

A fake inverter and a private Mosquitto check that a setpoint becomes a cap,
that silence gives a 0 percent cap and then standby, that data resumes, that
the status is published, that SIGTERM leaves a 0 percent cap, and that
nothing happens without --inverter.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Docs, and the live check with the real inverter

**Files:**
- Modify: `server/README.md`
- Modify: `memory/evsim-setpoint-server.md`
- Modify: `memory/evsim-inverter-fronius.md` (the tool's test command, and the live results)

**Interfaces:**
- Consumes: everything above.
- Produces: documentation, and measured answers to the spec's "still to measure" list.

- [ ] **Step 1: Document the flags**

Append to `server/README.md`:

```markdown

## Driving the inverter

By default the server only publishes the setpoint. To also drive the Fronius GEN24 over Modbus TCP:

    .venv/bin/python -m setpoint_server --inverter 192.168.21.120

The averaged setpoint is the DC power to take from the charger. The server caps the inverter's AC output so
that it takes about that much (calibration in `memory/evsim-inverter-fronius.md`), and publishes the inverter's
measured DC and AC power once a second on `evsim/inverter`.

- New targets are written only when they move by 100 W or more, and the cap is re-committed every 10 s.
- With no data the cap goes to 0 % at once (about 94 W of DC draw remains). After 120 s of no data
  (`--standby-after`) the inverter is put in standby (`Conn` = 0); coming back from standby takes about 57 s.
- On a clean stop (Ctrl-C) the server leaves the inverter at a 0 % cap.
- Known limit: if the link to the inverter dies while it holds a cap, the cap stays; the status shows
  `unreachable`. Design: `docs/superpowers/specs/2026-09-30-inverter-writer-design.md`.

Read or cap the inverter by hand with `tools/fronius_modbus.py` (`--help`).
```

- [ ] **Step 2: Update the notes**

```bash
python3 - <<'PYEDIT'
p = "memory/evsim-inverter-fronius.md"
s = open(p).read()
old = "one: `python3 -m unittest discover -s tools -p 'test_fronius*.py'`. Default host 192.168.21.120."
new = ("one: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_fronius*.py'`. Default host\n"
       "192.168.21.120. The code lives in `server/setpoint_server/fronius.py` and `fronius_cli.py`; the tool is a shim.")
assert s.count(old) == 1
open(p, "w").write(s.replace(old, new))

p = "memory/evsim-setpoint-server.md"
s = open(p).read()
s += """
## Driving the inverter (added 2026-09-30)

With `--inverter HOST` the server also caps the Fronius inverter over Modbus TCP so that its DC input from the
charger equals the setpoint; see [[evsim-inverter-fronius]] and
`docs/superpowers/specs/2026-09-30-inverter-writer-design.md`. Dead-band 100 W, refresh 10 s, 0 % cap on no
data, standby after 120 s, 0 % cap left on a clean stop. Status on `evsim/inverter`. Off unless the flag is given.
"""
open(p, "w").write(s)
PYEDIT
```

- [ ] **Step 3: Commit the docs**

```bash
git add server/README.md memory/evsim-setpoint-server.md memory/evsim-inverter-fronius.md
git commit -m "$(cat <<'EOF'
Document driving the inverter from the setpoint server

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Live check with the real inverter (STOP: ask the human partner first)**

**This step writes to the real inverter and needs the human partner present and agreeing.** It needs the lab
network, the lab Mosquitto on 127.0.0.1:1883 (`tools/mosquitto-lab.conf`), and the board publishing `evsim/cp` in
state C. If any of that is missing, skip the step and say so.

(a) Note the inverter's state before: `python3 tools/fronius_modbus.py status`.

(b) Start the server: `cd server && .venv/bin/python -m setpoint_server --inverter 192.168.21.120`.

(c) In another terminal watch both topics: `mosquitto_sub -v -t evsim/setpoint -t evsim/inverter`.
Expected within a few seconds: `evsim/inverter` messages with `mode` `capped`, `target_dc_w` close to the
board's setpoint, and `dc_w` within about 100 W of it (plus the measurement error noted in the calibration).

(d) Ask the human partner to change the AMTRON's current limit; the target and the cap should follow within a
few seconds. Then ask them to stop the board (or its Wi-Fi) for 5 s: `mode` should become `zero` and `dc_w` fall
to about 94 W; when the board returns, `capped` again.

(e) Press Ctrl-C on the server. Expected: `python3 tools/fronius_modbus.py status` shows `WMaxLimPct=0`, `Ena=1`.
Then, with the human partner's agreement, restore a sensible cap (`set WATTS`) or remove it (`off`).

(f) Measure the write rate the inverter tolerates. With the human partner's agreement, run this once (it
alternates two nearby caps once per second for 60 s, then restores 7050 W; adjust the values to a safe range
for the current charger offer first):

```bash
python3 - <<'PYRATE'
import sys, time
sys.path.insert(0, "server")
from setpoint_server.fronius import Inverter, ModbusTcp

inv = Inverter(ModbusTcp("192.168.21.120"))
t0, n, errors = time.monotonic(), 0, 0
while time.monotonic() - t0 < 60:
    try:
        inv.set_ac_cap(5000 if n % 2 == 0 else 5100)
    except Exception as exc:
        errors += 1
        print("error:", exc)
    n += 1
    time.sleep(max(0.0, n - (time.monotonic() - t0)))
inv.set_ac_cap(7050)
print(n, "writes,", errors, "errors")
PYRATE
```

Expected: 60 writes and 0 errors. Any errors mean 1 Hz is too fast: raise `REFRESH_S` and the dead-band accordingly and say so.

(g) Record the results (the answers to the spec's "still to measure" list that this step settled) in
`memory/evsim-inverter-fronius.md`, then commit:

```bash
git add memory/evsim-inverter-fronius.md
git commit -m "$(cat <<'EOF'
Record the live results of driving the inverter from the server

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review

- **Spec coverage:** the mode table and write rules (Tasks 2 and 3); the dead-band, refresh, standby time and stale mailbox (Task 2 and `config.py` in Task 1); the components and their responsibilities (Tasks 1 to 4); opt-in `--inverter` and its options (Task 4); the `evsim/inverter` status message (Task 3, checked end to end in Task 5); failure handling: unreachable, partial write, STARTING, invalid targets, standby found at connect (Task 3), stalled inverter (Task 3), MQTT down (Task 4); startup with no data and clean shutdown (Tasks 3 and 5); the testing list (Tasks 2, 3, 5 and 6); and "still to measure" (Task 6). The spec's retry sentence is aligned in Task 3 step 7.
- **Placeholders:** none. Every step has its code or exact command.
- **Type consistency:** `Action("cap"|"standby"|"resume", dc_w)`, `Controller.step(now, target_w, force)`, `Controller.reset(standby)`, `InverterWriter.set_target/step_once/start/stop`, `SetpointServer(..., writer=None)` and the config names are used with the same signatures in every task. The register value for a 4 000 W DC target is 3233 everywhere.
- **Review Focus:** items 1 to 5 map to `test_a_stalled_inverter_does_not_block_the_caller`, `test_a_failed_commit_is_re_asserted_after_reconnecting`, `test_a_cap_is_forced_every_step_while_the_inverter_is_starting`, `test_nan_infinite_and_negative_targets_count_as_no_data` and `test_an_inverter_found_in_standby_is_resumed_when_data_arrives`, all in Task 3.
- **Test counts** (for the run steps): 74 after Task 1, 92 after Task 2, 105 after Task 3, 110 after Task 4, 115 after Task 5.
