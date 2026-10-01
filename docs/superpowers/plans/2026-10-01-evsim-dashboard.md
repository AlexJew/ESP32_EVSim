# EVSim Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the local charger page into an EVSim dashboard: the inverter's MPPT 1 DC power (Modbus TCP), the board's `evsim/cp`, the setpoint topic and the server's `evsim/inverter` target (MQTT), and a 10-minute chart of the four power values. Everything added is read-only.

**Architecture:** Three new units inside `server/charger_web/`: `InverterReader` (one thread, one Modbus connection, one model-160 read per second), `Telemetry` (a paho subscriber that keeps the latest parsed value of each topic) and `History` (a 1 Hz sampler into a 600-frame ring). A small `Dashboard` object composes them and gives `web.py` two new GET endpoints. The page gets a power-chain row and a canvas chart in two new scripts, leaving `app.js` untouched. Pure parsers and a staleness helper keep the logic testable without a network.

**Tech Stack:** Python 3 standard library plus `paho-mqtt` (already in `server/requirements.txt`), `unittest` with the existing `FakeInverter`, plain HTML, CSS and browser JavaScript with a `<canvas>`. Node is used only to syntax-check the scripts and to run the chart's pure functions.

**Spec:** [docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md](../specs/2026-10-01-evsim-dashboard-design.md). It builds on [2026-10-01-charger-modbus-backend-design.md](../specs/2026-10-01-charger-modbus-backend-design.md) and [2026-09-30-inverter-writer-design.md](../specs/2026-09-30-inverter-writer-design.md). One addition to the spec's unit table: a `dashboard.py` that composes the three units (the spec says only that `__main__` builds them); Task 6 amends the spec.

Work on branch `feat/evsim-dashboard`, in the main checkout. **Another session may also use this repository**: run `git branch --show-current` before every commit and stop if it is not `feat/evsim-dashboard`. All commands run from `server/` unless stated. Always run Python as `.venv/bin/python -B` so stale `.pyc` files cannot hide a failure.

## Global Constraints

Every task's requirements include these, copied from the spec.

- **Read-only.** The inverter is only ever read (Modbus function code 3); nothing is ever published to MQTT. No new write path exists. The one write in the process stays the charger's register 1000.
- **Inverter: only the DC power.** MPPT 1's `DCW` from SunSpec model 160, **one read per second** (`INVERTER_POLL_PERIOD_S = 1`). The SunSpec chain is walked once per connection, only to find model 160. MPPT 1 is the module whose **id is 1**, not the first in the list. `dc_w = DCW x 10^DCW_SF`; a raw 65535 is `None`. Unit ID 1, port 502, default host `192.168.21.120` (`EVSIM_INVERTER_HOST`). Reconnect back-off 1 s up to 5 s.
- **MQTT:** client id `evsim-charger-web` (it must differ from `evsim-setpoint-server`), connect asynchronously, 1 to 5 s reconnect delay, **subscribe inside `on_connect`** at QoS 0, no will, never publish. Topics `evsim/cp`, the setpoint topic (`EVSIM_SETPOINT_TOPIC`, default `evsim/setpoint`; refused at start-up if empty, longer than 128 characters or containing `+`, `#` or whitespace) and `evsim/inverter`. Broker `EVSIM_MQTT_HOST` (default `127.0.0.1`), port 1883.
- **Parsers drop, never clamp:** a `setpoint_w` that is not a finite number from 0 to 12 000 is dropped. Parsers never raise.
- **Readings** are `{"value", "status", "updated_at"}`. `status` is `ok` below the limit, `stale` at or above it (or retained), `unavailable` with no value. Limits: **3 s** for MQTT values, the existing `STALE_AFTER_S` (**5 s**) for the inverter, which is also `stale` while its link is down.
- **Retained messages** are kept as the last known state but flagged stale and never enter the history.
- **History:** in memory, **600 frames at 1 Hz**. Frame: `{"t": <UTC epoch seconds>, "cp_w", "setpoint_w", "target_dc_w", "dc_w"}`. `cp_w` is the mean of the raw `evsim/cp` `setpoint_w` samples received since the previous tick; the other three are the reading's value only if its status is `ok`, else `None`. Missed frames are absent, not filled.
- **HTTP:** `GET /api/telemetry` and `GET /api/history`, loopback only, behind the same Host check and headers as the existing routes. `/api/charger/status` and the limit write are unchanged.
- **Page:** all text set with `textContent`, no `innerHTML`, no external libraries, a plain `<canvas>`; the heading becomes "EVSim"; a line is broken wherever a frame is missing or `None`.
- **Start-up order:** the page's port is bound first; a taken port starts nothing. The existing charger tests must still pass unchanged, except the one assertion on the heading text.

## Review Focus

Inputs and conditions the spec implies but the obvious tests would miss, most likely to bite first. Each has a test in the task named.

- **A wildcard or blank setpoint topic in the environment** (`evsim/#`, `""`): the process must refuse to start with a clear message, not subscribe to everything. Task 1.
- **A payload that is valid JSON but hostile:** `NaN`, `Infinity`, a 400-digit integer, `true`, a negative or 12 001 W value, a non-UTF-8 byte string, an array instead of an object. Each is dropped and the previous reading stays. Task 3.
- **The retained `server_offline` message** delivered on every subscribe, including after a reconnect when a fresh live value is already held: it must not overwrite the live value, and on its own it must show as stale. Task 4.
- **The broker is down at start-up, or the inverter is unreachable:** the page and the charger control still work, the affected cards say unavailable and the chart has a gap, and nothing raises in a thread. Tasks 2, 4 and 6.
- **A taken port or a bad setting must start no poller**, and `stop()` before `start()` must do nothing. Task 6.

---

### Task 1: Settings and the reading helper

**Files:**
- Modify: `server/charger_web/config.py` (overwritten with the new constants and three validators)
- Create: `server/charger_web/reading.py`
- Test: `server/tests/test_dashboard_config.py`

**Interfaces:**
- Produces:
  - `config.inverter_host() -> str`, `config.mqtt_host() -> str` (both raise `ValueError` for a blank value or one containing `/ ? # @`), `config.setpoint_topic() -> str` (raises `ValueError`).
  - Constants: `DEFAULT_INVERTER_HOST`, `INVERTER_PORT`, `INVERTER_UNIT`, `INVERTER_POLL_PERIOD_S`, `INVERTER_BACKOFF_MIN_S`, `INVERTER_BACKOFF_MAX_S`, `DEFAULT_MQTT_HOST`, `MQTT_PORT`, `MQTT_CLIENT_ID`, `MQTT_KEEPALIVE_S`, `MQTT_RECONNECT_MIN_S`, `MQTT_RECONNECT_MAX_S`, `MQTT_STALE_AFTER_S`, `TOPIC_CP`, `DEFAULT_SETPOINT_TOPIC`, `TOPIC_INVERTER`, `MAX_SETPOINT_W`, `HISTORY_WINDOW_S`, `HISTORY_PERIOD_S`.
  - `reading.reading(value, at, now, stale_after, force_stale=False, wall=time.time) -> dict` with keys `value`, `status`, `updated_at`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_dashboard_config.py`:

```python
import os
import unittest
from unittest import mock

from charger_web import config
from charger_web.reading import reading

NAMES = ("EVSIM_INVERTER_HOST", "EVSIM_MQTT_HOST", "EVSIM_SETPOINT_TOPIC")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in NAMES:
            os.environ.pop(name, None)

    def test_the_defaults_are_the_lab_values(self):
        self.assertEqual(config.inverter_host(), "192.168.21.120")
        self.assertEqual(config.mqtt_host(), "127.0.0.1")
        self.assertEqual(config.setpoint_topic(), "evsim/setpoint")

    def test_the_environment_overrides_them(self):
        os.environ["EVSIM_INVERTER_HOST"] = " 10.0.0.5 "
        os.environ["EVSIM_MQTT_HOST"] = "broker.lab"
        os.environ["EVSIM_SETPOINT_TOPIC"] = "evsim/electric_car"
        self.assertEqual(
            (config.inverter_host(), config.mqtt_host(), config.setpoint_topic()),
            ("10.0.0.5", "broker.lab", "evsim/electric_car"),
        )

    def test_a_host_with_a_url_or_nothing_is_refused(self):
        for bad in ("http://x", "a/b", "", "   ", "user@host", "h?x", "h#x"):
            for variable, function in (("EVSIM_INVERTER_HOST", config.inverter_host), ("EVSIM_MQTT_HOST", config.mqtt_host)):
                os.environ[variable] = bad
                with self.assertRaises(ValueError, msg=(variable, bad)):
                    function()
                del os.environ[variable]

    def test_a_setpoint_topic_with_a_wildcard_or_whitespace_is_refused(self):
        for bad in ("", "  ", "evsim/#", "evsim/+/x", "#", "a b", "evsim/\tx", "x" * 129):
            os.environ["EVSIM_SETPOINT_TOPIC"] = bad
            with self.assertRaises(ValueError, msg=bad):
                config.setpoint_topic()

    def test_the_charger_host_check_is_unchanged(self):
        os.environ["EVSIM_CHARGER_HOST"] = "http://x"
        with self.assertRaisesRegex(ValueError, "EVSIM_CHARGER_HOST must be a host name"):
            config.charger_host()


class ReadingTest(unittest.TestCase):
    def test_no_value_is_unavailable(self):
        self.assertEqual(reading(None, 10.0, 11.0, 5), {"value": None, "status": "unavailable", "updated_at": None})

    def test_a_young_value_is_ok_and_carries_its_time(self):
        result = reading(7000, 10.0, 12.0, 5, wall=lambda: 1000.0)
        self.assertEqual((result["value"], result["status"]), (7000, "ok"))
        self.assertEqual(result["updated_at"], "1970-01-01T00:16:38+00:00")    # 1000 s minus 2 s old

    def test_an_old_value_is_stale(self):
        self.assertEqual(reading(7000, 10.0, 16.0, 5)["status"], "stale")

    def test_the_limit_itself_is_stale(self):
        self.assertEqual(reading(7000, 10.0, 14.999, 5)["status"], "ok")
        self.assertEqual(reading(7000, 10.0, 15.0, 5)["status"], "stale")

    def test_a_forced_stale_value_keeps_its_value(self):
        result = reading(7000, 10.0, 11.0, 5, force_stale=True)
        self.assertEqual((result["value"], result["status"]), (7000, "stale"))

    def test_a_value_with_no_time_is_stale_with_no_timestamp(self):
        result = reading(0.0, None, 11.0, 5)
        self.assertEqual((result["value"], result["status"], result["updated_at"]), (0.0, "stale", None))

    def test_a_clock_that_ran_backwards_does_not_give_a_future_time(self):
        result = reading(1, 20.0, 10.0, 5, wall=lambda: 1000.0)
        self.assertEqual(result["updated_at"], "1970-01-01T00:16:40+00:00")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_config.py'`
Expected: FAIL with `No module named 'charger_web.reading'`.

- [ ] **Step 3: Write the implementation**

Overwrite `server/charger_web/config.py`:

```python
"""Runtime configuration for the standalone charger web process."""

import os

DEFAULT_CHARGER_HOST = "192.168.21.117"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
MODBUS_PORT = 502

# The Modbus charger service (docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md).
MODBUS_TIMEOUT_S = 3
POLL_PERIOD_S = 2                 # the poller reads this often; it is also the charger's heartbeat
STALE_AFTER_S = 5                 # a reading older than this, or polls failing this long, is stale
FALLBACK_AFTER_S = 30             # no Modbus traffic for this long: the charger drops to its safe current
RESTORE_MIN_INTERVAL_S = 10       # the desired limit is re-written at most this often
MIN_LIMIT_A = 6                   # the lowest non-zero limit the page accepts
MAX_LIMIT_A = 16                  # the highest limit the page accepts

# The dashboard (docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md). Read-only.
DEFAULT_INVERTER_HOST = "192.168.21.120"   # GEN24 12.0 Plus SC on the lab network
INVERTER_PORT = 502
INVERTER_UNIT = 1                 # the unit ID of the inverter over TCP is always 1
INVERTER_POLL_PERIOD_S = 1        # one model-160 read per second
INVERTER_BACKOFF_MIN_S = 1        # retry delay toward an unreachable inverter
INVERTER_BACKOFF_MAX_S = 5

DEFAULT_MQTT_HOST = "127.0.0.1"   # Mosquitto on the same computer
MQTT_PORT = 1883
MQTT_CLIENT_ID = "evsim-charger-web"   # must differ from "evsim-setpoint-server": equal ids kick each other off
MQTT_KEEPALIVE_S = 10
MQTT_RECONNECT_MIN_S = 1
MQTT_RECONNECT_MAX_S = 5
MQTT_STALE_AFTER_S = 3            # an MQTT value older than this is stale (the slowest topic publishes every 1 s)
TOPIC_CP = "evsim/cp"             # the board's telemetry, 5 Hz
DEFAULT_SETPOINT_TOPIC = "evsim/setpoint"   # becomes evsim/electric_car with the car emulation
TOPIC_INVERTER = "evsim/inverter"           # the setpoint server's inverter status, 1 Hz
MAX_SETPOINT_W = 12000            # the inverter's rating; a larger setpoint_w is dropped, not clamped

HISTORY_WINDOW_S = 600            # the chart shows the last 10 minutes
HISTORY_PERIOD_S = 1              # one frame per second


def _host(variable, default):
    host = os.environ.get(variable, default).strip()
    if not host or any(char in host for char in "/?#@"):
        raise ValueError(f"{variable} must be a host name or address, without a URL path.")
    return host


def charger_host():
    return _host("EVSIM_CHARGER_HOST", DEFAULT_CHARGER_HOST)


def inverter_host():
    return _host("EVSIM_INVERTER_HOST", DEFAULT_INVERTER_HOST)


def mqtt_host():
    return _host("EVSIM_MQTT_HOST", DEFAULT_MQTT_HOST)


def setpoint_topic():
    topic = os.environ.get("EVSIM_SETPOINT_TOPIC", DEFAULT_SETPOINT_TOPIC).strip()
    if not topic or len(topic) > 128 or any(char in topic for char in "+#") or any(char.isspace() for char in topic):
        raise ValueError("EVSIM_SETPOINT_TOPIC must be one MQTT topic: no wildcards (+ #) or spaces, at most 128 characters.")
    return topic


def modbus_register_map():
    return os.environ.get("EVSIM_CHARGER_REGISTERS") or None
```

Create `server/charger_web/reading.py`:

```python
"""One reading in the shape the page expects: a value, a status and the time it was taken."""

import time
from datetime import datetime, timezone


def reading(value, at, now, stale_after, force_stale=False, wall=time.time):
    """`at` and `now` come from the same monotonic clock; `wall` turns the age into a UTC timestamp.

    unavailable: there is no value. stale: the value is `stale_after` seconds old or more, or has no
    time, or `force_stale` (a retained message, or a link that is down). ok: otherwise.
    """
    if value is None:
        return {"value": None, "status": "unavailable", "updated_at": None}
    if force_stale or at is None or now - at >= stale_after:
        status = "stale"
    else:
        status = "ok"
    updated_at = None
    if at is not None:
        updated_at = datetime.fromtimestamp(wall() - max(0.0, now - at), timezone.utc).isoformat()
    return {"value": value, "status": status, "updated_at": updated_at}
```

- [ ] **Step 4: Run it to verify it passes, and that the charger tests still do**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_config.py'` then `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_*.py'`
Expected: both OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/charger_web/config.py server/charger_web/reading.py server/tests/test_dashboard_config.py
git commit -m "Add the dashboard settings and the reading helper

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The inverter reader

**Files:**
- Create: `server/charger_web/inverter.py`
- Test: `server/tests/test_dashboard_inverter.py`

**Interfaces:**
- Consumes: `charger_web.modbus.ModbusTcp(host, port, unit, timeout)` with `.read(address, count) -> list[int]`, `.close()`, raising `ModbusError` / `ModbusTransportError`; `charger_web.reading.reading`; `config.*` from Task 1; `tests/fake_inverter.py` (`FakeInverter`, `M160`).
- Produces: `InverterReader(host=None, port=None, unit=None, clock=time.monotonic, wall=time.time, period_s=None)` with `poll_once() -> bool` (True on success), `reading() -> dict` (the Task 1 shape, in watts), `start()` (idempotent), `stop()`. No method writes.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_dashboard_inverter.py`:

```python
import socket
import time
import unittest
from unittest import mock

from fake_inverter import FakeInverter, M160
from charger_web import config
from charger_web.inverter import InverterReader

MODULE_1 = M160 + 8     # the first module block of the fake's model 160
DCW = 11                # the offset of DCW inside a module block


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class InverterReaderTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInverter()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.clock = FakeClock()
        self.reader = InverterReader("127.0.0.1", self.port, clock=self.clock)
        self.addCleanup(self.reader.stop)

    def test_it_reads_the_dc_power_of_mppt_1(self):
        self.assertTrue(self.reader.poll_once())
        result = self.reader.reading()
        self.assertEqual((result["value"], result["status"]), (7272, "ok"))

    def test_the_scale_factor_is_read_with_the_value(self):
        self.fake.put(M160 + 2, [-1])
        self.fake.put(MODULE_1 + DCW, [43210])
        self.reader.poll_once()
        self.assertEqual(self.reader.reading()["value"], 4321.0)

    def test_it_takes_the_module_whose_id_is_1_not_the_first(self):
        self.fake.put(MODULE_1, [2])
        self.fake.put(MODULE_1 + DCW, [111])
        self.fake.put(MODULE_1 + 20, [1])
        self.fake.put(MODULE_1 + 20 + DCW, [5000])
        self.reader.poll_once()
        self.assertEqual(self.reader.reading()["value"], 5000)

    def test_not_implemented_is_unavailable(self):
        self.fake.put(MODULE_1 + DCW, [65535])
        self.assertTrue(self.reader.poll_once())
        result = self.reader.reading()
        self.assertEqual((result["value"], result["status"]), (None, "unavailable"))

    def test_without_a_module_with_id_1_it_is_unavailable(self):
        for block in (0, 20, 40):
            self.fake.put(MODULE_1 + block, [5 + block])
        self.assertTrue(self.reader.poll_once())
        self.assertEqual(self.reader.reading()["status"], "unavailable")

    def test_after_the_first_poll_each_poll_is_one_request(self):
        self.reader.poll_once()
        with mock.patch.object(self.reader._modbus, "read", wraps=self.reader._modbus.read) as spy:
            self.reader.poll_once()
        self.assertEqual(spy.call_count, 1)

    def test_it_never_writes(self):
        for _ in range(3):
            self.reader.poll_once()
        self.assertEqual(self.fake.writes, [])
        self.assertFalse(hasattr(self.reader, "write_single"))

    def test_an_old_value_is_stale(self):
        self.reader.poll_once()
        self.clock.t += config.STALE_AFTER_S
        result = self.reader.reading()
        self.assertEqual((result["value"], result["status"]), (7272, "stale"))

    def test_a_dropped_connection_goes_stale_then_recovers_with_a_fresh_walk(self):
        self.reader.poll_once()
        self.fake.stop()
        self.assertFalse(self.reader.poll_once())
        result = self.reader.reading()
        self.assertEqual((result["value"], result["status"]), (7272, "stale"))   # the last good value, flagged
        self.assertIsNone(self.reader._model)                                    # the chain is walked again
        self.fake.start(self.port)
        self.assertTrue(self.reader.poll_once())
        self.assertEqual(self.reader.reading()["status"], "ok")
        self.assertIsNotNone(self.reader._model)

    def test_an_unreachable_inverter_is_unavailable_and_never_raises(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        reader = InverterReader("127.0.0.1", closed_port, clock=self.clock)
        self.addCleanup(reader.stop)
        self.assertFalse(reader.poll_once())
        result = reader.reading()
        self.assertEqual((result["value"], result["status"]), (None, "unavailable"))

    def test_a_missing_sunspec_marker_is_an_error_not_a_crash(self):
        self.fake.put(40000, [0, 0])
        self.assertFalse(self.reader.poll_once())
        self.assertEqual(self.reader.reading()["status"], "unavailable")

    def test_a_model_160_that_is_too_short_is_an_error_not_a_crash(self):
        self.fake.put(self.fake.chain[160][0] - 1, [3])    # the length register of model 160
        self.assertFalse(self.reader.poll_once())

    def test_the_thread_polls_and_stops_and_start_is_idempotent(self):
        reader = InverterReader("127.0.0.1", self.port, period_s=0.05)
        self.addCleanup(reader.stop)
        reader.start()
        thread = reader._thread
        reader.start()
        self.assertIs(reader._thread, thread)
        deadline = time.monotonic() + 3
        while reader.reading()["value"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(reader.reading()["value"], 7272)
        reader.stop()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_inverter.py'`
Expected: FAIL with `No module named 'charger_web.inverter'`.

- [ ] **Step 3: Write the implementation**

Create `server/charger_web/inverter.py`:

```python
"""Reads the inverter's MPPT 1 DC power over Modbus TCP, once a second. Read-only by construction.

One thread owns one connection. Only SunSpec model 160 (the MPPT extension) is read, in a single request, so
the scale factor and the value come from the same instant: during start-up the GEN24's DC power once read
10x too large because a scale factor changed between two reads. Background: memory/evsim-inverter-fronius.md.
"""

from __future__ import annotations

import logging
import threading
import time

from . import config
from .modbus import ModbusError, ModbusTcp
from .reading import reading

log = logging.getLogger("charger_web.inverter")

SUNSPEC_BASE = 40000
SUNSPEC_MARKER = [0x5375, 0x6E53]      # "SunS"
CHAIN_LIMIT = 4000                     # registers to walk before giving up
END_OF_CHAIN = 0xFFFF
MODEL_MPPT = 160
M160_SF_W, M160_N, M160_MODULES, M160_BLOCK = 2, 6, 8, 20   # offsets inside model 160, registers per module
MODULE_ID, MODULE_DCW = 0, 11                               # offsets inside one module block
NOT_IMPLEMENTED = 0xFFFF
MPPT_ONE = 1


def _s16(raw):
    return raw - 65536 if raw >= 32768 else raw


class InverterReader:
    def __init__(self, host=None, port=None, unit=None, clock=time.monotonic, wall=time.time, period_s=None):
        self._modbus = ModbusTcp(
            host or config.inverter_host(), port or config.INVERTER_PORT,
            config.INVERTER_UNIT if unit is None else unit, config.MODBUS_TIMEOUT_S,
        )
        self._clock = clock
        self._wall = wall
        self._period = config.INVERTER_POLL_PERIOD_S if period_s is None else period_s
        self._lock = threading.Lock()          # guards the cache below
        self._value = None
        self._at = None
        self._error = None
        self._model = None                     # (first data address, length) of model 160, once found
        self._failing = False
        self._stop = threading.Event()
        self._thread = None

    def _locate(self):
        if self._modbus.read(SUNSPEC_BASE, 2) != SUNSPEC_MARKER:
            raise ModbusError("no SunSpec marker at 40000")
        address = SUNSPEC_BASE + 2
        while address < SUNSPEC_BASE + CHAIN_LIMIT:
            model, length = self._modbus.read(address, 2)
            if model == END_OF_CHAIN:
                break
            if model == MODEL_MPPT:
                return address + 2, length
            address += 2 + length
        raise ModbusError("the inverter has no SunSpec model 160")

    @staticmethod
    def _parse(words):
        if len(words) < M160_MODULES:
            raise ModbusError("SunSpec model 160 is too short")
        scale = 10 ** _s16(words[M160_SF_W])
        count = min(words[M160_N], (len(words) - M160_MODULES) // M160_BLOCK)
        for index in range(count):
            start = M160_MODULES + M160_BLOCK * index
            block = words[start:start + M160_BLOCK]
            if block[MODULE_ID] == MPPT_ONE:
                raw = block[MODULE_DCW]
                return None if raw == NOT_IMPLEMENTED else round(raw * scale, 3)
        return None

    def poll_once(self):
        """One read of model 160. True on success; on any Modbus failure the older value simply ages."""
        try:
            if self._model is None:
                self._model = self._locate()
            address, length = self._model
            value = self._parse(self._modbus.read(address, length))
        except ModbusError as exc:
            self._model = None                 # the next connection walks the chain again
            self._modbus.close()
            with self._lock:
                self._error = str(exc)
            if not self._failing:
                log.warning("cannot read the inverter: %s", exc)
            self._failing = True
            return False
        now = self._clock()
        with self._lock:
            self._value, self._at, self._error = value, now, None
        if self._failing:
            log.info("the inverter is readable again")
        self._failing = False
        return True

    def reading(self):
        with self._lock:
            value, at, error = self._value, self._at, self._error
        return reading(value, at, self._clock(), config.STALE_AFTER_S, force_stale=error is not None, wall=self._wall)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="inverter-reader", daemon=True)
        self._thread.start()

    def _run(self):
        backoff = config.INVERTER_BACKOFF_MIN_S
        while not self._stop.is_set():
            try:
                ok = self.poll_once()
            except Exception:                  # one bad poll must never stop the reader
                log.exception("inverter poll failed")
                ok = False
            if ok:
                backoff, delay = config.INVERTER_BACKOFF_MIN_S, self._period
            else:
                delay, backoff = backoff, min(backoff * 2, config.INVERTER_BACKOFF_MAX_S)
            self._stop.wait(delay)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * config.MODBUS_TIMEOUT_S + 2)
        self._modbus.close()
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_inverter.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/charger_web/inverter.py server/tests/test_dashboard_inverter.py
git commit -m "Read the inverter's MPPT 1 DC power once a second

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The MQTT payload parsers

**Files:**
- Create: `server/charger_web/parsers.py`
- Test: `server/tests/test_dashboard_parsers.py`

**Interfaces:**
- Consumes: `config.MAX_SETPOINT_W`.
- Produces (all take `bytes | str | None`, never raise, return `None` for anything unusable):
  - `parse_cp(payload) -> {"setpoint_w": float, "state": str | None, "status": str | None} | None`
  - `parse_setpoint(payload) -> {"setpoint_w": float, "status": str | None} | None` (the one function to change for the car emulation)
  - `parse_inverter(payload) -> {"target_dc_w": float | None, "mode": str | None, "inverter_state": int | None} | None`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_dashboard_parsers.py`:

```python
import unittest

from charger_web.parsers import parse_cp, parse_inverter, parse_setpoint

HOSTILE_WATTS = [
    b'{"setpoint_w": null}', b'{"setpoint_w": true}', b'{"setpoint_w": "4000"}', b'{"setpoint_w": [1]}',
    b'{"setpoint_w": NaN}', b'{"setpoint_w": Infinity}', b'{"setpoint_w": -Infinity}',
    b'{"setpoint_w": -1}', b'{"setpoint_w": 12000.5}', b'{"setpoint_w": 12001}',
    b'{"setpoint_w": 1' + b"0" * 400 + b"}", b"{}",
]
NOT_OBJECTS = [None, b"", b"not json", b"[1]", b"4000", b"null", b'"text"', b"\xff\xfe", b"{" * 2000]


class CpTest(unittest.TestCase):
    def test_a_normal_sample(self):
        payload = b'{"t":1,"freq":1000.0,"duty":10.5,"cp":"PWM_VALID","state":"C","amps":6.3,"watts":4361,"setpoint_w":4361,"status":"live","age_ms":0}'
        self.assertEqual(parse_cp(payload), {"setpoint_w": 4361.0, "state": "C", "status": "live"})

    def test_a_string_payload_is_accepted(self):
        self.assertEqual(parse_cp('{"setpoint_w": 5}')["setpoint_w"], 5.0)

    def test_the_bounds_are_inclusive(self):
        self.assertEqual(parse_cp(b'{"setpoint_w": 0}')["setpoint_w"], 0.0)
        self.assertEqual(parse_cp(b'{"setpoint_w": 12000}')["setpoint_w"], 12000.0)

    def test_hostile_values_are_dropped(self):
        for payload in HOSTILE_WATTS:
            self.assertIsNone(parse_cp(payload), payload[:40])

    def test_things_that_are_not_objects_are_dropped(self):
        for payload in NOT_OBJECTS:
            self.assertIsNone(parse_cp(payload), payload)

    def test_missing_or_oversized_text_does_not_drop_the_sample(self):
        self.assertEqual(parse_cp(b'{"setpoint_w": 5}'), {"setpoint_w": 5.0, "state": None, "status": None})
        long_text = ('{"setpoint_w": 5, "state": "%s", "status": 7}' % ("x" * 33)).encode()
        self.assertEqual(parse_cp(long_text), {"setpoint_w": 5.0, "state": None, "status": None})


class SetpointTest(unittest.TestCase):
    def test_the_servers_message(self):
        payload = b'{"setpoint_w":4312,"samples":5,"window_s":1,"status":"ok","ts":"2026-10-01T15:13:03Z"}'
        self.assertEqual(parse_setpoint(payload), {"setpoint_w": 4312.0, "status": "ok"})

    def test_the_offline_message_is_a_valid_zero(self):
        payload = b'{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}'
        self.assertEqual(parse_setpoint(payload), {"setpoint_w": 0.0, "status": "server_offline"})

    def test_hostile_values_and_non_objects_are_dropped(self):
        for payload in HOSTILE_WATTS + NOT_OBJECTS:
            self.assertIsNone(parse_setpoint(payload), payload)


class InverterTest(unittest.TestCase):
    def test_the_servers_status(self):
        payload = b'{"mode":"capped","target_dc_w":4312.6,"cap_ac_w":4182.7,"dc_w":4304.6,"ac_w":4181,"inverter_state":4,"error":null,"ts":"2026-10-01T15:13:04Z"}'
        self.assertEqual(parse_inverter(payload), {"target_dc_w": 4312.6, "mode": "capped", "inverter_state": 4})

    def test_a_null_target_is_kept_as_none(self):
        parsed = parse_inverter(b'{"mode":"unreachable","target_dc_w":null,"inverter_state":null}')
        self.assertEqual(parsed, {"target_dc_w": None, "mode": "unreachable", "inverter_state": None})

    def test_a_missing_target_is_none_too(self):
        self.assertEqual(parse_inverter(b'{"mode":"zero"}'), {"target_dc_w": None, "mode": "zero", "inverter_state": None})

    def test_a_target_that_is_not_a_good_power_drops_the_message(self):
        for bad in (b'"x"', b"true", b"-5", b"NaN", b"12001", b"[]"):
            self.assertIsNone(parse_inverter(b'{"target_dc_w": ' + bad + b"}"), bad)

    def test_a_bad_inverter_state_is_none_but_the_message_stays(self):
        for bad in (b'"4"', b"true", b"-1", b"256", b"4.5", b"null"):
            parsed = parse_inverter(b'{"target_dc_w": 100, "inverter_state": ' + bad + b"}")
            self.assertEqual(parsed["inverter_state"], None, bad)

    def test_non_objects_are_dropped(self):
        for payload in NOT_OBJECTS:
            self.assertIsNone(parse_inverter(payload), payload)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_parsers.py'`
Expected: FAIL with `No module named 'charger_web.parsers'`.

- [ ] **Step 3: Write the implementation**

Create `server/charger_web/parsers.py`:

```python
"""Parsers for the MQTT payloads the dashboard reads.

Pure functions that never raise: anything unusable gives None. A power outside 0 to MAX_SETPOINT_W is
dropped, never clamped (the setpoint server does the same). Text fields are short strings or None; the
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
    """The setpoint server's evsim/inverter status: its DC target, its mode and the inverter state."""
    data = _object(payload)
    if data is None:
        return None
    target = data.get("target_dc_w")
    if target is not None:
        target = _watts(target)
        if target is None:
            return None
    return {"target_dc_w": target, "mode": _text(data.get("mode")), "inverter_state": _state(data.get("inverter_state"))}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_parsers.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/charger_web/parsers.py server/tests/test_dashboard_parsers.py
git commit -m "Parse the MQTT payloads the dashboard reads

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The MQTT subscriber

**Files:**
- Create: `server/charger_web/telemetry.py`
- Create: `server/tests/fake_mqtt.py`
- Test: `server/tests/test_dashboard_telemetry.py`

**Interfaces:**
- Consumes: `parsers.parse_cp/parse_setpoint/parse_inverter`, `reading.reading`, `config.*`.
- Produces:
  - `Telemetry(client, setpoint_topic, clock=time.monotonic, wall=time.time)`: registers `on_connect`, `on_message`, `on_disconnect` on `client`; `snapshot() -> dict`; `drain_cp_samples() -> list[float]` (returns and clears the raw `evsim/cp` `setpoint_w` values received since the last call, never including retained ones).
  - `snapshot()` shape: `{"mqtt": {"connected": bool}, "board": {"setpoint_w": reading, "state": str|None, "status": str|None}, "setpoint": {"topic": str, "setpoint_w": reading, "status": str|None}, "server": {"target_dc_w": reading, "mode": str|None, "inverter_state": int|None}}`.
  - `build_client() -> paho.mqtt.client.Client` (client id `evsim-charger-web`, 1 to 5 s reconnect delay; raises `ValueError` if paho is missing).
  - `tests/fake_mqtt.py`: `FakeMqttClient` (records `subscribed` and `calls`; helpers `connect(reason=OK)`, `drop()`, `deliver(topic, payload, retain=False)`), `OK`, `REFUSED`.

- [ ] **Step 1: Write the fake client and the failing tests**

Create `server/tests/fake_mqtt.py`:

```python
"""Stands in for paho's client: records what the code under test asks of it and lets a test drive the callbacks."""

from types import SimpleNamespace

OK = SimpleNamespace(is_failure=False)
REFUSED = SimpleNamespace(is_failure=True)


class FakeMqttClient:
    def __init__(self):
        self.subscribed = []           # (topic, qos), in order
        self.calls = []                # ("connect_async", host, port), ("loop_start",), ("disconnect",), ("loop_stop",)
        self.on_connect = self.on_message = self.on_disconnect = None

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def connect_async(self, host, port, keepalive=60):
        self.calls.append(("connect_async", host, port))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    # -- what the broker would do ---------------------------------------------------------------

    def connect(self, reason=OK):
        self.on_connect(self, None, None, reason, None)

    def drop(self):
        self.on_disconnect(self, None, None, None, None)

    def deliver(self, topic, payload, retain=False):
        data = payload.encode() if isinstance(payload, str) else payload
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=data, retain=retain))
```

Create `server/tests/test_dashboard_telemetry.py`:

```python
import importlib.util
import json
import unittest

from fake_mqtt import FakeMqttClient, REFUSED
from charger_web import config
from charger_web.telemetry import Telemetry, build_client

CP = '{"t":1,"state":"C","status":"live","setpoint_w":4361,"watts":4361}'
SETPOINT = '{"setpoint_w":4312,"samples":5,"window_s":1,"status":"ok","ts":"2026-10-01T15:13:03Z"}'
OFFLINE = '{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}'
INVERTER = '{"mode":"capped","target_dc_w":4312.6,"cap_ac_w":4182.7,"dc_w":4304.6,"ac_w":4181,"inverter_state":4,"error":null}'


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMqttClient()
        self.clock = FakeClock()
        self.telemetry = Telemetry(self.client, "evsim/setpoint", clock=self.clock, wall=lambda: 1000.0)
        self.client.connect()

    def snapshot(self):
        return self.telemetry.snapshot()

    def test_it_subscribes_to_the_three_topics_and_again_after_a_reconnect(self):
        expected = [("evsim/cp", 0), ("evsim/setpoint", 0), ("evsim/inverter", 0)]
        self.assertEqual(self.client.subscribed, expected)
        self.client.drop()
        self.client.connect()
        self.assertEqual(self.client.subscribed, expected * 2)

    def test_a_refused_connection_subscribes_to_nothing_and_is_not_connected(self):
        client = FakeMqttClient()
        telemetry = Telemetry(client, "evsim/setpoint")
        client.connect(REFUSED)
        self.assertEqual(client.subscribed, [])
        self.assertFalse(telemetry.snapshot()["mqtt"]["connected"])

    def test_the_connection_flag_follows_the_broker(self):
        self.assertTrue(self.snapshot()["mqtt"]["connected"])
        self.client.drop()
        self.assertFalse(self.snapshot()["mqtt"]["connected"])

    def test_nothing_received_is_unavailable_and_serializes(self):
        snap = self.snapshot()
        self.assertEqual(snap["board"]["setpoint_w"]["status"], "unavailable")
        self.assertEqual(snap["setpoint"]["setpoint_w"]["status"], "unavailable")
        self.assertEqual(snap["server"]["target_dc_w"]["status"], "unavailable")
        self.assertEqual((snap["board"]["state"], snap["setpoint"]["status"], snap["server"]["mode"]), (None, None, None))
        json.dumps(snap, allow_nan=False)

    def test_the_three_topics_fill_their_cards(self):
        self.client.deliver("evsim/cp", CP)
        self.client.deliver("evsim/setpoint", SETPOINT)
        self.client.deliver("evsim/inverter", INVERTER)
        snap = self.snapshot()
        self.assertEqual((snap["board"]["setpoint_w"]["value"], snap["board"]["setpoint_w"]["status"]), (4361.0, "ok"))
        self.assertEqual((snap["board"]["state"], snap["board"]["status"]), ("C", "live"))
        self.assertEqual((snap["setpoint"]["setpoint_w"]["value"], snap["setpoint"]["status"]), (4312.0, "ok"))
        self.assertEqual(snap["server"]["target_dc_w"]["value"], 4312.6)
        self.assertEqual((snap["server"]["mode"], snap["server"]["inverter_state"]), ("capped", 4))
        json.dumps(snap, allow_nan=False)

    def test_a_null_target_is_unavailable_but_the_mode_shows(self):
        self.client.deliver("evsim/inverter", '{"mode":"zero","target_dc_w":null,"inverter_state":4}')
        server = self.snapshot()["server"]
        self.assertEqual((server["target_dc_w"]["status"], server["mode"]), ("unavailable", "zero"))

    def test_a_malformed_message_is_dropped_and_the_old_value_stays(self):
        self.client.deliver("evsim/cp", "not json")
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "unavailable")
        self.client.deliver("evsim/cp", CP)
        self.client.deliver("evsim/cp", '{"setpoint_w": NaN}')
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["value"], 4361.0)

    def test_an_unknown_topic_is_ignored(self):
        self.client.deliver("evsim/other", CP)
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "unavailable")

    def test_a_value_goes_stale_at_the_limit(self):
        self.client.deliver("evsim/cp", CP)
        self.clock.t += config.MQTT_STALE_AFTER_S - 0.1
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "ok")
        self.clock.t += 0.1
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "stale")

    def test_a_retained_message_is_the_last_known_state_but_stale(self):
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["value"], snap["setpoint_w"]["status"], snap["status"]), (0.0, "stale", "server_offline"))

    def test_a_retained_copy_never_overwrites_a_fresh_live_value(self):
        self.client.deliver("evsim/setpoint", SETPOINT)
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["value"], snap["setpoint_w"]["status"], snap["status"]), (4312.0, "ok", "ok"))

    def test_a_retained_copy_replaces_a_stale_live_value(self):
        self.client.deliver("evsim/setpoint", SETPOINT)
        self.clock.t += config.MQTT_STALE_AFTER_S
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        self.assertEqual(self.snapshot()["setpoint"]["status"], "server_offline")

    def test_a_live_message_replaces_a_retained_one(self):
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        self.client.deliver("evsim/setpoint", SETPOINT)
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["status"], snap["status"]), ("ok", "ok"))

    def test_raw_samples_are_drained_once_and_never_include_retained_ones(self):
        self.client.deliver("evsim/cp", CP, retain=True)
        for watts in (4300, 4350, 4400):
            self.client.deliver("evsim/cp", '{"setpoint_w": %d}' % watts)
        self.assertEqual(self.telemetry.drain_cp_samples(), [4300.0, 4350.0, 4400.0])
        self.assertEqual(self.telemetry.drain_cp_samples(), [])

    def test_dropped_messages_do_not_become_samples(self):
        self.client.deliver("evsim/cp", '{"setpoint_w": 99999}')
        self.assertEqual(self.telemetry.drain_cp_samples(), [])


class ConfigurableTopicTest(unittest.TestCase):
    def test_the_setpoint_topic_is_a_setting(self):
        client = FakeMqttClient()
        telemetry = Telemetry(client, "evsim/electric_car")
        client.connect()
        self.assertIn(("evsim/electric_car", 0), client.subscribed)
        self.assertNotIn(("evsim/setpoint", 0), client.subscribed)
        client.deliver("evsim/setpoint", SETPOINT)
        self.assertEqual(telemetry.snapshot()["setpoint"]["setpoint_w"]["status"], "unavailable")
        client.deliver("evsim/electric_car", SETPOINT)
        snap = telemetry.snapshot()["setpoint"]
        self.assertEqual((snap["topic"], snap["setpoint_w"]["value"]), ("evsim/electric_car", 4312.0))


@unittest.skipUnless(importlib.util.find_spec("paho"), "paho-mqtt is not installed")
class BuildClientTest(unittest.TestCase):
    def test_the_client_has_its_own_id_and_never_connects_by_itself(self):
        client = build_client()
        self.assertEqual(client._client_id, b"evsim-charger-web")
        self.assertFalse(client.is_connected())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_telemetry.py'`
Expected: FAIL with `No module named 'charger_web.telemetry'`.

- [ ] **Step 3: Write the implementation**

Create `server/charger_web/telemetry.py`:

```python
"""The MQTT side of the dashboard: the latest parsed value of each topic, with its receive time.

The client is handed in, so the tests use a fake; `build_client` makes the real paho one. Subscribing happens
in on_connect, so it is renewed on every reconnect. This module never publishes and sets no will.
paho's network thread calls the on_* callbacks; one lock guards the state they and the readers share.
"""

import logging
import threading
import time

from . import config
from .parsers import parse_cp, parse_inverter, parse_setpoint
from .reading import reading

log = logging.getLogger("charger_web.telemetry")


def build_client():
    """The real paho client: its own id, a short reconnect delay, not yet connected."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise ValueError("the dashboard needs paho-mqtt; run: pip install -r requirements.txt") from exc
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.MQTT_CLIENT_ID)
    client.reconnect_delay_set(min_delay=config.MQTT_RECONNECT_MIN_S, max_delay=config.MQTT_RECONNECT_MAX_S)
    return client


class Telemetry:
    def __init__(self, client, setpoint_topic, clock=time.monotonic, wall=time.time):
        self._setpoint_topic = setpoint_topic
        self._clock = clock
        self._wall = wall
        self._routes = {
            config.TOPIC_CP: ("cp", parse_cp),
            setpoint_topic: ("setpoint", parse_setpoint),
            config.TOPIC_INVERTER: ("server", parse_inverter),
        }
        self._lock = threading.Lock()
        self._connected = False
        self._latest = {}               # kind -> {"data": parsed dict, "at": receive time, "retained": bool}
        self._cp_samples = []           # raw evsim/cp setpoint_w values since the last drain
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

    # -- paho's callbacks --------------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            return
        for topic in self._routes:
            client.subscribe(topic, qos=0)
        with self._lock:
            self._connected = True
        log.info("connected, subscribed to %s", ", ".join(self._routes))

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._lock:
            self._connected = False
        log.warning("disconnected from the broker: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        route = self._routes.get(msg.topic)
        if route is None:
            return
        kind, parse = route
        data = parse(msg.payload)
        if data is None:
            log.warning("dropping unusable %s message: %r", msg.topic, bytes(msg.payload[:80]))
            return
        retained = bool(msg.retain)
        now = self._clock()
        with self._lock:
            current = self._latest.get(kind)
            if retained and current is not None and not current["retained"] \
                    and now - current["at"] < config.MQTT_STALE_AFTER_S:
                return                  # a retained copy never replaces a fresh live value
            self._latest[kind] = {"data": data, "at": now, "retained": retained}
            if kind == "cp" and not retained:
                self._cp_samples.append(data["setpoint_w"])

    # -- what the page and the history read ----------------------------------------------------------

    def drain_cp_samples(self):
        """The raw evsim/cp setpoint_w values received since the last call, oldest first."""
        with self._lock:
            samples, self._cp_samples = self._cp_samples, []
        return samples

    def snapshot(self):
        with self._lock:
            now = self._clock()
            latest = dict(self._latest)
            connected = self._connected

        def value(kind, field):
            entry = latest.get(kind)
            if entry is None:
                return reading(None, None, now, config.MQTT_STALE_AFTER_S)
            return reading(entry["data"].get(field), entry["at"], now, config.MQTT_STALE_AFTER_S,
                           force_stale=entry["retained"], wall=self._wall)

        def text(kind, field):
            entry = latest.get(kind)
            return None if entry is None else entry["data"].get(field)

        return {
            "mqtt": {"connected": connected},
            "board": {"setpoint_w": value("cp", "setpoint_w"), "state": text("cp", "state"), "status": text("cp", "status")},
            "setpoint": {"topic": self._setpoint_topic, "setpoint_w": value("setpoint", "setpoint_w"),
                         "status": text("setpoint", "status")},
            "server": {"target_dc_w": value("server", "target_dc_w"), "mode": text("server", "mode"),
                       "inverter_state": text("server", "inverter_state")},
        }
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_telemetry.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/charger_web/telemetry.py server/tests/fake_mqtt.py server/tests/test_dashboard_telemetry.py
git commit -m "Subscribe to the board, setpoint and inverter topics

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The history sampler

**Files:**
- Create: `server/charger_web/history.py`
- Test: `server/tests/test_dashboard_history.py`

**Interfaces:**
- Consumes: an object with `drain_cp_samples() -> list[float]` and `snapshot()` (Task 4's shape, only `["setpoint"]["setpoint_w"]` and `["server"]["target_dc_w"]` are read); an object with `reading() -> dict` (Task 2); `config.HISTORY_*`.
- Produces: `History(telemetry, inverter, clock=time.monotonic, wall=time.time, period_s=None)` with `tick()`, `snapshot() -> {"interval_s": 1, "window_s": 600, "frames": [frame, ...]}` (oldest first), `start()` (idempotent), `stop()`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_dashboard_history.py`:

```python
import itertools
import time
import unittest

from charger_web.history import History


def rd(value, status="ok"):
    return {"value": value, "status": status, "updated_at": None}


class StubTelemetry:
    def __init__(self):
        self.samples = []
        self.setpoint = rd(4312.0)
        self.target = rd(4312.6)

    def drain_cp_samples(self):
        samples, self.samples = self.samples, []
        return samples

    def snapshot(self):
        return {"setpoint": {"setpoint_w": self.setpoint}, "server": {"target_dc_w": self.target}}


class StubInverter:
    def __init__(self):
        self.dc = rd(4304.6)

    def reading(self):
        return self.dc


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.telemetry = StubTelemetry()
        self.inverter = StubInverter()
        self.wall = itertools.count(1_700_000_000)
        self.history = History(self.telemetry, self.inverter, wall=lambda: next(self.wall))

    def frames(self):
        return self.history.snapshot()["frames"]

    def test_a_frame_has_the_time_and_the_four_values(self):
        self.telemetry.samples = [4300.0, 4400.0]
        self.history.tick()
        self.assertEqual(self.frames(), [
            {"t": 1_700_000_000, "cp_w": 4350.0, "setpoint_w": 4312.0, "target_dc_w": 4312.6, "dc_w": 4304.6},
        ])

    def test_the_raw_value_is_the_mean_of_the_samples_since_the_last_tick(self):
        self.telemetry.samples = [100.0, 200.0, 300.0]
        self.history.tick()
        self.telemetry.samples = [1000.0]
        self.history.tick()
        self.assertEqual([f["cp_w"] for f in self.frames()], [200.0, 1000.0])

    def test_no_samples_is_a_gap(self):
        self.history.tick()
        self.assertIsNone(self.frames()[0]["cp_w"])

    def test_a_stale_or_unavailable_source_is_a_gap(self):
        self.telemetry.setpoint = rd(4312.0, "stale")
        self.telemetry.target = rd(None, "unavailable")
        self.inverter.dc = rd(4304.6, "stale")
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["setpoint_w"], frame["target_dc_w"], frame["dc_w"]), (None, None, None))

    def test_values_are_rounded_to_a_tenth_of_a_watt(self):
        self.inverter.dc = rd(4304.5678)
        self.history.tick()
        self.assertEqual(self.frames()[0]["dc_w"], 4304.6)

    def test_the_ring_keeps_600_frames_and_drops_the_oldest(self):
        for _ in range(601):
            self.history.tick()
        frames = self.frames()
        self.assertEqual(len(frames), 600)
        self.assertEqual(frames[0]["t"], 1_700_000_001)
        self.assertEqual(frames[-1]["t"], 1_700_000_600)

    def test_the_snapshot_describes_itself_and_returns_a_copy(self):
        self.history.tick()
        snap = self.history.snapshot()
        self.assertEqual((snap["interval_s"], snap["window_s"]), (1, 600))
        snap["frames"].clear()
        self.assertEqual(len(self.frames()), 1)

    def test_the_thread_samples_and_stops_and_start_is_idempotent(self):
        history = History(self.telemetry, self.inverter, period_s=0.02)
        history.start()
        thread = history._thread
        history.start()
        self.assertIs(history._thread, thread)
        deadline = time.monotonic() + 3
        while len(history.snapshot()["frames"]) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        history.stop()
        self.assertGreaterEqual(len(history.snapshot()["frames"]), 3)
        self.assertFalse(thread.is_alive())

    def test_stop_before_start_does_nothing(self):
        self.history.stop()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_history.py'`
Expected: FAIL with `No module named 'charger_web.history'`.

- [ ] **Step 3: Write the implementation**

Create `server/charger_web/history.py`:

```python
"""A 1 Hz sampler: one frame per second into a ring of the last 10 minutes, in memory only.

A frame holds the wall-clock second and four powers. A value is recorded only if its reading is `ok`, so a
stale or missing source is a gap (None), never a flat line. The raw board setpoint is the mean of the samples
received since the previous tick (about five), so it can be compared fairly with the 1 s average.
"""

import logging
import threading
import time
from collections import deque

from . import config

log = logging.getLogger("charger_web.history")


def _ok(source):
    return round(source["value"], 1) if source["status"] == "ok" else None


class History:
    def __init__(self, telemetry, inverter, clock=time.monotonic, wall=time.time, period_s=None):
        self._telemetry = telemetry
        self._inverter = inverter
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
        frame = {
            "t": int(self._wall()),
            "cp_w": round(sum(samples) / len(samples), 1) if samples else None,
            "setpoint_w": _ok(snap["setpoint"]["setpoint_w"]),
            "target_dc_w": _ok(snap["server"]["target_dc_w"]),
            "dc_w": _ok(self._inverter.reading()),
        }
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
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_history.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/charger_web/history.py server/tests/test_dashboard_history.py
git commit -m "Sample the four power values into a ten-minute history

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Compose the dashboard, serve it, wire the entry point

**Files:**
- Create: `server/charger_web/dashboard.py`
- Modify: `server/charger_web/web.py` (overwritten)
- Modify: `server/charger_web/__main__.py` (overwritten)
- Modify: `docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md` (two small amendments)
- Test: `server/tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `InverterReader` (Task 2), `Telemetry`, `build_client` (Task 4), `History` (Task 5), `FakeInverter`, `FakeMqttClient`, `config.*`.
- Produces:
  - `Dashboard(inverter, telemetry, history, mqtt_client=None, broker=None)`: `telemetry() -> dict` (Task 4's snapshot plus `"inverter": {"dc_w": reading}`), `history() -> dict`, `start()`, `stop()` (both idempotent; `stop()` before `start()` does nothing). `broker` is `(host, port)`.
  - `build_dashboard() -> Dashboard` (reads the three settings, raises `ValueError` on a bad one, builds the real paho client, starts nothing).
  - `create_server(charger=None, host=..., port=..., dashboard=None)`; with a dashboard, `GET /api/telemetry` and `GET /api/history`; a failing dashboard is a 500 JSON error, never a dropped connection.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_dashboard_web.py`:

```python
import contextlib
import http.client
import io
import json
import os
import socket
import threading
import unittest
from unittest import mock

from fake_inverter import FakeInverter
from fake_mqtt import FakeMqttClient
from charger_web import __main__ as charger_main
from charger_web.dashboard import Dashboard
from charger_web.history import History
from charger_web.inverter import InverterReader
from charger_web.telemetry import Telemetry
from charger_web.web import create_server

CP = '{"setpoint_w":4361,"state":"C","status":"live"}'


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInverter()
        port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.client = FakeMqttClient()
        self.inverter = InverterReader("127.0.0.1", port)
        self.telemetry = Telemetry(self.client, "evsim/setpoint")
        self.history = History(self.telemetry, self.inverter)
        self.dashboard = Dashboard(self.inverter, self.telemetry, self.history, self.client, ("127.0.0.1", 1883))
        self.addCleanup(self.dashboard.stop)
        self.addCleanup(self.inverter.stop)

    def test_before_anything_arrives_everything_is_unavailable_and_serializes(self):
        data = self.dashboard.telemetry()
        self.assertFalse(data["mqtt"]["connected"])
        self.assertEqual(data["inverter"]["dc_w"]["status"], "unavailable")
        self.assertEqual(data["board"]["setpoint_w"]["status"], "unavailable")
        json.dumps(data, allow_nan=False)
        json.dumps(self.dashboard.history(), allow_nan=False)

    def test_telemetry_joins_the_inverter_and_the_mqtt_values(self):
        self.client.connect()
        self.client.deliver("evsim/cp", CP)
        self.inverter.poll_once()
        data = self.dashboard.telemetry()
        self.assertEqual(data["inverter"]["dc_w"]["value"], 7272)
        self.assertEqual(data["board"]["setpoint_w"]["value"], 4361.0)
        self.assertTrue(data["mqtt"]["connected"])

    def test_history_returns_what_the_sampler_took(self):
        self.inverter.poll_once()
        self.history.tick()
        frames = self.dashboard.history()["frames"]
        self.assertEqual((len(frames), frames[0]["dc_w"]), (1, 7272))

    def test_start_connects_the_broker_and_stop_undoes_it_in_order(self):
        self.dashboard.start()
        self.assertEqual(self.client.calls, [("connect_async", "127.0.0.1", 1883), ("loop_start",)])
        self.dashboard.start()                                  # idempotent
        self.assertEqual(len(self.client.calls), 2)
        self.dashboard.stop()
        self.assertEqual(self.client.calls[2:], [("disconnect",), ("loop_stop",)])
        self.dashboard.stop()                                   # idempotent
        self.assertEqual(len(self.client.calls), 4)

    def test_stop_before_start_touches_nothing(self):
        self.dashboard.stop()
        self.assertEqual(self.client.calls, [])


class StubDashboard:
    def __init__(self):
        self.fail = False

    def telemetry(self):
        if self.fail:
            raise RuntimeError("boom")
        return {"mqtt": {"connected": True}, "inverter": {"dc_w": {"value": 7272, "status": "ok", "updated_at": None}}}

    def history(self):
        return {"interval_s": 1, "window_s": 600, "frames": [{"t": 1, "dc_w": 5.0}]}


class RoutesTest(unittest.TestCase):
    def serve(self, dashboard):
        server = create_server(charger=object(), dashboard=dashboard, port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def get(self, port, path, host=None, method="GET", origin=False):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host or "127.0.0.1:%d" % port}
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % port
        conn.request(method, path, None, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        kind = response.getheader("Content-Type", "")
        return response.status, (json.loads(raw) if kind.startswith("application/json") else raw), response

    def setUp(self):
        self.stub = StubDashboard()
        self.port = self.serve(self.stub)

    def test_telemetry_and_history_are_served_as_json(self):
        status, body, response = self.get(self.port, "/api/telemetry")
        self.assertEqual((status, body["inverter"]["dc_w"]["value"]), (200, 7272))
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        status, body, _ = self.get(self.port, "/api/history")
        self.assertEqual((status, body["window_s"], body["frames"][0]["dc_w"]), (200, 600, 5.0))

    def test_a_wrong_host_header_is_refused(self):
        for path in ("/api/telemetry", "/api/history"):
            self.assertEqual(self.get(self.port, path, host="evil.example")[0], 400)

    def test_the_dashboard_routes_do_not_take_posts(self):
        self.assertEqual(self.get(self.port, "/api/telemetry", method="POST", origin=True)[0], 404)

    def test_a_failing_dashboard_is_a_500_not_a_dropped_connection(self):
        self.stub.fail = True
        status, body, _ = self.get(self.port, "/api/telemetry")
        self.assertEqual(status, 500)
        self.assertTrue(body["error"])

    def test_without_a_dashboard_the_routes_are_not_found(self):
        port = self.serve(None)
        self.assertEqual(self.get(port, "/api/telemetry")[0], 404)
        self.assertEqual(self.get(port, "/api/history")[0], 404)


class MainTest(unittest.TestCase):
    def test_a_bad_setpoint_topic_stops_the_start_with_a_message(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"EVSIM_SETPOINT_TOPIC": "evsim/#"}), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            charger_main.main(["--port", "8099"])      # never bound: the setting is checked first
        self.assertIn("EVSIM_SETPOINT_TOPIC", stderr.getvalue())

    def test_a_taken_port_starts_no_dashboard(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        self.addCleanup(blocker.close)
        dashboard = mock.Mock()
        with mock.patch.object(charger_main, "build_dashboard", return_value=dashboard), self.assertRaises(OSError):
            charger_main.main(["--port", str(blocker.getsockname()[1])])
        dashboard.start.assert_not_called()
        dashboard.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_web.py'`
Expected: FAIL with `No module named 'charger_web.dashboard'`.

- [ ] **Step 3: Write the implementation**

Create `server/charger_web/dashboard.py`:

```python
"""Composes the three read-only sources of the dashboard and gives the web layer two methods to call."""

from . import config
from .history import History
from .inverter import InverterReader
from .telemetry import Telemetry, build_client


class Dashboard:
    def __init__(self, inverter, telemetry, history, mqtt_client=None, broker=None):
        self._inverter = inverter
        self._telemetry = telemetry
        self._history = history
        self._client = mqtt_client
        self._broker = broker              # (host, port)
        self._started = False

    def telemetry(self):
        """The latest readings: the MQTT values from the subscriber plus the inverter's DC power."""
        data = self._telemetry.snapshot()
        data["inverter"] = {"dc_w": self._inverter.reading()}
        return data

    def history(self):
        return self._history.snapshot()

    def start(self):
        """Start the background readers. connect_async: a broker that is down is retried, never raised."""
        if self._started:
            return
        self._started = True
        self._inverter.start()
        if self._client is not None:
            host, port = self._broker
            self._client.connect_async(host, port, keepalive=config.MQTT_KEEPALIVE_S)
            self._client.loop_start()
        self._history.start()

    def stop(self):
        if not self._started:
            return
        self._started = False
        self._history.stop()
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
        self._inverter.stop()


def build_dashboard():
    """The real dashboard from the environment. Raises ValueError for a bad setting; starts nothing."""
    inverter_host, mqtt_host, topic = config.inverter_host(), config.mqtt_host(), config.setpoint_topic()
    inverter = InverterReader(inverter_host)
    client = build_client()
    telemetry = Telemetry(client, topic)
    history = History(telemetry, inverter)
    return Dashboard(inverter, telemetry, history, client, (mqtt_host, config.MQTT_PORT))
```

Overwrite `server/charger_web/web.py`:

```python
"""Loopback-only HTTP API and static page server."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .service import ChargerError, ChargerService, LimitValidationError
from .config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT

log = logging.getLogger("charger_web.http")
STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 1024


class _LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(charger=None, host=DEFAULT_WEB_HOST, port=DEFAULT_WEB_PORT, dashboard=None):
    if host != DEFAULT_WEB_HOST:
        raise ValueError("The charger page must bind to 127.0.0.1.")
    client = charger or ChargerService()

    class Handler(BaseHTTPRequestHandler):
        server_version = "EVSimChargerWeb/1"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _host_matches(self):
            expected = "127.0.0.1" if self.server.server_port == 80 else f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host", "").lower() == expected

        def _send(self, code, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; style-src 'self'; "
                "script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, body):
            self._send(code, json.dumps(body, separators=(",", ":"), allow_nan=False))

        def _dashboard_json(self, produce):
            try:
                data = json.dumps(produce(), separators=(",", ":"), allow_nan=False)
            except Exception:
                log.exception("dashboard data failed")
                self._json(500, {"error": "Dashboard data is unavailable."})
                return
            self._send(200, data)

        def do_GET(self):
            if not self._host_matches():
                self._json(400, {"error": "Use the local http://127.0.0.1 URL."})
                return
            path = urlsplit(self.path).path
            if path == "/api/charger/status":
                self._json(200, client.status())
                return
            if dashboard is not None and path == "/api/telemetry":
                self._dashboard_json(dashboard.telemetry)
                return
            if dashboard is not None and path == "/api/history":
                self._dashboard_json(dashboard.history)
                return
            asset = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
            }.get(path)
            if asset is None:
                self._json(404, {"error": "Not found."})
                return
            filename, mime = asset
            try:
                content = (STATIC_DIR / filename).read_bytes()
            except OSError:
                self._json(500, {"error": "Page assets are unavailable."})
                return
            self._send(200, content, mime)

        def do_POST(self):
            if not self._host_matches():
                self._json(400, {"error": "Use the local http://127.0.0.1 URL."})
                return
            expected_origin = (
                "http://127.0.0.1"
                if self.server.server_port == 80
                else f"http://127.0.0.1:{self.server.server_port}"
            )
            if self.headers.get("Origin") != expected_origin:
                self._json(403, {"error": "Cross-origin writes are not allowed."})
                return
            path = urlsplit(self.path).path
            if path != "/api/charger/operator-limit":
                self._json(404, {"error": "Not found."})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "Expected application/json."})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(413, {"error": "Request body is missing or too large."})
                return
            try:
                payload = json.loads(self.rfile.read(length), object_pairs_hook=_unique_object)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._json(400, {"error": "Request body must be valid JSON."})
                return
            if not isinstance(payload, dict) or set(payload) != {"current_limit_a"}:
                self._json(400, {"error": "Expected current_limit_a."})
                return
            try:
                result = client.set_limit(payload["current_limit_a"])
            except LimitValidationError as exc:
                self._json(400, {"success": False, "error": str(exc)})
                return
            except ChargerError as exc:
                self._json(503, {"success": False, "error": str(exc)})
                return
            self._json(200, result)

        def do_OPTIONS(self):
            self._json(405, {"error": "Method not allowed."})

    return _LocalHTTPServer((host, port), Handler)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result
```

Overwrite `server/charger_web/__main__.py`:

```python
"""Run the local EVSim page: the AMTRON charger over Modbus TCP, plus the read-only dashboard."""

import argparse
import logging
from http.server import ThreadingHTTPServer

from .config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT
from .dashboard import build_dashboard
from .service import ChargerService
from .web import create_server


def _port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be from 1 through 65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m charger_web",
        description="Serve the local EVSim page: the AMTRON charger over Modbus TCP (read and set its current) and, "
                    "read-only, the inverter's DC power and the MQTT telemetry. It does not start the setpoint server.",
    )
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help="must be 127.0.0.1")
    parser.add_argument("--port", type=_port, default=DEFAULT_WEB_PORT, help="local page port (default %(default)s)")
    args = parser.parse_args(argv)
    if args.host != DEFAULT_WEB_HOST:
        parser.error("the charger web interface must bind to 127.0.0.1")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("charger_web")
    server: ThreadingHTTPServer | None = None
    service = None
    dashboard = None
    try:
        try:
            service = ChargerService()
            dashboard = build_dashboard()
        except ValueError as exc:
            parser.error(str(exc))
        server = create_server(charger=service, dashboard=dashboard, host=args.host, port=args.port)   # bind first: a taken port must not start a poll
        service.start()          # the background poller is also the charger's heartbeat
        dashboard.start()
        log.info("charger page listening at http://127.0.0.1:%d/", args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping charger page")
    finally:
        if server is not None:
            server.server_close()
        if dashboard is not None:
            dashboard.stop()
        if service is not None:
            service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Amend the spec. In `docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md`, in the unit table replace the row

```
| `web.py` | Two new GET endpoints. Everything existing is unchanged. | |
```

with

```
| `dashboard.py` — `Dashboard` | Composes the three units above, starts and stops them, and gives `web.py` two methods: the latest readings and the history. | the three units |
| `web.py` | Two new GET endpoints. Everything existing is unchanged. | `dashboard.py` |
```

and in the Testing section replace `The existing charger tests must still pass unchanged.` with `The existing charger tests must still pass unchanged, apart from the one assertion on the page's heading (the heading becomes "EVSim").`

- [ ] **Step 4: Run it to verify it passes, and that the charger tests still do**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_web.py'` then `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_*.py'`
Expected: the dashboard tests OK; the charger tests OK **except** `test_the_page_is_served` is still passing (the heading is unchanged until Task 7).

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/charger_web/dashboard.py server/charger_web/web.py server/charger_web/__main__.py \
  server/tests/test_dashboard_web.py docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md
git commit -m "Serve the dashboard data and start it with the charger page

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The page: power chain, chart and heading

**Files:**
- Modify: `server/charger_web/static/index.html` (overwritten)
- Create: `server/charger_web/static/chart.js`
- Create: `server/charger_web/static/dashboard.js`
- Modify: `server/charger_web/static/app.css` (appended)
- Modify: `server/charger_web/web.py` (two asset routes)
- Modify: `server/tests/test_charger_web.py` (one assertion)
- Modify: `server/tests/test_charger_page.py` (overwritten)
- Test: `server/tests/test_dashboard_assets.py`

**Interfaces:**
- Consumes: `GET /api/telemetry` and `GET /api/history` (Tasks 4 to 6) with the shapes defined there.
- Produces: `chart.js` exports (for Node tests only, through a `module.exports` guard) `segments(frames, key, maxGapS = 2.5) -> list[list[[t, value]]]` and `axis(value) -> {top, step}`; in the browser it defines `SERIES`, `segments`, `axis`, `drawChart(canvas, frames, visible, nowS)`. `dashboard.js` defines `byId`, polls telemetry every 1 s and history every 2 s and renders them. `app.js` is not touched.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_charger_web.py`, replace this line:

```python
        self.assertIn(b"AMTRON charger", body)
```

with:

```python
        self.assertIn(b"<h1>EVSim</h1>", body)
```

Overwrite `server/tests/test_charger_page.py`:

```python
import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "charger_web" / "static"
SCRIPTS = ("app.js", "chart.js", "dashboard.js")
SERIES = {"cp_w", "setpoint_w", "target_dc_w", "dc_w"}


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.series = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id":
                self.ids.add(value)
            if key == "data-series":
                self.series.append(value)


def parsed_page():
    parser = _Ids()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser


def page_ids():
    return parsed_page().ids


def script_text(name):
    return (STATIC / name).read_text(encoding="utf-8")


def node_json(expression):
    script = "const c = require(%s); console.log(JSON.stringify(%s));" % (json.dumps(str(STATIC / "chart.js")), expression)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class PageTest(unittest.TestCase):
    def test_every_element_the_scripts_look_up_exists_in_the_page(self):
        used = set()
        for name in SCRIPTS:
            used |= set(re.findall(r'\b(?:el|byId)\("([^"]+)"\)', script_text(name)))
        self.assertTrue(used)
        self.assertEqual(sorted(used - page_ids()), [])

    def test_the_page_has_the_modbus_elements(self):
        for needed in ("banner", "operator-limit", "operator-warning", "vehicle-state", "charger-status"):
            self.assertIn(needed, page_ids())

    def test_the_page_has_the_dashboard_elements(self):
        for needed in ("mqtt-state", "board-setpoint", "board-detail", "board-updated", "setpoint-label",
                       "setpoint-value", "target-value", "server-detail", "setpoint-updated", "inverter-dc",
                       "inverter-updated", "chart", "chart-note", "legend-setpoint"):
            self.assertIn(needed, page_ids())

    def test_no_rest_session_leftovers_in_the_page(self):
        text = script_text("app.js") + (STATIC / "index.html").read_text(encoding="utf-8")
        for leftover in ("reconnect", "session_taken", "not_configured", "Credentials"):
            self.assertNotIn(leftover, text)

    def test_the_scripts_never_build_markup_from_strings(self):
        for name in SCRIPTS:
            text = script_text(name)
            for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(banned, text, (name, banned))

    def test_the_legend_has_one_entry_per_series_and_a_canvas(self):
        page = parsed_page()
        self.assertEqual(sorted(page.series), sorted(SERIES))
        self.assertIn("chart", page.ids)

    def test_the_scripts_load_in_dependency_order(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("/chart.js"), html.index("/dashboard.js"))
        self.assertIn("/app.js", html)

    def test_the_heading_is_evsim(self):
        self.assertIn("<h1>EVSim</h1>", (STATIC / "index.html").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_scripts_parse(self):
        for name in SCRIPTS:
            result = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, (name, result.stderr))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_missing_frame_breaks_the_line(self):
        frames = "[{t:0,v:1},{t:1,v:2},{t:2,v:3},{t:6,v:4},{t:7,v:5}]"
        self.assertEqual(node_json('c.segments(%s, "v")' % frames), [[[0, 1], [1, 2], [2, 3]], [[6, 4], [7, 5]]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_null_value_breaks_the_line_and_a_lone_point_is_kept(self):
        frames = "[{t:0,v:1},{t:1,v:null},{t:2,v:3},{t:3},{t:4,v:5},{t:5,v:6}]"
        self.assertEqual(node_json('c.segments(%s, "v")' % frames), [[[0, 1]], [[2, 3]], [[4, 5], [5, 6]]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_axis_steps_with_the_data(self):
        self.assertEqual(
            node_json("[c.axis(0), c.axis(4361), c.axis(12000)]"),
            [{"top": 2000, "step": 1000}, {"top": 6000, "step": 2000}, {"top": 12000, "step": 4000}],
        )


if __name__ == "__main__":
    unittest.main()
```

Create `server/tests/test_dashboard_assets.py`:

```python
import http.client
import threading
import unittest

from charger_web.web import create_server


class AssetsTest(unittest.TestCase):
    def setUp(self):
        self.server = create_server(charger=object(), port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, response.getheader("Content-Type"), body

    def test_the_new_scripts_are_served_as_javascript(self):
        for path in ("/chart.js", "/dashboard.js", "/app.js"):
            status, kind, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(kind.startswith("text/javascript"), (path, kind))
            self.assertTrue(body, path)

    def test_the_page_names_the_scripts_it_loads(self):
        _, _, body = self.get("/")
        for path in (b"/chart.js", b"/dashboard.js", b"/app.js"):
            self.assertIn(path, body)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_page.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_assets.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_web.py'`
Expected: FAIL. The page tests fail on the missing files and ids; the assets test on a 404; `test_the_page_is_served` on the heading.

- [ ] **Step 3: Write the implementation**

Overwrite `server/charger_web/static/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EVSim</title>
  <link rel="stylesheet" href="/app.css">
  <script src="/chart.js" defer></script>
  <script src="/dashboard.js" defer></script>
  <script src="/app.js" defer></script>
</head>
<body>
  <main class="page">
    <header class="page-header">
      <div>
        <p class="eyebrow">EVSim <span aria-hidden="true">/</span> local control</p>
        <h1>EVSim</h1>
        <p class="subheading">Charger, setpoint and inverter, live</p>
      </div>
      <span id="connection" class="connection" data-state="loading">Connecting…</span>
    </header>

    <p id="banner" class="banner" role="status" hidden></p>

    <section class="panel limit-panel" aria-labelledby="limit-title">
      <div class="section-heading">
        <div>
          <p class="eyebrow">AMTRON charger · setting</p>
          <h2 id="limit-title">Maximum current</h2>
        </div>
        <span id="installation-limit" class="ceiling">Installation limit: —</span>
      </div>
      <form id="limit-form">
        <label for="current-limit">Set maximum current</label>
        <div class="control-row">
          <div class="input-wrap">
            <input id="current-limit" name="current_limit_a" type="number" inputmode="numeric" step="1" min="0" max="16" value="0" required>
            <span aria-hidden="true">A</span>
          </div>
          <button id="apply" type="submit">Apply</button>
        </div>
        <p class="hint">Choose 0 A (pause) or a whole value from 6 A to the installation limit. The charger keeps this value while this page is running.</p>
        <p id="write-message" class="message" role="status" aria-live="polite"></p>
      </form>
      <div class="current-setting">
        <span>Set on the charger</span>
        <strong id="configured-limit">—</strong>
      </div>
      <div class="current-setting">
        <span>Operator limit (set on the AMTRON page)</span>
        <strong id="operator-limit">—</strong>
      </div>
      <p id="operator-warning" class="warning" hidden></p>
      <p id="settings-updated" class="updated">Waiting for charger settings</p>
    </section>

    <section class="readings" aria-label="Charger current readings">
      <article class="panel reading-card">
        <p class="eyebrow">Vehicle signal</p>
        <h2>Signalled current</h2>
        <div id="signalled-current" class="reading-value">—</div>
        <p class="description">Limit currently advertised to the simulated vehicle</p>
        <p id="signalled-updated" class="updated">Waiting for charger data</p>
      </article>
      <article class="panel reading-card">
        <p class="eyebrow">Energy meter</p>
        <h2>Measured current draw</h2>
        <div id="meter-current" class="meter-values"><span class="reading-value">—</span></div>
        <p class="description">Current measured by the charger, per phase</p>
        <p id="meter-updated" class="updated">Waiting for charger data</p>
      </article>
    </section>

    <section class="panel state-panel" aria-label="Charger state">
      <div><span class="eyebrow">Vehicle state</span><strong id="vehicle-state">—</strong></div>
      <div><span class="eyebrow">Charger status</span><strong id="charger-status">—</strong></div>
    </section>

    <section class="chain" aria-label="Power chain">
      <div class="chain-heading">
        <h2>Power chain</h2>
        <span id="mqtt-state" class="connection" data-state="loading">Broker …</span>
      </div>
      <div class="chain-cards">
        <article class="panel chain-card">
          <p class="eyebrow">Board</p>
          <h3>Setpoint, raw</h3>
          <div id="board-setpoint" class="chain-value">—</div>
          <p id="board-detail" class="description">Waiting for the board</p>
          <p id="board-updated" class="updated">Waiting for data</p>
        </article>
        <article class="panel chain-card">
          <p id="setpoint-label" class="eyebrow">Setpoint</p>
          <h3>Setpoint, 1 s average</h3>
          <div id="setpoint-value" class="chain-value">—</div>
          <p class="description">DC target <strong id="target-value">—</strong></p>
          <p id="server-detail" class="description">Waiting for the server</p>
          <p id="setpoint-updated" class="updated">Waiting for data</p>
        </article>
        <article class="panel chain-card">
          <p class="eyebrow">Inverter</p>
          <h3>DC power, MPPT 1</h3>
          <div id="inverter-dc" class="chain-value">—</div>
          <p class="description">Taken from the charger side, read over Modbus</p>
          <p id="inverter-updated" class="updated">Waiting for data</p>
        </article>
      </div>
    </section>

    <section class="panel chart-panel" aria-labelledby="chart-title">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Last 10 minutes</p>
          <h2 id="chart-title">Power</h2>
        </div>
        <span id="chart-note" class="ceiling">Waiting for history</span>
      </div>
      <canvas id="chart" role="img" aria-label="Line chart of the board setpoint, the setpoint average, the DC target and the inverter DC power over the last ten minutes"></canvas>
      <div class="legend">
        <button type="button" class="legend-item" data-series="cp_w" aria-pressed="true"><i class="swatch"></i><span>Board, raw</span></button>
        <button type="button" class="legend-item" data-series="setpoint_w" aria-pressed="true"><i class="swatch"></i><span id="legend-setpoint">Setpoint, average</span></button>
        <button type="button" class="legend-item" data-series="target_dc_w" aria-pressed="true"><i class="swatch"></i><span>DC target</span></button>
        <button type="button" class="legend-item" data-series="dc_w" aria-pressed="true"><i class="swatch"></i><span>Inverter DC power</span></button>
      </div>
    </section>

    <footer>
      <span id="last-updated">Readings refresh every 2 seconds.</span>
      <button id="refresh" class="text-button" type="button">Refresh now</button>
    </footer>
  </main>
</body>
</html>
```

Create `server/charger_web/static/chart.js`:

```javascript
// The power chart: pure helpers (tested with Node) and the canvas drawing. No library.
const SERIES = [
  { key: "cp_w", label: "Board, raw", color: "#8aa699" },
  { key: "setpoint_w", label: "Setpoint, average", color: "#176b4a" },
  { key: "target_dc_w", label: "DC target", color: "#c27b14" },
  { key: "dc_w", label: "Inverter DC power", color: "#2a5fa8" },
];
const WINDOW_S = 600;
const MAX_GAP_S = 2.5;      // frames come once a second; a longer silence is a gap, not a slope
const GRID = "#dce4df";
const MUTED = "#69766f";

// Runs of consecutive frames that have a number for `key`. A null or missing value, or a missing frame,
// ends a run, so an outage is drawn as a gap.
function segments(frames, key, maxGapS = MAX_GAP_S) {
  const runs = [];
  let run = [];
  for (const frame of frames) {
    const value = frame[key];
    if (typeof value !== "number" || !Number.isFinite(value)) {
      if (run.length) runs.push(run);
      run = [];
      continue;
    }
    if (run.length && frame.t - run[run.length - 1][0] > maxGapS) {
      runs.push(run);
      run = [];
    }
    run.push([frame.t, value]);
  }
  if (run.length) runs.push(run);
  return runs;
}

// The top of the y-axis and the spacing of its grid lines, in watts, for the largest value shown.
function axis(value) {
  const step = value <= 4000 ? 1000 : value <= 10000 ? 2000 : 4000;
  return { top: Math.max(step * 2, Math.ceil(value / step) * step), step };
}

function drawChart(canvas, frames, visible, nowS) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 860;
  const height = canvas.clientHeight || 300;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const left = 58, right = 12, top = 12, bottom = 28;
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  const tMin = nowS - WINDOW_S;
  let highest = 0;
  for (const series of SERIES) {
    if (!visible.has(series.key)) continue;
    for (const frame of frames) {
      if (typeof frame[series.key] === "number" && frame[series.key] > highest) highest = frame[series.key];
    }
  }
  const scale = axis(highest);
  const x = (t) => left + ((t - tMin) / WINDOW_S) * plotW;
  const y = (v) => top + plotH - (v / scale.top) * plotH;

  ctx.font = "12px system-ui, sans-serif";
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let v = 0; v <= scale.top; v += scale.step) {
    ctx.strokeStyle = GRID;
    ctx.beginPath();
    ctx.moveTo(left, y(v));
    ctx.lineTo(left + plotW, y(v));
    ctx.stroke();
    ctx.fillStyle = MUTED;
    ctx.fillText(`${v} W`, left - 8, y(v));
  }
  ctx.textBaseline = "top";
  for (const [seconds, label, align] of [[-WINDOW_S, "−10 min", "left"], [-WINDOW_S / 2, "−5 min", "center"], [0, "now", "right"]]) {
    ctx.textAlign = align;
    ctx.fillText(label, x(nowS + seconds), top + plotH + 8);
  }

  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  for (const series of SERIES) {
    if (!visible.has(series.key)) continue;
    ctx.strokeStyle = series.color;
    ctx.fillStyle = series.color;
    for (const run of segments(frames, series.key)) {
      if (run.length === 1) {
        ctx.beginPath();
        ctx.arc(x(run[0][0]), y(run[0][1]), 2, 0, 2 * Math.PI);
        ctx.fill();
        continue;
      }
      ctx.beginPath();
      run.forEach(([t, v], index) => (index === 0 ? ctx.moveTo(x(t), y(v)) : ctx.lineTo(x(t), y(v))));
      ctx.stroke();
    }
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = { SERIES, segments, axis };
```

Create `server/charger_web/static/dashboard.js`:

```javascript
// The power chain and the chart: polls /api/telemetry every second and /api/history every two.
const byId = (id) => document.getElementById(id);
const dashboardState = {
  frames: [],
  visible: new Set(SERIES.map((series) => series.key)),
  busy: { telemetry: false, history: false },
};

function formatWatts(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} W`;
}

function ageText(reading) {
  if (!reading || reading.status === "unavailable") return "No reading yet";
  const stale = reading.status === "stale";
  if (!reading.updated_at) return stale ? "Stale" : "Updated";
  const date = new Date(reading.updated_at);
  const time = Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleTimeString();
  return `${stale ? "Stale · last updated" : "Updated"} ${time}`;
}

function showWatts(valueId, reading, updatedId) {
  const target = byId(valueId);
  target.textContent = formatWatts(reading && reading.value);
  target.dataset.status = reading ? reading.status : "unavailable";
  if (updatedId) byId(updatedId).textContent = ageText(reading);
}

function renderTelemetry(data) {
  const connected = Boolean(data.mqtt && data.mqtt.connected);
  const badge = byId("mqtt-state");
  badge.dataset.state = connected ? "ok" : "unavailable";
  badge.textContent = connected ? "Broker connected" : "Broker not connected";

  const board = data.board || {};
  showWatts("board-setpoint", board.setpoint_w, "board-updated");
  byId("board-detail").textContent = `${board.status || "no status"} · pilot ${board.state || "—"}`;

  const setpoint = data.setpoint || {};
  const topic = setpoint.topic || "—";
  byId("setpoint-label").textContent = `Setpoint · ${topic}`;
  byId("legend-setpoint").textContent = `Setpoint, average (${topic})`;
  showWatts("setpoint-value", setpoint.setpoint_w, "setpoint-updated");

  const server = data.server || {};
  showWatts("target-value", server.target_dc_w);
  byId("server-detail").textContent = `${setpoint.status || "no status"} · server ${server.mode || "mode unknown"}`;

  showWatts("inverter-dc", data.inverter && data.inverter.dc_w, "inverter-updated");
}

function redrawChart() {
  drawChart(byId("chart"), dashboardState.frames, dashboardState.visible, Date.now() / 1000);
}

function renderHistory(data) {
  dashboardState.frames = Array.isArray(data.frames) ? data.frames : [];
  byId("chart-note").textContent = `${dashboardState.frames.length} s of history · updated ${new Date().toLocaleTimeString()}`;
  redrawChart();
}

async function pollDashboard(kind, path, render, fail) {
  if (dashboardState.busy[kind]) return;
  dashboardState.busy[kind] = true;
  try {
    const response = await fetch(path, { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    render(body);
  } catch (error) {
    fail(error);
  } finally {
    dashboardState.busy[kind] = false;
  }
}

function pollTelemetry() {
  void pollDashboard("telemetry", "/api/telemetry", renderTelemetry, () => {
    const badge = byId("mqtt-state");
    badge.dataset.state = "unavailable";
    badge.textContent = "Dashboard data unavailable";
  });
}

function pollHistory() {
  void pollDashboard("history", "/api/history", renderHistory, () => {
    byId("chart-note").textContent = "History unavailable";
  });
}

for (const button of document.querySelectorAll(".legend-item")) {
  button.addEventListener("click", () => {
    const key = button.dataset.series;
    if (dashboardState.visible.has(key)) dashboardState.visible.delete(key);
    else dashboardState.visible.add(key);
    button.setAttribute("aria-pressed", String(dashboardState.visible.has(key)));
    redrawChart();
  });
}
window.addEventListener("resize", redrawChart);
pollTelemetry();
pollHistory();
window.setInterval(pollTelemetry, 1000);
window.setInterval(pollHistory, 2000);
```

Append to `server/charger_web/static/app.css`:

```css

/* The power chain and the chart. The swatch colours repeat the ones in chart.js. */
h3 { font-size: .98rem; letter-spacing: -.01em; margin: 0 0 14px; }
.chain { margin-top: 28px; }
.chain-heading { align-items: center; display: flex; gap: 20px; justify-content: space-between; margin-bottom: 14px; }
.chain-cards { display: grid; gap: 18px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.chain-card { padding: 22px; }
.chain-value { font-size: clamp(1.7rem, 3.4vw, 2.4rem); font-variant-numeric: tabular-nums; font-weight: 740; letter-spacing: -.05em; margin-bottom: 11px; }
.chain-card .description { margin-bottom: 10px; }
[data-status="stale"], [data-status="unavailable"] { opacity: .45; }
.connection[data-state="ok"] { background: var(--green-pale); color: var(--green-dark); }
.chart-panel { margin-top: 28px; padding: 24px; }
.chart-panel .section-heading { margin-bottom: 14px; }
#chart { display: block; height: 300px; width: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.legend-item { align-items: center; background: #f0f5f2; color: var(--ink); display: inline-flex; font-size: .82rem; font-weight: 650; gap: 8px; padding: 7px 12px; }
.legend-item:hover { background: #e4ede8; }
.legend-item[aria-pressed="false"] { opacity: .4; text-decoration: line-through; }
.swatch { border-radius: 50%; display: inline-block; height: 12px; width: 12px; }
.legend-item[data-series="cp_w"] .swatch { background: #8aa699; }
.legend-item[data-series="setpoint_w"] .swatch { background: #176b4a; }
.legend-item[data-series="target_dc_w"] .swatch { background: #c27b14; }
.legend-item[data-series="dc_w"] .swatch { background: #2a5fa8; }

@media (max-width: 620px) {
  .chain-cards { grid-template-columns: 1fr; }
}
```

Serve the two scripts. In `server/charger_web/web.py`, replace this block:

```python
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
```

with:

```python
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/chart.js": ("chart.js", "text/javascript; charset=utf-8"),
                "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
```

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_page.py'`, `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_assets.py'`, then `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_*.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_*.py'`
Expected: all OK. Node is installed here, so the five Node-based tests run rather than skip.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/charger_web/static server/charger_web/web.py server/tests/test_charger_page.py \
  server/tests/test_charger_web.py server/tests/test_dashboard_assets.py
git commit -m "Show the power chain and a ten-minute chart on the page

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Docs, the whole suite and the live check

**Files:**
- Modify: `server/README.md`
- Create: `memory/evsim-dashboard.md`
- Modify: `memory/MEMORY.md` (one line appended)

**Interfaces:**
- Consumes: everything above.
- Produces: the user-facing documentation and a note for later sessions.

- [ ] **Step 1: Update the README**

In `server/README.md`, replace this paragraph:

```
A small page, run as a separate process from the MQTT averaging server, to read the AMTRON's current and set its
limit over **Modbus TCP**. It uses only the Python standard library and binds to `127.0.0.1`, so it is available
only on the development computer.
```

with:

```
A small page, run as a separate process from the MQTT averaging server, to read the AMTRON's current and set its
limit over **Modbus TCP**. It also shows, read-only, the Fronius inverter's DC power, the board's `evsim/cp`, the
setpoint topic and the server's DC target, with a 10-minute chart (see "The dashboard" below). It binds to
`127.0.0.1`, so it is available only on the development computer. It needs `paho-mqtt` (see `requirements.txt`).
```

Then insert, immediately before the line `## Test`, this section:

```
### The dashboard

The same page shows the whole chain. Nothing in it can write to the inverter or publish to MQTT.

- **Inverter:** MPPT 1's DC power, read over Modbus TCP once a second from `192.168.21.120` (unit 1). Only SunSpec
  model 160 is read. `EVSIM_INVERTER_HOST` changes the address. The inverter allows several Modbus clients; the
  setpoint server's writer and this page can run together.
- **MQTT:** the page subscribes (client id `evsim-charger-web`) to `evsim/cp`, the setpoint topic and
  `evsim/inverter`, on the broker at `127.0.0.1:1883`. `EVSIM_MQTT_HOST` changes the broker. The
  `evsim/inverter` values (the DC target and the server's mode) exist only while the setpoint server runs with
  `--inverter`. A retained message, such as `server_offline`, is shown as the last known state and flagged stale.
- **The setpoint topic** is `evsim/setpoint`. Set `EVSIM_SETPOINT_TOPIC` (for example `evsim/electric_car`) to follow
  another topic. Its message must carry a numeric `setpoint_w`; `charger_web/parsers.py` (`parse_setpoint`) is the
  one place to change if the format differs. A `setpoint_w` outside 0 to 12 000 W is dropped, not clamped.
- **The chart** keeps the last 10 minutes in memory (a restart empties it): the board's raw setpoint (the mean of the
  5 Hz samples in each second), the 1 s average, the DC target and the inverter's DC power. A value that is stale or
  missing leaves a gap. Click a legend entry to hide or show a line.
- Each source fails alone: with the broker down the charger and inverter parts still work, and the reverse.

Design: `docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md`.
```

- [ ] **Step 2: Write the note for later sessions**

Create `memory/evsim-dashboard.md`:

```markdown
---
name: evsim-dashboard
description: The local web page (server/charger_web) also shows the inverter's DC power, evsim/cp, the setpoint topic and the server's DC target, with a 10-minute chart; read-only
metadata:
  type: project
---

The charger page became an EVSim dashboard on 2026-10-01 (spec `docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md`,
plan `docs/superpowers/plans/2026-10-01-evsim-dashboard.md`). Same process, same port (8080), still loopback only.

- **Inverter:** `charger_web/inverter.py` reads SunSpec model 160 once a second (one request) and keeps MPPT 1's
  `DCW` (the module whose id is 1). Only the DC power; no AC, no model 103, no writes. A second Modbus client next to
  the setpoint server's writer.
- **MQTT:** `charger_web/telemetry.py` subscribes (client id `evsim-charger-web`, never the setpoint server's id) to
  `evsim/cp`, the setpoint topic and `evsim/inverter`; parsers in `parsers.py`. Retained messages are shown stale.
- **History:** `charger_web/history.py`, 600 frames at 1 Hz in memory; the chart is `static/chart.js`.
- **For the car emulation:** the setpoint topic is `EVSIM_SETPOINT_TOPIC` (default `evsim/setpoint`). When
  `evsim/electric_car` exists, set it and adapt `parse_setpoint` if the payload differs.

**Why:** the loop only proves itself by comparing the board's setpoint, the average, the DC target and the DC power
the inverter really takes; before this they lived in `mosquitto_sub` and the inverter tool.

**How to apply:** see [[evsim-inverter-fronius]] for the SunSpec details, [[evsim-setpoint-server]] for the topics
and [[evsim-amtron-modbus]] for the charger half of the page.
```

Append this line to `memory/MEMORY.md`:

```
- [EVSim dashboard](evsim-dashboard.md) — the page also shows inverter DC power, evsim/cp, the setpoint and the DC target with a 10-minute chart; read-only.
```

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/python -B -m unittest discover -s tests`
Expected: OK (the earlier 178 tests plus the new dashboard tests; about two minutes, the bench tests start their own Mosquitto).

- [ ] **Step 4: The live check (read-only, real broker, real inverter)**

Needs the lab: the Mosquitto broker, the board publishing, and ideally the setpoint server running with `--inverter 192.168.21.120` (it is the third Modbus client in the test below). The old page may already be running on port 8080; use 8081 for this check. The check writes nothing: the page only reads the charger and the inverter and never publishes.

```bash
cd server
.venv/bin/python -B -m charger_web --port 8081 > /tmp/dashboard-live.log 2>&1 &
echo $! > /tmp/dashboard-live.pid
sleep 15
curl -s http://127.0.0.1:8081/api/telemetry | python3 -m json.tool
```

Expected: `mqtt.connected` true; `board`, `setpoint` and `server` readings with `status` `ok`; `inverter.dc_w` `ok` with a value in watts.

Compare with the server's own number, and add a third Modbus client at the same time (read-only):

```bash
mosquitto_sub -h 127.0.0.1 -t evsim/inverter -C 1 -W 5
.venv/bin/python ../tools/fronius_modbus.py status
curl -s http://127.0.0.1:8081/api/telemetry | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['inverter']['dc_w'], d['server']['target_dc_w'])"
```

Expected: the page's `inverter.dc_w` agrees with `dc_w` in the `evsim/inverter` message and with the tool's MPPT 1 DC power, each within a few watts (they are read at slightly different moments); no errors in `/tmp/dashboard-live.log` while three clients are connected.

```bash
sleep 120
curl -s http://127.0.0.1:8081/api/history | python3 -c "import json,sys; d=json.load(sys.stdin); f=d['frames']; print(len(f), f[-1])"
```

Expected: about 130 frames, the last one with all four values numbers (or `cp_w` null if the board is silent).

Open `http://127.0.0.1:8081/` in a browser and check: the charger section as before; three power-chain cards with values; four lines on the chart that follow each other (the DC power tracks the DC target within about 100 W); a legend click hides and shows a line; stopping the setpoint server (Ctrl-C) turns its cards stale within a few seconds and shows `server_offline`, and leaves a gap in the three lines it feeds. Restart the setpoint server afterwards if it was running.

Then stop the check and record the result:

```bash
kill "$(cat /tmp/dashboard-live.pid)"; rm /tmp/dashboard-live.pid
```

Append a `## Live check, <date>` section to `memory/evsim-dashboard.md` with what was seen (agreement in watts, gaps, any error). If three Modbus clients caused any error on the inverter, say so there: it is the open risk in the spec.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/evsim-dashboard
git add server/README.md memory/evsim-dashboard.md memory/MEMORY.md
git commit -m "Document the dashboard and record the live check

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.**

| Spec requirement | Task |
|---|---|
| Inverter reader: model 160 only, one request a second, module id 1, scale factor with the value, 65535 is `None`, chain walked per connection, back-off, env host | 2 (settings in 1) |
| Subscriber: own client id, async connect, subscribe in `on_connect`, QoS 0, no will, setpoint topic validation, parsers, retained handling | 1, 3, 4 |
| Readings and staleness (3 s MQTT, 5 s inverter, forced stale) | 1, 2, 4 |
| History: 600 frames, 1 Hz, mean of raw samples, gaps | 5 |
| HTTP: `/api/telemetry`, `/api/history`, Host check, headers, unchanged charger routes | 6 |
| Dashboard composition and start-up order (bind first, nothing started on a taken port) | 6 |
| Page: heading, power-chain cards, chart with four lines, legend toggles, `textContent` only, gaps | 7 |
| Error handling table (broker down, inverter down, server stopped, malformed, bad topic) | 2, 3, 4, 6 |
| Testing list, including the live check with three clients | 2 to 8 |
| README and notes | 8 |

**Placeholder scan.** No TBD or "similar to" steps; every code step holds the code.

**Type consistency.** `reading()` is `(value, at, now, stale_after, force_stale, wall)` in Tasks 1, 2 and 4; `InverterReader.reading()` and `Telemetry.snapshot()` produce that shape, which `History` (Task 5) reads through `["status"]` and `["value"]`; `Dashboard.telemetry()` adds `"inverter": {"dc_w": ...}`, which `dashboard.js` reads as `data.inverter.dc_w`. Field names `cp_w`, `setpoint_w`, `target_dc_w`, `dc_w` are the same in `history.py`, `chart.js`, `dashboard.js` and the page's `data-series`.
