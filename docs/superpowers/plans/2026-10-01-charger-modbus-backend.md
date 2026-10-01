# Charger Page over Modbus TCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local AMTRON charger page read and set the charger's current over Modbus TCP, keep the charger alive with a background poller, and remove the REST code.

**Architecture:** A new `ChargerService` in `server/charger_web/service.py` owns one Modbus connection and a poller thread that reads the charger every 2 s (the heartbeat the charger needs) into a cache. The HTTP layer answers from the cache and sends writes through the service, which writes register 1000, reads it back, and then keeps the confirmed limit as the "desired" limit. Tests run against a fake AMTRON on localhost. The page is updated to the new status, and the REST client, session code and credentials are deleted last.

**Tech Stack:** Python 3 standard library only (`socketserver`, `http.server`, `threading`, `unittest`), HTML, CSS and browser JavaScript. Node is used only to syntax-check the script.

**Spec:** [docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md](../specs/2026-10-01-charger-modbus-backend-design.md). It amends [2026-09-30-charger-web-interface-design.md](../specs/2026-09-30-charger-web-interface-design.md); where they disagree, the 2026-10-01 spec wins.

Work on branch `feat/amtron-charger-modbus`, in a worktree of that exact branch (not on `master`). **Another session also pushes to this branch**: run `git pull --ff-only` before starting and before any push, and stop to ask if the pull is not a fast-forward.

## Global Constraints

Every task's requirements include these, copied from the spec.

- **Modbus only.** Every register is a **holding register (function code 3)**; function code 4 is refused by the charger. The only write is function code 6 to **register 1000** (`HEMS_CURRENT_LIMIT`), value 0 to 16. **Register 134 (the operator limit) is read only and is never written.**
- Unit ID **255**, port **502**. Register map: 1000 set limit, 134 operator limit, 133 installation limit (inferred, reads 16), 706 signalled current, 122 vehicle state (A=1, B=2, C=3), 104 charger status, 212/214/216 per-phase current in **mA as 32-bit values, high word first**; `0xFFFFFFFF` means no meter and is shown as unavailable, never as 0.
- **One thread owns one persistent Modbus connection and polls every 2 s** (`POLL_PERIOD_S`), whether or not a page is open. This is the heartbeat: after 30 s without Modbus traffic the charger falls back to 0 A. HTTP handlers answer from a cache and **never talk to the charger on the read path**; a stalled charger must not block them.
- Allowed limits: **0 A or a whole number from 6 A through `min(16, installation limit)`**, where the installation limit is register 133 when it reads, else 16. The backend validates (the browser's checks are a convenience); booleans, fractions, strings, 1-5 A, negatives and values above the ceiling are rejected **before any write**. A write is reported successful only when register 1000 reads back the requested value.
- **The service never writes on its own at start**, and a read or page load never writes. The only writes are Apply and the restore: after a confirmed Apply the service keeps a *desired* limit and re-writes it **at most once per 10 s** if register 1000 differs. The desired limit is empty at start and is set only after a confirmed write.
- Link state from the time since the last good poll: `ok` (under 5 s), `stale` (5 to 30 s), `fallback_expected` (30 s or more), `unavailable` (no good poll yet). A zero reading is shown as zero; a missing or refused register as unavailable.
- Loopback only (`127.0.0.1`), standard library only, no credentials. The REST client, login, session code and `--backend` option are removed in Task 5.

## Review Focus

Inputs and conditions the spec implies but the tests might not exercise, most likely to bite first. Each has a test in the task that owns the code.

1. **Reading the charger can resume charging** (it restores its stored limit on the first request after a fallback). The service must therefore never write at start or on its own, and must not set a desired limit from a value it merely read. Tested in Task 2.
2. **The poller thread dies on one unexpected exception.** The heartbeat then stops and the charger drops to 0 A within 30 s. The thread must survive any exception in a poll. Tested in Task 2.
3. **A charger that accepts the connection but never answers.** `status()` and the HTTP handlers must still answer at once from the cache while the poll waits for its timeout. Tested in Task 1.
4. **The restore loop.** It must write at most once per 10 s, never when no limit was ever confirmed, and never after a write that was refused or not confirmed. Tested in Task 2.
5. **A missing meter versus a real zero** (`0xFFFFFFFF` against 0 mA), and a single refused register not failing the whole poll. Tested in Task 1.

---

### Task 1: The read path: register map, fake charger, poller cache and status

**Files:**
- Modify: `server/charger_web/config.py` (append constants)
- Replace: `server/charger_web/amtron_registers.json`
- Create: `server/charger_web/registers.py`, `server/charger_web/service.py`
- Create: `server/tests/fake_amtron.py`, `server/tests/test_charger_service.py`

**Interfaces:**
- Produces:
  - `config`: `MODBUS_TIMEOUT_S`, `POLL_PERIOD_S`, `STALE_AFTER_S`, `FALLBACK_AFTER_S`, `RESTORE_MIN_INTERVAL_S`, `MIN_LIMIT_A`, `MAX_LIMIT_A`.
  - `registers.load_map(path=None) -> dict` with `unit` and `registers` (each `{"address", "size"?, "scale"?}`); raises `ValueError` if a required register is missing.
  - `service.ChargerError(RuntimeError)`, `service.LimitValidationError(ChargerError)`.
  - `service.ChargerService(host=None, port=None, register_map=None, clock=time.monotonic, period_s=None)` with `poll_once()`, `status() -> dict` and `stop()`. `status()` returns `status`, `link`, `error`, `set_limit_a`, `operator_limit_a`, `installation_limit_a`, `signalled_current_a`, `meter_current_a` (value is a dict of phase to A, or `None`), `vehicle_state` (value `"A"` to `"E"`), `charger_status` (value is text), each as `{"value", "status", "updated_at"}`, plus `paused`, `operator_below_set`, `desired_limit_a`, `restored_at`.
  - `tests/fake_amtron.py`: `FakeAmtron()` with `start() -> port`, `stop()`, `regs`, `log` (`(function code, address, count or value)`), `writes`, `refuse_writes`, `refuse_reads` (a set of addresses), `ignore_writes`, `fall_back()`, `set_u32(address, value)`.

- [ ] **Step 1: Add the constants, the register map and the loader**

Append to `server/charger_web/config.py`:

```python

# The Modbus charger service (docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md).
MODBUS_TIMEOUT_S = 3
POLL_PERIOD_S = 2                 # the poller reads this often; it is also the charger's heartbeat
STALE_AFTER_S = 5                 # a reading older than this, or polls failing this long, is stale
FALLBACK_AFTER_S = 30             # no Modbus traffic for this long: the charger drops to its safe current
RESTORE_MIN_INTERVAL_S = 10       # the desired limit is re-written at most this often
MIN_LIMIT_A = 6                   # the lowest non-zero limit the page accepts
MAX_LIMIT_A = 16                  # the highest limit the page accepts
```

Overwrite `server/charger_web/amtron_registers.json`:

```json
{
  "_note": "AMTRON Charge Control (CC612), MENNEKES ECU Modbus TCP spec rev 1.07, confirmed on firmware 5.33 (2026-09-30). Every register is a holding register (function code 3). size 2 means two registers, high word first. Register 134 is read only; the writable limit is 1000.",
  "unit": 255,
  "registers": {
    "set_limit_a": {"address": 1000},
    "operator_limit_a": {"address": 134},
    "installation_limit_a": {"address": 133},
    "signalled_current_a": {"address": 706},
    "vehicle_state": {"address": 122},
    "charger_status": {"address": 104},
    "meter_current_l1_a": {"address": 212, "size": 2, "scale": 0.001},
    "meter_current_l2_a": {"address": 214, "size": 2, "scale": 0.001},
    "meter_current_l3_a": {"address": 216, "size": 2, "scale": 0.001}
  }
}
```

Create `server/charger_web/registers.py`:

```python
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
```

- [ ] **Step 2: Write the fake AMTRON**

Create `server/tests/fake_amtron.py`:

```python
"""A fake AMTRON Modbus TCP server on localhost, for the charger page's tests.

Holding registers only (function codes 3 and 6; function code 4 is refused with exception 2, as on the
real unit). It records every request, and can imitate the charger's fallback: after fall_back() the
next request sees the safe current (limit 0, signalled 0), and the charger then restores the stored limit.
Nothing here touches the real charger.
"""

import socket
import socketserver
import struct
import threading

DEFAULTS = {
    1000: 10,                 # HEMS_CURRENT_LIMIT
    134: 16,                  # OPERATOR_CURRENT_LIMIT
    133: 16,                  # installation limit (inferred)
    706: 10,                  # SIGNALED_CURRENT
    122: 3,                   # VEHICLE_STATE: C
    104: 1,                   # OCPP_CP_STATUS: Occupied
    212: 0, 213: 874,         # METER_CUR_L1 = 874 mA (high word, low word)
    214: 0, 215: 651,
    216: 0, 217: 808,
}


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        fake = self.server.fake
        fake.clients.append(self.request)
        while True:
            head = self.request.recv(7)
            if len(head) < 7:
                return
            tid, _, length, unit = struct.unpack(">HHHB", head)
            body = self.request.recv(length - 1)
            fc = body[0]
            addr, arg = struct.unpack(">HH", body[1:5])
            with fake.lock:
                out = fake.respond(fc, addr, arg, body)
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(out) + 1, unit) + out)


class FakeAmtron:
    def __init__(self):
        self.regs = dict(DEFAULTS)
        self.log = []                 # (function code, address, count or value)
        self.writes = []              # (address, value)
        self.refuse_writes = False    # answer every write with Modbus exception 3
        self.refuse_reads = set()     # addresses answered with exception 2
        self.ignore_writes = False    # echo a write but keep the old value
        self.in_fallback = False
        self._stored = None
        self.clients = []
        self.lock = threading.Lock()
        self._server = None

    def start(self):
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        return self._server.server_address[1]

    def stop(self):
        for client in list(self.clients):
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._server.shutdown()
        self._server.server_close()

    def set_u32(self, address, value):
        self.regs[address], self.regs[address + 1] = value >> 16, value & 0xFFFF

    def fall_back(self):
        """The charger lost its Modbus master: it signals the safe current (0 A) until the next request."""
        self._stored = self.regs[1000]
        self.regs[1000] = 0
        self.regs[706] = 0
        self.in_fallback = True

    def _signal(self):
        self.regs[706] = min(self.regs[1000], self.regs[134])

    def _restore(self):
        self.regs[1000] = self._stored
        self._signal()
        self.in_fallback = False

    def respond(self, fc, addr, arg, body):
        self.log.append((fc, addr, arg))
        restore_after = self.in_fallback
        try:
            if fc == 3:
                addrs = range(addr, addr + arg)
                if any(a not in self.regs or a in self.refuse_reads for a in addrs):
                    return bytes([fc | 0x80, 2])
                return bytes([fc, 2 * arg]) + struct.pack(">%dH" % arg, *(self.regs[a] for a in addrs))
            if fc == 6:
                self.writes.append((addr, arg))
                if self.refuse_writes or addr not in self.regs:
                    return bytes([fc | 0x80, 3])
                if not self.ignore_writes:
                    self.regs[addr] = arg
                    if addr == 1000:
                        self._signal()          # the signalled current follows the limit
                return body[:5]
            return bytes([fc | 0x80, 2])        # input registers and everything else: refused
        finally:
            if restore_after:
                self._restore()                 # the request that found it in fallback sees 0; later ones see the limit
```

- [ ] **Step 3: Write the failing tests**

Create `server/tests/test_charger_service.py`:

```python
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from fake_amtron import FakeAmtron
from charger_web import config
from charger_web.registers import load_map
from charger_web.service import ChargerError, ChargerService, LimitValidationError


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.clock = FakeClock()
        self.service = ChargerService("127.0.0.1", self.port, clock=self.clock)
        self.addCleanup(self.service.stop)


class RegisterMapTest(unittest.TestCase):
    def test_the_shipped_map_matches_the_documented_registers(self):
        m = load_map()
        self.assertEqual(m["unit"], 255)
        addresses = {name: spec["address"] for name, spec in m["registers"].items()}
        self.assertEqual(addresses, {
            "set_limit_a": 1000, "operator_limit_a": 134, "installation_limit_a": 133,
            "signalled_current_a": 706, "vehicle_state": 122, "charger_status": 104,
            "meter_current_l1_a": 212, "meter_current_l2_a": 214, "meter_current_l3_a": 216,
        })
        self.assertEqual(m["registers"]["meter_current_l1_a"], {"address": 212, "size": 2, "scale": 0.001})

    def test_a_map_missing_a_register_is_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"unit": 255, "registers": {"set_limit_a": {"address": 1000}}}, f)
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ValueError) as cm:
            load_map(f.name)
        self.assertIn("operator_limit_a", str(cm.exception))


class ReadTest(ServiceTest):
    def test_before_the_first_poll_everything_is_unavailable_and_nothing_is_sent(self):
        s = self.service.status()
        self.assertEqual((s["status"], s["link"]), ("unavailable", "unavailable"))
        self.assertEqual(s["set_limit_a"]["status"], "unavailable")
        self.assertEqual(self.fake.log, [])

    def test_a_poll_fills_the_status(self):
        self.service.poll_once()
        s = self.service.status()
        self.assertEqual((s["status"], s["link"], s["error"]), ("ok", "ok", None))
        self.assertEqual(s["set_limit_a"]["value"], 10)
        self.assertEqual(s["operator_limit_a"]["value"], 16)
        self.assertEqual(s["installation_limit_a"]["value"], 16)
        self.assertEqual(s["signalled_current_a"]["value"], 10)
        self.assertEqual(s["vehicle_state"]["value"], "C")
        self.assertEqual(s["charger_status"]["value"], "Occupied")
        self.assertEqual(s["meter_current_a"]["value"], {"L1": 0.874, "L2": 0.651, "L3": 0.808})
        self.assertEqual(s["set_limit_a"]["status"], "ok")
        self.assertIsNotNone(s["set_limit_a"]["updated_at"])
        self.assertEqual((s["paused"], s["operator_below_set"], s["desired_limit_a"]), (False, False, None))

    def test_32_bit_currents_are_read_as_two_registers_high_word_first(self):
        self.fake.set_u32(212, 70_000)                 # 70.000 A needs both words
        self.service.poll_once()
        self.assertEqual(self.service.status()["meter_current_a"]["value"]["L1"], 70.0)
        self.assertIn((3, 212, 2), self.fake.log)

    def test_a_missing_meter_is_unavailable_and_a_real_zero_is_zero(self):
        self.fake.set_u32(212, 0xFFFFFFFF)
        self.service.poll_once()
        self.assertEqual(sorted(self.service.status()["meter_current_a"]["value"]), ["L2", "L3"])
        for address in (212, 214, 216):
            self.fake.set_u32(address, 0xFFFFFFFF)
        self.service.poll_once()
        meter = self.service.status()["meter_current_a"]
        self.assertEqual((meter["value"], meter["status"]), (None, "unavailable"))
        for address in (212, 214, 216):
            self.fake.set_u32(address, 0)
        self.service.poll_once()
        self.assertEqual(self.service.status()["meter_current_a"]["value"], {"L1": 0.0, "L2": 0.0, "L3": 0.0})

    def test_zero_is_a_reading_not_missing_data(self):
        self.fake.regs[706] = 0
        self.service.poll_once()
        r = self.service.status()["signalled_current_a"]
        self.assertEqual((r["value"], r["status"]), (0, "ok"))

    def test_only_holding_registers_are_used_and_nothing_is_written(self):
        for _ in range(3):
            self.service.poll_once()
        self.assertEqual({entry[0] for entry in self.fake.log}, {3})
        self.assertEqual(self.fake.writes, [])

    def test_one_refused_register_does_not_fail_the_others(self):
        self.fake.refuse_reads = {133}
        self.service.poll_once()
        s = self.service.status()
        self.assertEqual(s["installation_limit_a"]["status"], "unavailable")
        self.assertEqual(s["set_limit_a"]["value"], 10)
        self.assertEqual(s["link"], "ok")

    def test_paused_and_operator_below_set_flags(self):
        self.fake.regs[706] = 0
        self.service.poll_once()
        self.assertTrue(self.service.status()["paused"])
        self.fake.regs[706] = 6
        self.fake.regs[134] = 6
        self.service.poll_once()
        s = self.service.status()
        self.assertFalse(s["paused"])
        self.assertTrue(s["operator_below_set"])

    def test_link_states_follow_the_time_since_the_last_good_poll(self):
        self.service.poll_once()
        self.fake.stop()
        self.clock.t += 4
        self.service.poll_once()                        # fails, but the last good poll is only 4 s old
        self.assertEqual(self.service.status()["link"], "ok")
        self.clock.t += 2                               # 6 s
        s = self.service.status()
        self.assertEqual((s["link"], s["status"]), ("stale", "stale"))
        self.assertIsNotNone(s["error"])
        self.assertEqual((s["set_limit_a"]["value"], s["set_limit_a"]["status"]), (10, "stale"))
        self.clock.t += 25                              # 31 s
        self.assertEqual(self.service.status()["link"], "fallback_expected")

    def test_a_charger_that_never_answers_does_not_block_status(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        with mock.patch.object(config, "MODBUS_TIMEOUT_S", 1):
            stalled = ChargerService("127.0.0.1", listener.getsockname()[1])
            self.addCleanup(stalled.stop)
            poll = threading.Thread(target=stalled.poll_once)    # accepted by the backlog, never answered
            poll.start()
            time.sleep(0.2)
            started = time.monotonic()
            status = stalled.status()
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(status["link"], "unavailable")
            poll.join(timeout=5)
            self.assertFalse(poll.is_alive())
        self.assertIsNotNone(stalled.status()["error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_service.py' 2>&1 | grep -E "Error"`
Expected: `ModuleNotFoundError: No module named 'charger_web.service'` (and the same for `charger_web.registers` if the loader is missing).

- [ ] **Step 5: Write the service (read path)**

Create `server/charger_web/service.py`:

```python
"""Background Modbus poller and limit keeper for the AMTRON charger page.

One thread owns one Modbus connection and reads the charger every POLL_PERIOD_S. That is also the
heartbeat the charger needs: after FALLBACK_AFTER_S without Modbus traffic it drops to its safe current
(0 A on this unit), and the next request restores the limit last written. HTTP handlers read a cache and
never talk to the charger on the read path; writes go through the same I/O lock.
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
)
PHASES = (("L1", "meter_current_l1_a"), ("L2", "meter_current_l2_a"), ("L3", "meter_current_l3_a"))
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
                "vehicle_state": self._reading(state_text, at["vehicle_state"], link, now),
                "charger_status": self._reading(status_text, at["charger_status"], link, now),
                "paused": bool(set_limit and signalled == 0),
                "operator_below_set": bool(set_limit and operator is not None and operator < set_limit),
                "desired_limit_a": desired,
                "restored_at": self._timestamp(restored_at),
            }

    def stop(self):
        """Stop the poller (if it is running) and close the connection."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * config.MODBUS_TIMEOUT_S + 2)
        self._modbus.close()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_service.py' -v 2>&1 | tail -18`
Expected: `Ran 12 tests` and `OK`.

- [ ] **Step 7: Commit**

```bash
git add server/charger_web/config.py server/charger_web/amtron_registers.json server/charger_web/registers.py \
        server/charger_web/service.py server/tests/fake_amtron.py server/tests/test_charger_service.py
git commit -m "$(cat <<'EOF'
Read the charger over Modbus: register map, fake charger and polled status

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The write path, the desired limit and the poller thread

**Files:**
- Modify: `server/charger_web/service.py`
- Test: `server/tests/test_charger_service.py`

**Interfaces:**
- Consumes: everything from Task 1.
- Produces, on `ChargerService`:
  - `set_limit(value) -> {"success": True, "set_limit_a": int, "updated_at": str}`: validates, writes register 1000, reads it back, and only then records the desired limit. Raises `LimitValidationError` (nothing written) or `ChargerError` (refused, unreachable or not confirmed; the desired limit stays as it was).
  - `start()`: runs `poll_once()` every `period_s` on a daemon thread that survives any exception. `stop()` already exists.
  - After each good poll the service re-writes the desired limit if register 1000 differs, at most once per `RESTORE_MIN_INTERVAL_S`, and `status()["restored_at"]` shows when.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_charger_service.py`, before the final `if __name__ == "__main__":` block:

```python
class WriteTest(ServiceTest):
    def test_set_limit_writes_register_1000_and_confirms_it(self):
        self.service.poll_once()
        result = self.service.set_limit(8)
        self.assertEqual((result["success"], result["set_limit_a"]), (True, 8))
        self.assertEqual(self.fake.writes, [(1000, 8)])
        self.assertEqual(self.fake.regs[1000], 8)
        s = self.service.status()
        self.assertEqual((s["set_limit_a"]["value"], s["desired_limit_a"]), (8, 8))

    def test_zero_is_accepted(self):
        self.service.poll_once()
        self.assertEqual(self.service.set_limit(0)["set_limit_a"], 0)
        self.assertEqual(self.fake.writes, [(1000, 0)])

    def test_invalid_limits_are_rejected_before_any_write(self):
        self.service.poll_once()
        for bad in (5, 1, -1, 17, 7.5, 8.0, True, False, "8", None):
            with self.subTest(bad=bad):
                with self.assertRaises(LimitValidationError):
                    self.service.set_limit(bad)
        self.assertEqual(self.fake.writes, [])

    def test_the_installation_limit_caps_the_choice_and_defaults_to_16(self):
        self.fake.regs[133] = 10
        self.service.poll_once()
        with self.assertRaises(LimitValidationError) as cm:
            self.service.set_limit(12)
        self.assertIn("installation limit of 10 A", str(cm.exception))
        self.assertEqual(self.fake.writes, [])
        self.fake.refuse_reads = {133}                 # unreadable: the ceiling is 16
        self.service.poll_once()
        self.assertEqual(self.service.set_limit(12)["set_limit_a"], 12)

    def test_a_refused_write_is_an_error_and_sets_no_desired_limit(self):
        self.service.poll_once()
        self.fake.refuse_writes = True
        with self.assertRaises(ChargerError):
            self.service.set_limit(8)
        self.assertIsNone(self.service.status()["desired_limit_a"])

    def test_an_unconfirmed_write_is_reported_and_sets_no_desired_limit(self):
        self.service.poll_once()
        self.fake.ignore_writes = True                 # the charger echoes the write but keeps the old value
        with self.assertRaises(ChargerError) as cm:
            self.service.set_limit(8)
        self.assertIn("not confirmed", str(cm.exception))
        self.assertIsNone(self.service.status()["desired_limit_a"])

    def test_an_unreachable_charger_during_a_write_is_an_error(self):
        self.service.poll_once()
        self.fake.stop()
        with self.assertRaises(ChargerError):
            self.service.set_limit(8)
        self.assertIsNone(self.service.status()["desired_limit_a"])


class RestoreTest(ServiceTest):
    def test_it_never_writes_on_its_own_at_start(self):
        self.fake.regs[1000] = 0                       # e.g. the charger fell back to 0 A
        for _ in range(3):
            self.clock.t += 20
            self.service.poll_once()
        self.assertEqual(self.fake.writes, [])
        self.assertIsNone(self.service.status()["desired_limit_a"])

    def test_a_value_it_merely_read_never_becomes_the_desired_limit(self):
        self.service.poll_once()
        self.assertIsNone(self.service.status()["desired_limit_a"])
        self.assertEqual(self.fake.writes, [])

    def test_a_lost_limit_is_restored_at_most_once_per_interval(self):
        self.service.poll_once()
        self.service.set_limit(8)
        self.fake.regs[1000] = 0                       # a charger reboot, or another Modbus master
        self.clock.t += 1
        self.service.poll_once()
        self.assertEqual(self.fake.writes, [(1000, 8), (1000, 8)])
        self.assertIsNotNone(self.service.status()["restored_at"])
        self.fake.regs[1000] = 0
        self.clock.t += 1
        self.service.poll_once()                       # within 10 s: no new write
        self.assertEqual(len(self.fake.writes), 2)
        self.clock.t += config.RESTORE_MIN_INTERVAL_S
        self.service.poll_once()
        self.assertEqual(len(self.fake.writes), 3)

    def test_nothing_is_restored_when_the_limit_already_matches(self):
        self.service.poll_once()
        self.service.set_limit(8)
        for _ in range(3):
            self.clock.t += 20
            self.service.poll_once()
        self.assertEqual(self.fake.writes, [(1000, 8)])

    def test_the_fallback_cycle_ends_with_the_limit_back_in_force(self):
        self.service.poll_once()
        self.service.set_limit(10)
        self.fake.fall_back()                          # 30 s of silence: the charger signals 0 A
        self.clock.t += 31
        self.service.poll_once()                       # the first request sees 0 and puts the limit back
        self.assertEqual(self.fake.regs[1000], 10)
        self.assertEqual(self.fake.regs[706], 10)
        self.assertEqual(self.fake.writes[-1], (1000, 10))
        self.assertIsNotNone(self.service.status()["restored_at"])

    def test_a_refused_restore_write_does_not_raise_or_spin(self):
        self.service.poll_once()
        self.service.set_limit(8)
        self.fake.refuse_writes = True
        self.fake.regs[1000] = 0
        for _ in range(3):
            self.clock.t += 1
            self.service.poll_once()                   # logs and carries on
        self.assertEqual(len([w for w in self.fake.writes if w == (1000, 8)]), 2)   # the Apply and one attempt


class ThreadTest(ServiceTest):
    def test_the_poller_thread_polls_repeatedly_and_stops(self):
        service = ChargerService("127.0.0.1", self.port, period_s=0.05)
        self.addCleanup(service.stop)
        service.start()
        deadline = time.monotonic() + 3
        while len([e for e in self.fake.log if e[1] == 1000]) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len([e for e in self.fake.log if e[1] == 1000]), 4)
        service.stop()
        polls = len(self.fake.log)
        time.sleep(0.2)
        self.assertEqual(len(self.fake.log), polls)

    def test_the_poller_thread_survives_an_unexpected_exception(self):
        service = ChargerService("127.0.0.1", self.port, period_s=0.05)
        self.addCleanup(service.stop)
        real = service.poll_once
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("something unexpected")
            real()

        service.poll_once = flaky
        service.start()
        deadline = time.monotonic() + 3
        while len(calls) < 5 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(calls), 5)         # it kept polling after the exception
        self.assertEqual(service.status()["link"], "ok")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_service.py' 2>&1 | grep -E "^(ERROR|FAIL):|^Ran|^FAILED"`
Expected: failures for the new tests (`AttributeError: ... has no attribute 'set_limit'` and `'start'`), and the 13 Task 1 tests still pass.

- [ ] **Step 3: Add the write path, the restore and the thread**

```bash
python3 - <<'PYEDIT'
p = "server/charger_web/service.py"
s = open(p).read()


def sub(old, new):
    global s
    assert s.count(old) == 1, old[:60]
    s = s.replace(old, new)


# the restore runs after every good poll
sub("""                for name, value in values.items():
                    self._values[name] = value
                    self._at[name] = None if value is None else now
""", """                for name, value in values.items():
                    self._values[name] = value
                    self._at[name] = None if value is None else now
            if error is None:
                self._restore_if_needed(now)
""")

sub("    def stop(self):\n", '''    # -- keeping and setting the limit ------------------------------------------------------

    def _restore_if_needed(self, now):
        """Put the desired limit back if the charger lost it. Never runs before a confirmed Apply."""
        with self._cache_lock:
            desired, current = self._desired, self._values["set_limit_a"]
        if desired is None or current is None or current == desired:
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

    def set_limit(self, value):
        """Write register 1000, read it back, and only then remember it as the desired limit."""
        with self._cache_lock:
            installation = self._values["installation_limit_a"]
        self._validate_limit(value, config.MAX_LIMIT_A if installation is None else installation)
        with self._io_lock:
            try:
                self._modbus.write_single(self._registers["set_limit_a"]["address"], value)
                confirmed = self._read_register("set_limit_a")
            except ModbusError as exc:
                self._modbus.close()
                raise ChargerError(f"Modbus write was not confirmed ({exc}); check the setting before retrying.") from exc
            if confirmed != value:
                raise ChargerError(f"Write was not confirmed; charger reports {confirmed!r} A.")
            now = self._clock()
            with self._cache_lock:
                self._values["set_limit_a"], self._at["set_limit_a"] = confirmed, now
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
''')
open(p, "w").write(s)
PYEDIT
cd server && python3 -B -m unittest discover -s tests -p 'test_charger_service.py' 2>&1 | tail -3
```

Expected: `Ran 27 tests` and `OK` (12 from Task 1 and 15 new).

- [ ] **Step 4: Commit**

```bash
git add server/charger_web/service.py server/tests/test_charger_service.py
git commit -m "$(cat <<'EOF'
Set the charger's limit over Modbus and keep it with a background poller

Apply writes register 1000, reads it back and only then remembers the
desired limit. The poller is the heartbeat, survives any exception, and
re-writes a lost limit at most once per 10 s. The service never writes on
its own at start.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Wire the service into the HTTP layer and the entry point

**Files:**
- Modify: `server/charger_web/web.py`
- Replace: `server/charger_web/__main__.py`
- Test: `server/tests/test_charger_web.py`

**Interfaces:**
- Consumes: `ChargerService`, `ChargerError`, `LimitValidationError` (Tasks 1 and 2).
- Produces:
  - `web.create_server(charger=None, host="127.0.0.1", port=8080)` now takes a `ChargerService` (default: a new one). `GET /api/charger/status` returns `service.status()`; `POST /api/charger/operator-limit` with `{"current_limit_a": n}` calls `service.set_limit(n)` and returns its result (200), `400` for a validation error or a malformed body, `503` for a charger error. `POST /api/charger/reconnect` no longer exists (404).
  - `python -m charger_web` takes only `--host` (must be 127.0.0.1) and `--port`; it creates and **starts** the service, serves the page, and stops the service on exit.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_charger_web.py`:

```python
import contextlib
import http.client
import io
import json
import threading
import unittest

from fake_amtron import FakeAmtron
from charger_web import __main__ as charger_main
from charger_web.service import ChargerService
from charger_web.web import create_server


class WebTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.service = ChargerService("127.0.0.1", port)
        self.addCleanup(self.service.stop)
        self.service.poll_once()
        self.http = create_server(charger=self.service, port=0)
        self.web_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def request(self, method, path, body=None, origin=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=5)
        headers = {"Host": host or "127.0.0.1:%d" % self.web_port}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if method == "POST" and origin:
            headers["Origin"] = "http://127.0.0.1:%d" % self.web_port
        conn.request(method, path, data, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        kind = response.getheader("Content-Type", "")
        return response.status, (json.loads(raw) if kind.startswith("application/json") else raw)

    def test_status_comes_from_the_cache_and_never_touches_the_charger(self):
        before = len(self.fake.log)
        status, body = self.request("GET", "/api/charger/status")
        self.assertEqual(status, 200)
        self.assertEqual((body["link"], body["set_limit_a"]["value"], body["vehicle_state"]["value"]), ("ok", 10, "C"))
        self.assertEqual(len(self.fake.log), before)

    def test_apply_writes_register_1000(self):
        status, body = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8})
        self.assertEqual((status, body["success"], body["set_limit_a"]), (200, True, 8))
        self.assertEqual(self.fake.writes, [(1000, 8)])

    def test_invalid_values_are_rejected_without_a_write(self):
        for bad in (5, 17, 7.5, True, "8", None):
            status, _ = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": bad})
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.fake.writes, [])

    def test_other_bodies_are_rejected(self):
        self.assertEqual(self.request("POST", "/api/charger/operator-limit", {"limit": 8})[0], 400)
        self.assertEqual(self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8, "x": 1})[0], 400)
        self.assertEqual(self.fake.writes, [])

    def test_a_cross_origin_post_is_refused(self):
        status, _ = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8}, origin=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.fake.writes, [])

    def test_a_wrong_host_header_is_refused(self):
        self.assertEqual(self.request("GET", "/api/charger/status", host="evil.example")[0], 400)

    def test_a_refused_write_is_a_503_with_the_reason(self):
        self.fake.refuse_writes = True
        status, body = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8})
        self.assertEqual((status, body["success"]), (503, False))
        self.assertTrue(body["error"])

    def test_the_rest_reconnect_route_is_gone(self):
        self.assertEqual(self.request("POST", "/api/charger/reconnect", {})[0], 404)

    def test_the_page_is_served(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"AMTRON charger", body)

    def test_only_loopback_is_allowed(self):
        with self.assertRaises(ValueError):
            create_server(charger=self.service, host="0.0.0.0", port=0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            charger_main.main(["--host", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_web.py' 2>&1 | grep -E "^(ERROR|FAIL):|^Ran|^FAILED"`
Expected: `FAILED`, with errors in the apply, invalid-value, other-body, refused-write and reconnect tests (the old handler calls methods the service does not have).

- [ ] **Step 3: Update `web.py` and replace `__main__.py`**

```bash
python3 - <<'PYEDIT'
p = "server/charger_web/web.py"
s = open(p).read()


def sub(old, new):
    global s
    assert s.count(old) == 1, old[:60]
    s = s.replace(old, new)


sub("from .charger import ChargerClient, ChargerError, LimitValidationError\n",
    "from .service import ChargerError, ChargerService, LimitValidationError\n")
sub("    client = charger or ChargerClient()\n", "    client = charger or ChargerService()\n")
sub('''            if path == "/api/charger/reconnect":
                self._json(200, client.reconnect())
                return
''', "")
sub('result = client.set_operator_limit(payload["current_limit_a"])', 'result = client.set_limit(payload["current_limit_a"])')
open(p, "w").write(s)
PYEDIT
```

Overwrite `server/charger_web/__main__.py`:

```python
"""Run the independent AMTRON charger web interface (Modbus TCP)."""

import argparse
import logging
from http.server import ThreadingHTTPServer

from .config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT
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
        description="Serve the local AMTRON charger page over Modbus TCP, without starting the MQTT setpoint server.",
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
    try:
        try:
            service = ChargerService()
        except ValueError as exc:
            parser.error(str(exc))
        service.start()          # the background poller is also the charger's heartbeat
        server = create_server(charger=service, host=args.host, port=args.port)
        log.info("charger page listening at http://127.0.0.1:%d/", args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping charger page")
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Then run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_web.py' 2>&1 | tail -3`
Expected: `Ran 10 tests` and `OK`.

- [ ] **Step 4: Commit**

```bash
git add server/charger_web/web.py server/charger_web/__main__.py server/tests/test_charger_web.py
git commit -m "$(cat <<'EOF'
Serve the charger page from the Modbus service

The HTTP layer answers from the poller's cache and sends Apply to register
1000. The entry point starts the poller as the charger's heartbeat. The REST
reconnect route is gone.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Update the page

**Files:**
- Replace: `server/charger_web/static/index.html`, `server/charger_web/static/app.js`
- Modify: `server/charger_web/static/app.css` (append)
- Test: `server/tests/test_charger_page.py`

**Interfaces:**
- Consumes: the status shape from Task 1 (`set_limit_a`, `operator_limit_a`, `installation_limit_a`, `signalled_current_a`, `meter_current_a`, `vehicle_state`, `charger_status`, `link`, `paused`, `operator_below_set`, `restored_at`) and the POST route from Task 3.
- Produces: a page that shows the set limit, the read-only operator limit with a warning when it caps the set limit, the signalled and per-phase measured current, the vehicle state and charger status, and a banner for a paused charger, an expected fallback, or a just-restored limit. The REST "session taken", "credentials" and "Reconnect" elements are gone.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_charger_page.py`:

```python
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "charger_web" / "static"


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        self.ids.update(value for key, value in attrs if key == "id")


def page_ids():
    parser = _Ids()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser.ids


class PageTest(unittest.TestCase):
    def test_every_element_the_script_looks_up_exists_in_the_page(self):
        used = set(re.findall(r'\bel\("([^"]+)"\)', (STATIC / "app.js").read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertEqual(sorted(used - page_ids()), [])

    def test_the_page_has_the_modbus_elements(self):
        for needed in ("banner", "operator-limit", "operator-warning", "vehicle-state", "charger-status"):
            self.assertIn(needed, page_ids())

    def test_no_rest_session_leftovers_in_the_page(self):
        text = (STATIC / "app.js").read_text(encoding="utf-8") + (STATIC / "index.html").read_text(encoding="utf-8")
        for leftover in ("reconnect", "session_taken", "not_configured", "Credentials"):
            self.assertNotIn(leftover, text)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_parses(self):
        result = subprocess.run(["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_page.py' 2>&1 | grep -E "^(FAIL|ERROR):|^Ran|^FAILED"`
Expected: `FAILED` with `test_the_page_has_the_modbus_elements` and `test_no_rest_session_leftovers_in_the_page` failing; the other two pass.

- [ ] **Step 3: Replace the page**

Overwrite `server/charger_web/static/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AMTRON charger</title>
  <link rel="stylesheet" href="/app.css">
  <script src="/app.js" defer></script>
</head>
<body>
  <main class="page">
    <header class="page-header">
      <div>
        <p class="eyebrow">EVSim <span aria-hidden="true">/</span> local control</p>
        <h1>AMTRON charger</h1>
        <p class="subheading">Current limit and live charger readings over Modbus</p>
      </div>
      <span id="connection" class="connection" data-state="loading">Connecting…</span>
    </header>

    <p id="banner" class="banner" role="status" hidden></p>

    <section class="panel limit-panel" aria-labelledby="limit-title">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Charger setting</p>
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

    <footer>
      <span id="last-updated">Readings refresh every 2 seconds.</span>
      <button id="refresh" class="text-button" type="button">Refresh now</button>
    </footer>
  </main>
</body>
</html>
```

Overwrite `server/charger_web/static/app.js`:

```javascript
const el = (id) => document.getElementById(id);
const form = el("limit-form");
const input = el("current-limit");
const applyButton = el("apply");
let polling = false;
let pollAgain = false;
let writePending = false;
let installationLimit = null;

function formatAmps(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unavailable";
  return `${value} A`;
}

function updateConnection(status, error) {
  const badge = el("connection");
  badge.dataset.state = status;
  const labels = {
    ok: "Charger connected",
    stale: "Showing stale readings",
    fallback_expected: "Charger silent",
    unavailable: "Charger unavailable",
  };
  badge.textContent = labels[status] || "Charger status unknown";
  el("last-updated").textContent = error || "Readings refresh every 2 seconds.";
}

function renderReading(target, reading, phases = false) {
  target.replaceChildren();
  if (!reading || reading.status === "unavailable" || reading.value === null || reading.value === undefined) {
    const value = document.createElement("span");
    value.className = "reading-value";
    value.textContent = "Unavailable";
    target.append(value);
    return;
  }
  if (phases && typeof reading.value === "object" && !Array.isArray(reading.value)) {
    for (const [phase, current] of Object.entries(reading.value)) {
      const item = document.createElement("span");
      item.className = "meter-phase";
      const label = document.createElement("small");
      label.textContent = phase;
      item.append(label, document.createTextNode(formatAmps(current)));
      target.append(item);
    }
    if (target.childElementCount === 0) {
      const value = document.createElement("span");
      value.className = "reading-value";
      value.textContent = "Unavailable";
      target.append(value);
    }
    return;
  }
  const value = document.createElement("span");
  value.className = "reading-value";
  value.textContent = formatAmps(reading.value);
  target.append(value);
}

function updateTimestamp(target, reading) {
  if (reading && reading.status === "unavailable") {
    target.textContent = "Reading unavailable";
    return;
  }
  if (!reading || !reading.updated_at) {
    target.textContent = "No successful reading yet";
    return;
  }
  const date = new Date(reading.updated_at);
  const time = Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleTimeString();
  target.textContent = `${reading.status === "stale" ? "Stale · last updated" : "Updated"} ${time}`;
}

function updateBanner(status) {
  const banner = el("banner");
  const setLimit = status.set_limit_a ? status.set_limit_a.value : null;
  let text = "";
  let kind = "warning";
  if (status.link === "fallback_expected") {
    text = "No reply from the charger for 30 s or more. It drops to 0 A when its Modbus master goes silent, and puts the limit back on the next request.";
  } else if (status.paused) {
    text = `Charging is paused: the charger signals 0 A although the limit is ${setLimit} A.`;
  } else if (status.restored_at) {
    const age = (Date.now() - new Date(status.restored_at).getTime()) / 1000;
    if (age >= 0 && age < 60) {
      text = "The charger lost its limit and this page put it back.";
      kind = "info";
    }
  }
  banner.hidden = text === "";
  banner.textContent = text;
  banner.dataset.kind = kind;
}

function applyStatus(status) {
  const empty = { value: null, status: "unavailable" };
  const signalled = status.signalled_current_a || empty;
  const meter = status.meter_current_a || empty;
  renderReading(el("signalled-current"), signalled);
  renderReading(el("meter-current"), meter, true);
  updateTimestamp(el("signalled-updated"), signalled);
  updateTimestamp(el("meter-updated"), meter);

  const setLimit = status.set_limit_a || empty;
  const operator = status.operator_limit_a || empty;
  const installation = status.installation_limit_a || empty;
  installationLimit = installation.value;
  el("configured-limit").textContent = formatAmps(setLimit.value);
  el("operator-limit").textContent = formatAmps(operator.value);
  el("installation-limit").textContent = `Installation limit: ${formatAmps(installation.value)}`;
  const warning = el("operator-warning");
  warning.hidden = !status.operator_below_set;
  warning.textContent = status.operator_below_set
    ? `The AMTRON page's own limit of ${operator.value} A caps the signalled current below the ${setLimit.value} A set here.`
    : "";
  el("vehicle-state").textContent = (status.vehicle_state && status.vehicle_state.value) || "Unavailable";
  el("charger-status").textContent = (status.charger_status && status.charger_status.value) || "Unavailable";
  updateTimestamp(el("settings-updated"), setLimit);
  updateBanner(status);
  input.max = installation.value === null ? 16 : Math.min(16, installation.value);
  if (setLimit.value !== null && document.activeElement !== input) input.value = setLimit.value;
  applyButton.disabled = writePending;
}

async function refresh() {
  if (polling) {
    pollAgain = true;
    return;
  }
  polling = true;
  try {
    const response = await fetch("/api/charger/status", { cache: "no-store" });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || `Request failed (${response.status})`);
    updateConnection(status.status, status.error);
    applyStatus(status);
  } catch (error) {
    updateConnection("unavailable", error.message);
    el("signalled-updated").textContent = "Refresh failed; last displayed value may be stale";
    el("meter-updated").textContent = "Refresh failed; last displayed value may be stale";
  } finally {
    polling = false;
    if (pollAgain) {
      pollAgain = false;
      void refresh();
    }
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = el("write-message");
  const value = Number(input.value);
  message.textContent = "";
  message.dataset.kind = "";
  if (!Number.isInteger(value) || (value !== 0 && (value < 6 || value > 16)) ||
      (installationLimit !== null && value > installationLimit)) {
    message.dataset.kind = "error";
    message.textContent = "Choose 0 A or a whole value from 6 A to the installation limit.";
    return;
  }

  writePending = true;
  applyButton.disabled = true;
  applyButton.textContent = "Applying…";
  try {
    const response = await fetch("/api/charger/operator-limit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_limit_a: value }),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || "The charger did not confirm the setting.");
    message.dataset.kind = "success";
    message.textContent = `Confirmed by charger: ${formatAmps(result.set_limit_a)}.`;
    pollAgain = true;
    await refresh();
  } catch (error) {
    message.dataset.kind = "error";
    message.textContent = error.message;
    pollAgain = true;
    await refresh();
  } finally {
    writePending = false;
    applyButton.textContent = "Apply";
    applyButton.disabled = false;
  }
});

el("refresh").addEventListener("click", () => void refresh());
void refresh();
window.setInterval(() => void refresh(), 2000);
```

Append to `server/charger_web/static/app.css`:

```css

.banner { background: #fff1dd; border-radius: 12px; color: var(--warning); font-size: .9rem; line-height: 1.5; margin: 0 0 18px; padding: 13px 16px; }
.banner[data-kind="info"] { background: var(--green-pale); color: var(--green-dark); }
.banner[hidden], .warning[hidden] { display: none; }
.warning { color: var(--warning); font-size: .84rem; margin: 12px 0 0; }
.state-panel { display: flex; gap: 48px; margin-top: 18px; padding: 20px 25px; }
.state-panel div { display: flex; flex-direction: column; gap: 6px; }
.state-panel .eyebrow { margin: 0; }
.state-panel strong { font-size: 1.05rem; }
.connection[data-state="fallback_expected"] { background: #fff1dd; color: var(--warning); }
```

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_page.py' -v 2>&1 | tail -6`
Expected: `Ran 4 tests` and `OK`.

- [ ] **Step 4: Commit**

```bash
git add server/charger_web/static/index.html server/charger_web/static/app.js server/charger_web/static/app.css server/tests/test_charger_page.py
git commit -m "$(cat <<'EOF'
Show the Modbus state on the charger page

The page shows the set limit, the read-only operator limit with a warning
when it caps the set limit, per-phase current, vehicle state and charger
status, and a banner for a paused charger, an expected fallback, or a
restored limit. The REST session elements are gone.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Remove the REST code and update the docs

**Files:**
- Delete: `server/charger_web/charger.py`, `server/charger_web/charger_modbus.py`, `server/tests/test_charger_turns.py`, `server/tests/test_charger_modbus.py`
- Replace: `server/charger_web/config.py`
- Modify: `server/README.md`, `tools/amtron_modbus_probe.py`
- Modify (notices): `docs/superpowers/specs/2026-09-30-charger-web-interface-design.md`, `docs/superpowers/plans/2026-09-30-charger-web-interface.md`, `docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md`
- Create: `memory/evsim-amtron-modbus.md`; modify `memory/MEMORY.md`, `memory/evsim-amtron-interfaces.md`
- Test: `server/tests/test_charger_modbus_only.py`

**Interfaces:**
- Consumes: the finished Modbus path from Tasks 1 to 4.
- Produces: a package with no REST client, no login, no credentials and no `--backend` option, and documentation that describes the Modbus page.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_charger_modbus_only.py`:

```python
import importlib
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "charger_web"
TESTS = Path(__file__).resolve().parent


class ModbusOnlyTest(unittest.TestCase):
    def test_the_rest_modules_are_gone(self):
        for name in ("charger", "charger_modbus"):
            with self.assertRaises(ImportError):
                importlib.import_module(f"charger_web.{name}")

    def test_no_rest_session_or_credential_code_remains(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
        for leftover in (
            "urllib.request", "/v1/login", "Authorization", "EVSIM_CHARGER_USERNAME", "EVSIM_CHARGER_PASSWORD",
            "charger_credentials", "SETTINGS_CACHE_S", "DASHBOARD_CACHE_S", "LOGIN_RETRY_DELAY_S",
            "IDLE_RELEASE_S", "HTTP_TIMEOUT_S", "--backend",
        ):
            self.assertNotIn(leftover, text, leftover)

    def test_the_old_rest_and_first_modbus_tests_are_gone(self):
        for name in ("test_charger_turns.py", "test_charger_modbus.py"):
            self.assertFalse((TESTS / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_modbus_only.py' 2>&1 | grep -E "^(FAIL|ERROR):|^Ran|^FAILED"`
Expected: `FAILED`, three failing tests.

- [ ] **Step 3: Delete the REST code and its tests, and replace the config**

```bash
git rm -q server/charger_web/charger.py server/charger_web/charger_modbus.py \
         server/tests/test_charger_turns.py server/tests/test_charger_modbus.py
```

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


def charger_host():
    host = os.environ.get("EVSIM_CHARGER_HOST", DEFAULT_CHARGER_HOST).strip()
    if not host or any(char in host for char in "/?#@"):
        raise ValueError("EVSIM_CHARGER_HOST must be a host name or address, without a URL path.")
    return host


def modbus_register_map():
    return os.environ.get("EVSIM_CHARGER_REGISTERS") or None
```

- [ ] **Step 4: Update the README, the probe tool and the doc notices**

```bash
python3 - <<'PYEDIT'
def sub(path, old, new):
    s = open(path).read()
    assert s.count(old) == 1, (path, old[:60])
    open(path, "w").write(s.replace(old, new))


# README: replace the charger section (it sits inside the setpoint server's "Run" section) and put it before "## Test"
p = "server/README.md"
s = open(p).read()
i, j = s.index("## Local AMTRON charger page"), s.index("Watch the output:")
s = s[:i] + s[j:]
section = """## Local AMTRON charger page

A small page, run as a separate process from the MQTT averaging server, to read the AMTRON's current and set its
limit over **Modbus TCP**. It uses only the Python standard library and binds to `127.0.0.1`, so it is available
only on the development computer.

Enable the charger's Modbus server once, in the AMTRON web config: "Modbus TCP Server for energy management
systems" On, base port 502, register address set MENNEKES, "Allow Start/Stop Transaction" Off. Set the operator
limit on the AMTRON page to 16 A (the ceiling that Modbus works below). No username or password is needed.

    cd server
    .venv/bin/python -m charger_web

Open `http://127.0.0.1:8080/`. `EVSIM_CHARGER_HOST` overrides the charger address (default `192.168.21.117`) and
`--port` the page's port. Stop it with Ctrl-C.

How it works, and what to know:

- A background poller reads the charger every 2 s. That is also the heartbeat the charger needs: after 30 s without
  Modbus traffic it drops to 0 A, so if this process stops, charging stops within 30 s.
- "Maximum current" writes register 1000 (0 A pauses; otherwise 6 to 16 A). The AMTRON's own operator limit
  (register 134, set on its page) is a ceiling: the effective limit is the lower of the two.
- After you press Apply the page keeps that limit: if the charger loses it (a reboot, another Modbus master), the
  service puts it back, at most once every 10 s.
- The service never writes on its own at start. But the charger remembers the last limit written and restores it on
  the first Modbus request after a fallback, so starting this page can resume charging at that limit.
- Modbus has no login: anything on the lab network can set the charger's current. Keep the page on loopback.

Design: `docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md`. The read-only scanner
`tools/amtron_modbus_probe.py` lists registers (any read can resume charging after a fallback).

"""
k = s.index("## Test")
open(p, "w").write(s[:k] + section + s[k:])

# the probe: holding registers only, and the resume warning
p = "tools/amtron_modbus_probe.py"
sub(p, "Only function codes 3 and 4 (reads) are sent. Nothing is ever written.\n",
    "Only function codes 3 and 4 (reads) are sent. Nothing is ever written.\n"
    "\nWARNING: any request can resume charging. After a Modbus fallback (30 s without traffic) the charger\n"
    "restores the limit last written on the first request, even a read. All the AMTRON's registers are holding\n"
    "registers; function code 4 is refused.\n")
sub(p, "    ./amtron_modbus_probe.py --unit 1 --table input --start 100 --end 800 > input_a.txt",
    "    ./amtron_modbus_probe.py --unit 255 --table holding --start 100 --end 1100 > holding_a.txt")
sub(p, 'ap.add_argument("--table", choices=("holding", "input", "both"), default="both")',
    'ap.add_argument("--table", choices=("holding", "input", "both"), default="holding")')

# superseded notices, and the new spec's status
sub("docs/superpowers/specs/2026-09-30-charger-web-interface-design.md",
    "**Status:** revised architecture pending user review",
    "**Status:** superseded: the page now uses Modbus TCP instead of REST; see "
    "[2026-10-01-charger-modbus-backend-design.md](2026-10-01-charger-modbus-backend-design.md). "
    "The REST session and credential sections below no longer apply.")
sub("docs/superpowers/plans/2026-09-30-charger-web-interface.md",
    "**Goal:** Add a loopback-only page to set the AMTRON operator current limit and view charger-reported current readings.",
    "> **Superseded (2026-10-01):** the page now uses Modbus TCP, not REST; see "
    "[2026-10-01-charger-modbus-backend.md](2026-10-01-charger-modbus-backend.md).\n\n"
    "**Goal:** Add a loopback-only page to set the AMTRON operator current limit and view charger-reported current readings.")
sub("docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md",
    "**Status:** awaiting review", "**Status:** approved, implemented on this branch")
PYEDIT
```

Create `memory/evsim-amtron-modbus.md`:

```markdown
---
name: evsim-amtron-modbus
description: The AMTRON's Modbus TCP interface as measured 2026-09-30 - register map, 30 s fallback to 0 A, the limit restored on the next request; the charger page uses it instead of REST
metadata:
  type: project
---

The AMTRON Charge Control (CC612, firmware 5.33, protocol 0.14) has a Modbus TCP server, switched on in its web
config (port 502, register address set MENNEKES, Allow Start/Stop Transaction Off). Unit ID 255 (any 1 to 255 works).
The map is MENNEKES's published ECU Modbus TCP specification, rev 1.07, which names AMTRON Charge Control. Design:
`docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md`.

**Every register is a holding register (function code 3)**; function code 4 is refused. Register 130 does not exist.

| Register | Meaning |
|---|---|
| 1000 | HEMS current limit, read/write, A. **0 pauses charging.** The only register the page writes. |
| 134 | Operator limit (set on the AMTRON page), **read only**. The effective limit is the lower of 134 and 1000. |
| 133 | Reads 16: probably the installation limit (not in the spec). |
| 706 | Signalled current, A. |
| 212, 214, 216 | Per-phase current in mA, 32-bit, high word first; 0xFFFFFFFF means no meter. |
| 122, 104 | Vehicle state (A=1, B=2, C=3), OCPP status (1 Occupied, 6 Charging, 7 Suspended by the charger). |
| 131, 132 | Safe current (0 A) and communication timeout (30 s). |

**Behaviour that matters (measured):**
- Writing 1000 = 10 gave a signalled 10 A within about 3 s, and the board saw a 10.19 A pilot.
- After **30 s without Modbus traffic the charger falls back to 0 A**, and register 1000 reads 0.
- The charger **remembers the last limit written and restores it on the next request after a fallback, even a plain
  read**, within a few seconds. Reading the charger is therefore not neutral: it can resume charging.
- Plain reads keep the limit alive (70 s of reads only, past the 30 s timeout), so a polling service needs no
  separate heartbeat.

**Why Modbus and not REST:** the REST login is a single session shared with the AMTRON's own page, so the two
fought over it. Modbus has no login. It is also unauthenticated, so anything on the lab network can set the current.

Related: [[evsim-amtron-interfaces]], [[evsim-amtron-rest-api]], [[evsim-inverter-fronius]].
```

```bash
python3 - <<'PYEDIT'
def sub(path, old, new):
    s = open(path).read()
    assert s.count(old) == 1, (path, old[:60])
    open(path, "w").write(s.replace(old, new))


sub("memory/evsim-amtron-interfaces.md",
    "- `Modbus TCP Server for energy management systems` = **Off** (available, could be enabled)",
    "- `Modbus TCP Server for energy management systems` = **On since 2026-10-01** (port 502, address set MENNEKES); see [[evsim-amtron-modbus]]")
s = open("memory/MEMORY.md").read()
if not s.endswith("\n"):
    s += "\n"
s += "- [AMTRON Modbus](evsim-amtron-modbus.md) — register map, the 30 s fallback to 0 A and the limit restored on the next request; the charger page uses it.\n"
open("memory/MEMORY.md", "w").write(s)
PYEDIT
```

- [ ] **Step 5: Run the charger tests**

Run: `cd server && python3 -B -m unittest discover -s tests -p 'test_charger_*.py' 2>&1 | tail -3`
Expected: `Ran 44 tests` and `OK` (27 service, 10 web, 4 page, 3 modbus-only).

Then the whole suite: `cd server && python3 -B -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK` (the bench tests skip when Mosquitto or paho-mqtt is missing).

Also confirm no stale reference to a removed module: `grep -rn "charger_modbus\|charger\.py\|ChargerClient\|ModbusChargerClient\|--backend" server tools docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md | grep -v "\.pyc" || echo "no stale references"`
Expected: `no stale references`, except the lines in the superseded 2026-09-30 documents, which stay as history.

- [ ] **Step 6: Commit**

```bash
git add -A server/charger_web server/tests server/README.md tools/amtron_modbus_probe.py docs memory
git commit -m "$(cat <<'EOF'
Remove the REST code: the charger page is Modbus only

Deletes the REST client, login and session handling, the credentials and
the --backend option, and the tests that covered them. The README, the
probe tool, the superseded 2026-09-30 documents and the notes now describe
the Modbus page.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Live check with the real charger

**Files:**
- Modify: `memory/evsim-amtron-modbus.md` (the results)

**Interfaces:**
- Consumes: the finished page and service.
- Produces: the answers to the spec's open points, recorded in the notes.

**STOP and ask the human partner first.** This step writes to the real charger and needs them present. It also needs the lab network, the charger's Modbus server on (`nc -z 192.168.21.117 502` must succeed), and the AMTRON's own operator limit at 16 A. If any of that is missing, skip the step and say so.

- [ ] **Step 1: Start the page and look at it**

Run: `cd server && .venv/bin/python -m charger_web`, and open `http://127.0.0.1:8080/` in a browser.
Expected within a few seconds: the badge reads "Charger connected"; the operator limit shows 16 A; the vehicle state and charger status show; the per-phase currents show in A. Note that starting the page can resume charging at the limit the charger last held.

- [ ] **Step 2: Apply a limit**

In the page, apply 10 A. Expected: "Confirmed by charger: 10 A", the signalled current becomes 10 A, and the board's pilot follows (`mosquitto_sub -h 127.0.0.1 -t evsim/cp -C 3` shows about 10.2 A offered).

- [ ] **Step 3: Use it next to the AMTRON's own page**

With the page still open, open the AMTRON's own web page. Expected: both work at the same time and the charger page keeps updating (this was the REST problem). On the AMTRON page lower the operator limit to 6 A: the charger page should show the operator limit of 6 A, the warning that it caps the set limit, and a signalled current of 6 A. Put it back to 16 A.

- [ ] **Step 4: Pause and resume**

Apply 0 A. Expected: the "Charging is paused" banner, signalled 0 A, status "Suspended by the charger". Apply 10 A again.

- [ ] **Step 5: The fallback and the restore**

Stop the page with Ctrl-C. Within about 30 s the board's pilot should go to no offer (`cp` becomes `DC_12V` in `evsim/cp`); watch it with `mosquitto_sub`, **not** with a Modbus read, which would resume charging. Then start the page again: the charger restores the 10 A limit within a few seconds. Record how long the fallback took and how long the restore took.

- [ ] **Step 6: Record the results and commit**

Add a "Live check" section to `memory/evsim-amtron-modbus.md` with what Steps 1 to 5 showed (including the register 133 value after changing the installation limit if the human partner changes it, and any surprise), then:

```bash
git add memory/evsim-amtron-modbus.md
git commit -m "$(cat <<'EOF'
Record the live check of the Modbus charger page

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

Pushing the branch is the human partner's decision: pull with `git pull --ff-only` first, since another session also pushes to it.

---

## Self-review

- **Spec coverage:** the register map and holding-only access (Task 1); the 32-bit mA currents and `0xFFFFFFFF` (Task 1); the background poller as the heartbeat and the cache-only read path (Tasks 1 and 2); the write to register 1000 with validation and read-back (Task 2); the desired limit, the restore at most once per 10 s, and never writing at start (Task 2); the link states and the stale and fallback display (Tasks 1 and 4); the status additions and the unchanged route names (Task 3); the page changes (Task 4); removing the REST code and the docs (Task 5); the live check (Task 6). Out of scope items are not built.
- **Placeholders:** none. Every step has its code or exact command.
- **Type consistency:** `ChargerService.status()` keys, `set_limit(value)` and the HTTP result `set_limit_a` are used with the same names in the service, the web layer, the page and the tests.
- **Review Focus:** items 1 and 4 are pinned by `test_it_never_writes_on_its_own_at_start`, `test_a_value_it_merely_read_never_becomes_the_desired_limit`, `test_a_lost_limit_is_restored_at_most_once_per_interval` and `test_a_refused_restore_write_does_not_raise_or_spin` (Task 2); item 2 by `test_the_poller_thread_survives_an_unexpected_exception` (Task 2); item 3 by `test_a_charger_that_never_answers_does_not_block_status` (Task 1); item 5 by `test_a_missing_meter_is_unavailable_and_a_real_zero_is_zero` and `test_one_refused_register_does_not_fail_the_others` (Task 1).
- **Test counts** (charger tests): 12 after Task 1, 27 after Task 2, 37 after Task 3, 41 after Task 4, 44 after Task 5.
