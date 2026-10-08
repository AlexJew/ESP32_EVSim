# EVSim Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command, `python -m evsim_recorder run`, that steps the AMTRON's maximum current through a list of values and records, with one clock and into one SQLite file, the charger's own measurements, every MQTT message of the chain (`evsim/#`), and each limit write, so propagation delays and power differentials can be analysed afterwards.

**Architecture:** A new package `server/evsim_recorder/`: a `Store` (SQLite in WAL mode, fed through a bounded queue and written by one thread), a `Capture` (the paho callbacks that stamp every message and queue it), a `ChargerLogger` (turns each poll of the page's `ChargerService` into a row) and an `Experiment` (validates the plan, writes register 1000 step by step, restores the original limit). Three small additive changes elsewhere: the setpoint server and the inverter writer publish timestamp fields, and `ChargerService` gains an `on_poll` callback and the write times.

**Tech Stack:** Python 3 standard library (`sqlite3`, `threading`, `argparse`), `paho-mqtt`, `unittest` with the existing fakes (`FakeAmtron`, `FakeMqttClient`, `FakeInverter`).

**Spec:** [docs/superpowers/specs/2026-10-08-evsim-recorder-design.md](../specs/2026-10-08-evsim-recorder-design.md). Two small additions to it are made in Task 6: a `--baseline` option (a quiet period before the first write, so the value before the write is measured) and a `charger_ok` event kind (the recovery after a `charger_error`).

Work on branch `feat/recorder`. **Another session may use this repository**: run `git branch --show-current` before every commit and stop if it is not `feat/recorder`. All commands run from `server/` unless stated; always run Python as `.venv/bin/python -B`. Edit conventions: **Create** and **Overwrite** write a whole file; **In `path`, replace this text: ... with: ...** swaps one exact piece of text (it must occur exactly once); **Append to** adds to the end of a file.

## Global Constraints

Every task's requirements include these, copied from the spec.

- **Time zero is the Modbus write** of the new maximum current, made by the recorder with the page's `ChargerService`; its time (`write_sent_ns`, just before the request) is stored in `limit_writes.t_write_ns`. **Only register 1000 is ever written**, with the page's safety rules: 0 or a whole number from 6 to 16 and not above the installation limit; register 134 is never written. There is **no marker topic** and no separate `set-limit` command.
- **The command:** `python -m evsim_recorder run --steps 6,7,6,12,16,6 --dwell 60 [--repeat 1] [--baseline 10] [--db PATH] [--host H] [--port P] [--charger-period 1] [--leave] [--yes]`. All steps are validated **before anything is written**; the plan is printed and **confirmed** (`--yes` skips the question); the limit set at the start is **written back at the end, on Ctrl-C and on SIGTERM**, unless `--leave`.
- **No sampling faster than 1 Hz:** the charger is polled every `--charger-period` seconds (1 to 10, default 1); the page's own poll stays 2 s. The charger poll is also the heartbeat: the poller keeps running through the baseline, every dwell and the restore, and stops only after the restore.
- **Stored (SQLite, WAL):** tables `sessions`, `messages`, `charger`, `limit_writes`, `recorder_events`; views `cp`, `setpoint`, `inverter` over `messages` (retained messages excluded; invalid JSON, a non-object or a missing field gives `NULL`, never an error). `messages.payload` is the text of the bytes; `payload_raw` holds the bytes only when they are not valid UTF-8.
- **MQTT:** subscribe to `evsim/#` at QoS 0 inside `on_connect`; client id `evsim-recorder-` plus a random suffix; connect asynchronously; **publish nothing**. Each message is stamped in the callback with `time.time_ns()` and `time.monotonic_ns()`.
- **Writer:** one thread, batches of up to 200 rows or 0.5 s, WAL; queue of 100 000 rows, the oldest dropped and counted when full; a write error is retried and recorded; nothing raises into the MQTT thread or the charger poll.
- **Timestamp fields (additive only):** `evsim/setpoint` gains `ts_ms`; `evsim/inverter` gains `read_ts_ms` (epoch ms right after the power read, `null` when unreadable) and `cap_ts_ms` (epoch ms of the last successful cap write, `null` before the first). Every existing field, topic and behaviour stays.
- **Charger service (additive only):** `ChargerService(..., on_poll=None)` calls `on_poll({"t_start_ns", "t_end_ns", "values", "error"})` after each poll (an exception in it is logged, never raised); `set_limit` also returns `write_sent_ns` and `write_acked_ns`.
- **Do not break:** the page and its tests; `data/recordings/*.sqlite*` are git-ignored.

## Review Focus

Inputs and conditions the spec implies but the obvious tests would miss, most likely to bite first. Each has a test in the task named.

- **The charger must never be left at an experiment value or at 0 A by a crash or an interrupt:** a refused or unconfirmed write mid-run, Ctrl-C and SIGTERM during a dwell or the baseline, and a failure of the restore itself. The original limit is written back on every path, and the keep-alive poll runs until the restore has been tried. Task 5 and Task 6.
- **A bad plan must write nothing:** a value outside 0 or 6 to 16, above the installation limit, not a whole number, an empty list, a negative number, text; a declined confirmation; an unreachable charger. Task 5 and Task 6.
- **The recorder must never stall the chain it observes:** a blocked or failing database must not delay the MQTT callback or the charger poll; overflow drops the oldest rows and says so. Task 3.
- **Hostile or odd data must not break the analysis:** invalid JSON, JSON that is not an object, a missing field, bytes that are not UTF-8, retained messages and unknown topics must neither raise nor corrupt a view. Task 3.
- **A gap must be distinguishable from a quiet system:** a broker disconnect, a charger that stops answering and a dropped row each leave an event, and the charger recovery is recorded too. Tasks 3, 4 and 5.

---

### Task 1: The setpoint server and the inverter writer publish timestamps

**Files:**
- Modify: `server/setpoint_server/messages.py`, `server/setpoint_server/inverter_writer.py`
- Modify: `server/tests/test_messages.py`, `server/tests/test_inverter_writer.py`
- Modify: `docs/superpowers/specs/2026-09-30-inverter-writer-design.md` (the message table)

**Interfaces:**
- Produces: `format_setpoint(...)` ends with `"ts_ms": <epoch ms of now_utc>`; the `evsim/inverter` status ends with `"read_ts_ms"` and `"cap_ts_ms"` (after `"ts"`).

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_messages.py`, replace this text:

```python
NOW = datetime(2026, 9, 30, 13, 42, 7, 123456, tzinfo=timezone.utc)
```

with:

```python
NOW = datetime(2026, 9, 30, 13, 42, 7, 123456, tzinfo=timezone.utc)
NOW_MS = 1790775727123          # the same instant in epoch milliseconds
```

and replace this text:

```python
            format_setpoint(5, 10060.0, 1, NOW),
            '{"setpoint_w":10060,"samples":5,"window_s":1,"status":"ok",'
            '"ts":"2026-09-30T13:42:07Z"}',
```

with:

```python
            format_setpoint(5, 10060.0, 1, NOW),
            '{"setpoint_w":10060,"samples":5,"window_s":1,"status":"ok",'
            '"ts":"2026-09-30T13:42:07Z","ts_ms":%d}' % NOW_MS,
```

and replace this text:

```python
            format_setpoint(0, None, 1, NOW),
            '{"setpoint_w":0,"samples":0,"window_s":1,"status":"no_data",'
            '"ts":"2026-09-30T13:42:07Z"}',
```

with:

```python
            format_setpoint(0, None, 1, NOW),
            '{"setpoint_w":0,"samples":0,"window_s":1,"status":"no_data",'
            '"ts":"2026-09-30T13:42:07Z","ts_ms":%d}' % NOW_MS,
```

and replace this text:

```python
    def test_ts_is_converted_to_utc(self):
```

with:

```python
    def test_ts_ms_is_the_epoch_in_milliseconds_whatever_the_timezone(self):
        local = datetime(2026, 9, 30, 15, 42, 7, 123456, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(json.loads(format_setpoint(1, 100.0, 1, local))["ts_ms"], NOW_MS)
        self.assertEqual(json.loads(format_setpoint(1, 100.0, 1, NOW))["ts_ms"], NOW_MS)

    def test_the_offline_message_has_no_timestamp(self):
        self.assertNotIn("ts_ms", json.loads(offline_payload(1)))

    def test_ts_is_converted_to_utc(self):
```

In `server/tests/test_inverter_writer.py`, replace this text:

```python
        self.assertEqual(list(s), ["mode", "target_dc_w", "cap_ac_w", "dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v",
                                   "inverter_state", "error", "ts"])
```

with:

```python
        self.assertEqual(list(s), ["mode", "target_dc_w", "cap_ac_w", "dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v",
                                   "inverter_state", "error", "ts", "read_ts_ms", "cap_ts_ms"])
```

and replace this text:

```python
    def test_an_unreachable_inverter_reports_null_measurements(self):
```

with:

```python
    def test_the_message_carries_the_read_time_and_the_time_of_the_last_cap_write(self):
        base = int(NOW.timestamp() * 1000)
        now = [NOW]
        writer = InverterWriter("127.0.0.1", self.port, timeout_s=1, publish=self.status.append, clock=self.clock,
                                utcnow=lambda: now[0])
        self.addCleanup(writer.stop)
        writer.step_once()                                          # no target yet: a first, zero cap is written
        first = json.loads(self.status[-1])
        self.assertEqual((first["read_ts_ms"], first["cap_ts_ms"]), (base, base))
        now[0] = NOW + timedelta(seconds=2)
        self.clock.t += 2
        writer.set_target(4000.0)
        writer.step_once()                                          # the target moved: the cap is written again
        second = json.loads(self.status[-1])
        self.assertEqual((second["read_ts_ms"], second["cap_ts_ms"]), (base + 2000, base + 2000))
        now[0] = NOW + timedelta(seconds=3)
        self.clock.t += 1
        writer.set_target(4000.0)
        writer.step_once()                                          # nothing to write: only the read time moves
        third = json.loads(self.status[-1])
        self.assertEqual((third["read_ts_ms"], third["cap_ts_ms"]), (base + 3000, base + 2000))

    def test_before_any_read_or_cap_write_both_times_are_null(self):
        self.fake.stop()                                            # the inverter is unreachable from the start
        self.writer.step_once()
        s = self.last()
        self.assertEqual(s["mode"], "unreachable")
        self.assertEqual((s["read_ts_ms"], s["cap_ts_ms"]), (None, None))

    def test_an_unreachable_inverter_after_a_cap_write_keeps_the_cap_time_and_has_no_read_time(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        cap_ts = self.last()["cap_ts_ms"]
        self.assertIsNotNone(cap_ts)
        self.fake.stop()
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        s = self.last()
        self.assertEqual((s["mode"], s["read_ts_ms"], s["cap_ts_ms"]), ("unreachable", None, cap_ts))

    def test_an_unreachable_inverter_reports_null_measurements(self):
```

and replace this text:

```python
from datetime import datetime, timezone
```

with:

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_messages.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_inverter_writer.py'`
Expected: FAIL: the exact-string tests miss `ts_ms`, and the writer tests hit `KeyError: 'read_ts_ms'`.

- [ ] **Step 3: Write the implementation**

In `server/setpoint_server/messages.py`, replace this text:

```python
        "ts": now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
```

with:

```python
        "ts": now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_ms": int(now_utc.timestamp() * 1000),       # the same instant in epoch milliseconds
    })
```

In `server/setpoint_server/inverter_writer.py`, replace this text:

```python
        self._cap_ac_w = None
```

with:

```python
        self._cap_ac_w = None
        self._read_ts_ms = None           # epoch ms right after the last power read
        self._cap_ts_ms = None            # epoch ms of the last successful cap write
```

and replace this text:

```python
    def _current_target(self, now):
```

with:

```python
    def _now_ms(self):
        return int(self._utcnow().timestamp() * 1000)

    def _current_target(self, now):
```

and replace this text:

```python
            power = inv.power()
            if self._starting and power["state"] == MPPT:
```

with:

```python
            power = inv.power()
            self._read_ts_ms = self._now_ms()
            if self._starting and power["state"] == MPPT:
```

and replace this text:

```python
            inv.set_dc_target(action.dc_w)
            self._cap_ac_w = ac_cap_for_dc(action.dc_w)
```

with:

```python
            inv.set_dc_target(action.dc_w)
            self._cap_ac_w = ac_cap_for_dc(action.dc_w)
            self._cap_ts_ms = self._now_ms()
```

and replace this text:

```python
            "ts": self._utcnow().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, separators=(",", ":"))
```

with:

```python
            "ts": self._utcnow().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "read_ts_ms": self._read_ts_ms if power else None,
            "cap_ts_ms": self._cap_ts_ms,
        }, separators=(",", ":"))
```

Update the message table in `docs/superpowers/specs/2026-09-30-inverter-writer-design.md`. Replace this text:

```
| `ac_a`, `ac_v` | The mean of the three AC phase currents and of the three phase-to-neutral voltages, or `null` (added 2026-10-07) |
```

with:

```
| `ac_a`, `ac_v` | The mean of the three AC phase currents and of the three phase-to-neutral voltages, or `null` (added 2026-10-07) |
| `read_ts_ms` | Epoch milliseconds right after the inverter's power was read (the measurement time), or `null` when it could not be read (added 2026-10-08) |
| `cap_ts_ms` | Epoch milliseconds of the last successful cap write, or `null` before the first (added 2026-10-08) |
```

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_messages.py'` then `.venv/bin/python -B -m unittest discover -s tests -p 'test_inverter_*.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_server.py'`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/recorder
git add server/setpoint_server/messages.py server/setpoint_server/inverter_writer.py \
  server/tests/test_messages.py server/tests/test_inverter_writer.py \
  docs/superpowers/specs/2026-09-30-inverter-writer-design.md
git commit -m "Publish timestamps on evsim/setpoint and evsim/inverter

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The charger service reports each poll and the write times

**Files:**
- Modify: `server/charger_web/service.py`
- Modify: `server/tests/test_charger_service.py`

**Interfaces:**
- Produces:
  - `ChargerService(host=None, port=None, register_map=None, clock=time.monotonic, period_s=None, on_poll=None)`; `on_poll` is called after every poll with `{"t_start_ns": int, "t_end_ns": int, "values": {register name: raw value or None for every name in READ_ORDER}, "error": str | None}`; an exception in it is logged and swallowed.
  - `set_limit(value)` returns the existing keys plus `write_sent_ns` and `write_acked_ns` (wall clock `time.time_ns()` just before the write request and just after its reply).

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_charger_service.py`, replace this text:

```python
class SignalledAfterApplyTest(ServiceTest):
```

with:

```python
class PollReportTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.reports = []

    def service(self, on_poll):
        service = ChargerService("127.0.0.1", self.port, on_poll=on_poll)
        self.addCleanup(service.stop)
        return service

    def test_each_poll_is_reported_with_its_times_and_every_raw_value(self):
        service = self.service(self.reports.append)
        before = time.time_ns()
        service.poll_once()
        after = time.time_ns()
        report = self.reports[0]
        self.assertTrue(before <= report["t_start_ns"] <= report["t_end_ns"] <= after)
        self.assertIsNone(report["error"])
        values = report["values"]
        self.assertEqual(set(values), set(READ_ORDER))
        self.assertEqual((values["set_limit_a"], values["signalled_current_a"], values["meter_power_w"]), (10, 10, 600))
        self.assertEqual((values["meter_current_l1_a"], values["meter_voltage_l3_v"]), (0.874, 233))

    def test_a_failed_poll_is_reported_with_the_error_and_the_values_it_did_not_reach_are_none(self):
        service = self.service(self.reports.append)
        service.poll_once()
        self.fake.stop()
        service.poll_once()
        report = self.reports[-1]
        self.assertTrue(report["error"])
        self.assertIsNone(report["values"]["meter_power_w"])
        self.assertEqual(set(report["values"]), set(READ_ORDER))

    def test_a_callback_that_raises_never_breaks_the_poll(self):
        def broken(report):
            raise RuntimeError("boom")
        service = self.service(broken)
        service.poll_once()
        self.assertEqual(service.status()["set_limit_a"]["value"], 10)

    def test_without_a_callback_nothing_changes(self):
        service = self.service(None)
        service.poll_once()
        self.assertEqual(service.status()["link"], "ok")


class WriteTimesTest(ServiceTest):
    def test_set_limit_returns_the_wall_clock_just_before_and_after_the_write(self):
        self.service.poll_once()
        before = time.time_ns()
        result = self.service.set_limit(12)
        after = time.time_ns()
        self.assertTrue(before <= result["write_sent_ns"] <= result["write_acked_ns"] <= after)
        self.assertEqual((result["success"], result["set_limit_a"]), (True, 12))

    def test_the_existing_keys_are_still_there(self):
        self.service.poll_once()
        result = self.service.set_limit(12)
        self.assertTrue({"success", "set_limit_a", "updated_at"} <= set(result))


class SignalledAfterApplyTest(ServiceTest):
```

and replace this text:

```python
from charger_web.service import ChargerError, ChargerService, LimitValidationError
```

with:

```python
from charger_web.service import READ_ORDER, ChargerError, ChargerService, LimitValidationError
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_service.py'`
Expected: FAIL: `TypeError: ... unexpected keyword argument 'on_poll'` and `KeyError: 'write_sent_ns'`.

- [ ] **Step 3: Write the implementation**

In `server/charger_web/service.py`, replace this text:

```python
    def __init__(self, host=None, port=None, register_map=None, clock=time.monotonic, period_s=None):
```

with:

```python
    def __init__(self, host=None, port=None, register_map=None, clock=time.monotonic, period_s=None, on_poll=None):
```

and replace this text:

```python
        self._period = config.POLL_PERIOD_S if period_s is None else period_s
```

with:

```python
        self._period = config.POLL_PERIOD_S if period_s is None else period_s
        self._on_poll = on_poll                # called after every poll (the recorder logs from it)
```

and replace this text:

```python
        with self._io_lock:
            values, error = {}, None
            for name in READ_ORDER:
```

with:

```python
        with self._io_lock:
            values, error = {}, None
            t_start_ns = time.time_ns()
            for name in READ_ORDER:
```

and replace this text:

```python
            now = self._clock()
            with self._cache_lock:
                self._error = error
```

with:

```python
            t_end_ns = time.time_ns()
            now = self._clock()
            with self._cache_lock:
                self._error = error
```

and replace this text:

```python
            if error is None:
                self._restore_if_needed(now)

    def _link(self, now):
```

with:

```python
            if error is None:
                self._restore_if_needed(now)
            self._report_poll(t_start_ns, t_end_ns, values, error)

    def _report_poll(self, t_start_ns, t_end_ns, values, error):
        if self._on_poll is None:
            return
        try:
            self._on_poll({"t_start_ns": t_start_ns, "t_end_ns": t_end_ns, "error": error,
                           "values": {name: values.get(name) for name in READ_ORDER}})
        except Exception:                      # a logger must never break the heartbeat
            log.exception("the poll callback failed")

    def _link(self, now):
```

and replace this text:

```python
                self._modbus.write_single(self._registers["set_limit_a"]["address"], value)
                sent = True
```

with:

```python
                write_sent_ns = time.time_ns()
                self._modbus.write_single(self._registers["set_limit_a"]["address"], value)
                write_acked_ns = time.time_ns()
                sent = True
```

and replace this text:

```python
        return {"success": True, "set_limit_a": confirmed, "updated_at": self._timestamp(now)}
```

with:

```python
        return {"success": True, "set_limit_a": confirmed, "updated_at": self._timestamp(now),
                "write_sent_ns": write_sent_ns, "write_acked_ns": write_acked_ns}
```

- [ ] **Step 4: Run them to verify they pass, and that the page's tests still do**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_*.py'` then `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_*.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_vehicle_*.py'`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/recorder
git add server/charger_web/service.py server/tests/test_charger_service.py
git commit -m "Report each charger poll and the write times from ChargerService

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The SQLite store and its writer thread

**Files:**
- Create: `server/evsim_recorder/__init__.py`, `server/evsim_recorder/storage.py`
- Test: `server/tests/test_recorder_storage.py`

**Interfaces:**
- Produces `Store(path, queue_max=100_000, batch_rows=200, batch_seconds=0.5, retry_seconds=1.0)`:
  - `start_session(broker, charger, plan, argv) -> int` (also sets `store.session_id`); `start()` starts the writer thread; `stop()` drains the queue, sets `sessions.ended_ns`, closes the file (idempotent); `flush(timeout=5.0) -> bool` waits until everything queued is committed.
  - `add_message(t_ns, mono_ns, topic, retained, payload_bytes)`, `add_charger(t_start_ns, t_end_ns, error=None, **values)` with the value names `set_limit_a operator_limit_a installation_limit_a signalled_current_a vehicle_state charger_status i_l1_a i_l2_a i_l3_a v_l1_v v_l2_v v_l3_v power_w`, `add_limit_write(run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error=None)`, `add_event(kind, detail="", t_ns=None)`. All only enqueue and never block or raise.
  - Tables and views as in the spec; `sqlite3.OperationalError` propagates from `Store(...)` when the file cannot be created.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_recorder_storage.py`:

```python
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from evsim_recorder.storage import Store

CP = ('{"t":9912523,"freq":999.9,"duty":10.53,"adc":0,"cp":"PWM_VALID","state":"C","amps":6.32,"watts":4361,'
      '"setpoint_w":4361,"status":"live","age_ms":0}')
SETPOINT = '{"setpoint_w":4312,"samples":5,"window_s":1,"status":"ok","ts":"2026-10-08T10:00:03Z","ts_ms":1791453603000}'
INVERTER = ('{"mode":"capped","target_dc_w":4312.6,"cap_ac_w":4182.7,"dc_w":4304.6,"dc_a":8.2,"dc_v":524.7,"ac_w":4181,'
            '"ac_a":6.0,"ac_v":233.3,"inverter_state":4,"error":null,"ts":"2026-10-08T10:00:04Z",'
            '"read_ts_ms":1791453604120,"cap_ts_ms":1791453601500}')


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "t.sqlite")
        self.store = Store(self.path, batch_seconds=0.05, retry_seconds=0.02)
        self.store.start_session(broker="127.0.0.1:1883", charger="127.0.0.1", plan={"steps": [6, 7]}, argv=["run", "--steps", "6,7"])
        self.store.start()
        self.addCleanup(self.store.stop)

    def query(self, sql, *args):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def message(self, topic, payload, retained=False, t_ns=1_000, mono_ns=2_000):
        self.store.add_message(t_ns, mono_ns, topic, retained, payload.encode() if isinstance(payload, str) else payload)


class SessionAndMessageTest(StoreCase):
    def test_a_session_row_records_how_the_run_was_started(self):
        row = self.query("SELECT broker, charger, plan, argv, ended_ns FROM sessions")[0]
        self.assertEqual(row[:2], ("127.0.0.1:1883", "127.0.0.1"))
        self.assertEqual(json.loads(row[2]), {"steps": [6, 7]})
        self.assertEqual(json.loads(row[3]), ["run", "--steps", "6,7"])
        self.assertIsNone(row[4])
        self.assertEqual(self.store.session_id, 1)

    def test_a_message_is_stored_with_its_times_topic_flag_and_payload(self):
        self.message("evsim/cp", CP, retained=False, t_ns=1_791_453_600_123_456_789, mono_ns=77)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT session_id, t_ns, mono_ns, topic, retained, payload, payload_raw FROM messages")[0]
        self.assertEqual(row, (1, 1_791_453_600_123_456_789, 77, "evsim/cp", 0, CP, None))

    def test_a_retained_flag_is_kept(self):
        self.message("evsim/setpoint", SETPOINT, retained=True)
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT retained FROM messages"), [(1,)])

    def test_bytes_that_are_not_utf8_are_kept_verbatim_next_to_a_readable_text(self):
        self.message("evsim/reply", b"state A \xff\xfe B")
        self.assertTrue(self.store.flush())
        text, raw = self.query("SELECT payload, payload_raw FROM messages")[0]
        self.assertEqual(raw, b"state A \xff\xfe B")
        self.assertIn("state A", text)

    def test_valid_utf8_has_no_raw_copy(self):
        self.message("evsim/state", "C")
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT payload, payload_raw FROM messages"), [("C", None)])

    def test_an_empty_payload_is_stored(self):
        self.message("evsim/reply", b"")
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT payload FROM messages"), [("",)])


class ViewTest(StoreCase):
    def test_the_cp_view_has_the_boards_fields(self):
        self.message("evsim/cp", CP)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT session_id, t_ns, mono_ns, board_ms, freq, duty, adc, cp, state, amps, watts, setpoint_w, "
                         "status, age_ms FROM cp")[0]
        self.assertEqual(row, (1, 1_000, 2_000, 9912523, 999.9, 10.53, 0, "PWM_VALID", "C", 6.32, 4361, 4361, "live", 0))

    def test_the_setpoint_view_has_the_servers_tick_time(self):
        self.message("evsim/setpoint", SETPOINT)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT setpoint_w, samples, window_s, status, server_ts, tick_ms FROM setpoint")[0]
        self.assertEqual(row, (4312, 5, 1, "ok", "2026-10-08T10:00:03Z", 1791453603000))

    def test_the_inverter_view_has_the_measurements_and_the_new_timestamps(self):
        self.message("evsim/inverter", INVERTER)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT mode, target_dc_w, cap_ac_w, dc_w, dc_a, dc_v, ac_w, ac_a, ac_v, inverter_state, error, "
                         "server_ts, read_ts_ms, cap_ts_ms FROM inverter")[0]
        self.assertEqual(row, ("capped", 4312.6, 4182.7, 4304.6, 8.2, 524.7, 4181, 6.0, 233.3, 4, None,
                               "2026-10-08T10:00:04Z", 1791453604120, 1791453601500))

    def test_an_old_message_without_the_new_fields_gives_null_not_an_error(self):
        self.message("evsim/inverter", '{"mode":"capped","dc_w":4304.6}')
        self.assertTrue(self.store.flush())
        row = self.query("SELECT dc_w, dc_a, read_ts_ms, cap_ts_ms FROM inverter")[0]
        self.assertEqual(row, (4304.6, None, None, None))

    def test_hostile_payloads_give_nulls_and_never_break_a_view(self):
        for payload in ("not json", "[1, 2]", "4000", "null", '"text"', "{", "", b"\xff\xfe"):
            self.message("evsim/cp", payload)
            self.message("evsim/setpoint", payload)
            self.message("evsim/inverter", payload)
        self.assertTrue(self.store.flush())
        for view in ("cp", "setpoint", "inverter"):
            rows = self.query("SELECT * FROM %s" % view)
            self.assertEqual(len(rows), 8, view)
        self.assertEqual(self.query("SELECT COUNT(*) FROM cp WHERE setpoint_w IS NOT NULL"), [(0,)])

    def test_a_field_of_an_unexpected_type_is_returned_as_it_was_sent_not_coerced(self):
        self.message("evsim/cp", '{"setpoint_w": "x", "state": 7}')
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT setpoint_w, state FROM cp"), [("x", 7)])

    def test_retained_messages_are_in_messages_but_not_in_the_views(self):
        self.message("evsim/setpoint", SETPOINT, retained=True)
        self.message("evsim/setpoint", SETPOINT, retained=False)
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM setpoint"), [(1,)])

    def test_a_topic_without_a_view_is_still_recorded(self):
        self.message("evsim/electric_car", '{"anything": 1}')
        self.message("evsim/state", "C")
        self.assertTrue(self.store.flush())
        self.assertEqual(sorted(t for (t,) in self.query("SELECT topic FROM messages")), ["evsim/electric_car", "evsim/state"])
        for view in ("cp", "setpoint", "inverter"):
            self.assertEqual(self.query("SELECT COUNT(*) FROM %s" % view), [(0,)])


class OtherTablesTest(StoreCase):
    def test_a_charger_row_has_its_times_and_raw_values(self):
        self.store.add_charger(t_start_ns=10, t_end_ns=25, set_limit_a=14, operator_limit_a=11, installation_limit_a=16,
                               signalled_current_a=11, vehicle_state=3, charger_status=6, i_l1_a=11.8, i_l2_a=11.9,
                               i_l3_a=12.0, v_l1_v=230, v_l2_v=231, v_l3_v=232, power_w=8250)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT session_id, t_start_ns, t_end_ns, set_limit_a, operator_limit_a, installation_limit_a, "
                         "signalled_current_a, vehicle_state, charger_status, i_l1_a, i_l2_a, i_l3_a, v_l1_v, v_l2_v, v_l3_v, "
                         "power_w, error FROM charger")[0]
        self.assertEqual(row, (1, 10, 25, 14, 11, 16, 11, 3, 6, 11.8, 11.9, 12.0, 230, 231, 232, 8250, None))

    def test_a_failed_poll_row_has_the_error_and_null_values(self):
        self.store.add_charger(t_start_ns=10, t_end_ns=25, error="timeout")
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT error, power_w, i_l1_a FROM charger"), [("timeout", None, None)])

    def test_a_limit_write_row(self):
        self.store.add_limit_write(run=1, step=0, t_write_ns=100, t_ack_ns=140, from_a=10, to_a=11, readback_a=11, label="10 -> 11 A")
        self.store.add_limit_write(run=1, step=1, t_write_ns=200, t_ack_ns=None, from_a=11, to_a=12, readback_a=None,
                                   label="11 -> 12 A", error="refused")
        self.assertTrue(self.store.flush())
        rows = self.query("SELECT run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error FROM limit_writes ORDER BY id")
        self.assertEqual(rows, [(1, 0, 100, 140, 10, 11, 11, "10 -> 11 A", None), (1, 1, 200, None, 11, 12, None, "11 -> 12 A", "refused")])

    def test_an_event_has_a_time_a_kind_and_a_detail(self):
        self.store.add_event("connected", "broker 127.0.0.1", t_ns=555)
        self.store.add_event("charger_error", "timeout")
        self.assertTrue(self.store.flush())
        rows = self.query("SELECT t_ns, kind, detail FROM recorder_events ORDER BY id")
        self.assertEqual(rows[0], (555, "connected", "broker 127.0.0.1"))
        self.assertEqual((rows[1][1], rows[1][2]), ("charger_error", "timeout"))
        self.assertGreater(rows[1][0], 1_700_000_000_000_000_000)           # defaulted to the wall clock


class WriterTest(StoreCase):
    def test_rows_are_committed_by_time_without_a_flush(self):
        self.message("evsim/cp", CP)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.query("SELECT 1 FROM messages"):
            time.sleep(0.02)
        self.assertEqual(len(self.query("SELECT 1 FROM messages")), 1)

    def test_a_large_burst_is_committed_in_batches_and_in_order(self):
        for index in range(1000):
            self.message("evsim/cp", '{"t": %d}' % index, t_ns=index)
        self.assertTrue(self.store.flush(timeout=10))
        rows = self.query("SELECT t_ns FROM messages ORDER BY id")
        self.assertEqual([r[0] for r in rows], list(range(1000)))

    def test_stop_flushes_everything_and_marks_the_session_ended(self):
        for index in range(50):
            self.message("evsim/cp", CP, t_ns=index)
        self.store.stop()
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(50,)])
        self.assertIsNotNone(self.query("SELECT ended_ns FROM sessions")[0][0])
        self.store.stop()                                                   # idempotent

    def test_adding_never_blocks_while_the_writer_is_stuck(self):
        gate = threading.Event()
        real = self.store._transaction

        def stuck(rows):
            gate.wait(5)
            return real(rows)

        with mock.patch.object(self.store, "_transaction", side_effect=stuck):
            self.message("evsim/cp", CP)                                    # the writer takes it and sticks
            time.sleep(0.2)
            started = time.monotonic()
            for index in range(2000):
                self.message("evsim/cp", CP, t_ns=index)
            self.assertLess(time.monotonic() - started, 1.0)
            gate.set()
        self.assertTrue(self.store.flush(timeout=10))
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2001,)])

    def test_a_full_queue_drops_the_oldest_rows_and_says_how_many(self):
        small = Store(os.path.join(self.dir, "small.sqlite"), queue_max=50, batch_seconds=0.05, retry_seconds=0.02)
        small.start_session("b", "c", {}, [])
        self.addCleanup(small.stop)
        gate = threading.Event()
        real = small._transaction

        def stuck(rows):
            gate.wait(5)
            return real(rows)

        with mock.patch.object(small, "_transaction", side_effect=stuck):
            small.start()
            small.add_message(0, 0, "evsim/cp", False, b"first")            # taken by the writer, which then sticks
            time.sleep(0.2)
            for index in range(1, 121):
                small.add_message(index, index, "evsim/cp", False, b"x")
            gate.set()
        self.assertTrue(small.flush(timeout=10))
        conn = sqlite3.connect(os.path.join(self.dir, "small.sqlite"))
        kept = [r[0] for r in conn.execute("SELECT t_ns FROM messages ORDER BY t_ns")]
        dropped = conn.execute("SELECT detail FROM recorder_events WHERE kind='dropped'").fetchall()
        conn.close()
        self.assertEqual(len(kept), 51)                                     # the first one plus the newest 50
        self.assertEqual(kept[-1], 120)
        self.assertEqual(kept[1], 71)                                       # 1 to 70 were dropped
        self.assertEqual(len(dropped), 1)
        self.assertIn("70", dropped[0][0])

    def test_a_failing_write_is_retried_and_recorded_and_nothing_is_lost(self):
        real = self.store._transaction
        attempts = []

        def flaky(rows):
            attempts.append(1)
            if len(attempts) <= 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(rows)

        with mock.patch.object(self.store, "_transaction", side_effect=flaky):
            self.message("evsim/cp", CP)
            self.assertTrue(self.store.flush(timeout=10))
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(1,)])
        kinds = [k for (k,) in self.query("SELECT kind FROM recorder_events")]
        self.assertIn("write_error", kinds)

    def test_a_second_connection_can_read_while_the_recorder_writes(self):
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        for index in range(300):
            self.message("evsim/cp", CP, t_ns=index)
            if index % 50 == 0:
                reader.execute("SELECT COUNT(*) FROM messages").fetchone()
        self.assertTrue(self.store.flush(timeout=10))
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM messages").fetchone(), (300,))


class OpeningTest(unittest.TestCase):
    def test_a_file_that_cannot_be_created_raises_at_once(self):
        with self.assertRaises(sqlite3.OperationalError):
            Store("/nonexistent-folder-for-the-recorder-test/x.sqlite")

    def test_reopening_a_file_keeps_its_data_and_starts_a_new_session(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "again.sqlite")
        for _ in range(2):
            store = Store(path, batch_seconds=0.05)
            store.start_session("b", "c", {}, [])
            store.start()
            store.add_message(1, 1, "evsim/state", False, b"C")
            store.stop()
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone(), (2,))
        self.assertEqual(conn.execute("SELECT DISTINCT session_id FROM messages ORDER BY 1").fetchall(), [(1,), (2,)])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_storage.py'`
Expected: FAIL with `No module named 'evsim_recorder'`.

- [ ] **Step 3: Write the implementation**

Create `server/evsim_recorder/__init__.py`:

```python
"""Steps the AMTRON's maximum current and records the whole EVSim chain to SQLite."""

__version__ = "1"
```

Create `server/evsim_recorder/storage.py`:

```python
"""The recorder's SQLite file and the thread that writes it.

Everything the recorder sees is queued and written in batches by one thread, so a slow disk never delays an MQTT
callback or a charger poll. The queue is bounded: when it is full the oldest rows are dropped and counted. The file is in
WAL mode, so it can be read while a run is in progress. Rows are only ever added; the views (cp, setpoint, inverter)
read the messages table with SQLite's JSON functions and give NULL for anything they cannot read.
"""

import json
import logging
import sqlite3
import threading
import time
from collections import deque

log = logging.getLogger("evsim_recorder.storage")

QUEUE_MAX = 100_000
BATCH_ROWS = 200
BATCH_SECONDS = 0.5
BATCH_MAX = 2000                  # rows in one transaction when the queue is long

CHARGER_VALUES = (
    "set_limit_a", "operator_limit_a", "installation_limit_a", "signalled_current_a", "vehicle_state", "charger_status",
    "i_l1_a", "i_l2_a", "i_l3_a", "v_l1_v", "v_l2_v", "v_l3_v", "power_w",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, started_ns INTEGER NOT NULL, ended_ns INTEGER,
  broker TEXT, charger TEXT, plan TEXT, argv TEXT, version TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, t_ns INTEGER NOT NULL, mono_ns INTEGER NOT NULL,
  topic TEXT NOT NULL, retained INTEGER NOT NULL, payload TEXT, payload_raw BLOB);
CREATE INDEX IF NOT EXISTS messages_topic_time ON messages (topic, t_ns);
CREATE TABLE IF NOT EXISTS charger (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, t_start_ns INTEGER NOT NULL, t_end_ns INTEGER NOT NULL,
  set_limit_a INTEGER, operator_limit_a INTEGER, installation_limit_a INTEGER, signalled_current_a INTEGER,
  vehicle_state INTEGER, charger_status INTEGER,
  i_l1_a REAL, i_l2_a REAL, i_l3_a REAL, v_l1_v REAL, v_l2_v REAL, v_l3_v REAL, power_w REAL, error TEXT);
CREATE TABLE IF NOT EXISTS limit_writes (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, run INTEGER, step INTEGER, t_write_ns INTEGER, t_ack_ns INTEGER,
  from_a INTEGER, to_a INTEGER, readback_a INTEGER, label TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS recorder_events (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, t_ns INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""


def _view(name, topic, columns):
    """A view over messages for one topic: the typed JSON fields, NULL for a payload that is not a JSON object."""
    fields = ",\n  ".join("json_extract(j, '$.%s') AS %s" % (key, alias) for alias, key in columns)
    return f"""
CREATE VIEW IF NOT EXISTS {name} AS
SELECT session_id, t_ns, mono_ns,
  {fields}
FROM (SELECT session_id, t_ns, mono_ns,
        CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload) = 'object' THEN payload END END AS j
      FROM messages WHERE topic = '{topic}' AND retained = 0);
"""


VIEWS = (
    _view("cp", "evsim/cp", (
        ("board_ms", "t"), ("freq", "freq"), ("duty", "duty"), ("adc", "adc"), ("cp", "cp"), ("state", "state"),
        ("amps", "amps"), ("watts", "watts"), ("setpoint_w", "setpoint_w"), ("status", "status"), ("age_ms", "age_ms"))),
    _view("setpoint", "evsim/setpoint", (
        ("setpoint_w", "setpoint_w"), ("samples", "samples"), ("window_s", "window_s"), ("status", "status"),
        ("server_ts", "ts"), ("tick_ms", "ts_ms"))),
    _view("inverter", "evsim/inverter", (
        ("mode", "mode"), ("target_dc_w", "target_dc_w"), ("cap_ac_w", "cap_ac_w"), ("dc_w", "dc_w"), ("dc_a", "dc_a"),
        ("dc_v", "dc_v"), ("ac_w", "ac_w"), ("ac_a", "ac_a"), ("ac_v", "ac_v"), ("inverter_state", "inverter_state"),
        ("error", "error"), ("server_ts", "ts"), ("read_ts_ms", "read_ts_ms"), ("cap_ts_ms", "cap_ts_ms"))),
)

INSERTS = {
    "messages": "INSERT INTO messages (session_id, t_ns, mono_ns, topic, retained, payload, payload_raw) VALUES (?,?,?,?,?,?,?)",
    "charger": "INSERT INTO charger (session_id, t_start_ns, t_end_ns, %s, error) VALUES (?,?,?,%s,?)"
               % (", ".join(CHARGER_VALUES), ",".join("?" * len(CHARGER_VALUES))),
    "limit_writes": "INSERT INTO limit_writes (session_id, run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
    "recorder_events": "INSERT INTO recorder_events (session_id, t_ns, kind, detail) VALUES (?,?,?,?)",
}


class Store:
    def __init__(self, path, queue_max=QUEUE_MAX, batch_rows=BATCH_ROWS, batch_seconds=BATCH_SECONDS, retry_seconds=1.0):
        # autocommit: the writer opens and commits its own transactions. check_same_thread is off because the
        # connection is made here but used by the writer thread; nothing else touches it while that thread runs.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA + "".join(VIEWS))
        self._queue_max, self._batch_rows, self._batch_seconds, self._retry_seconds = queue_max, batch_rows, batch_seconds, retry_seconds
        self._rows = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._idle = threading.Condition(self._lock)
        self._busy = False
        self._stopping = False
        self._dropped = 0
        self._notes = []                     # write errors waiting to be recorded as events
        self._thread = None
        self._closed = False
        self.session_id = None

    # -- the session ---------------------------------------------------------------------------

    def start_session(self, broker, charger, plan, argv):
        from . import __version__
        cursor = self._db.execute(
            "INSERT INTO sessions (started_ns, broker, charger, plan, argv, version) VALUES (?,?,?,?,?,?)",
            (time.time_ns(), broker, charger, json.dumps(plan), json.dumps(list(argv)), __version__))
        self.session_id = cursor.lastrowid
        return self.session_id

    # -- adding rows: these only enqueue ------------------------------------------------------------

    def put(self, table, row):
        with self._wake:
            if len(self._rows) >= self._queue_max:
                self._rows.popleft()
                self._dropped += 1
            self._rows.append((table, row))
            self._wake.notify()

    def add_message(self, t_ns, mono_ns, topic, retained, payload):
        payload = bytes(payload)
        try:
            text, raw = payload.decode("utf-8"), None
        except UnicodeDecodeError:
            text, raw = payload.decode("utf-8", "replace"), payload
        self.put("messages", (self.session_id, t_ns, mono_ns, topic, int(bool(retained)), text, raw))

    def add_charger(self, t_start_ns, t_end_ns, error=None, **values):
        self.put("charger", (self.session_id, t_start_ns, t_end_ns, *(values.get(name) for name in CHARGER_VALUES), error))

    def add_limit_write(self, run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error=None):
        self.put("limit_writes", (self.session_id, run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error))

    def add_event(self, kind, detail="", t_ns=None):
        self.put("recorder_events", (self.session_id, time.time_ns() if t_ns is None else t_ns, kind, detail))

    # -- the writer thread -----------------------------------------------------------------------------

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="recorder-writer", daemon=True)
            self._thread.start()

    def _take(self):
        """Wait for a batch: up to batch_rows, or whatever has arrived after batch_seconds. None when stopped and empty."""
        with self._wake:
            while not self._rows and not self._stopping:
                self._wake.wait()
            if not self._rows:
                return None
            deadline = time.monotonic() + self._batch_seconds
            while len(self._rows) < self._batch_rows and not self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wake.wait(remaining)
            batch = [self._rows.popleft() for _ in range(min(len(self._rows), BATCH_MAX))]
            dropped, self._dropped = self._dropped, 0
            self._busy = True
        if dropped:
            batch.append(("recorder_events", (self.session_id, time.time_ns(), "dropped",
                                              f"{dropped} rows dropped: the write queue was full")))
        return batch

    def _transaction(self, rows):
        self._db.execute("BEGIN")
        try:
            for table, row in rows:
                self._db.execute(INSERTS[table], row)
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _write(self, batch):
        while True:
            try:
                self._transaction(batch)
                break
            except sqlite3.Error as exc:
                log.error("cannot write %d rows (%s); retrying", len(batch), exc)
                self._notes.append(str(exc))
                if self._stopping:
                    return                       # shutting down: one more try was made, give up rather than hang
                time.sleep(self._retry_seconds)
        if self._notes:
            notes, self._notes = self._notes, []
            events = [("recorder_events", (self.session_id, time.time_ns(), "write_error", note)) for note in notes]
            try:
                self._transaction(events)
            except sqlite3.Error:
                log.exception("cannot record the write errors")

    def _run(self):
        while True:
            batch = self._take()
            if batch is None:
                return
            try:
                self._write(batch)
            except Exception:                    # the writer must survive anything
                log.exception("the recorder's writer failed on a batch")
            finally:
                with self._idle:
                    self._busy = False
                    self._idle.notify_all()

    def flush(self, timeout=5.0):
        """Wait until everything queued so far has been committed (or dropped). False on timeout."""
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._rows or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._wake.notify()
                self._idle.wait(min(remaining, 0.05))
        return True

    def stop(self):
        """Drain the queue, mark the session ended and close the file. Safe to call twice."""
        if self._closed:
            return
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self._closed = True
        try:
            if self.session_id is not None:
                self._db.execute("UPDATE sessions SET ended_ns = ? WHERE id = ?", (time.time_ns(), self.session_id))
        except sqlite3.Error:
            log.exception("cannot mark the session ended")
        finally:
            self._db.close()
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_storage.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/recorder
git add server/evsim_recorder/__init__.py server/evsim_recorder/storage.py server/tests/test_recorder_storage.py
git commit -m "Add the recorder's SQLite store and writer thread

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Capturing the MQTT messages

**Files:**
- Create: `server/evsim_recorder/capture.py`
- Test: `server/tests/test_recorder_capture.py`

**Interfaces:**
- Consumes: `Store.add_message`, `Store.add_event` (Task 3); `tests/fake_mqtt.py` `FakeMqttClient` (`subscribed`, `published`, `connect`, `drop`, `deliver`).
- Produces: `Capture(client, store, clock_ns=time.time_ns, mono_ns=time.monotonic_ns)` (registers `on_connect`, `on_message`, `on_disconnect`); `build_client() -> paho Client` with id `evsim-recorder-<random>`; `TOPIC = "evsim/#"`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_recorder_capture.py`:

```python
import importlib.util
import os
import shutil
import sqlite3
import tempfile
import unittest

from fake_mqtt import FakeMqttClient, REFUSED
from evsim_recorder.capture import TOPIC, Capture, build_client
from evsim_recorder.storage import Store


class FakeClocks:
    def __init__(self):
        self.wall = 1_791_453_600_000_000_000
        self.mono = 5_000_000_000

    def now(self):
        self.wall += 1_000
        return self.wall

    def tick(self):
        self.mono += 7
        return self.mono


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "c.sqlite")
        self.store = Store(self.path, batch_seconds=0.05)
        self.store.start_session("b", "c", {}, [])
        self.store.start()
        self.addCleanup(self.store.stop)
        self.client = FakeMqttClient()
        self.clocks = FakeClocks()
        self.capture = Capture(self.client, self.store, clock_ns=self.clocks.now, mono_ns=self.clocks.tick)

    def query(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def settle(self):
        self.assertTrue(self.store.flush())

    def test_it_subscribes_to_everything_under_evsim_and_again_after_a_reconnect(self):
        self.client.connect()
        self.assertEqual(self.client.subscribed, [("evsim/#", 0)])
        self.assertEqual(TOPIC, "evsim/#")
        self.client.drop()
        self.client.connect()
        self.assertEqual(self.client.subscribed, [("evsim/#", 0)] * 2)

    def test_a_message_is_stamped_in_the_callback_and_stored_verbatim(self):
        self.client.connect()
        self.client.deliver("evsim/cp", '{"setpoint_w": 4361}')
        self.client.deliver("evsim/state", "C", retain=True)
        self.settle()
        rows = self.query("SELECT t_ns, mono_ns, topic, retained, payload FROM messages ORDER BY id")
        self.assertEqual(rows[0], (1_791_453_600_000_001_000 + 0, 5_000_000_007, "evsim/cp", 0, '{"setpoint_w": 4361}'))
        self.assertEqual(rows[1][1:], (5_000_000_014, "evsim/state", 1, "C"))
        self.assertLess(rows[0][0], rows[1][0])

    def test_connect_and_disconnect_are_recorded_as_events(self):
        self.client.connect()
        self.client.drop()
        self.client.connect()
        self.settle()
        kinds = [k for (k,) in self.query("SELECT kind FROM recorder_events ORDER BY id")]
        self.assertEqual(kinds, ["connected", "disconnected", "connected"])

    def test_a_refused_connection_is_a_disconnected_event_with_the_reason_and_no_subscription(self):
        self.client.connect(REFUSED)
        self.settle()
        self.assertEqual(self.client.subscribed, [])
        kind, detail = self.query("SELECT kind, detail FROM recorder_events")[0]
        self.assertEqual(kind, "disconnected")
        self.assertIn("refused", detail)

    def test_it_never_publishes(self):
        self.client.connect()
        self.client.deliver("evsim/reply", "state C -> B")
        self.client.drop()
        self.assertEqual(self.client.published, [])

    def test_a_failing_store_never_raises_into_the_mqtt_thread(self):
        class Broken:
            session_id = 1

            def add_message(self, *args):
                raise RuntimeError("boom")

            def add_event(self, *args, **kwargs):
                raise RuntimeError("boom")

        client = FakeMqttClient()
        Capture(client, Broken())
        client.connect()
        client.deliver("evsim/cp", "{}")
        client.drop()

    def test_any_payload_is_accepted(self):
        self.client.connect()
        for payload in (b"", b"\xff\xfe", b"{", b"x" * 100_000):
            self.client.deliver("evsim/anything", payload)
        self.settle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(4,)])


@unittest.skipUnless(importlib.util.find_spec("paho"), "paho-mqtt is not installed")
class BuildClientTest(unittest.TestCase):
    def test_the_client_has_its_own_unique_id_and_never_connects_by_itself(self):
        ids = {build_client()._client_id for _ in range(20)}
        self.assertEqual(len(ids), 20)
        self.assertTrue(all(i.startswith(b"evsim-recorder-") for i in ids))
        self.assertFalse(build_client().is_connected())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_capture.py'`
Expected: FAIL with `No module named 'evsim_recorder.capture'`.

- [ ] **Step 3: Write the implementation**

Create `server/evsim_recorder/capture.py`:

```python
"""The MQTT side of the recorder: stamp every message of the chain the moment it arrives and queue it.

Subscribing happens in on_connect, so it is renewed on every reconnect. The callbacks do nothing but stamp, hand the
message to the store's queue and return; an exception here would kill paho's network thread, so none escapes.
"""

import logging
import secrets
import time

log = logging.getLogger("evsim_recorder.capture")

TOPIC = "evsim/#"
KEEPALIVE_S = 10
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 5


def build_client():
    """The real paho client: a unique id (equal ids kick each other off the broker), a short reconnect delay."""
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"evsim-recorder-{secrets.token_hex(3)}")
    client.reconnect_delay_set(min_delay=RECONNECT_MIN_S, max_delay=RECONNECT_MAX_S)
    return client


class Capture:
    def __init__(self, client, store, clock_ns=time.time_ns, mono_ns=time.monotonic_ns):
        self._store = store
        self._clock_ns = clock_ns
        self._mono_ns = mono_ns
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

    def _event(self, kind, detail):
        try:
            self._store.add_event(kind, detail)
        except Exception:
            log.exception("could not record a %s event", kind)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            self._event("disconnected", f"refused: {reason_code}")
            return
        client.subscribe(TOPIC, qos=0)
        log.info("connected, subscribed to %s", TOPIC)
        self._event("connected", TOPIC)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        log.warning("disconnected from the broker: %s", reason_code)
        self._event("disconnected", str(reason_code))

    def _on_message(self, client, userdata, msg):
        t_ns = self._clock_ns()                 # first thing: the arrival time
        mono_ns = self._mono_ns()
        try:
            self._store.add_message(t_ns, mono_ns, msg.topic, msg.retain, msg.payload)
        except Exception:
            log.exception("could not queue a message from %s", msg.topic)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_*.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/recorder
git add server/evsim_recorder/capture.py server/tests/test_recorder_capture.py
git commit -m "Capture every message of the chain with its arrival time

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The charger logger and the experiment

**Files:**
- Create: `server/evsim_recorder/charger_log.py`, `server/evsim_recorder/experiment.py`
- Test: `server/tests/test_recorder_experiment.py`

**Interfaces:**
- Consumes: `Store` (Task 3), `ChargerService(on_poll=...)`, `set_limit` times (Task 2), `config.MIN_LIMIT_A`, `config.MAX_LIMIT_A`, `FakeAmtron`.
- Produces:
  - `ChargerLogger(store)`: a callable for `on_poll`; adds one `charger` row per poll (raw `meter_current_l*_a` to `i_l*_a`, `meter_voltage_l*_v` to `v_l*_v`, `meter_power_w` to `power_w`, the rest by name) and a `charger_error` event when polls start failing and a `charger_ok` event when they recover (transitions only).
  - `parse_steps(text) -> list[int]` and `validate_steps(steps, installation_limit)`, both raising `PlanError`; `describe(steps, dwell_s, repeat, baseline_s, current_limit, charger_host) -> str`.
  - `Experiment(service, store, steps, dwell_s, repeat=1, baseline_s=0, leave=False, stop=None, clock_ns=time.time_ns)` with `run() -> bool` (True when the whole plan ran) and `request_stop()`. Writes only through `service.set_limit`; stores a `limit_writes` row per write (restore: `run=0`, `step=-1`, label `restore`); restores the limit found at the start unless `leave`, on every exit path.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_recorder_experiment.py`:

```python
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from fake_amtron import FakeAmtron
from charger_web.service import ChargerService
from evsim_recorder.charger_log import ChargerLogger
from evsim_recorder.experiment import Experiment, PlanError, describe, parse_steps, validate_steps
from evsim_recorder.storage import Store


class ParseStepsTest(unittest.TestCase):
    def test_a_comma_separated_list_of_whole_numbers(self):
        self.assertEqual(parse_steps("6,7, 12 ,16,0"), [6, 7, 12, 16, 0])

    def test_anything_else_is_refused(self):
        for bad in ("", " ", ",", "6,,7", "6;7", "six", "6.5", "-6", "+6", "6,x", "1e1", "6 7", "0x10"):
            with self.assertRaises(PlanError, msg=repr(bad)):
                parse_steps(bad)


class ValidateStepsTest(unittest.TestCase):
    def test_zero_and_six_to_sixteen_are_allowed(self):
        validate_steps([0, 6, 7, 16], 16)

    def test_everything_else_is_refused(self):
        for bad in (1, 5, 17, 20, 100):
            with self.assertRaises(PlanError, msg=bad):
                validate_steps([10, bad], 16)

    def test_the_installation_limit_is_a_ceiling(self):
        validate_steps([13], 13)
        with self.assertRaises(PlanError):
            validate_steps([14], 13)

    def test_an_unknown_installation_limit_means_16(self):
        validate_steps([16], None)
        with self.assertRaises(PlanError):
            validate_steps([17], None)

    def test_an_empty_plan_is_refused(self):
        with self.assertRaises(PlanError):
            validate_steps([], 16)


class DescribeTest(unittest.TestCase):
    def test_the_plan_text_names_the_steps_the_time_the_charger_and_the_current_limit(self):
        text = describe([10, 11, 10], 45, 2, 10, 14, "192.168.21.117")
        for needle in ("10", "11", "45", "192.168.21.117", "14 A", "restore"):
            self.assertIn(needle, text)
        self.assertIn("6 writes", text)                       # 3 steps x 2 runs
        self.assertIn("280 s", text)                          # 10 baseline + 6 x 45
        self.assertNotIn("pause", text)

    def test_a_zero_step_is_flagged(self):
        self.assertIn("pause", describe([10, 0, 10], 30, 1, 0, 10, "h"))

    def test_leaving_the_last_step_is_said(self):
        self.assertIn("stay", describe([10, 11], 30, 1, 0, 10, "h", leave=True))


class RigCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "x.sqlite")
        self.store = Store(self.path, batch_seconds=0.05)
        self.store.start_session("b", "c", {}, [])
        self.store.start()
        self.addCleanup(self.store.stop)
        self.service = ChargerService("127.0.0.1", self.port, on_poll=ChargerLogger(self.store), period_s=0.05)
        self.addCleanup(self.service.stop)
        self.service.poll_once()

    def query(self, sql):
        self.assertTrue(self.store.flush())
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def experiment(self, steps, dwell=0.05, **kwargs):
        return Experiment(self.service, self.store, steps, dwell, **kwargs)


class ChargerLoggerTest(RigCase):
    def test_each_poll_is_a_row_with_the_raw_phase_values(self):
        row = self.query("SELECT t_start_ns, t_end_ns, set_limit_a, operator_limit_a, installation_limit_a, "
                         "signalled_current_a, i_l1_a, i_l2_a, i_l3_a, v_l1_v, v_l2_v, v_l3_v, power_w, error FROM charger")[0]
        self.assertLess(row[0], row[1])
        self.assertEqual(row[2:], (10, 16, 16, 10, 0.874, 0.651, 0.808, 231, 232, 233, 600, None))

    def test_a_failing_charger_is_one_error_event_not_one_per_poll(self):
        self.fake.stop()
        for _ in range(3):
            self.service.poll_once()
        events = self.query("SELECT kind, detail FROM recorder_events ORDER BY id")
        self.assertEqual([e[0] for e in events], ["charger_error"])
        self.assertTrue(events[0][1])
        self.assertEqual(self.query("SELECT COUNT(*) FROM charger WHERE error IS NOT NULL"), [(3,)])

    def test_the_end_of_a_run_of_failures_is_one_recovery_event(self):
        logger = ChargerLogger(self.store)
        ok = {"t_start_ns": 1, "t_end_ns": 2, "values": {}, "error": None}
        bad = {"t_start_ns": 3, "t_end_ns": 4, "values": {}, "error": "timeout"}
        for poll in (ok, bad, bad, bad, ok, ok, bad, ok):
            logger(poll)
        events = [e[0] for e in self.query("SELECT kind FROM recorder_events ORDER BY id")]
        self.assertEqual(events, ["charger_error", "charger_ok", "charger_error", "charger_ok"])


class ExperimentTest(RigCase):
    def writes(self):
        return [value for address, value in self.fake.writes]

    def test_the_steps_are_written_in_order_each_logged_with_its_times_and_readback(self):
        self.assertTrue(self.experiment([11, 12, 11]).run())
        rows = self.query("SELECT run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error FROM limit_writes "
                          "WHERE step >= 0 ORDER BY id")
        self.assertEqual([(r[0], r[1], r[4], r[5], r[6], r[7], r[8]) for r in rows],
                         [(1, 0, 10, 11, 11, "10 -> 11 A", None), (1, 1, 11, 12, 12, "11 -> 12 A", None),
                          (1, 2, 12, 11, 11, "12 -> 11 A", None)])
        for row in rows:
            self.assertLess(row[2], row[3])
            self.assertGreater(row[2], 1_700_000_000_000_000_000)

    def test_only_register_1000_is_ever_written(self):
        self.experiment([11, 0, 16]).run()
        self.assertTrue(self.fake.writes)
        self.assertEqual({address for address, value in self.fake.writes}, {1000})

    def test_the_limit_found_at_the_start_is_written_back_at_the_end(self):
        self.experiment([12, 13]).run()
        self.assertEqual(self.writes(), [12, 13, 10])
        self.assertEqual(self.fake.regs[1000], 10)
        restore = self.query("SELECT run, step, from_a, to_a, label FROM limit_writes ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(restore, (0, -1, 13, 10, "restore"))

    def test_with_leave_the_last_step_stays(self):
        self.experiment([12, 13], leave=True).run()
        self.assertEqual(self.writes(), [12, 13])
        self.assertEqual(self.fake.regs[1000], 13)

    def test_a_run_that_already_ends_on_the_original_limit_writes_no_restore(self):
        self.experiment([12, 10]).run()
        self.assertEqual(self.writes(), [12, 10])

    def test_repeat_runs_the_list_again_with_the_run_number(self):
        self.experiment([11, 12], repeat=2).run()
        self.assertEqual(self.writes(), [11, 12, 11, 12, 10])
        rows = self.query("SELECT run, step FROM limit_writes WHERE step >= 0 ORDER BY id")
        self.assertEqual(rows, [(1, 0), (1, 1), (2, 0), (2, 1)])

    def test_each_step_is_held_for_the_dwell(self):
        started = time.monotonic()
        self.experiment([11, 12], dwell=0.3).run()
        self.assertGreaterEqual(time.monotonic() - started, 0.6)

    def test_the_baseline_passes_before_the_first_write(self):
        started = time.monotonic()
        experiment = self.experiment([11], dwell=0.05, baseline_s=0.4)
        first = []
        original = self.service.set_limit

        def spy(value):
            first.append(time.monotonic() - started)
            return original(value)

        self.service.set_limit = spy
        experiment.run()
        self.assertGreaterEqual(first[0], 0.4)

    def test_a_step_of_zero_is_allowed_and_written(self):
        self.experiment([0, 12]).run()
        self.assertEqual(self.writes()[:2], [0, 12])

    def test_a_refused_write_is_logged_with_its_error_stops_the_run_and_still_tries_the_restore(self):
        self.fake.refuse_writes = True
        self.assertFalse(self.experiment([11, 12]).run())
        rows = self.query("SELECT step, error FROM limit_writes ORDER BY id")
        self.assertEqual(rows[0][0], 0)
        self.assertTrue(rows[0][1])
        self.assertEqual(len([r for r in rows if r[0] >= 0]), 1)               # the second step was never tried
        self.assertEqual(rows[-1][0], -1)                                       # the restore was attempted (and refused too)
        self.assertTrue(rows[-1][1])

    def test_an_unconfirmed_write_is_an_error_too_and_the_original_is_written_again_to_be_safe(self):
        self.fake.ignore_writes = True
        self.assertFalse(self.experiment([11, 12]).run())
        self.assertTrue(self.query("SELECT error FROM limit_writes ORDER BY id")[0][0])
        self.assertEqual(self.writes(), [11, 10])

    def test_a_failed_write_is_restored_even_with_leave(self):
        self.fake.refuse_writes = True
        self.assertFalse(self.experiment([11, 12], leave=True).run())
        self.assertEqual(self.writes(), [11, 10])

    def test_a_stop_during_a_dwell_ends_the_run_and_restores_the_original_limit(self):
        experiment = self.experiment([12, 13, 14], dwell=5)
        threading.Timer(0.3, experiment.request_stop).start()
        started = time.monotonic()
        self.assertFalse(experiment.run())
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.writes(), [12, 10])
        self.assertEqual(self.fake.regs[1000], 10)

    def test_a_stop_during_the_baseline_writes_nothing_at_all(self):
        experiment = self.experiment([12], baseline_s=5)
        threading.Timer(0.2, experiment.request_stop).start()
        self.assertFalse(experiment.run())
        self.assertEqual(self.writes(), [])

    def test_an_exception_in_the_middle_still_restores_the_limit(self):
        experiment = self.experiment([12, 13], dwell=0.05)
        original = self.service.set_limit
        calls = []

        def explode(value):
            calls.append(value)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return original(value)

        self.service.set_limit = explode
        with self.assertRaises(KeyboardInterrupt):
            experiment.run()
        self.assertEqual(calls, [12, 13, 10])                                   # the restore was still attempted
        self.assertEqual(self.fake.regs[1000], 10)

    def test_the_poll_keeps_running_through_the_run_so_the_charger_never_falls_back(self):
        self.service.start()
        self.experiment([11, 12], dwell=0.3).run()
        reads = [e for e in self.fake.log if e[0] == 3 and e[1] == 1000]
        self.assertGreater(len(reads), 5)
        self.assertFalse(self.fake.in_fallback)

    def test_the_original_limit_unreadable_means_no_restore_and_an_event(self):
        self.service._values["set_limit_a"] = None
        self.experiment([12]).run()
        self.assertEqual(self.writes(), [12])
        self.assertIn("restore_skipped", [k for (k,) in self.query("SELECT kind FROM recorder_events")])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_experiment.py'`
Expected: FAIL with `No module named 'evsim_recorder.charger_log'`.

- [ ] **Step 3: Write the implementation**

Create `server/evsim_recorder/charger_log.py`:

```python
"""Turns each poll of the charger service into a row of the charger table.

The values are the raw ones, per phase (not means). A run of failing polls is one `charger_error` event and its end is
one `charger_ok` event, so a gap in the charger data can be told from a quiet system without one event per second.
"""

import logging

log = logging.getLogger("evsim_recorder.charger_log")

COLUMNS = {                        # charger table column <- register name
    "set_limit_a": "set_limit_a", "operator_limit_a": "operator_limit_a", "installation_limit_a": "installation_limit_a",
    "signalled_current_a": "signalled_current_a", "vehicle_state": "vehicle_state", "charger_status": "charger_status",
    "i_l1_a": "meter_current_l1_a", "i_l2_a": "meter_current_l2_a", "i_l3_a": "meter_current_l3_a",
    "v_l1_v": "meter_voltage_l1_v", "v_l2_v": "meter_voltage_l2_v", "v_l3_v": "meter_voltage_l3_v",
    "power_w": "meter_power_w",
}


class ChargerLogger:
    def __init__(self, store):
        self._store = store
        self._failing = False

    def __call__(self, poll):
        values = poll["values"]
        self._store.add_charger(
            t_start_ns=poll["t_start_ns"], t_end_ns=poll["t_end_ns"], error=poll["error"],
            **{column: values.get(register) for column, register in COLUMNS.items()})
        failing = poll["error"] is not None
        if failing and not self._failing:
            self._store.add_event("charger_error", poll["error"], t_ns=poll["t_end_ns"])
        elif self._failing and not failing:
            self._store.add_event("charger_ok", "polls succeed again", t_ns=poll["t_end_ns"])
        self._failing = failing
```

Create `server/evsim_recorder/experiment.py`:

```python
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
```

The restore runs in a `finally`, so a `KeyboardInterrupt` or any other exception still restores the limit before it propagates.

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_*.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/recorder
git add server/evsim_recorder/charger_log.py server/evsim_recorder/experiment.py server/tests/test_recorder_experiment.py
git commit -m "Log the charger polls and run the stepping experiment

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The command, its documentation and the spec additions

**Files:**
- Create: `server/evsim_recorder/__main__.py`
- Test: `server/tests/test_recorder_main.py`
- Modify: `server/README.md`, `.gitignore`
- Modify: `docs/superpowers/specs/2026-10-08-evsim-recorder-design.md` (two small additions)
- Create: `memory/evsim-recorder.md`; Modify: `memory/MEMORY.md`

**Interfaces:**
- Consumes: everything above; `charger_web.config.charger_host()`.
- Produces: `main(argv=None, confirm=input) -> int` (0 on success, 1 when the experiment failed or the charger could not be reached, 2 for a bad argument via argparse); `python -m evsim_recorder run ...`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_recorder_main.py`:

```python
import contextlib
import io
import os
import shutil
import signal
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from fake_amtron import FakeAmtron
from fake_mqtt import FakeMqttClient
from charger_web import config
from evsim_recorder import __main__ as recorder_main


class MainCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "m.sqlite")
        self.client = FakeMqttClient()
        for patcher in (
            mock.patch.object(config, "MODBUS_PORT", self.port),
            mock.patch.dict(os.environ, {"EVSIM_CHARGER_HOST": "127.0.0.1"}),
            mock.patch.object(recorder_main, "build_client", return_value=self.client),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *args, confirm=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = recorder_main.main(["run", "--db", self.path, "--charger-period", "1", *args],
                                          confirm=confirm or (lambda prompt: "y"))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def rows(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def writes(self):
        return [value for address, value in self.fake.writes]


class RunTest(MainCase):
    def test_a_run_steps_the_limit_records_it_and_restores_the_original(self):
        code, out, err = self.run_main("--steps", "11,12", "--dwell", "0.3", "--baseline", "0.2", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.writes(), [11, 12, 10])
        self.assertEqual(self.fake.regs[1000], 10)
        steps = self.rows("SELECT step, from_a, to_a FROM limit_writes ORDER BY id")
        self.assertEqual(steps, [(0, 10, 11), (1, 11, 12), (-1, 12, 10)])
        self.assertGreaterEqual(self.rows("SELECT COUNT(*) FROM charger")[0][0], 1)
        session = self.rows("SELECT broker, charger, plan, ended_ns FROM sessions")[0]
        self.assertIn("127.0.0.1", session[1])
        self.assertIn('"steps": [11, 12]', session[2])
        self.assertIsNotNone(session[3])

    def test_the_mqtt_client_is_connected_for_the_run_and_closed_after_it_in_order(self):
        self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual([c[0] for c in self.client.calls], ["connect_async", "loop_start", "disconnect", "loop_stop"])
        self.assertEqual(self.client.published, [])

    def test_the_plan_is_printed_and_the_answer_is_asked(self):
        asked = []
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", confirm=lambda p: asked.append(p) or "y")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(asked), 1)
        self.assertIn("11", out)
        self.assertIn("127.0.0.1", out)
        self.assertIn("10 A", out)                                  # the limit found on the charger

    def test_declining_writes_nothing(self):
        for answer in ("n", "", "no", "maybe", "N"):
            code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", confirm=lambda p, a=answer: a)
            self.assertEqual(code, 0)
            self.assertEqual(self.writes(), [], answer)
            self.assertIn("Nothing was written", out)

    def test_yes_skips_the_question(self):
        def never(prompt):
            raise AssertionError("asked")
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes", confirm=never)
        self.assertEqual(code, 0, err)

    def test_leave_keeps_the_last_step(self):
        self.run_main("--steps", "11,12", "--dwell", "0.1", "--baseline", "0", "--yes", "--leave")
        self.assertEqual(self.writes(), [11, 12])

    def test_a_stop_signal_during_a_dwell_restores_the_original_limit(self):
        timer = threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGINT))
        timer.start()
        self.addCleanup(timer.cancel)
        code, out, err = self.run_main("--steps", "12,13", "--dwell", "30", "--baseline", "0", "--yes")
        self.assertEqual(self.writes(), [12, 10])
        self.assertEqual(self.fake.regs[1000], 10)
        self.assertEqual(code, 1)
        self.assertIsNotNone(self.rows("SELECT ended_ns FROM sessions")[0][0])

    def test_sigterm_does_the_same(self):
        timer = threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        self.addCleanup(timer.cancel)
        self.run_main("--steps", "12", "--dwell", "30", "--baseline", "0", "--yes")
        self.assertEqual(self.writes(), [12, 10])

    def test_the_signal_handlers_are_put_back_afterwards(self):
        before = signal.getsignal(signal.SIGINT)
        self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_a_refused_write_is_exit_code_1_and_the_restore_is_attempted(self):
        self.fake.refuse_writes = True
        code, out, err = self.run_main("--steps", "11,12", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [11, 10])                  # the failed step, then the attempted restore


class BadPlanTest(MainCase):
    def test_a_bad_plan_writes_nothing_and_exits_with_a_message(self):
        for steps in ("5", "17", "6,7,99", "6.5", "", "x", "-6", "6,,7"):
            code, out, err = self.run_main("--steps", steps, "--dwell", "0.1", "--yes")
            self.assertNotEqual(code, 0, steps)
            self.assertEqual(self.writes(), [], steps)

    def test_a_step_above_the_installation_limit_is_refused_before_any_write(self):
        self.fake.regs[133] = 13
        code, out, err = self.run_main("--steps", "12,14", "--dwell", "0.1", "--yes")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("14", err + out)

    def test_bad_options_are_refused(self):
        for args in (("--dwell", "0"), ("--dwell", "-5"), ("--dwell", "x"), ("--repeat", "0"), ("--baseline", "-1"),
                     ("--charger-period", "0.5"), ("--charger-period", "11")):
            code, out, err = self.run_main("--steps", "11", "--yes", *args)
            self.assertEqual(code, 2, args)
            self.assertEqual(self.writes(), [], args)

    def test_steps_are_required(self):
        code, out, err = self.run_main("--yes")
        self.assertEqual(code, 2)

    def test_an_unreachable_charger_stops_before_writing_anything(self):
        self.fake.stop()
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])
        self.assertIn("charger", (out + err).lower())

    def test_a_database_that_cannot_be_created_exits_with_a_message(self):
        code, out, err = self.run_main("--steps", "11", "--yes", "--db", "/nonexistent-recorder-folder/x/y.sqlite")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])
        self.assertIn("database", (out + err).lower())

    def test_a_missing_database_folder_is_created(self):
        path = os.path.join(self.dir, "new", "folder", "r.sqlite")
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes", "--db", path)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(path))


class DefaultsTest(unittest.TestCase):
    def test_the_default_database_name_has_the_date_and_lives_in_data_recordings(self):
        path = recorder_main.default_db_path()
        self.assertTrue(path.endswith(".sqlite"))
        self.assertIn(os.path.join("data", "recordings", "evsim-"), path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_main.py'`
Expected: FAIL with `cannot import name '__main__'` or no `main`.

- [ ] **Step 3: Write the implementation**

Create `server/evsim_recorder/__main__.py`:

```python
"""python -m evsim_recorder run --steps 6,7,6,12 --dwell 60: step the charger's limit and record the chain to SQLite."""

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone

from charger_web import config as charger_config
from charger_web.service import ChargerService

from .capture import KEEPALIVE_S, Capture, build_client
from .charger_log import ChargerLogger
from .experiment import Experiment, PlanError, describe, parse_steps, validate_steps
from .storage import Store

log = logging.getLogger("evsim_recorder")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def default_db_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return os.path.join(REPO_ROOT, "data", "recordings", f"evsim-{stamp}.sqlite")


def _positive(kind, minimum=0, strict=True):
    def convert(text):
        try:
            value = kind(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
        if (value <= minimum) if strict else (value < minimum):
            raise argparse.ArgumentTypeError(f"must be {'above' if strict else 'at least'} {minimum}")
        return value
    return convert


def _period(text):
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
    if not 1 <= value <= 10:
        raise argparse.ArgumentTypeError("must be from 1 to 10 seconds")
    return value


def _steps(text):
    try:
        return parse_steps(text)
    except PlanError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m evsim_recorder",
        description="Step the AMTRON's maximum current and record the charger, the board, the setpoint server and the "
                    "inverter into one SQLite file, to measure propagation delays and power at each current.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an experiment")
    run.add_argument("--steps", required=True, type=_steps, help="maximum currents to write, in order: 0 or 6 to 16, e.g. 6,7,6,12")
    run.add_argument("--dwell", type=_positive(float), default=60.0, help="seconds to hold each step (default %(default)s)")
    run.add_argument("--repeat", type=_positive(int), default=1, help="how many times to run the list (default %(default)s)")
    run.add_argument("--baseline", type=_positive(float, 0, strict=False), default=10.0,
                     help="seconds of recording before the first write (default %(default)s)")
    run.add_argument("--db", default=None, help="SQLite file (default data/recordings/evsim-<UTC time>.sqlite)")
    run.add_argument("--host", default="127.0.0.1", help="MQTT broker (default %(default)s)")
    run.add_argument("--port", type=int, default=1883, help="MQTT broker port (default %(default)s)")
    run.add_argument("--charger-period", type=_period, default=1.0, help="seconds between charger polls, 1 to 10 (default %(default)s)")
    run.add_argument("--leave", action="store_true", help="keep the last step on the charger instead of restoring the first limit")
    run.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


def main(argv=None, confirm=input):
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        host = charger_config.charger_host()
    except ValueError as exc:
        parser.error(str(exc))

    path = args.db or default_db_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        store = Store(path)
    except (OSError, sqlite3.Error) as exc:
        print(f"Cannot create the database {path}: {exc}", file=sys.stderr)
        return 1
    stop = threading.Event()
    service = client = None
    started = False
    old_handlers = {}
    try:
        plan = {"steps": args.steps, "dwell_s": args.dwell, "repeat": args.repeat, "baseline_s": args.baseline,
                "charger_period_s": args.charger_period, "leave": args.leave}
        store.start_session(f"{args.host}:{args.port}", host, plan, sys.argv if argv is None else ["evsim_recorder", *argv])
        store.start()
        service = ChargerService(host, period_s=args.charger_period, on_poll=ChargerLogger(store))
        service.poll_once()
        status = service.status()
        if status["status"] != "ok":
            print(f"Cannot reach the charger at {host}: {status['error'] or 'no answer'}. Nothing was written.", file=sys.stderr)
            return 1
        current = status["set_limit_a"]["value"]
        try:
            validate_steps(args.steps, status["installation_limit_a"]["value"])
        except PlanError as exc:
            print(f"{exc} Nothing was written.", file=sys.stderr)
            return 1
        print(describe(args.steps, args.dwell, args.repeat, args.baseline, current, host, args.leave))
        if not args.yes and str(confirm("Run this experiment on the charger? [y/N] ")).strip().lower() not in ("y", "yes"):
            print("Nothing was written.")
            return 0

        client = build_client()
        Capture(client, store)
        client.connect_async(args.host, args.port, keepalive=KEEPALIVE_S)
        client.loop_start()
        started = True
        service.start()                                           # the heartbeat, until the restore is done
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, lambda signum, frame: stop.set())
        experiment = Experiment(service, store, args.steps, args.dwell, args.repeat, args.baseline, args.leave, stop)
        done = experiment.run()
        print("Done." if done else "Stopped before the end: the limit was restored." if not args.leave else "Stopped before the end.")
        return 0 if done else 1
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if service is not None:
            service.stop()
        if client is not None and started:
            client.disconnect()
            client.loop_stop()
        store.stop()
        print(f"Recorded to {path}")


if __name__ == "__main__":
    sys.exit(main())
```

Append to `.gitignore`:

```
# Recordings of the evsim_recorder (SQLite and its WAL files)
data/recordings/
```

Append to `server/README.md`:

````markdown

## The recorder: step the limit and log the chain

`evsim_recorder` runs a characterization experiment: it steps the AMTRON's maximum current through a list of values and
records, with one clock and into one SQLite file, the charger's own measurements, every MQTT message of the chain
(`evsim/#`: the board's raw samples, the setpoint, the inverter) and each limit write. It is for measuring how long a
change of the limit takes to reach each hop, and the power at each current.

    cd server
    .venv/bin/python -m evsim_recorder run --steps 6,7,6,12,16,6 --dwell 60

It prints the plan and asks for confirmation (`--yes` skips the question). Options: `--dwell` seconds per step (60),
`--repeat` runs of the list (1), `--baseline` seconds recorded before the first write (10), `--db PATH` (default
`data/recordings/evsim-<UTC time>.sqlite`, git-ignored), `--host`/`--port` for the broker, `--charger-period` seconds
between charger polls (1 to 10, default 1), `--leave` to keep the last step.

- **Safety:** it writes only register 1000, only 0 or 6 to 16 A and not above the installation limit, and checks the
  whole list before writing anything. The limit found at the start is written back at the end and on Ctrl-C or
  SIGTERM (unless `--leave`). A step of 0 A pauses charging. Do not press Apply on the page during a run: the page
  re-writes the limit it last applied and would undo the steps.
- **Time zero** is `limit_writes.t_write_ns`, the computer's clock just before the Modbus write.
- **Tables:** `sessions`, `messages` (every MQTT message, verbatim, with `t_ns` wall clock and `mono_ns` monotonic),
  `charger` (one row per poll: raw phase currents, voltages, power, signalled current, limits, states), `limit_writes`,
  `recorder_events` (broker disconnects, charger errors and recoveries, dropped rows: a gap in the data is not a quiet
  system). **Views:** `cp`, `setpoint`, `inverter` (typed columns; `NULL` for anything unreadable).
- **Resolution:** `evsim/cp` is 5 Hz and crosses the WiFi (its arrival time includes a transit time that is not
  known; `board_ms` is the board's own uptime and shows its 200 ms rhythm); `evsim/setpoint` and `evsim/inverter` are
  1 Hz from the setpoint server, with their own times in `tick_ms`, `read_ts_ms` and `cap_ts_ms`; the charger is polled
  once a second. All times are one computer's clock.

Example (pandas): the delay from each write to the first `cp` sample whose setpoint moved by more than 50 W, and the
mean powers over the last 20 s of each step.

```python
import sqlite3, pandas as pd
db = sqlite3.connect("data/recordings/evsim-XXXX.sqlite")
writes = pd.read_sql("SELECT * FROM limit_writes WHERE step >= 0", db)
cp = pd.read_sql("SELECT t_ns, setpoint_w FROM cp ORDER BY t_ns", db)
for w in writes.itertuples():
    before = cp[cp.t_ns < w.t_write_ns].setpoint_w.tail(5).median()
    after = cp[(cp.t_ns >= w.t_write_ns) & ((cp.setpoint_w - before).abs() > 50)]
    delay = (after.t_ns.iloc[0] - w.t_write_ns) / 1e9 if len(after) else None
    print(w.label, "first cp change after", delay, "s")
```

Design: `docs/superpowers/specs/2026-10-08-evsim-recorder-design.md`.
````

Add the two small additions to the spec `docs/superpowers/specs/2026-10-08-evsim-recorder-design.md`. Replace this text:

```
python -m evsim_recorder run --steps 6,7,6,12,16,6 --dwell 60 [--repeat 1] [--db PATH]
```

with:

```
python -m evsim_recorder run --steps 6,7,6,12,16,6 --dwell 60 [--repeat 1] [--baseline 10] [--db PATH]
```

and replace this text:

```
- **`--dwell`**: seconds to hold each step (default 60), so each response settles and the steady state can be averaged. **`--repeat`**: how many times to run the whole list (default 1).
```

with:

```
- **`--dwell`**: seconds to hold each step (default 60), so each response settles and the steady state can be averaged. **`--repeat`**: how many times to run the whole list (default 1). **`--baseline`**: seconds of recording before the first write (default 10), so the value before the first step is measured too.
```

and replace this text:

```
| `recorder_events` | `id`, `session_id`, `t_ns`, `kind` (`connected`, `disconnected`, `dropped`, `write_error`, `charger_error`), `detail` |
```

with:

```
| `recorder_events` | `id`, `session_id`, `t_ns`, `kind` (`connected`, `disconnected`, `dropped`, `write_error`, `charger_error`, `charger_ok`, `restore_skipped`), `detail` |
```

Create `memory/evsim-recorder.md`:

```markdown
---
name: evsim-recorder
description: server/evsim_recorder steps the AMTRON's limit (register 1000) and records the charger, board, setpoint and inverter to SQLite to measure propagation delays and power per current
metadata:
  type: project
---

`python -m evsim_recorder run --steps 6,7,6,12 --dwell 60` (spec `docs/superpowers/specs/2026-10-08-evsim-recorder-design.md`,
plan `docs/superpowers/plans/2026-10-08-evsim-recorder.md`). One process: an MQTT subscriber on `evsim/#`, the page's
`ChargerService` polled every 1 s (also the heartbeat), and a step runner that writes register 1000 and logs the exact time
(`limit_writes.t_write_ns` = time zero). Output is one SQLite file in `data/recordings/` (git-ignored): `messages` (lossless),
`charger`, `limit_writes`, `recorder_events`, views `cp`, `setpoint`, `inverter`.

- **Writes only register 1000**, 0 or 6 to 16 A, plan validated first, confirmation asked (`--yes`), the first limit restored on
  every exit (Ctrl-C, SIGTERM, a refused write) unless `--leave`.
- **Do not press Apply on the page during a run:** its service re-writes the limit it last applied.
- **Additive timestamps** added for it: `ts_ms` in `evsim/setpoint`; `read_ts_ms` and `cap_ts_ms` in `evsim/inverter`.
- **Limits of the timing:** the board's `evsim/cp` crosses the WiFi (unknown transit); the setpoint server averages over 1 s and
  publishes at 1 Hz; the inverter writer reads at 1 Hz; the charger is polled at 1 s. Nothing here samples faster on purpose:
  it characterizes the system as it is.

Related: [[evsim-dashboard]], [[evsim-setpoint-server]], [[evsim-inverter-fronius]], [[evsim-amtron-modbus]].
```

Append this line to `memory/MEMORY.md`:

```
- [EVSim recorder](evsim-recorder.md) — `python -m evsim_recorder run` steps the AMTRON limit and records charger, board, setpoint and inverter to SQLite for delay and power characterization.
```

- [ ] **Step 4: Run it to verify it passes, then the whole suite**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_recorder_*.py'` then `.venv/bin/python -B -m unittest discover -s tests`
Expected: both OK (the whole suite takes about two minutes).

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/recorder
git add server/evsim_recorder/__main__.py server/tests/test_recorder_main.py server/README.md .gitignore \
  docs/superpowers/specs/2026-10-08-evsim-recorder-design.md memory/evsim-recorder.md memory/MEMORY.md
git commit -m "Add the evsim_recorder command and document it

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The live check on the rig

**Files:**
- Modify: `memory/evsim-recorder.md` (a result section)

**Interfaces:**
- Consumes: the whole recorder, the real broker, board, AMTRON, inverter and setpoint server.
- Produces: a record of what a real run showed.

- [ ] **Step 1: Prepare**

The rig must be running: the Mosquitto broker, the board publishing, the AMTRON charging and the inverter taking its power. **Restart the setpoint server from this branch** so it publishes the timestamp fields (the old one does not). Stop the page's Apply use for the duration (the page may stay open).

```bash
cd server
kill $(cat /tmp/setpoint-live.pid) 2>/dev/null; sleep 3
(nohup .venv/bin/python -B -m setpoint_server --inverter 192.168.21.120 > /tmp/setpoint-live.log 2>&1 & echo $! > /tmp/setpoint-live.pid)
sleep 20
mosquitto_sub -h 127.0.0.1 -t evsim/setpoint -C 1 -W 5
mosquitto_sub -h 127.0.0.1 -t evsim/inverter -C 1 -W 5
```

Expected: `evsim/setpoint` has `ts_ms`; `evsim/inverter` has `read_ts_ms` and a `cap_ts_ms` (a number once a cap has been written).

- [ ] **Step 2: Run a short experiment: a 1 A step only (ask the user first, it changes the charger's limit)**

Note the limit now set (`curl -s http://127.0.0.1:8081/api/charger/status` if a page is running, or let the recorder print it). The AMTRON's own operator limit caps the effect, so choose steps at or below it.

```bash
.venv/bin/python -B -m evsim_recorder run --steps 10,11,10 --dwell 45 --baseline 10 --yes
```

Expected: the plan is printed; three writes about 45 s apart; at the end the original limit is back (`Recorded to data/recordings/evsim-….sqlite`); exit code 0; no `charger_error` in the output.

- [ ] **Step 3: Check the recording**

```bash
DB=$(ls -t ../data/recordings/*.sqlite | head -1)
sqlite3 "$DB" "select run, step, from_a, to_a, readback_a, (t_ack_ns - t_write_ns)/1e6 as ack_ms from limit_writes order by id;"
sqlite3 "$DB" "select kind, count(*) from recorder_events group by kind;"
sqlite3 "$DB" "select count(*) as cp_rows, round((max(t_ns)-min(t_ns))/1e9,1) as seconds, round(count(*)/((max(t_ns)-min(t_ns))/1e9),2) as per_s from cp;"
sqlite3 "$DB" "select count(*), round(count(*)/((max(t_ns)-min(t_ns))/1e9),2) from setpoint;"
sqlite3 "$DB" "select count(*), round(count(*)/((max(t_ns)-min(t_ns))/1e9),2), min(cap_ts_ms is null) from inverter;"
sqlite3 "$DB" "select count(*), round(count(*)/((max(t_end_ns)-min(t_end_ns))/1e9),2), sum(error is not null) from charger;"
sqlite3 "$DB" "select round(avg(power_w)), round(avg(i_l1_a),2) from charger where t_end_ns between (select t_write_ns from limit_writes where step=1)+25e9 and (select t_write_ns from limit_writes where step=1)+44e9;"
```

Expected: the three writes plus a restore row if the first limit differed; `connected` events and no `write_error` or `dropped`; `cp` about 5 rows a second with `board_ms` steps near 200 (`select board_ms - lag(board_ms) over (order by t_ns)` shows it); `setpoint` and `inverter` about 1 a second; `charger` about 1 a second with no errors. Then run the README's delay example on the 1 A step and write down what it gives for the first `cp`, `setpoint` and `inverter` change.

- [ ] **Step 4: Record the result**

Append a `## Live check, <date>` section to `memory/evsim-recorder.md` with what was seen: the rates, the ack time of a write, the delays the example query found for the 1 A step, any charger errors at the 1 s poll (and, if there were some, that a longer `--charger-period` was needed), and anything not checked (larger jumps are left to the user's own runs).

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/recorder
git add memory/evsim-recorder.md
git commit -m "Record the live check of the recorder

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.**

| Spec requirement | Task |
|---|---|
| `evsim/setpoint` `ts_ms`; `evsim/inverter` `read_ts_ms`, `cap_ts_ms`; existing fields unchanged | 1 |
| `ChargerService` `on_poll`, `write_sent_ns`/`write_acked_ns` | 2 |
| SQLite tables and views, WAL, batches, queue, overflow, retry, session rows, `payload_raw`, retained excluded | 3 |
| Subscribe to `evsim/#`, unique client id, stamping in the callback, connect/disconnect events, publishes nothing | 4 |
| Charger rows (raw per phase), `charger_error`/`charger_ok` events | 5 |
| Step runner: validate first, only register 1000, original limit restored on every exit, time zero in `limit_writes`, baseline, repeat | 5 |
| Command line, confirmation, `--yes`, `--leave`, `--charger-period`, signals, exit codes, database creation | 6 |
| README, `.gitignore`, notes, the two spec additions | 6 |
| Live check with a 1 A step only | 7 |

**Placeholder scan.** No TBD or "similar to" steps; every code step holds the code, and edits to existing files give the exact text to find and replace.

**Type consistency.** `Store.add_charger(t_start_ns, t_end_ns, error, **values)` (Task 3) is what `ChargerLogger` (Task 5) calls with the column names in `CHARGER_VALUES`; `ChargerService`'s `on_poll` dict keys (`t_start_ns`, `t_end_ns`, `values`, `error`, Task 2) are what `ChargerLogger` reads; `set_limit`'s `write_sent_ns`/`write_acked_ns` (Task 2) are what `Experiment._write` stores in `limit_writes` (Task 3's columns `t_write_ns`, `t_ack_ns`); `Experiment`'s restore row (`run=0`, `step=-1`) and the views' column names match the README example; `main` (Task 6) passes `store`, `service`, `stop` as `Experiment` and `Capture` expect.
