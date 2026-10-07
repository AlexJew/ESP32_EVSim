# Full Measurements and Vehicle-State Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show current, voltage and power (A / V / W) for the car charger, the inverter's DC input and the inverter's AC output, in the Measurements list and on a power, current and voltage chart; and let the page command the board's pilot state (A, B, C) from a dropdown over MQTT.

**Architecture:** The setpoint server's `evsim/inverter` message gains `dc_a`, `dc_v`, `ac_a`, `ac_v`. The charger service reads four more AMTRON registers (total power, three phase voltages). `Telemetry` parses the new inverter fields and keeps the board's `evsim/reply` lines; a new `StateCommander` publishes a state command and waits for the matching reply; `History` also reads the charger service, so all three charts draw the server's 10-minute history. The page gets three-part measurement rows, a Voltage chart and a state dropdown.

**Tech Stack:** Python 3 standard library plus `paho-mqtt`, `unittest` with the existing fakes (`FakeInverter`, `FakeAmtron`, `FakeMqttClient`), plain HTML, CSS and browser JavaScript, Node only for the page tests (a stub DOM, `tests/dom_stub.js`).

**Spec:** [docs/superpowers/specs/2026-10-07-evsim-measurements-and-state-control-design.md](../specs/2026-10-07-evsim-measurements-and-state-control-design.md). It amends [2026-10-01-evsim-dashboard-design.md](../specs/2026-10-01-evsim-dashboard-design.md) and [2026-09-30-inverter-writer-design.md](../specs/2026-09-30-inverter-writer-design.md).

Work on branch `feat/new-layout` (the redesigned page is committed there, 01ef95f). **Another session may use this repository**: run `git branch --show-current` before every commit and stop if it is not `feat/new-layout`. All commands run from `server/` unless stated; always run Python as `.venv/bin/python -B`. The task steps use these edit conventions: **Create** and **Overwrite** write a whole file; **In `path`, replace this text: ... with: ...** swaps one exact piece of text (it must occur exactly once).

## Global Constraints

Every task's requirements include these, copied from the spec.

- **Triples:** every measurement is current A / voltage V / power W; for three-phase quantities, current and voltage are the **mean of the three phases**. A value that is not available is a dash.
- **Charger registers (AMTRON, Modbus, read-only, inferred from values):** 220 total active power W; 222, 224, 226 phase voltages V; each 32 bits, high word first; 0xFFFFFFFF means no meter. 212, 214, 216 phase currents in mA (existing). The charger poll stays one connection, read every 2 s; the only Modbus write remains register 1000.
- **`evsim/inverter` message:** gains `dc_a`, `dc_v`, `ac_a`, `ac_v`, each a number rounded to 0.1 or `null`; additive, existing fields and readers unchanged. `ac_a` and `ac_v` are the mean of the three phases (SunSpec model 103); `dc_a` and `dc_v` are MPPT 1's.
- **Sanity bounds in the page's parser:** `dc_a` and `ac_a` 0 to 100 A, `dc_v` and `ac_v` 0 to 1 000 V, `ac_w` and `dc_w` 0 to 30 000 W; outside the bound the value is `None` and the message is kept.
- **State command:** the dropdown offers A, B, C; `POST /api/vehicle/state` with `{"state": "A" | "B" | "C"}` and the same protections as the limit write (loopback, Host and Origin checks, `application/json`, at most 1 KiB, exactly one key). Publish the letter to `evsim/cmd/state`, **QoS 1, not retained**, through the page's MQTT client; wait up to **3 s** for a matching line on `evsim/reply`; accepted (`state X -> <requested>` or `already in <requested>`) is 200, refused (`refused X -> <requested>: <reason>`) is 409 with the reason, no matching reply or the broker not connected is 503, a command already in progress is 409, a bad value is 400. Replies carry no command id, so match by content and ignore the rest. "Validated" means the board accepted; the page does not wait for the charger.
- **History:** frames gain `charger_w`, `charger_a`, `charger_v`, `sig_a`, `dc_a`, `dc_v`, `ac_w`, `ac_a`, `ac_v` (existing `cp_w`, `setpoint_w`, `target_dc_w`, `dc_w` stay); a value is recorded only if its reading is `ok`, else `None`. `charger_a` and `charger_v` are the mean of the phases that read and need an `ok` meter reading. The browser-side current sampling is removed. `History` reads the charger service's cached `status()`, never Modbus.
- **Page:** all text set with `textContent`, no `innerHTML`, no libraries; the chart quantity dropdown is Power [W], Current [A], Voltage [V] with a shared hide/show state per line; the state control is a dropdown (not a stepper) whose value follows the board's presented state (`evsim/cp` `state`) and which is disabled when the broker is down or the state is unknown.
- **Do not break:** the existing tests pass unchanged except where a task says it replaces one; the setpoint server's other behaviour is unchanged.

## Review Focus

Inputs and conditions the spec implies but the obvious tests would miss, most likely to bite first. Each has a test in the task named.

- **A reply that is not for this command:** another letter, a serial command's line, `unknown command 'X'`, non-ASCII bytes, a retained copy, a reply that arrives before the wait starts. Only a matching line decides; the rest are ignored and a missing match is a 503 after 3 s. Tasks 3 and 5.
- **Two state commands at once, and a failure mid-command:** the second gets 409 and nothing extra is published; the lock is released after a refusal, a timeout and a publish failure, so the next command works. Task 5.
- **A POST that must publish nothing:** wrong Host, foreign Origin, wrong type, oversize or invalid JSON, extra keys, a lower-case or unknown letter, the broker down. Task 5.
- **An old setpoint server (no new fields), or an unfitted meter (0xFFFFFFFF):** the page and the history show dashes, nothing raises, no mean is taken of nothing. Tasks 1 to 4.
- **The poll must not undo a command in flight:** while a command is pending, a telemetry poll must not move or re-enable the dropdown; after a refusal it returns to the board's state. Task 6.

---

### Task 1: The setpoint server publishes the inverter's currents and voltages

**Files:**
- Modify: `server/setpoint_server/fronius.py`
- Modify: `server/setpoint_server/inverter_writer.py`
- Modify: `server/tests/test_fronius.py`, `server/tests/test_inverter_writer.py`
- Modify: `docs/superpowers/specs/2026-09-30-inverter-writer-design.md` (the message table)

**Interfaces:**
- Produces: `Inverter.power()` returns, besides `ac_w`, `state` and `mppt`, `ac_a` and `ac_v` (floats, or `None`); the status message has `dc_w, dc_a, dc_v, ac_w, ac_a, ac_v` in that order after `cap_ac_w`.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_fronius.py`, replace this text:

```python
    def test_int16_not_implemented_is_none(self):
        self.fake.put(M103 + 12, [0x8000])
        self.assertIsNone(self.inv.power()["ac_w"])
```

with:

```python
    def test_int16_not_implemented_is_none(self):
        self.fake.put(M103 + 12, [0x8000])
        self.assertIsNone(self.inv.power()["ac_w"])

    def test_power_reads_the_mean_ac_current_and_voltage_of_the_phases(self):
        self.fake.put(M103 + 1, [5976, 5976, 5977, 65533])        # AphA, AphB, AphC, A_SF = -3
        self.fake.put(M103 + 8, [2329, 2331, 2338, 65535])        # PhVphA, PhVphB, PhVphC, V_SF = -1
        p = self.inv.power()
        self.assertAlmostEqual(p["ac_a"], 5.97633, places=4)
        self.assertAlmostEqual(p["ac_v"], 233.2667, places=3)

    def test_a_phase_that_is_not_implemented_is_left_out_of_the_mean(self):
        self.fake.put(M103 + 1, [5000, 65535, 7000, 65533])
        self.assertAlmostEqual(self.inv.power()["ac_a"], 6.0)

    def test_no_phase_or_no_scale_factor_gives_none(self):
        self.fake.put(M103 + 1, [65535, 65535, 65535, 65533])
        self.assertIsNone(self.inv.power()["ac_a"])
        self.fake.put(M103 + 8, [2329, 2331, 2338, 0x8000])
        self.assertIsNone(self.inv.power()["ac_v"])
```

In `server/tests/test_inverter_writer.py`, replace this text:

```python
    def test_a_target_is_written_committed_and_reported(self):
        self.writer.set_target(4000.0)
```

with:

```python
    def test_a_target_is_written_committed_and_reported(self):
        self.fake.put(M103 + 1, [5976, 5976, 5977, 65533])          # phase currents, SF -3
        self.fake.put(M103 + 8, [2329, 2331, 2338, 65535])          # phase voltages, SF -1
        self.writer.set_target(4000.0)
```

Then, in the same file, replace this text:

```python
        self.assertEqual((s["dc_w"], s["ac_w"], s["inverter_state"], s["error"]), (7272, 7047, 4, None))
        self.assertEqual(list(s), ["mode", "target_dc_w", "cap_ac_w", "dc_w", "ac_w", "inverter_state", "error", "ts"])
```

with:

```python
        self.assertEqual((s["dc_w"], s["ac_w"], s["inverter_state"], s["error"]), (7272, 7047, 4, None))
        self.assertEqual((s["dc_a"], s["dc_v"], s["ac_a"], s["ac_v"]), (14.1, 515.0, 6.0, 233.3))
        self.assertEqual(list(s), ["mode", "target_dc_w", "cap_ac_w", "dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v",
                                   "inverter_state", "error", "ts"])
```

And replace this text:

```python
    def test_a_failed_commit_is_re_asserted_after_reconnecting(self):
```

with:

```python
    def test_an_unreachable_inverter_reports_null_measurements(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.fake.stop()
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        s = self.last()
        self.assertEqual(s["mode"], "unreachable")
        self.assertEqual([s[k] for k in ("dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v")], [None] * 6)

    def test_a_failed_commit_is_re_asserted_after_reconnecting(self):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_fronius.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_inverter_writer.py'`
Expected: FAIL: `KeyError: 'ac_a'` in the fronius tests; the writer tests fail on the missing `dc_a` and the field order.

- [ ] **Step 3: Write the implementation**

In `server/setpoint_server/fronius.py`, replace this text:

```python
M103_W, M103_W_SF, M103_ST = 12, 13, 36                    # inverter (integer): AC power, its SF, state
```

with:

```python
M103_W, M103_W_SF, M103_ST = 12, 13, 36                    # inverter (integer): AC power, its SF, state
M103_AP, M103_A_SF, M103_PHV, M103_V_SF = 1, 4, 8, 11      # phase currents, their SF, phase voltages, their SF
```

In the same file, replace this text:

```python
def pct_register(watts, wmax_w, sf):
```

with:

```python
def mean_of_phases(d, first, sf_index):
    """The mean of three consecutive uint16 phase registers with the scale factor at `sf_index`.

    A phase that reads 0xFFFF (not implemented) is left out; None if none is implemented or the scale
    factor is not implemented (0x8000).
    """
    sf = d[sf_index]
    if sf == 0x8000:
        return None
    values = [scaled(raw, sf) for raw in d[first:first + 3] if raw != 0xFFFF]
    return sum(values) / len(values) if values else None


def pct_register(watts, wmax_w, sf):
```

In the same file, replace this text:

```python
        return {"ac_w": ac, "state": d[M103_ST], "mppt": mppt}
```

with:

```python
        return {"ac_w": ac, "ac_a": mean_of_phases(d, M103_AP, M103_A_SF), "ac_v": mean_of_phases(d, M103_PHV, M103_V_SF),
                "state": d[M103_ST], "mppt": mppt}
```

In `server/setpoint_server/inverter_writer.py`, replace this text:

```python
            "dc_w": _round(mppt["dc_w"]) if mppt else None,
            "ac_w": _round(power["ac_w"]) if power else None,
```

with:

```python
            "dc_w": _round(mppt["dc_w"]) if mppt else None,
            "dc_a": _round(mppt["dc_a"]) if mppt else None,
            "dc_v": _round(mppt["dc_v"]) if mppt else None,
            "ac_w": _round(power["ac_w"]) if power else None,
            "ac_a": _round(power["ac_a"]) if power else None,
            "ac_v": _round(power["ac_v"]) if power else None,
```

Update the message table in `docs/superpowers/specs/2026-09-30-inverter-writer-design.md`. Replace this text:

```
{"mode":"capped","target_dc_w":7000,"cap_ac_w":6791,"dc_w":7012,"ac_w":6788,"inverter_state":4,"error":null,"ts":"2026-09-30T13:42:07Z"}
```

with:

```
{"mode":"capped","target_dc_w":7000,"cap_ac_w":6791,"dc_w":7012,"dc_a":13.6,"dc_v":515.0,"ac_w":6788,"ac_a":9.9,"ac_v":233.3,"inverter_state":4,"error":null,"ts":"2026-09-30T13:42:07Z"}
```

and replace this text:

```
| `dc_w`, `ac_w` | The inverter's measured MPPT 1 DC power and AC power, or `null` if unreadable |
```

with:

```
| `dc_w`, `ac_w` | The inverter's measured MPPT 1 DC power and AC power, or `null` if unreadable |
| `dc_a`, `dc_v` | MPPT 1's DC current and voltage, or `null` (added 2026-10-07) |
| `ac_a`, `ac_v` | The mean of the three AC phase currents and of the three phase-to-neutral voltages, or `null` (added 2026-10-07) |
```

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_fronius*.py'` then `.venv/bin/python -B -m unittest discover -s tests -p 'test_inverter_*.py'`
Expected: both OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/new-layout
git add server/setpoint_server/fronius.py server/setpoint_server/inverter_writer.py \
  server/tests/test_fronius.py server/tests/test_inverter_writer.py \
  docs/superpowers/specs/2026-09-30-inverter-writer-design.md
git commit -m "Publish the inverter's currents and voltages on evsim/inverter

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The charger service reads power and phase voltages

**Files:**
- Overwrite: `server/charger_web/amtron_registers.json`
- Modify: `server/charger_web/registers.py`, `server/charger_web/service.py`
- Modify: `server/tests/fake_amtron.py`, `server/tests/test_charger_service.py`

**Interfaces:**
- Produces: `ChargerService.status()` gains `meter_power_w` (a reading of watts) and `meter_voltage_v` (a reading whose value is `{"L1": v, "L2": v, "L3": v}`, like `meter_current_a`); both `unavailable` with no meter.

- [ ] **Step 1: Write the failing tests**

In `server/tests/fake_amtron.py`, replace this text:

```python
    216: 0, 217: 808,
}
```

with:

```python
    216: 0, 217: 808,
    220: 0, 221: 600,         # total active power, 600 W
    222: 0, 223: 231,         # phase voltages L1, L2, L3, V
    224: 0, 225: 232,
    226: 0, 227: 233,
}
```

In `server/tests/test_charger_service.py`, replace this text:

```python
            "meter_current_l1_a": 212, "meter_current_l2_a": 214, "meter_current_l3_a": 216,
        })
```

with:

```python
            "meter_current_l1_a": 212, "meter_current_l2_a": 214, "meter_current_l3_a": 216,
            "meter_power_w": 220, "meter_voltage_l1_v": 222, "meter_voltage_l2_v": 224, "meter_voltage_l3_v": 226,
        })
```

and replace this text:

```python
class ReadTest(ServiceTest):
```

with:

```python
class MeterTest(ServiceTest):
    def test_the_status_carries_the_power_and_the_phase_voltages(self):
        self.service.poll_once()
        s = self.service.status()
        self.assertEqual((s["meter_power_w"]["value"], s["meter_power_w"]["status"]), (600, "ok"))
        self.assertEqual((s["meter_voltage_v"]["value"], s["meter_voltage_v"]["status"]),
                         ({"L1": 231, "L2": 232, "L3": 233}, "ok"))

    def test_no_meter_is_unavailable_not_a_huge_number(self):
        for address in (220, 222, 224, 226):
            self.fake.set_u32(address, 0xFFFFFFFF)
        self.service.poll_once()
        s = self.service.status()
        self.assertEqual((s["meter_power_w"]["value"], s["meter_power_w"]["status"]), (None, "unavailable"))
        self.assertEqual(s["meter_voltage_v"]["status"], "unavailable")

    def test_a_refused_voltage_register_leaves_the_others_reading(self):
        self.fake.refuse_reads = {224}
        self.service.poll_once()
        s = self.service.status()
        self.assertEqual(s["meter_voltage_v"]["value"], {"L1": 231, "L3": 233})
        self.assertEqual(s["meter_power_w"]["value"], 600)

    def test_the_new_registers_are_only_read(self):
        self.service.poll_once()
        self.assertEqual({entry[0] for entry in self.fake.log}, {3})
        self.assertIn((3, 220, 2), self.fake.log)
        self.assertEqual(self.fake.writes, [])


class ReadTest(ServiceTest):
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_service.py'`
Expected: FAIL: the register-map test (missing addresses) and `KeyError: 'meter_power_w'` in the new tests.

- [ ] **Step 3: Write the implementation**

Overwrite `server/charger_web/amtron_registers.json`:

```json
{
  "_note": "AMTRON Charge Control (CC612), MENNEKES ECU Modbus TCP spec rev 1.07, confirmed on firmware 5.33 (2026-09-30). Every register is a holding register (function code 3). size 2 means two registers, high word first. Register 134 is read only; the writable limit is 1000. Registers 220 (total active power, W) and 222, 224, 226 (phase voltages, V) were found on 2026-10-07 by reading the meter block: their meaning is inferred from the values (the phase powers at 206, 208, 210 sum to the total; current times voltage matches the phase power).",
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
    "meter_current_l3_a": {"address": 216, "size": 2, "scale": 0.001},
    "meter_power_w": {"address": 220, "size": 2},
    "meter_voltage_l1_v": {"address": 222, "size": 2},
    "meter_voltage_l2_v": {"address": 224, "size": 2},
    "meter_voltage_l3_v": {"address": 226, "size": 2}
  }
}
```

In `server/charger_web/registers.py`, replace this text:

```python
    "vehicle_state", "charger_status", "meter_current_l1_a", "meter_current_l2_a", "meter_current_l3_a",
)
```

with:

```python
    "vehicle_state", "charger_status", "meter_current_l1_a", "meter_current_l2_a", "meter_current_l3_a",
    "meter_power_w", "meter_voltage_l1_v", "meter_voltage_l2_v", "meter_voltage_l3_v",
)
```

In `server/charger_web/service.py`, replace this text:

```python
    "charger_status", "meter_current_l1_a", "meter_current_l2_a", "meter_current_l3_a",
)
PHASES = (("L1", "meter_current_l1_a"), ("L2", "meter_current_l2_a"), ("L3", "meter_current_l3_a"))
```

with:

```python
    "charger_status", "meter_current_l1_a", "meter_current_l2_a", "meter_current_l3_a",
    "meter_power_w", "meter_voltage_l1_v", "meter_voltage_l2_v", "meter_voltage_l3_v",
)
PHASES = (("L1", "meter_current_l1_a"), ("L2", "meter_current_l2_a"), ("L3", "meter_current_l3_a"))
VOLT_PHASES = (("L1", "meter_voltage_l1_v"), ("L2", "meter_voltage_l2_v"), ("L3", "meter_voltage_l3_v"))
```

In the same file, replace this text:

```python
            meter_at = min((at[name] for phase, name in PHASES if values[name] is not None), default=None)
```

with:

```python
            meter_at = min((at[name] for phase, name in PHASES if values[name] is not None), default=None)
            volts = {phase: values[name] for phase, name in VOLT_PHASES if values[name] is not None}
            volts_at = min((at[name] for phase, name in VOLT_PHASES if values[name] is not None), default=None)
```

and replace this text:

```python
                "meter_current_a": self._reading(phases or None, meter_at, link, now),
```

with:

```python
                "meter_current_a": self._reading(phases or None, meter_at, link, now),
                "meter_voltage_v": self._reading(volts or None, volts_at, link, now),
                "meter_power_w": reading("meter_power_w"),
```

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_*.py'`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/new-layout
git add server/charger_web/amtron_registers.json server/charger_web/registers.py server/charger_web/service.py \
  server/tests/fake_amtron.py server/tests/test_charger_service.py
git commit -m "Read the charger's total power and phase voltages

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The parser and the subscriber learn the new inverter fields and the board's replies

**Files:**
- Modify: `server/charger_web/config.py`
- Overwrite: `server/charger_web/parsers.py`, `server/charger_web/telemetry.py`
- Overwrite: `server/tests/fake_mqtt.py`
- Modify: `server/tests/test_dashboard_parsers.py`, `server/tests/test_dashboard_telemetry.py`

**Interfaces:**
- Consumes: `config.MAX_DC_W`.
- Produces:
  - `config.TOPIC_REPLY = "evsim/reply"`, `config.TOPIC_CMD_STATE = "evsim/cmd/state"`, `config.STATE_REPLY_TIMEOUT_S = 3`, `config.MAX_A = 100`, `config.MAX_V = 1000`.
  - `parse_inverter(payload)` returns `{"target_dc_w", "dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v", "mode", "inverter_state"}`; `parse_reply(payload) -> {"text": str} | None` (printable ASCII, 1 to 120 characters).
  - `Telemetry.snapshot()["inverter"]` is `{"dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v"}`, each a reading; `Telemetry.reply_marker() -> int`; `Telemetry.wait_reply(since, accept, timeout_s) -> str | None` (the first reply text after marker `since` for which `accept(text)` is true; retained replies are never recorded).
  - `FakeMqttClient.publish(topic, payload, qos=0, retain=False)` records `(topic, payload, qos, retain)` in `published`, calls `publish_hook(topic, payload)` if set, and returns an object with `rc` (`publish_rc`, default 0).

- [ ] **Step 1: Write the failing tests**

Overwrite `server/tests/fake_mqtt.py`:

```python
"""Stands in for paho's client: records what the code under test asks of it and lets a test drive the callbacks."""

from types import SimpleNamespace

OK = SimpleNamespace(is_failure=False)
REFUSED = SimpleNamespace(is_failure=True)


class FakeMqttClient:
    def __init__(self):
        self.subscribed = []           # (topic, qos), in order
        self.published = []            # (topic, payload, qos, retain), in order
        self.publish_rc = 0            # what publish() reports; 4 is paho's "not connected"
        self.publish_hook = None       # called with (topic, payload) while publishing: a test's way to answer at once
        self.calls = []                # ("connect_async", host, port), ("loop_start",), ("disconnect",), ("loop_stop",)
        self.on_connect = self.on_message = self.on_disconnect = None

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        if self.publish_hook is not None:
            self.publish_hook(topic, payload)
        return SimpleNamespace(rc=self.publish_rc)

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

In `server/tests/test_dashboard_parsers.py`, replace this text:

```python
from charger_web.parsers import parse_cp, parse_inverter, parse_setpoint
```

with:

```python
from charger_web.parsers import parse_cp, parse_inverter, parse_reply, parse_setpoint
```

Then replace the whole class `InverterTest` in that file (from `class InverterTest(unittest.TestCase):` up to, but not including, `if __name__ == "__main__":`) with:

```python
class InverterTest(unittest.TestCase):
    FULL = {"target_dc_w": 4312.6, "dc_w": 4304.6, "dc_a": 8.2, "dc_v": 524.7, "ac_w": 4181.0, "ac_a": 6.0,
            "ac_v": 233.3, "mode": "capped", "inverter_state": 4}

    def test_the_servers_status(self):
        payload = (b'{"mode":"capped","target_dc_w":4312.6,"cap_ac_w":4182.7,"dc_w":4304.6,"dc_a":8.2,"dc_v":524.7,'
                   b'"ac_w":4181,"ac_a":6.0,"ac_v":233.3,"inverter_state":4,"error":null,"ts":"2026-10-01T15:13:04Z"}')
        self.assertEqual(parse_inverter(payload), self.FULL)

    def test_an_old_servers_message_without_the_new_fields_still_parses(self):
        payload = b'{"mode":"capped","target_dc_w":4312.6,"dc_w":4304.6,"ac_w":4181,"inverter_state":4}'
        parsed = parse_inverter(payload)
        self.assertEqual((parsed["dc_w"], parsed["ac_w"]), (4304.6, 4181.0))
        self.assertEqual([parsed[k] for k in ("dc_a", "dc_v", "ac_a", "ac_v")], [None] * 4)

    def test_null_values_are_kept_as_none(self):
        parsed = parse_inverter(b'{"mode":"unreachable","target_dc_w":null,"dc_w":null,"dc_a":null,"dc_v":null,'
                                b'"ac_w":null,"ac_a":null,"ac_v":null,"inverter_state":null}')
        self.assertEqual(parsed, {"target_dc_w": None, "dc_w": None, "dc_a": None, "dc_v": None, "ac_w": None,
                                  "ac_a": None, "ac_v": None, "mode": "unreachable", "inverter_state": None})

    def test_missing_values_are_none_too(self):
        parsed = parse_inverter(b'{"mode":"zero"}')
        self.assertEqual((parsed["target_dc_w"], parsed["dc_w"], parsed["ac_a"], parsed["mode"]), (None, None, None, "zero"))

    def test_a_target_that_is_not_a_good_power_drops_the_message(self):
        for bad in (b'"x"', b"true", b"-5", b"NaN", b"12001", b"[]"):
            self.assertIsNone(parse_inverter(b'{"target_dc_w": ' + bad + b"}"), bad)

    def test_a_measured_power_can_exceed_the_setpoint_cap(self):
        self.assertEqual(parse_inverter(b'{"dc_w": 13000}')["dc_w"], 13000.0)
        self.assertEqual(parse_inverter(b'{"ac_w": 30000}')["ac_w"], 30000.0)
        self.assertEqual(parse_inverter(b'{"dc_w": 0}')["dc_w"], 0.0)

    def test_each_measurement_has_its_own_bound(self):
        for field, high in (("dc_w", 30000), ("ac_w", 30000), ("dc_a", 100), ("ac_a", 100), ("dc_v", 1000), ("ac_v", 1000)):
            ok = parse_inverter(('{"%s": %s}' % (field, high)).encode())
            too_high = parse_inverter(('{"%s": %s}' % (field, high + 1)).encode())
            self.assertEqual(ok[field], float(high), field)
            self.assertIsNone(too_high[field], field)

    def test_a_bad_measurement_becomes_none_and_keeps_the_target_and_the_others(self):
        for field in ("dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v"):
            for bad in (b'"x"', b"true", b"-1", b"NaN", b"Infinity", b"[]", b"1" + b"0" * 400):
                parsed = parse_inverter(b'{"target_dc_w": 4300, "dc_w": 5000, "' + field.encode() + b'": ' + bad + b"}")
                self.assertEqual(parsed["target_dc_w"], 4300.0, (field, bad))
                self.assertIsNone(parsed[field], (field, bad))
                if field != "dc_w":
                    self.assertEqual(parsed["dc_w"], 5000.0, (field, bad))

    def test_a_bad_inverter_state_is_none_but_the_message_stays(self):
        for bad in (b'"4"', b"true", b"-1", b"256", b"4.5", b"null"):
            parsed = parse_inverter(b'{"target_dc_w": 100, "inverter_state": ' + bad + b"}")
            self.assertEqual(parsed["inverter_state"], None, bad)

    def test_non_objects_are_dropped(self):
        for payload in NOT_OBJECTS:
            self.assertIsNone(parse_inverter(payload), payload)


class ReplyTest(unittest.TestCase):
    def test_the_boards_lines(self):
        for line in ("state A -> B", "already in B", "refused A -> C: state B must be asserted first", "unknown command 'X'"):
            self.assertEqual(parse_reply(line.encode()), {"text": line}, line)

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(parse_reply(b"  state A -> B \n"), {"text": "state A -> B"})

    def test_anything_else_is_dropped(self):
        for payload in (None, b"", b"   ", b"\xff\xfe", b"state\x00A", b"bell\x07", "é".encode(), b"x" * 121, b"line\nbreak"):
            self.assertIsNone(parse_reply(payload), payload)
```

In `server/tests/test_dashboard_telemetry.py`, replace this text:

```python
        expected = [("evsim/cp", 0), ("evsim/setpoint", 0), ("evsim/inverter", 0)]
```

with:

```python
        expected = [("evsim/cp", 0), ("evsim/setpoint", 0), ("evsim/inverter", 0), ("evsim/reply", 0)]
```

and replace this text:

```python
    def test_raw_samples_are_drained_once_and_never_include_retained_ones(self):
```

with:

```python
    def test_the_inverter_card_carries_current_voltage_and_power_from_one_message(self):
        self.client.deliver("evsim/inverter", '{"mode":"capped","target_dc_w":4300,"dc_w":4304.6,"dc_a":8.2,"dc_v":524.7,'
                                              '"ac_w":4181,"ac_a":6.0,"ac_v":233.3,"inverter_state":4}')
        inverter = self.snapshot()["inverter"]
        self.assertEqual({k: (r["value"], r["status"]) for k, r in inverter.items()}, {
            "dc_w": (4304.6, "ok"), "dc_a": (8.2, "ok"), "dc_v": (524.7, "ok"),
            "ac_w": (4181.0, "ok"), "ac_a": (6.0, "ok"), "ac_v": (233.3, "ok")})
        self.clock.t += config.MQTT_STALE_AFTER_S
        self.assertEqual({r["status"] for r in self.snapshot()["inverter"].values()}, {"stale"})

    def test_an_old_servers_message_leaves_the_new_values_unavailable(self):
        self.client.deliver("evsim/inverter", '{"mode":"capped","target_dc_w":4300,"dc_w":4304.6,"inverter_state":4}')
        inverter = self.snapshot()["inverter"]
        self.assertEqual(inverter["dc_w"]["status"], "ok")
        for name in ("dc_a", "dc_v", "ac_w", "ac_a", "ac_v"):
            self.assertEqual(inverter[name]["status"], "unavailable", name)

    def test_replies_are_waited_for_by_content_and_after_the_marker(self):
        self.client.deliver("evsim/reply", "state A -> B")                    # before the marker: never matches
        marker = self.telemetry.reply_marker()
        self.client.deliver("evsim/reply", "unknown command 'X'")
        self.client.deliver("evsim/reply", "state B -> C")
        self.assertEqual(self.telemetry.wait_reply(marker, lambda text: text.endswith("-> C"), 0.2), "state B -> C")
        self.assertIsNone(self.telemetry.wait_reply(marker, lambda text: text == "state A -> B", 0.05))

    def test_a_reply_that_arrives_while_waiting_wakes_the_waiter(self):
        marker = self.telemetry.reply_marker()
        threading.Timer(0.05, self.client.deliver, ("evsim/reply", "already in B")).start()
        self.assertEqual(self.telemetry.wait_reply(marker, lambda text: "B" in text, 2), "already in B")

    def test_retained_unusable_and_non_ascii_replies_are_never_recorded(self):
        marker = self.telemetry.reply_marker()
        self.client.deliver("evsim/reply", "state A -> B", retain=True)
        self.client.deliver("evsim/reply", b"\xff\xfe")
        self.client.deliver("evsim/reply", "")
        self.assertIsNone(self.telemetry.wait_reply(marker, lambda text: True, 0.05))
        self.assertEqual(self.telemetry.reply_marker(), marker)

    def test_replies_never_touch_the_cards_or_the_raw_samples(self):
        self.client.deliver("evsim/reply", "state A -> B")
        snap = self.snapshot()
        self.assertEqual(snap["board"]["setpoint_w"]["status"], "unavailable")
        self.assertEqual(self.telemetry.drain_cp_samples(), [])

    def test_raw_samples_are_drained_once_and_never_include_retained_ones(self):
```

and replace this text:

```python
import importlib.util
import json
import unittest
```

with:

```python
import importlib.util
import json
import threading
import unittest
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_parsers.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_telemetry.py'`
Expected: FAIL: `ImportError: cannot import name 'parse_reply'`; the telemetry tests fail on the missing `reply_marker` and the subscription list.

- [ ] **Step 3: Write the implementation**

In `server/charger_web/config.py`, replace this text:

```python
MAX_DC_W = 30000                  # a measured DC power above this is not believed (the inverter is a 12 kW unit)
```

with:

```python
MAX_DC_W = 30000                  # a measured DC power above this is not believed (the inverter is a 12 kW unit)
MAX_A = 100                       # likewise for a measured current ...
MAX_V = 1000                      # ... and a measured voltage (the DC side runs near 520 V)
TOPIC_CMD_STATE = "evsim/cmd/state"   # the page publishes A, B or C here (QoS 1, not retained)
TOPIC_REPLY = "evsim/reply"           # the board's one-line answer to every command
STATE_REPLY_TIMEOUT_S = 3             # how long a state command waits for the board's answer
```

Overwrite `server/charger_web/parsers.py`:

```python
"""Parsers for the MQTT payloads the dashboard reads.

Pure functions that never raise: anything unusable gives None. A setpoint outside 0 to MAX_SETPOINT_W is
dropped, never clamped (the setpoint server does the same); a measurement from the inverter is bounded more
loosely, and a bad one becomes None without dropping its message. Text fields are short strings or None; the
page shows them with textContent, so their content cannot inject markup.
"""

import json
import math

from . import config

MAX_TEXT = 32
MAX_REPLY = 120


def _object(payload):
    try:
        data = json.loads(payload)
    except (ValueError, TypeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _bounded(value, high):
    """A finite number from 0 to `high` as a float, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or not 0 <= value <= high:
            return None
        return float(value)
    except OverflowError:           # an integer too large to convert to a float
        return None


def _watts(value):
    return _bounded(value, config.MAX_SETPOINT_W)


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
    """The setpoint server's evsim/inverter status: its DC target, the inverter's measured DC and AC values, the mode."""
    data = _object(payload)
    if data is None:
        return None
    target = data.get("target_dc_w")
    if target is not None:
        target = _watts(target)
        if target is None:
            return None
    return {
        "target_dc_w": target,
        "dc_w": _bounded(data.get("dc_w"), config.MAX_DC_W),
        "dc_a": _bounded(data.get("dc_a"), config.MAX_A),
        "dc_v": _bounded(data.get("dc_v"), config.MAX_V),
        "ac_w": _bounded(data.get("ac_w"), config.MAX_DC_W),
        "ac_a": _bounded(data.get("ac_a"), config.MAX_A),
        "ac_v": _bounded(data.get("ac_v"), config.MAX_V),
        "mode": _text(data.get("mode")),
        "inverter_state": _state(data.get("inverter_state")),
    }


def parse_reply(payload):
    """One line from the board's evsim/reply: printable ASCII, trimmed, 1 to 120 characters."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not 0 < len(text) <= MAX_REPLY or not text.isprintable():
        return None
    return {"text": text}
```

Overwrite `server/charger_web/telemetry.py`:

```python
"""The MQTT side of the dashboard: the latest parsed value of each topic, with its receive time.

The client is handed in, so the tests use a fake; `build_client` makes the real paho one. Subscribing happens
in on_connect, so it is renewed on every reconnect. This module sets no will and never publishes: the state
command (commands.py) publishes through the same client and waits here for the board's reply.
paho's network thread calls the on_* callbacks; one lock guards the state they and the readers share.
"""

import logging
import threading
import time
from collections import deque

from . import config
from .parsers import parse_cp, parse_inverter, parse_reply, parse_setpoint
from .reading import reading

log = logging.getLogger("charger_web.telemetry")

INVERTER_FIELDS = ("dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v")


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
            config.TOPIC_REPLY: ("reply", parse_reply),
        }
        self._lock = threading.Lock()
        self._reply_arrived = threading.Condition(self._lock)
        self._connected = False
        self._latest = {}               # kind -> {"data": parsed dict, "at": receive time, "retained": bool (shown stale)}
        self._cp_samples = []           # raw evsim/cp setpoint_w values since the last drain
        self._replies = deque(maxlen=16)    # (sequence number, text) of the board's recent replies
        self._reply_seq = 0
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
        if kind == "reply":
            if retained:                # a reply is an event, never a state: a retained copy is old news
                return
            with self._reply_arrived:
                self._reply_seq += 1
                self._replies.append((self._reply_seq, data["text"]))
                self._reply_arrived.notify_all()
            return
        # The setpoint server's last word is "I am gone". A connected subscriber gets it live, not retained, but
        # it is the last known state all the same: whatever setpoint it carries, it is no longer a fresh value.
        gone = kind == "setpoint" and data.get("status") == "server_offline"
        now = self._clock()
        with self._lock:
            current = self._latest.get(kind)
            if retained and current is not None and not current["retained"] \
                    and now - current["at"] < config.MQTT_STALE_AFTER_S:
                return                  # a retained copy never replaces a fresh live value
            self._latest[kind] = {"data": data, "at": now, "retained": retained or gone}
            if kind == "cp" and not retained:
                self._cp_samples.append(data["setpoint_w"])

    # -- what the page, the history and the state command read ---------------------------------------

    def drain_cp_samples(self):
        """The raw evsim/cp setpoint_w values received since the last call, oldest first."""
        with self._lock:
            samples, self._cp_samples = self._cp_samples, []
        return samples

    def reply_marker(self):
        """A marker for wait_reply: replies after it are the ones a command published now can be answered by."""
        with self._lock:
            return self._reply_seq

    def wait_reply(self, since, accept, timeout_s):
        """The first board reply after marker `since` for which accept(text) is true, or None after timeout_s."""
        end = time.monotonic() + timeout_s
        with self._reply_arrived:
            while True:
                for seq, text in self._replies:
                    if seq > since and accept(text):
                        return text
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self._reply_arrived.wait(remaining)

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
            "inverter": {name: value("server", name) for name in INVERTER_FIELDS},   # one message: they age together
        }
```

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_*.py'`
Expected: OK. (The older `test_dashboard_web.py` and `test_dashboard_history.py` still pass: their stubs and fakes do not depend on the new keys yet.)

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/new-layout
git add server/charger_web/config.py server/charger_web/parsers.py server/charger_web/telemetry.py \
  server/tests/fake_mqtt.py server/tests/test_dashboard_parsers.py server/tests/test_dashboard_telemetry.py
git commit -m "Parse the inverter's currents and voltages and keep the board's replies

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The history carries every measurement

**Files:**
- Overwrite: `server/charger_web/history.py`, `server/tests/test_dashboard_history.py`
- Modify: `server/charger_web/dashboard.py`, `server/charger_web/__main__.py`

**Interfaces:**
- Consumes: `Telemetry.snapshot()` (Task 3: `["inverter"]` has the six readings), `ChargerService.status()` (Task 2: `meter_power_w`, `meter_current_a`, `meter_voltage_v`, `signalled_current_a`).
- Produces: `History(telemetry, charger=None, clock=..., wall=..., period_s=None)`; every frame has `t, cp_w, setpoint_w, target_dc_w, dc_w, dc_a, dc_v, ac_w, ac_a, ac_v, charger_w, charger_a, charger_v, sig_a` (the charger keys are `None` when `charger` is `None`); `build_dashboard(charger=None)` passes the charger to `History`; `__main__` calls `build_dashboard(service)`.

- [ ] **Step 1: Write the failing tests**

Overwrite `server/tests/test_dashboard_history.py`:

```python
import itertools
import time
import unittest

from charger_web.history import History

FRAME_KEYS = ["t", "cp_w", "setpoint_w", "target_dc_w", "dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v",
              "charger_w", "charger_a", "charger_v", "sig_a"]


def rd(value, status="ok"):
    return {"value": value, "status": status, "updated_at": None}


class StubTelemetry:
    def __init__(self):
        self.samples = []
        self.setpoint = rd(4312.0)
        self.target = rd(4312.6)
        self.inverter = {"dc_w": rd(4304.6), "dc_a": rd(8.2), "dc_v": rd(524.7),
                         "ac_w": rd(4181.0), "ac_a": rd(6.0), "ac_v": rd(233.3)}

    def drain_cp_samples(self):
        samples, self.samples = self.samples, []
        return samples

    def snapshot(self):
        return {"setpoint": {"setpoint_w": self.setpoint}, "server": {"target_dc_w": self.target},
                "inverter": self.inverter}


class StubCharger:
    def __init__(self):
        self.calls = 0
        self.power = rd(8245)
        self.current = rd({"L1": 11.785, "L2": 11.593, "L3": 12.078})
        self.voltage = rd({"L1": 232, "L2": 232, "L3": 233})
        self.signalled = rd(12)

    def status(self):
        self.calls += 1
        return {"meter_power_w": self.power, "meter_current_a": self.current, "meter_voltage_v": self.voltage,
                "signalled_current_a": self.signalled}


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.telemetry = StubTelemetry()
        self.charger = StubCharger()
        self.wall = itertools.count(1_700_000_000)
        self.history = History(self.telemetry, self.charger, wall=lambda: next(self.wall))

    def frames(self):
        return self.history.snapshot()["frames"]

    def test_a_frame_has_the_time_and_every_value(self):
        self.telemetry.samples = [4300.0, 4400.0]
        self.history.tick()
        self.assertEqual(self.frames(), [{
            "t": 1_700_000_000, "cp_w": 4350.0, "setpoint_w": 4312.0, "target_dc_w": 4312.6,
            "dc_w": 4304.6, "dc_a": 8.2, "dc_v": 524.7, "ac_w": 4181.0, "ac_a": 6.0, "ac_v": 233.3,
            "charger_w": 8245.0, "charger_a": 11.8, "charger_v": 232.3, "sig_a": 12.0,
        }])
        self.assertEqual(list(self.frames()[0]), FRAME_KEYS)

    def test_the_charger_values_are_the_mean_of_the_phases(self):
        self.charger.current = rd({"L1": 10.0, "L2": 11.0, "L3": 15.0})
        self.charger.voltage = rd({"L1": 230, "L2": 231, "L3": 232})
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["charger_a"], frame["charger_v"]), (12.0, 231.0))

    def test_a_missing_phase_is_left_out_of_the_mean(self):
        self.charger.current = rd({"L1": 10.0, "L3": 14.0})
        self.history.tick()
        self.assertEqual(self.frames()[0]["charger_a"], 12.0)

    def test_no_meter_is_a_gap_not_a_zero(self):
        self.charger.power = rd(None, "unavailable")
        self.charger.current = rd(None, "unavailable")
        self.charger.voltage = rd(None, "unavailable")
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["charger_w"], frame["charger_a"], frame["charger_v"]), (None, None, None))
        self.assertEqual(frame["sig_a"], 12.0)                       # the other charger values still record

    def test_a_stale_charger_reading_is_a_gap(self):
        self.charger.power = rd(8245, "stale")
        self.charger.current = rd({"L1": 11.0, "L2": 11.0, "L3": 11.0}, "stale")
        self.charger.signalled = rd(12, "stale")
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["charger_w"], frame["charger_a"], frame["sig_a"]), (None, None, None))

    def test_without_a_charger_the_charger_keys_are_gaps(self):
        history = History(self.telemetry, wall=lambda: next(self.wall))
        history.tick()
        frame = history.snapshot()["frames"][0]
        self.assertEqual([frame[k] for k in ("charger_w", "charger_a", "charger_v", "sig_a")], [None] * 4)
        self.assertEqual(frame["dc_a"], 8.2)

    def test_the_inverter_values_age_together_and_each_can_be_a_gap(self):
        self.telemetry.inverter = {**self.telemetry.inverter, "dc_a": rd(None, "unavailable"), "ac_v": rd(233.3, "stale")}
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["dc_a"], frame["ac_v"], frame["dc_w"], frame["ac_w"]), (None, None, 4304.6, 4181.0))

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
        self.telemetry.inverter = {**self.telemetry.inverter, "dc_w": rd(4304.6, "stale")}
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["setpoint_w"], frame["target_dc_w"], frame["dc_w"]), (None, None, None))

    def test_values_are_rounded_to_a_tenth(self):
        self.telemetry.inverter = {**self.telemetry.inverter, "dc_w": rd(4304.5678)}
        self.history.tick()
        self.assertEqual(self.frames()[0]["dc_w"], 4304.6)

    def test_the_charger_is_read_from_its_cache_once_per_tick(self):
        self.history.tick()
        self.history.tick()
        self.assertEqual(self.charger.calls, 2)

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
        history = History(self.telemetry, self.charger, period_s=0.02)
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
Expected: FAIL: `TypeError` (History takes no charger) and frames without the new keys.

- [ ] **Step 3: Write the implementation**

Overwrite `server/charger_web/history.py`:

```python
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
```

In `server/charger_web/dashboard.py`, replace this text:

```python
def build_dashboard():
    """The real dashboard from the environment. Raises ValueError for a bad setting; starts nothing."""
    mqtt_host, topic = config.mqtt_host(), config.setpoint_topic()
    client = build_client()
    telemetry = Telemetry(client, topic)
    history = History(telemetry)
```

with:

```python
def build_dashboard(charger=None):
    """The real dashboard from the environment. Raises ValueError for a bad setting; starts nothing.

    `charger` is the charger service: the history reads its cached status for the meter values."""
    mqtt_host, topic = config.mqtt_host(), config.setpoint_topic()
    client = build_client()
    telemetry = Telemetry(client, topic)
    history = History(telemetry, charger)
```

In `server/charger_web/__main__.py`, replace this text:

```python
            dashboard = build_dashboard()
```

with:

```python
            dashboard = build_dashboard(service)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_*.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_*.py'`
Expected: both OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/new-layout
git add server/charger_web/history.py server/charger_web/dashboard.py server/charger_web/__main__.py \
  server/tests/test_dashboard_history.py
git commit -m "Record every measurement in the history

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The state command and its route

**Files:**
- Create: `server/charger_web/commands.py`
- Overwrite: `server/charger_web/dashboard.py`, `server/charger_web/web.py`
- Create: `server/tests/test_vehicle_commands.py`, `server/tests/test_vehicle_web.py`

**Interfaces:**
- Consumes: `Telemetry.reply_marker()`, `Telemetry.wait_reply()`, `Telemetry.snapshot()["mqtt"]["connected"]` (Task 3), `FakeMqttClient` with `publish`/`publish_hook`/`publish_rc` (Task 3), `config.TOPIC_CMD_STATE`, `config.STATE_REPLY_TIMEOUT_S`.
- Produces:
  - `CommandError(status, message)` (a `RuntimeError` with `.status`).
  - `StateCommander(client, telemetry, timeout_s=config.STATE_REPLY_TIMEOUT_S).set_state(state) -> {"success": True, "state": "B", "reply": "state C -> B"}`; raises `CommandError` 400 (not exactly "A", "B" or "C"), 503 (broker not connected, publish failed, or no matching reply), 409 (refused by the board: the message is the board's reason; or a command already in progress).
  - `Dashboard(telemetry, history, mqtt_client=None, broker=None, commander=None)` with `set_state(state)` (raises `CommandError(503, ...)` without a commander); `build_dashboard(charger=None)` builds a `StateCommander`.
  - `POST /api/vehicle/state` (404 without a dashboard); a `CommandError` becomes its status with `{"success": false, "error": message}`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_vehicle_commands.py`:

```python
import threading
import unittest

from fake_mqtt import FakeMqttClient
from charger_web.commands import CommandError, StateCommander
from charger_web.telemetry import Telemetry


class CommanderTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMqttClient()
        self.telemetry = Telemetry(self.client, "evsim/setpoint")
        self.client.connect()
        self.commander = StateCommander(self.client, self.telemetry, timeout_s=0.4)

    def answer_with(self, *lines):
        """The board answers during the publish itself: the reply can arrive before the wait starts."""
        def hook(topic, payload):
            for line in lines:
                self.client.deliver("evsim/reply", line)
        self.client.publish_hook = hook

    def error_of(self, state):
        with self.assertRaises(CommandError) as raised:
            self.commander.set_state(state)
        return raised.exception

    def test_an_accepted_command_publishes_the_letter_and_returns_the_reply(self):
        self.answer_with("state C -> B")
        self.assertEqual(self.commander.set_state("B"), {"success": True, "state": "B", "reply": "state C -> B"})
        self.assertEqual(self.client.published, [("evsim/cmd/state", "B", 1, False)])    # QoS 1, not retained

    def test_already_in_the_requested_state_is_accepted_too(self):
        self.answer_with("already in B")
        self.assertEqual(self.commander.set_state("B")["reply"], "already in B")

    def test_a_refusal_is_a_409_with_the_boards_reason(self):
        self.answer_with("refused A -> C: state B must be asserted first")
        error = self.error_of("C")
        self.assertEqual((error.status, str(error)), (409, "state B must be asserted first"))

    def test_no_answer_is_a_503_after_the_timeout(self):
        error = self.error_of("B")
        self.assertEqual((error.status, str(error)), (503, "The board did not answer."))

    def test_replies_that_are_not_for_this_command_are_ignored(self):
        self.answer_with("state A -> C", "unknown command 'X'", "already in A", "refused B -> A: nope", "state C -> B")
        self.assertEqual(self.commander.set_state("B")["reply"], "state C -> B")

    def test_only_unrelated_replies_end_in_a_timeout(self):
        self.answer_with("state A -> C", "already in A")
        self.assertEqual(self.error_of("B").status, 503)

    def test_a_retained_or_non_ascii_reply_never_answers(self):
        def hook(topic, payload):
            self.client.deliver("evsim/reply", "state C -> B", retain=True)
            self.client.deliver("evsim/reply", b"state C -> B\xff")
        self.client.publish_hook = hook
        self.assertEqual(self.error_of("B").status, 503)

    def test_a_reply_from_before_the_command_does_not_count(self):
        self.client.deliver("evsim/reply", "state C -> B")
        self.assertEqual(self.error_of("B").status, 503)

    def test_with_the_broker_down_nothing_is_published(self):
        self.client.drop()
        error = self.error_of("B")
        self.assertEqual((error.status, str(error)), (503, "Not connected to the MQTT broker."))
        self.assertEqual(self.client.published, [])

    def test_a_publish_that_fails_is_a_503_and_frees_the_lock(self):
        self.client.publish_rc = 4
        error = self.error_of("B")
        self.assertEqual((error.status, str(error)), (503, "The command could not be sent."))
        self.client.publish_rc = 0
        self.answer_with("state C -> B")
        self.assertTrue(self.commander.set_state("B")["success"])

    def test_anything_but_a_b_or_c_is_a_400_and_nothing_is_published(self):
        for bad in ("a", "D", "", "BB", " B", "B\n", None, 1, True, ["B"], {"state": "B"}):
            self.assertEqual(self.error_of(bad).status, 400, repr(bad))
        self.assertEqual(self.client.published, [])

    def test_a_second_command_while_one_waits_is_a_409_and_publishes_nothing(self):
        first = {}
        thread = threading.Thread(target=lambda: first.setdefault("error", self.error_of("B")))
        thread.start()
        while not self.client.published:
            thread.join(0.01)
        second = self.error_of("C")
        self.assertEqual((second.status, str(second)), (409, "A command is already in progress."))
        thread.join(2)
        self.assertEqual(first["error"].status, 503)                  # the first one timed out
        self.assertEqual(len(self.client.published), 1)

    def test_the_lock_is_free_again_after_a_timeout_and_after_a_refusal(self):
        self.assertEqual(self.error_of("B").status, 503)
        self.answer_with("refused A -> C: state B must be asserted first")
        self.assertEqual(self.error_of("C").status, 409)
        self.answer_with("state A -> B")
        self.assertTrue(self.commander.set_state("B")["success"])


if __name__ == "__main__":
    unittest.main()
```

Create `server/tests/test_vehicle_web.py`:

```python
import http.client
import json
import threading
import unittest

from fake_mqtt import FakeMqttClient
from charger_web.commands import StateCommander
from charger_web.dashboard import Dashboard
from charger_web.history import History
from charger_web.telemetry import Telemetry
from charger_web.web import create_server


class VehicleRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMqttClient()
        self.telemetry = Telemetry(self.client, "evsim/setpoint")
        self.client.connect()
        commander = StateCommander(self.client, self.telemetry, timeout_s=0.3)
        self.dashboard = Dashboard(self.telemetry, History(self.telemetry), self.client, ("127.0.0.1", 1883), commander)
        self.port = self.serve(self.dashboard)

    def serve(self, dashboard):
        server = create_server(charger=object(), dashboard=dashboard, port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def post(self, body, port=None, path="/api/vehicle/state", origin=True, host=None, content_type="application/json"):
        port = port or self.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host or "127.0.0.1:%d" % port, "Content-Type": content_type}
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % port
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn.request("POST", path, data, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, (json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw)

    def answer_with(self, line):
        self.client.publish_hook = lambda topic, payload: self.client.deliver("evsim/reply", line)

    def test_an_accepted_command_is_a_200(self):
        self.answer_with("state C -> B")
        status, body = self.post({"state": "B"})
        self.assertEqual((status, body), (200, {"success": True, "state": "B", "reply": "state C -> B"}))
        self.assertEqual(self.client.published, [("evsim/cmd/state", "B", 1, False)])

    def test_a_refusal_is_a_409_with_the_reason(self):
        self.answer_with("refused A -> C: state B must be asserted first")
        status, body = self.post({"state": "C"})
        self.assertEqual((status, body), (409, {"success": False, "error": "state B must be asserted first"}))

    def test_no_answer_is_a_503(self):
        status, body = self.post({"state": "B"})
        self.assertEqual((status, body["success"], body["error"]), (503, False, "The board did not answer."))

    def test_the_broker_down_is_a_503_and_publishes_nothing(self):
        self.client.drop()
        self.assertEqual(self.post({"state": "B"})[0], 503)
        self.assertEqual(self.client.published, [])

    def test_a_bad_letter_is_a_400(self):
        for bad in ("b", "D", "", "BB", None, 1):
            self.assertEqual(self.post({"state": bad})[0], 400, repr(bad))
        self.assertEqual(self.client.published, [])

    def test_a_request_that_must_publish_nothing(self):
        cases = {
            "other key": lambda: self.post({"current_limit_a": 8}),
            "extra key": lambda: self.post({"state": "B", "x": 1}),
            "not an object": lambda: self.post("[\"B\"]"),
            "invalid json": lambda: self.post("{nope"),
            "duplicate key": lambda: self.post('{"state": "B", "state": "C"}'),
            "empty body": lambda: self.post(""),
            "too large": lambda: self.post(json.dumps({"state": "B", "pad": "x" * 2000})),
            "wrong type": lambda: self.post("state=B", content_type="text/plain"),
            "foreign origin": lambda: self.post({"state": "B"}, origin=False),
            "wrong host": lambda: self.post({"state": "B"}, host="evil.example"),
        }
        for name, request in cases.items():
            status, _ = request()
            self.assertIn(status, (400, 403, 413, 415), name)
        self.assertEqual(self.client.published, [], "nothing may be published for a rejected request")

    def test_without_a_dashboard_the_route_is_not_found(self):
        port = self.serve(None)
        self.assertEqual(self.post({"state": "B"}, port=port)[0], 404)

    def test_without_a_commander_the_route_answers_503(self):
        bare = Dashboard(self.telemetry, History(self.telemetry), self.client, ("127.0.0.1", 1883))
        self.assertEqual(self.post({"state": "B"}, port=self.serve(bare))[0], 503)

    def test_the_limit_route_is_unchanged(self):
        status, _ = self.post({"state": "B"}, path="/api/charger/operator-limit")
        self.assertEqual(status, 400)                               # expects current_limit_a, not state


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_vehicle_*.py'`
Expected: FAIL: `No module named 'charger_web.commands'`.

- [ ] **Step 3: Write the implementation**

Create `server/charger_web/commands.py`:

```python
"""The state command: publish A, B or C to the board and wait for its one-line answer.

This is the page's only write path to MQTT. Replies carry no command id, so a reply is matched by content: a line
naming the requested state is the answer, any other line is somebody else's and is ignored. One command at a time.
"""

import logging
import re
import threading

from . import config

log = logging.getLogger("charger_web.commands")

STATES = ("A", "B", "C")


class CommandError(RuntimeError):
    """A command that did not succeed; `status` is the HTTP status to answer with."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class StateCommander:
    def __init__(self, client, telemetry, timeout_s=config.STATE_REPLY_TIMEOUT_S):
        self._client = client
        self._telemetry = telemetry
        self._timeout = timeout_s
        self._busy = threading.Lock()

    def set_state(self, state):
        if type(state) is not str or state not in STATES:
            raise CommandError(400, "State must be A, B or C.")
        if not self._telemetry.snapshot()["mqtt"]["connected"]:
            raise CommandError(503, "Not connected to the MQTT broker.")
        if not self._busy.acquire(blocking=False):
            raise CommandError(409, "A command is already in progress.")
        try:
            accepted = re.compile(r"^(state [ABC] -> %s|already in %s)$" % (state, state))
            refused = re.compile(r"^refused [ABC] -> %s(?:: (.*))?$" % state)
            since = self._telemetry.reply_marker()            # taken before publishing: a fast reply is not missed
            info = self._client.publish(config.TOPIC_CMD_STATE, state, qos=1, retain=False)
            if info.rc != 0:
                raise CommandError(503, "The command could not be sent.")
            reply = self._telemetry.wait_reply(
                since, lambda text: bool(accepted.match(text) or refused.match(text)), self._timeout)
        finally:
            self._busy.release()
        if reply is None:
            raise CommandError(503, "The board did not answer.")
        refusal = refused.match(reply)
        if refusal:
            raise CommandError(409, refusal.group(1) or reply)
        log.info("state %s accepted: %s", state, reply)
        return {"success": True, "state": state, "reply": reply}
```

Overwrite `server/charger_web/dashboard.py`:

```python
"""Owns the lifecycle of the dashboard's two sources and the state command, and gives the web layer its calls."""

from . import config
from .commands import CommandError, StateCommander
from .history import History
from .telemetry import Telemetry, build_client


class Dashboard:
    def __init__(self, telemetry, history, mqtt_client=None, broker=None, commander=None):
        self._telemetry = telemetry
        self._history = history
        self._client = mqtt_client
        self._broker = broker              # (host, port)
        self._commander = commander
        self._started = False

    def telemetry(self):
        """The latest readings: board, setpoint, the server's target and the inverter's measurements."""
        return self._telemetry.snapshot()

    def history(self):
        return self._history.snapshot()

    def set_state(self, state):
        """Command the board's pilot state; raises CommandError (its `status` is the HTTP status)."""
        if self._commander is None:
            raise CommandError(503, "State commands are not available.")
        return self._commander.set_state(state)

    def start(self):
        """Connect the broker and start the sampler. connect_async: a broker that is down is retried, never raised."""
        if self._started:
            return
        self._started = True
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


def build_dashboard(charger=None):
    """The real dashboard from the environment. Raises ValueError for a bad setting; starts nothing.

    `charger` is the charger service: the history reads its cached status for the meter values."""
    mqtt_host, topic = config.mqtt_host(), config.setpoint_topic()
    client = build_client()
    telemetry = Telemetry(client, topic)
    history = History(telemetry, charger)
    commander = StateCommander(client, telemetry)
    return Dashboard(telemetry, history, client, (mqtt_host, config.MQTT_PORT), commander)
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

from .commands import CommandError
from .service import ChargerError, ChargerService, LimitValidationError
from .config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT

log = logging.getLogger("charger_web.http")
STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 1024
LIMIT_PATH = "/api/charger/operator-limit"
STATE_PATH = "/api/vehicle/state"
POST_KEYS = {LIMIT_PATH: "current_limit_a", STATE_PATH: "state"}      # each write takes exactly this one key


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
                "/chart.js": ("chart.js", "text/javascript; charset=utf-8"),
                "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
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
            key = POST_KEYS.get(path)
            if key is None or (path == STATE_PATH and dashboard is None):
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
            if not isinstance(payload, dict) or set(payload) != {key}:
                self._json(400, {"error": f"Expected {key}."})
                return
            if path == STATE_PATH:
                self._command(payload[key])
                return
            try:
                result = client.set_limit(payload[key])
            except LimitValidationError as exc:
                self._json(400, {"success": False, "error": str(exc)})
                return
            except ChargerError as exc:
                self._json(503, {"success": False, "error": str(exc)})
                return
            self._json(200, result)

        def _command(self, state):
            try:
                result = dashboard.set_state(state)
            except CommandError as exc:
                self._json(exc.status, {"success": False, "error": str(exc)})
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

- [ ] **Step 4: Run them to verify they pass, and that the older web tests still do**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_vehicle_*.py'` then `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_*.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_*.py'`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/new-layout
git add server/charger_web/commands.py server/charger_web/dashboard.py server/charger_web/web.py \
  server/tests/test_vehicle_commands.py server/tests/test_vehicle_web.py
git commit -m "Command the board's pilot state from the page

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The page shows the measurements and drives the state dropdown

**Files:**
- Overwrite: `server/charger_web/static/index.html`, `server/charger_web/static/dashboard.js`
- Modify: `server/charger_web/static/chart.js`, `server/charger_web/static/app.js`, `server/charger_web/static/app.css`
- Overwrite: `server/tests/test_dashboard_render.py`
- Modify: `server/tests/test_charger_page.py`, `server/tests/test_charger_slider.py`
- Create: `server/tests/test_vehicle_state_ui.py`

**Interfaces:**
- Consumes: `GET /api/telemetry` (Task 3/4: `inverter` has `dc_w dc_a dc_v ac_w ac_a ac_v`; `board.state`; `mqtt.connected`), `GET /api/history` (Task 4: frames), `GET /api/charger/status` (Task 2: `meter_power_w`, `meter_voltage_v`), `POST /api/vehicle/state` (Task 5).
- Produces: `chart.js` also defines `VOLT_SERIES` (keys `charger_v`, `dc_v`, `ac_v`) and `axisVolts(value) -> {top, step}` (100 V steps, never below 300 V) and exports them; `dashboard.js` defines `formatTriple(amps, volts, watts)` and `dashboardState.stateBusy` / `boardState`; `app.js` calls `sendState(letter)` when the dropdown changes.

- [ ] **Step 1: Write the failing tests**

In `server/tests/test_charger_page.py`, replace this text:

```python
    def test_the_chart_offers_power_and_current(self):
```

with:

```python
    def test_the_vehicle_state_is_a_dropdown_of_a_b_and_c(self):
        select = [attrs for tag, attrs in parsed_page().tags if attrs.get("id") == "vehicle-state"]
        self.assertEqual([attrs.get("class") for attrs in select], ["unit-select"])
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        block = html[html.index('id="vehicle-state"'):html.index("</select>", html.index('id="vehicle-state"'))]
        self.assertEqual(re.findall(r'<option value="([^"]*)"', block), ["", "A", "B", "C"])
        self.assertIn("disabled", block.split(">")[0])                   # off until the board's state is known
        self.assertNotIn("stepper", html)

    def test_the_chart_offers_power_current_and_voltage(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        start = html.index('id="chart-unit"')
        block = html[start:html.index("</select>", start)]
        self.assertEqual(re.findall(r'<option value="([^"]*)">', block), ["W", "A", "V"])

    def test_the_chart_offers_power_and_current(self):
```

and replace this text:

```python
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_two_views_name_their_lines(self):
        power = node_json("c.POWER_SERIES.map((s) => [s.id, s.key])")
        current = node_json("c.CURRENT_SERIES.map((s) => [s.id, s.key])")
```

with:

```python
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_voltage_axis_steps_by_100_volts_and_never_goes_below_300(self):
        self.assertEqual(
            node_json("[c.axisVolts(0), c.axisVolts(233), c.axisVolts(524.7), c.axisVolts(1000)]"),
            [{"top": 300, "step": 100}, {"top": 300, "step": 100}, {"top": 600, "step": 100}, {"top": 1000, "step": 100}],
        )

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_voltage_view_names_its_lines_after_the_other_two(self):
        volts = node_json("c.VOLT_SERIES.map((s) => [s.id, s.key])")
        self.assertEqual(volts, [["car", "charger_v"], ["dc", "dc_v"], ["ac", "ac_v"]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_two_views_name_their_lines(self):
        power = node_json("c.POWER_SERIES.map((s) => [s.id, s.key])")
        current = node_json("c.CURRENT_SERIES.map((s) => [s.id, s.key])")
```

and replace this text:

```python
        for removed in ("operator-limit", "configured-limit", "charger-status", "board-detail", "server-detail",
                        "target-value", "apply", "signalled-updated", "meter-updated"):
```

with:

```python
        for removed in ("operator-limit", "configured-limit", "charger-status", "board-detail", "server-detail",
                        "target-value", "apply", "signalled-updated", "meter-updated", "stepper-value"):
```

In `server/tests/test_charger_slider.py`, replace this text:

```python
        self.assertEqual((first["slider"], first["limit"], first["state"]), ("10", "10 A", "C"))
        self.assertEqual((first["signalled"], first["charger"]), ("10 A", "11.8 A / —"))    # mean of the three phases
```

with:

```python
        self.assertEqual((first["slider"], first["limit"]), ("10", "10 A"))
        self.assertEqual((first["signalled"], first["charger"]), ("10 A", "11.8 A / 232 V / 600 W"))   # means of the phases
```

and replace this text:

```python
    "meter_current_a": {"value": {"L1": 11.7, "L2": 11.6, "L3": 12.0}, "status": "ok", "updated_at": None},
```

with:

```python
    "meter_current_a": {"value": {"L1": 11.7, "L2": 11.6, "L3": 12.0}, "status": "ok", "updated_at": None},
    "meter_voltage_v": {"value": {"L1": 231, "L2": 232, "L3": 233}, "status": "ok", "updated_at": None},
    "meter_power_w": {"value": 600, "status": "ok", "updated_at": None},
```

Overwrite `server/tests/test_dashboard_render.py`:

```python
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "charger_web" / "static"

# Runs chart.js and dashboard.js in Node against the stub DOM. The scenario is {"telemetry": ..., "steps": [...]}:
# renderTelemetry runs first, then each step: ["unit", "A"] picks a chart quantity, ["legend", "DC input"] clicks a legend
# entry, ["history", frames] renders a history. The result is what the script put on the page.
HARNESS = r"""
const fs = require("fs");
const { installDom } = require(process.argv[3]);
const { elements, document } = installDom();
global.fetch = () => Promise.reject(new Error("no network in this test"));
const code = ["chart.js", "dashboard.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code + "\nglobalThis.page = { renderTelemetry, renderHistory };");
const scenario = JSON.parse(process.argv[2]);
if (scenario.telemetry) page.renderTelemetry(scenario.telemetry);
for (const [kind, a] of scenario.steps || []) {
  if (kind === "unit") document.getElementById("chart-unit").listeners.change({ target: { value: a } });
  if (kind === "legend") document.getElementById("legend").children.find((btn) => btn.children[1].textContent === a).listeners.click();
  if (kind === "history") page.renderHistory({ frames: a });
}
const out = { elements: {}, legend: [] };
for (const [id, el] of Object.entries(elements)) {
  out.elements[id] = { text: el.textContent, status: el.dataset.status || null, state: el.dataset.state || null,
                       title: el.title, value: el.value, disabled: el.disabled === true };
}
for (const btn of document.getElementById("legend").children) {
  out.legend.push({ label: btn.children[1].textContent, pressed: btn.attrs["aria-pressed"], noData: btn.classes.has("no-data") });
}
console.log(JSON.stringify(out));
"""

INVERTER = {"dc_w": 5661.8, "dc_a": 10.9, "dc_v": 520.4, "ac_w": 5500.2, "ac_a": 8.1, "ac_v": 233.3}


def reading(value, status):
    return {"value": value, "status": status, "updated_at": None}


def telemetry(board="ok", setpoint="ok", inverter="ok", connected=True, state="C", values=None):
    def make(value, status):
        return reading(None, "unavailable") if status == "unavailable" else reading(value, status)

    values = INVERTER if values is None else values
    return {
        "mqtt": {"connected": connected},
        "board": {"setpoint_w": make(5739.0, board), "state": state, "status": "live"},
        "setpoint": {"topic": "evsim/setpoint", "setpoint_w": make(5678.0, setpoint), "status": "ok"},
        "server": {"target_dc_w": make(5682.4, "ok"), "mode": "capped", "inverter_state": 4},
        "inverter": {name: make(values.get(name), inverter) for name in INVERTER},
    }


def run(telemetry_data=None, *steps):
    scenario = {"telemetry": telemetry_data, "steps": list(steps)}
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(scenario), str(HERE / "dom_stub.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def labels(out):
    return [entry["label"] for entry in out["legend"]]


def by_label(out, field):
    return {entry["label"]: entry[field] for entry in out["legend"]}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RenderTest(unittest.TestCase):
    def test_the_lists_show_the_setpoints(self):
        out = run(telemetry())["elements"]
        self.assertEqual((out["board-setpoint"]["text"], out["board-setpoint"]["status"]), ("5739 W", "ok"))
        self.assertEqual((out["setpoint-value"]["text"], out["setpoint-value"]["status"]), ("5678 W", "ok"))
        self.assertIn("evsim/setpoint", out["setpoint-value"]["title"])

    def test_the_inverter_rows_show_current_voltage_and_power(self):
        out = run(telemetry())["elements"]
        self.assertEqual((out["inverter-dc"]["text"], out["inverter-dc"]["status"]), ("10.9 A / 520 V / 5662 W", "ok"))
        self.assertEqual((out["inverter-ac"]["text"], out["inverter-ac"]["status"]), ("8.1 A / 233 V / 5500 W", "ok"))
        self.assertTrue(out["measurements-updated"]["text"].startswith("Inverter · "))

    def test_an_old_setpoint_server_leaves_dashes_for_the_new_values(self):
        out = run(telemetry(values={"dc_w": 5661.8, "ac_w": 5500.2}))["elements"]
        self.assertEqual(out["inverter-dc"]["text"], "— / — / 5662 W")
        self.assertEqual(out["inverter-ac"]["text"], "— / — / 5500 W")

    def test_the_broker_badge_follows_the_connection(self):
        up = run(telemetry(connected=True))["elements"]["mqtt-state"]
        down = run(telemetry(connected=False))["elements"]["mqtt-state"]
        self.assertEqual((up["text"], up["state"]), ("Broker connected", "ok"))
        self.assertEqual((down["text"], down["state"]), ("Broker not connected", "unavailable"))

    def test_a_stale_value_is_dimmed_and_its_age_line_says_so(self):
        out = run(telemetry(board="stale", inverter="stale"))["elements"]
        self.assertEqual(out["board-setpoint"]["status"], "stale")
        self.assertTrue(out["setpoints-updated"]["text"].startswith("Stale"))
        self.assertEqual((out["inverter-dc"]["status"], out["inverter-ac"]["status"]), ("stale", "stale"))

    def test_nothing_received_shows_dashes_and_dims_everything(self):
        out = run(telemetry(board="unavailable", setpoint="unavailable", inverter="unavailable"))["elements"]
        self.assertEqual((out["board-setpoint"]["text"], out["setpoint-value"]["text"]), ("—", "—"))
        self.assertEqual((out["inverter-dc"]["text"], out["inverter-ac"]["text"]), ("— / — / —", "— / — / —"))
        for element in ("board-setpoint", "setpoint-value", "inverter-dc", "inverter-ac"):
            self.assertEqual(out[element]["status"], "unavailable", element)
        self.assertEqual(out["setpoints-updated"]["text"], "No reading yet")

    def test_the_power_legend_has_five_entries_all_shown(self):
        out = run()
        self.assertEqual(labels(out), ["Board", "EV setpoint", "Car charger", "DC input", "AC output"])
        self.assertEqual(set(by_label(out, "pressed").values()), {"true"})

    def test_switching_to_amps_and_to_volts_rebuilds_the_legend(self):
        self.assertEqual(labels(run(None, ("unit", "A"))), ["Signalled", "Car charger", "DC input", "AC output"])
        self.assertEqual(labels(run(None, ("unit", "V"))), ["Car charger", "DC input", "AC output"])

    def test_an_unknown_quantity_falls_back_to_power(self):
        self.assertEqual(labels(run(None, ("unit", "X"))), ["Board", "EV setpoint", "Car charger", "DC input", "AC output"])

    def test_a_hidden_line_stays_hidden_across_all_three_views(self):
        out = run(None, ("legend", "DC input"), ("unit", "A"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("unit", "V"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("unit", "A"), ("unit", "W"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("legend", "DC input"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "true")

    def test_a_power_line_with_no_data_source_yet_is_marked(self):
        frames = [{"t": 1, "cp_w": 5000, "setpoint_w": 5000, "dc_w": 5000}]
        out = run(None, ("history", frames))
        self.assertEqual(by_label(out, "noData"),
                         {"Board": False, "EV setpoint": False, "Car charger": True, "DC input": False, "AC output": True})

    def test_all_three_charts_draw_the_servers_history(self):
        frames = [{"t": 1, "sig_a": 12.0, "charger_a": 11.8, "dc_a": 10.9, "ac_a": 8.1,
                   "charger_w": 8245, "dc_w": 5662, "ac_w": 5500, "cp_w": 5700, "setpoint_w": 5700,
                   "charger_v": 232, "dc_v": 520, "ac_v": 233}]
        for unit in ("W", "A", "V"):
            out = run(None, ("history", frames), ("unit", unit))
            self.assertEqual(set(by_label(out, "noData").values()), {False}, unit)

    def test_the_current_chart_is_empty_until_the_history_carries_currents(self):
        frames = [{"t": 1, "cp_w": 5000, "setpoint_w": 5000, "dc_w": 5000}]
        out = run(None, ("history", frames), ("unit", "A"))
        self.assertEqual(set(by_label(out, "noData").values()), {True})

    def test_the_dropdown_follows_the_board_and_is_off_when_the_state_is_unknown(self):
        for state, value, disabled in (("C", "C", False), ("A", "A", False), (None, "", True), ("X", "", True)):
            select = run(telemetry(state=state))["elements"]["vehicle-state"]
            self.assertEqual((select["value"], select["disabled"]), (value, disabled), state)

    def test_the_dropdown_is_off_while_the_broker_is_down(self):
        select = run(telemetry(connected=False, state="C"))["elements"]["vehicle-state"]
        self.assertEqual((select["value"], select["disabled"]), ("", True))


if __name__ == "__main__":
    unittest.main()
```

Create `server/tests/test_vehicle_state_ui.py`:

```python
import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "charger_web" / "static"

# Runs the three page scripts in Node against the stub DOM with a scripted fetch. Steps: ["select", "B"] picks an option
# in the state dropdown, ["telemetry", telemetry] changes what /api/telemetry returns and polls it, ["wait", ms] waits.
HARNESS = r"""
const fs = require("fs");
const { installDom } = require(process.argv[3]);
const { elements, document } = installDom();
const scenario = JSON.parse(process.argv[2]);
const posts = [];
const respond = (ok, status, body, delay = 0) =>
  new Promise((resolve) => setTimeout(() => resolve({ ok, status, json: async () => body }), delay));
global.fetch = (url, options) => {
  if (options && options.method === "POST") {
    posts.push(JSON.parse(options.body));
    const post = scenario.post;
    return respond(post.ok, post.ok ? 200 : post.status, post.body, post.delay || 0);
  }
  if (url === "/api/charger/status") return respond(true, 200, scenario.charger);
  if (url === "/api/telemetry") return respond(true, 200, scenario.telemetry);
  return Promise.reject(new Error("not served in this test: " + url));
};
const code = ["chart.js", "dashboard.js", "app.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code + "\nglobalThis.page = { pollTelemetry };");
const select = document.getElementById("vehicle-state");
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const snapshot = () => {
  const pill = document.getElementById("command-status");
  return { posts: posts.slice(), select: { value: select.value, disabled: select.disabled === true },
           pill: { text: pill.textContent, kind: pill.dataset.kind, title: pill.title } };
};
(async () => {
  await wait(30);
  const log = [snapshot()];
  for (const [kind, value] of scenario.steps) {
    if (kind === "select") { select.value = value; select.listeners.change(); }
    if (kind === "telemetry") { scenario.telemetry = value; page.pollTelemetry(); }
    if (kind === "wait") await wait(value);
    log.push(snapshot());
  }
  console.log(JSON.stringify(log));
})();
"""

CHARGER = {
    "status": "ok", "link": "ok", "error": None, "paused": False, "operator_below_set": False,
    "set_limit_a": {"value": 10, "status": "ok", "updated_at": None},
    "operator_limit_a": {"value": 16, "status": "ok", "updated_at": None},
    "installation_limit_a": {"value": 16, "status": "ok", "updated_at": None},
    "signalled_current_a": {"value": 10, "status": "ok", "updated_at": None},
    "meter_current_a": {"value": {"L1": 11.7, "L2": 11.6, "L3": 12.0}, "status": "ok", "updated_at": None},
    "meter_voltage_v": {"value": {"L1": 231, "L2": 232, "L3": 233}, "status": "ok", "updated_at": None},
    "meter_power_w": {"value": 600, "status": "ok", "updated_at": None},
    "vehicle_state": {"value": "C", "status": "ok", "updated_at": None},
    "charger_status": {"value": "Charging", "status": "ok", "updated_at": None},
    "desired_limit_a": 10, "restored_at": None,
}


def telemetry(state="C", connected=True):
    ok = {"value": 5000.0, "status": "ok", "updated_at": None}
    gone = {"value": None, "status": "unavailable", "updated_at": None}
    return {
        "mqtt": {"connected": connected},
        "board": {"setpoint_w": ok, "state": state, "status": "live"},
        "setpoint": {"topic": "evsim/setpoint", "setpoint_w": ok, "status": "ok"},
        "server": {"target_dc_w": ok, "mode": "capped", "inverter_state": 4},
        "inverter": {name: gone for name in ("dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v")},
    }


ACCEPTED = {"ok": True, "body": {"success": True, "state": "B", "reply": "state C -> B"}}
REFUSED = {"ok": False, "status": 409, "body": {"success": False, "error": "state B must be asserted first"}}
NO_ANSWER = {"ok": False, "status": 503, "body": {"success": False, "error": "The board did not answer."}}


def run(steps, post=ACCEPTED, board=None):
    scenario = {"charger": copy.deepcopy(CHARGER), "telemetry": board or telemetry(), "post": post, "steps": steps}
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(scenario), str(HERE / "dom_stub.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class StateDropdownTest(unittest.TestCase):
    def test_the_dropdown_starts_on_the_boards_state_and_enabled(self):
        first = run([])[0]
        self.assertEqual((first["select"], first["posts"]), ({"value": "C", "disabled": False}, []))

    def test_with_the_broker_down_it_is_empty_and_disabled(self):
        first = run([], board=telemetry(connected=False))[0]
        self.assertEqual(first["select"], {"value": "", "disabled": True})

    def test_choosing_a_state_sends_it_once_and_reports_validated(self):
        log = run([("select", "B"), ("wait", 60)])
        self.assertEqual(log[-1]["posts"], [{"state": "B"}])
        self.assertEqual((log[-1]["pill"]["text"], log[-1]["pill"]["kind"]), ("Validated", "ok"))
        self.assertFalse(log[-1]["select"]["disabled"])

    def test_the_dropdown_is_off_while_the_command_is_in_flight(self):
        slow = {"ok": True, "delay": 60, "body": ACCEPTED["body"]}
        log = run([("select", "B"), ("wait", 20), ("wait", 100)], post=slow)
        self.assertEqual(log[1]["select"]["disabled"], True)                    # right after the choice
        self.assertEqual(log[1]["pill"]["text"], "Sending…")
        self.assertEqual(log[-1]["select"]["disabled"], False)

    def test_a_poll_while_pending_does_not_move_the_dropdown(self):
        slow = {"ok": True, "delay": 80, "body": ACCEPTED["body"]}
        log = run([("select", "B"), ("telemetry", telemetry("C")), ("wait", 30)], post=slow)
        self.assertEqual(log[-1]["select"], {"value": "B", "disabled": True})

    def test_a_refusal_says_not_validated_with_the_reason_and_returns_to_the_boards_state(self):
        log = run([("select", "B"), ("wait", 60)], post=REFUSED)
        last = log[-1]
        self.assertEqual((last["pill"]["text"], last["pill"]["kind"]), ("Not validated", "error"))
        self.assertIn("must be asserted first", last["pill"]["title"])
        self.assertEqual(last["select"], {"value": "C", "disabled": False})

    def test_no_answer_from_the_board_is_not_validated_and_restores_the_dropdown(self):
        last = run([("select", "A"), ("wait", 60)], post=NO_ANSWER)[-1]
        self.assertEqual((last["pill"]["text"], last["select"]["value"]), ("Not validated", "C"))
        self.assertIn("did not answer", last["pill"]["title"])

    def test_after_an_accepted_command_the_next_poll_shows_the_new_state(self):
        log = run([("select", "B"), ("wait", 60), ("telemetry", telemetry("B")), ("wait", 40)])
        self.assertEqual(log[-1]["select"], {"value": "B", "disabled": False})

    def test_choosing_the_empty_option_sends_nothing(self):
        log = run([("select", ""), ("wait", 40)])
        self.assertEqual(log[-1]["posts"], [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_page.py'`, `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_render.py'`, `.venv/bin/python -B -m unittest discover -s tests -p 'test_vehicle_state_ui.py'` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_slider.py'`
Expected: FAIL: the page still has the stepper and two chart options, `VOLT_SERIES` and `formatTriple` do not exist, the measurement rows are pairs, and `/api/vehicle/state` is never called.

- [ ] **Step 3: Write the implementation**

In `server/charger_web/static/chart.js`, replace this text:

```javascript
const WINDOW_S = 600;
```

with:

```javascript
const VOLT_SERIES = [
  { id: "car", key: "charger_v", label: "Car charger", color: "#c27b14" },
  { id: "dc", key: "dc_v", label: "DC input", color: "#2a5fa8" },
  { id: "ac", key: "ac_v", label: "AC output", color: "#7b4fa3" },
];
const WINDOW_S = 600;
```

and replace this text:

```javascript
function drawChart(canvas, frames, visible, nowS, view = POWER_VIEW) {
```

with:

```javascript
// The same for volts: 100 V steps, never lower than 300 V so the 230 V mains and the 520 V DC side both fit.
function axisVolts(value) {
  return { top: Math.max(300, Math.ceil(value / 100) * 100), step: 100 };
}

function drawChart(canvas, frames, visible, nowS, view = POWER_VIEW) {
```

and replace this text:

```javascript
  const scale = view.unit === "A" ? axisAmps(highest) : axis(highest);
```

with:

```javascript
  const scale = ({ W: axis, A: axisAmps, V: axisVolts }[view.unit] || axis)(highest);
```

and replace this text:

```javascript
  module.exports = { SERIES, POWER_SERIES, CURRENT_SERIES, segments, axis, axisAmps };
```

with:

```javascript
  module.exports = { SERIES, POWER_SERIES, CURRENT_SERIES, VOLT_SERIES, segments, axis, axisAmps, axisVolts };
```

Overwrite `server/charger_web/static/dashboard.js`:

```javascript
// The setpoints, the measurements and the chart: polls /api/telemetry every second and /api/history every two.
// All three charts (power, current, voltage) draw the server's 10-minute history.
const byId = (id) => document.getElementById(id);
const dashboardState = {
  frames: [],
  unit: "W",
  visible: new Set([...POWER_SERIES, ...CURRENT_SERIES, ...VOLT_SERIES].map((series) => series.id)),
  busy: { telemetry: false, history: false },
  stateBusy: false,          // a vehicle-state command is in flight: the poll must not move or enable the dropdown
  boardState: null,          // the state the board presents (A, B or C) once known, else null
};
const VIEWS = {
  W: POWER_VIEW,
  A: { series: CURRENT_SERIES, unit: "A", maxGapS: MAX_GAP_S },
  V: { series: VOLT_SERIES, unit: "V", maxGapS: MAX_GAP_S },
};

function formatWatts(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} W`;
}

function formatAmpsValue(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Number.isInteger(value) ? value : value.toFixed(1)} A`;
}

function formatVolts(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} V`;
}

// "11.8 A / 232 V / 8245 W", with a dash for whatever is not measured.
function formatTriple(amps, volts, watts) {
  return `${formatAmpsValue(amps)} / ${formatVolts(volts)} / ${formatWatts(watts)}`;
}

function ageText(reading) {
  if (!reading || reading.status === "unavailable") return "No reading yet";
  const stale = reading.status === "stale";
  if (!reading.updated_at) return stale ? "Stale" : "Updated";
  const date = new Date(reading.updated_at);
  const time = Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleTimeString();
  return `${stale ? "Stale · last updated" : "Updated"} ${time}`;
}

function readingStatus(reading) {
  return reading ? reading.status : "unavailable";
}

function setValue(id, text, reading) {
  const target = byId(id);
  target.textContent = text;
  target.dataset.status = readingStatus(reading);
}

// The dropdown shows the state the board presents; it is off when the broker is down or the state is unknown,
// and while a command is in flight it is left alone (app.js sets it back when the command ends).
function syncStateSelect(connected, state) {
  const select = byId("vehicle-state");
  const known = connected && (state === "A" || state === "B" || state === "C");
  dashboardState.boardState = known ? state : null;
  select.disabled = !known || dashboardState.stateBusy;
  if (!dashboardState.stateBusy) select.value = known ? state : "";
}

function renderTelemetry(data) {
  const connected = Boolean(data.mqtt && data.mqtt.connected);
  const badge = byId("mqtt-state");
  badge.dataset.state = connected ? "ok" : "unavailable";
  badge.textContent = connected ? "Broker connected" : "Broker not connected";

  const board = data.board || {};
  setValue("board-setpoint", formatWatts(board.setpoint_w && board.setpoint_w.value), board.setpoint_w);
  byId("setpoints-updated").textContent = ageText(board.setpoint_w);
  syncStateSelect(connected, board.state);

  const setpoint = data.setpoint || {};
  setValue("setpoint-value", formatWatts(setpoint.setpoint_w && setpoint.setpoint_w.value), setpoint.setpoint_w);
  byId("setpoint-value").title = setpoint.topic ? `Topic ${setpoint.topic}` : "";

  const inverter = data.inverter || {};
  const value = (name) => (inverter[name] ? inverter[name].value : null);
  setValue("inverter-dc", formatTriple(value("dc_a"), value("dc_v"), value("dc_w")), inverter.dc_w);
  setValue("inverter-ac", formatTriple(value("ac_a"), value("ac_v"), value("ac_w")), inverter.ac_w);
  byId("measurements-updated").textContent = `Inverter · ${ageText(inverter.dc_w)}`;
}

// -- the chart ---------------------------------------------------------------------------------------

function currentView() {
  return VIEWS[dashboardState.unit];
}

function updateLegendAvailability() {
  const frames = dashboardState.frames;
  for (const button of document.querySelectorAll(".legend-item")) {
    const series = currentView().series.find((item) => item.id === button.dataset.series);
    const hasData = Boolean(series) && frames.some((frame) => typeof frame[series.key] === "number");
    button.classList.toggle("no-data", !hasData);
    button.title = hasData ? "" : "No data for this line";
  }
}

function buildLegend() {
  const legend = byId("legend");
  legend.replaceChildren();
  for (const series of currentView().series) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "legend-item";
    button.dataset.series = series.id;
    button.setAttribute("aria-pressed", String(dashboardState.visible.has(series.id)));
    const swatch = document.createElement("i");
    swatch.className = "swatch";
    swatch.style.background = series.color;
    const label = document.createElement("span");
    label.textContent = series.label;
    button.append(swatch, label);
    button.addEventListener("click", () => {
      if (dashboardState.visible.has(series.id)) dashboardState.visible.delete(series.id);
      else dashboardState.visible.add(series.id);
      button.setAttribute("aria-pressed", String(dashboardState.visible.has(series.id)));
      redrawChart();
    });
    legend.append(button);
  }
  updateLegendAvailability();
}

function updateChartNote() {
  byId("chart-note").textContent = `${dashboardState.frames.length} s of history · updated ${new Date().toLocaleTimeString()}`;
}

function redrawChart() {
  drawChart(byId("chart"), dashboardState.frames, dashboardState.visible, Date.now() / 1000, currentView());
}

function renderHistory(data) {
  dashboardState.frames = Array.isArray(data.frames) ? data.frames : [];
  updateChartNote();
  updateLegendAvailability();
  redrawChart();
}

// -- polling -----------------------------------------------------------------------------------------

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

byId("chart-unit").addEventListener("change", (event) => {
  dashboardState.unit = ["W", "A", "V"].includes(event.target.value) ? event.target.value : "W";
  buildLegend();
  redrawChart();
});
window.addEventListener("resize", redrawChart);
buildLegend();
pollTelemetry();
pollHistory();
window.setInterval(pollTelemetry, 1000);
window.setInterval(pollHistory, 2000);
```

In `server/charger_web/static/app.js`, replace this text:

```javascript
  const meter = status.meter_current_a || empty;
  const charger = meter.status === "ok" ? meanOf(meter.value) : null;
```

with:

```javascript
  const meter = status.meter_current_a || empty;
  const voltage = status.meter_voltage_v || empty;
  const power = status.meter_power_w || empty;
```

and replace this text:

```javascript
  chargerCell.textContent = formatPair(meanOf(meter.value), null);   // watts are not read from the charger yet
  chargerCell.dataset.status = meter.status;
  chargerCell.title = "Mean of the three phases";
  recordCurrentSample(signalled, charger);
```

with:

```javascript
  chargerCell.textContent = formatTriple(meanOf(meter.value), meanOf(voltage.value), power.value);
  chargerCell.dataset.status = power.status;
  chargerCell.title = "Current and voltage: mean of the three phases";
```

and replace this text:

```javascript
  el("vehicle-state").textContent = (status.vehicle_state && status.vehicle_state.value) || "—";
```

with nothing (delete that line), and replace this text:

```javascript
slider.addEventListener("input", () => {
```

with:

```javascript
// The state dropdown: choosing a letter commands the board; "Validated" means the board accepted it.
const stateSelect = el("vehicle-state");

async function sendState(letter) {
  if (dashboardState.stateBusy || !["A", "B", "C"].includes(letter)) return;
  dashboardState.stateBusy = true;
  stateSelect.disabled = true;
  setStatus("pending", "Sending…");
  try {
    const response = await fetch("/api/vehicle/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: letter }),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || "The board did not accept the command.");
    setStatus("ok", "Validated");
  } catch (error) {
    setStatus("error", "Not validated", error.message);
    stateSelect.value = dashboardState.boardState || "";      // back to what the board presents
  } finally {
    dashboardState.stateBusy = false;
    stateSelect.disabled = dashboardState.boardState === null;
    pollTelemetry();
  }
}

stateSelect.addEventListener("change", () => void sendState(stateSelect.value));

slider.addEventListener("input", () => {
```

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
        <h1>EVSim</h1>
        <p class="subheading">Charger, setpoint and inverter, live</p>
      </div>
      <div class="badges">
        <span id="connection" class="connection" data-state="loading">Connecting…</span>
        <span id="mqtt-state" class="connection" data-state="loading">Broker …</span>
      </div>
    </header>

    <p id="banner" class="banner" role="status" hidden></p>

    <section class="panel controls" aria-label="Controls">
      <div class="state-block">
        <p class="eyebrow">Car charger state</p>
        <div class="state-row">
          <label class="control-label" for="vehicle-state">State</label>
          <select id="vehicle-state" class="unit-select" aria-label="Vehicle state" disabled>
            <option value="" selected disabled>—</option>
            <option value="A">A</option>
            <option value="B">B</option>
            <option value="C">C</option>
          </select>
        </div>
        <div class="status-row">
          <span>Status</span>
          <span id="command-status" class="status-pill" data-kind="idle" role="status" aria-live="polite">No command yet</span>
        </div>
      </div>

      <div class="limit-block">
        <div class="section-heading">
          <p class="eyebrow">Maximum current</p>
          <span id="installation-limit" class="ceiling">Installation limit: —</span>
        </div>
        <form id="limit-form">
          <div class="limit-row">
            <label class="control-label" for="current-limit">Maximum current</label>
            <output id="limit-value" class="limit-value" for="current-limit">—</output>
            <span id="operator-warning" class="warning" hidden></span>
          </div>
          <div class="range-wrap">
            <input id="current-limit" name="current_limit_a" type="range" min="6" max="16" step="1" value="6" aria-label="Maximum current in amps">
            <div class="ticks" aria-hidden="true">
              <span id="tick-min">6 A</span>
              <span id="tick-mid" hidden>—</span>
              <span id="tick-max">16 A</span>
            </div>
          </div>
        </form>
      </div>
    </section>

    <div class="layout">
      <section class="panel chart-panel" aria-labelledby="chart-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Last 10 minutes</p>
            <h2 id="chart-title" class="visually-hidden">History</h2>
            <select id="chart-unit" class="unit-select" aria-label="Chart quantity">
              <option value="W">Power [W]</option>
              <option value="A">Current [A]</option>
              <option value="V">Voltage [V]</option>
            </select>
          </div>
          <span id="chart-note" class="ceiling">Waiting for history</span>
        </div>
        <canvas id="chart" role="img" aria-label="Line chart of the selected quantity over the last ten minutes"></canvas>
        <div id="legend" class="legend"></div>
      </section>

      <div class="side">
        <section class="panel list-panel" aria-labelledby="setpoints-title">
          <h2 id="setpoints-title">Setpoints</h2>
          <dl class="rows">
            <div class="row"><dt>Signalled current</dt><dd id="signalled-current">—</dd></div>
            <div class="row"><dt>Board setpoint</dt><dd id="board-setpoint">—</dd></div>
            <div class="row"><dt>EV setpoint</dt><dd id="setpoint-value">—</dd></div>
          </dl>
          <p id="setpoints-updated" class="updated">Waiting for data</p>
        </section>

        <section class="panel list-panel" aria-labelledby="measurements-title">
          <h2 id="measurements-title">Measurements</h2>
          <dl class="rows">
            <div class="row"><dt>Car charger</dt><dd id="meter-charger">—</dd></div>
            <div class="row"><dt>DC input</dt><dd id="inverter-dc">—</dd></div>
            <div class="row"><dt>AC output</dt><dd id="inverter-ac">—</dd></div>
          </dl>
          <p id="measurements-updated" class="updated">Waiting for data</p>
        </section>
      </div>
    </div>

    <footer>
      <span id="last-updated">Readings refresh every 2 seconds.</span>
      <button id="refresh" class="text-button" type="button">Refresh now</button>
    </footer>
  </main>
</body>
</html>
```

Append to `server/charger_web/static/app.css`:

```css

.unit-select:disabled { cursor: not-allowed; opacity: .5; }
```

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_page.py'`, `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_render.py'`, `.venv/bin/python -B -m unittest discover -s tests -p 'test_vehicle_state_ui.py'`, `.venv/bin/python -B -m unittest discover -s tests -p 'test_charger_slider.py'`, then `.venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard_*.py'`
Expected: all OK. Node is installed here, so the Node-based tests run rather than skip.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/new-layout
git add server/charger_web/static server/tests/test_charger_page.py server/tests/test_charger_slider.py \
  server/tests/test_dashboard_render.py server/tests/test_vehicle_state_ui.py
git commit -m "Show current, voltage and power and drive the state from a dropdown

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Docs, the whole suite and the live check

**Files:**
- Modify: `server/README.md`
- Modify: `memory/evsim-dashboard.md`, `memory/evsim-amtron-modbus.md`

**Interfaces:**
- Consumes: everything above.
- Produces: the user-facing documentation and the notes for later sessions.

- [ ] **Step 1: Update the README**

In `server/README.md`, replace this text:

```
- Each source fails alone: with the broker down the charger part still works, and the reverse.
```

with:

```
- **Measurements:** the Car charger, DC input and AC output rows each read "current / voltage / power". The charger's
  values come from the AMTRON's meter (current and voltage are the mean of the three phases; total power from register
  220). The inverter's come from `evsim/inverter`: `dc_a`, `dc_v`, `dc_w` (MPPT 1) and `ac_a`, `ac_v` (means of the
  three phases), `ac_w`. A setpoint server older than 2026-10-07 does not publish the currents and voltages: they show
  dashes. The chart switches between Power, Current and Voltage; all three draw the same server-side history.
- **Vehicle state:** the dropdown shows the state the board presents and, when you pick A, B or C, publishes it to
  `evsim/cmd/state` and waits up to 3 s for the board's answer on `evsim/reply`. "Validated" means the board accepted it;
  "Not validated" gives the board's reason (for example A to C is refused until B). This is the page's only write to
  MQTT. Going to A opens the pilot: the charger stops and the inverter loses its DC input for about two minutes.
- Each source fails alone: with the broker down the charger part still works, and the reverse.
```

- [ ] **Step 2: Update the notes**

Append to `memory/evsim-dashboard.md`:

```markdown

## Measurements and state control (2026-10-07)

Spec `docs/superpowers/specs/2026-10-07-evsim-measurements-and-state-control-design.md`, plan
`docs/superpowers/plans/2026-10-07-evsim-measurements-and-state-control.md`.

- **Every measurement is A / V / W.** Charger: mean of the three phase currents (212, 214, 216) and voltages (222, 224,
  226), total power from 220 (see [[evsim-amtron-modbus]]). Inverter: `evsim/inverter` now also carries `dc_a`, `dc_v`,
  `ac_a`, `ac_v` (AC values are the mean of the three phases); the page still never reads the inverter.
- **History frames** carry all of them (`charger_w/a/v`, `sig_a`, `dc_a/v`, `ac_w/a/v`), so the power, current and
  voltage charts all draw the server's 10 minutes.
- **The page now publishes once:** `POST /api/vehicle/state` sends A, B or C to `evsim/cmd/state` (QoS 1, not retained)
  and waits 3 s for the matching line on `evsim/reply`. Replies have no command id, so they are matched by content; one
  command at a time (409 otherwise). "Validated" = the board accepted; the charger is not waited for.
```

Append to `memory/evsim-amtron-modbus.md`:

```markdown

## Meter registers found 2026-10-07 (inferred from the values)

Read-only scan of 200 to 235: **220-221 total active power (W)**; **222, 224, 226 phase voltages (V)**; **206, 208, 210
phase powers (W)**; 212, 214, 216 phase currents (mA, already used). All 32-bit, high word first. Checked by arithmetic,
not against the specification text: the phase powers summed to 8 243 W against a total of 8 245 W, and current times
voltage gave 2 734 W per phase against 2 737 W. 200-201 and 218-219 both held 309 893 (an energy counter, unit not
confirmed). The page reads 220 and 222-226.
```

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/python -B -m unittest discover -s tests`
Expected: OK (the earlier 288 tests plus the new ones; about two minutes).

- [ ] **Step 4: The live check (the rig, read-only except the state commands)**

Needs the lab: the broker, the board publishing, the setpoint server **restarted from this branch** with `--inverter 192.168.21.120` (it must publish the new fields), and the AMTRON charging. Use port 8081 for the page so the old one on 8080 is left alone:

```bash
cd server
.venv/bin/python -B -m charger_web --port 8081 > /tmp/layout-live.log 2>&1 &
echo $! > /tmp/layout-live.pid
sleep 15
curl -s http://127.0.0.1:8081/api/telemetry | python3 -m json.tool | sed -n 1,60p
curl -s http://127.0.0.1:8081/api/charger/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['meter_power_w'], d['meter_voltage_v'], d['meter_current_a'])"
mosquitto_sub -h 127.0.0.1 -t evsim/inverter -C 1 -W 5
.venv/bin/python ../tools/fronius_modbus.py status
```

Expected: `inverter` has all six readings `ok`; the page's `dc_a`, `dc_v`, `dc_w` equal the message and agree within a few units with the inverter tool's MPPT 1 line; `ac_a` and `ac_v` agree with the message; the charger power is near the three phase currents times voltages. Compare the three rows with the AMTRON's own web page for the car charger.

After about two minutes, the history carries every key:

```bash
curl -s http://127.0.0.1:8081/api/history | python3 -c "import json,sys; f=json.load(sys.stdin)['frames']; print(len(f), f[-1]); print({k: sum(1 for x in f if x[k] is None) for k in f[-1]})"
```

Expected: about 130 frames, the last with every key a number; few or no gaps.

**State command (it disturbs the rig: ask the user first).** The charger is normally in state C. Moving to B pauses charging and the inverter loses its DC input; moving back to C restores it, and the inverter then needs about two minutes. Do not command A. From a second terminal watch `mosquitto_sub -h 127.0.0.1 -t 'evsim/reply' -t 'evsim/state' -v`, then:

```bash
curl -s -X POST http://127.0.0.1:8081/api/vehicle/state -H 'Origin: http://127.0.0.1:8081' -H 'Content-Type: application/json' -d '{"state":"B"}'
curl -s -X POST http://127.0.0.1:8081/api/vehicle/state -H 'Origin: http://127.0.0.1:8081' -H 'Content-Type: application/json' -d '{"state":"C"}'
curl -s -X POST http://127.0.0.1:8081/api/vehicle/state -H 'Origin: http://127.0.0.1:8081' -H 'Content-Type: application/json' -d '{"state":"C"}'
```

Expected: the first answers `{"success":true,"state":"B","reply":"state C -> B"}`, the second `state B -> C`, the third `already in C`; the board's replies appear on `evsim/reply`; the dropdown in the browser follows. Then open `http://127.0.0.1:8081/` and check the dropdown, the three-part rows, the three chart quantities and the legend toggles. Stop the check:

```bash
kill "$(cat /tmp/layout-live.pid)"; rm /tmp/layout-live.pid
```

Append a `## Live check, <date>` section to `memory/evsim-dashboard.md` with what was seen (agreements, the replies, how long the inverter took to recover, anything not checked).

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # must print feat/new-layout
git add server/README.md memory/evsim-dashboard.md memory/evsim-amtron-modbus.md
git commit -m "Document the measurements and the state control

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.**

| Spec requirement | Task |
|---|---|
| A / V / W triples, means of the phases | 1 (inverter means), 2 (charger values), 4 (history means), 6 (rows) |
| Charger registers 220, 222, 224, 226 | 2 |
| `evsim/inverter` gains `dc_a`, `dc_v`, `ac_a`, `ac_v`; writer spec updated | 1 |
| Parser fields, bounds, snapshot `inverter` with six readings | 3 |
| History frames and the charger source; browser-side sampling removed | 4, 6 |
| Voltage chart, three-part rows | 6 |
| State command: dropdown, POST route, QoS 1 non-retained, 3 s reply wait, matching by content, 200/400/409/503, one at a time | 3 (replies), 5 (command, route), 6 (dropdown) |
| "Validated" means the board accepted | 5, 6 |
| Error handling table (no meter, old server, broker down, malformed reply) | 1 to 6 |
| Testing list and the live check (B, then back to C, never A) | 1 to 7 |

**Placeholder scan.** No TBD or "similar to" steps; every code step holds the code. Edits to existing files give the exact text to find and its replacement.

**Type consistency.** `snapshot()["inverter"]` has the keys `dc_w dc_a dc_v ac_w ac_a ac_v` in Tasks 3, 4 and 6; `parse_inverter` (Task 3) returns the same names; the history frame keys match the chart series keys (`charger_w`, `ac_w`, `sig_a`, `charger_a`, `dc_a`, `ac_a`, `charger_v`, `dc_v`, `ac_v`); `CommandError.status` (Task 5) is what `web.py` answers with; `Dashboard.set_state` (Task 5) is what the route calls; `dashboardState.stateBusy` and `boardState` (Task 6, `dashboard.js`) are what `app.js` reads; `build_dashboard(charger)` (Task 4) is called with the service in `__main__`.
