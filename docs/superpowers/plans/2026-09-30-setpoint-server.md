# Setpoint Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python server that subscribes to `evsim/cp`, averages the board's `setpoint_w` over a 1 s window, and publishes the result as JSON on `evsim/setpoint` for the Fronius inverter.

**Architecture:** A pure sliding-window averager and pure message parse/format functions, wired to MQTT by a small `SetpointServer` class that takes its client, clocks and lock as injected pieces so it is tested with a fake client. A separate entry point builds the real `paho-mqtt` client. A bench test suite runs the whole server against a private Mosquitto.

**Tech Stack:** Python 3 (the machine has 3.14), `paho-mqtt` 2.x, standard-library `unittest`, Mosquitto 2.x (already at `/opt/homebrew/sbin/mosquitto`).

**Spec:** [docs/superpowers/specs/2026-09-30-setpoint-server-design.md](../specs/2026-09-30-setpoint-server-design.md). It builds on [2026-09-29-mqtt-cp-telemetry-design.md](../specs/2026-09-29-mqtt-cp-telemetry-design.md), which defines the `evsim/cp` input.

## Global Constraints

Every task's requirements include these, copied from the spec.

- Input topic `evsim/cp`, QoS 0. The server reads only `setpoint_w`; the board's `status`, `t` and other fields are ignored.
- Output topic `evsim/setpoint`, QoS 1, **retained**, one message per second.
- Output JSON: `{"setpoint_w":10060,"samples":5,"window_s":1,"status":"ok","ts":"2026-09-30T13:42:07Z"}`, keys in that order, no spaces.
- `status` is `ok`, `no_data` or `server_offline`. Only `ok` carries a mean; `no_data` and `server_offline` have `setpoint_w` 0 and `samples` 0.
- `setpoint_w` is the mean rounded to a whole watt, **half rounds up**.
- `ts` is the computer's UTC time, ISO 8601, whole seconds, `Z` suffix. **`server_offline` messages have no `ts`.**
- The window is the 1.0 s before each tick, in **server arrival time on a monotonic clock**. A sample counts if `now - arrival < 1.0 s`. The board's `t` is never used.
- `held` and `zero` samples are averaged exactly as the board sends them.
- Unusable input (invalid JSON, `setpoint_w` missing, `null`, non-numeric, a boolean, not finite, negative, or **above 11 000 W**) is logged and dropped, not clamped; it never enters the average and never stops the loop. 11 000 W itself is accepted. The cap is the `MAX_SETPOINT_W` constant.
- Client ID `evsim-setpoint-server`, clean session, subscription made in the connect callback so it is renewed on every reconnect.
- Last-will on `evsim/setpoint`, retained, QoS 1, payload `{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}`.
- Clean shutdown (Ctrl-C or SIGTERM) publishes the same `server_offline` message itself, then disconnects.
- Default broker `127.0.0.1:1883`; `--host` and `--port` override.
- The only dependency is `paho-mqtt`, in a venv. Tests use standard-library `unittest`.
- Started by hand with `python -m setpoint_server` from `server/`.
- Publishes that cannot be sent are logged and skipped, never queued for later.

## Review Focus

Inputs and conditions the spec implies but does not spell out, most likely to bite first. Each has a test in the task that owns the code.

1. **`setpoint_w` is absurdly large**, either above the 11 000 W cap or an integer too large for a float (a JSON number with hundreds of digits, where `math.isfinite` raises `OverflowError`). Expected: dropped like any bad message, never clamped, and the MQTT thread survives. Tested in Tasks 2, 3 and 5.
2. **A payload that is deeply nested JSON or not UTF-8**: `json.loads` raises `RecursionError` or `UnicodeDecodeError`. Expected: dropped, not a crash. Tested in Task 2.
3. **A tick while the broker is disconnected**: paho would queue a QoS 1 publish and replay every stale setpoint in a burst on reconnect. Expected: nothing is published or queued while disconnected. Tested in Task 3.
4. **Stopping the server while the broker is down**: waiting for a publish acknowledgement that can never arrive would hang the shutdown. Expected: no publish and no wait when disconnected, and the disconnect still happens. Tested in Task 3.
5. **Starting the server before the broker exists, and a broker restart while running**: a blocking connect at startup would crash, and a missing re-subscribe after reconnect would leave the server deaf. Expected: it retries in the background and recovers without a restart. Tested in Tasks 3 and 5.

---

### Task 1: Scaffold, config and the averager

**Files:**
- Modify: `.gitignore`
- Create: `server/setpoint_server/__init__.py`
- Create: `server/setpoint_server/config.py`
- Create: `server/setpoint_server/averager.py`
- Test: `server/tests/test_averager.py`

**Interfaces:**
- Produces:
  - `config`: the constants `BROKER_HOST`, `BROKER_PORT`, `CLIENT_ID`, `TOPIC_CP`, `TOPIC_SETPOINT`, `WINDOW_S`, `MAX_SETPOINT_W`, `KEEPALIVE_S`, `RECONNECT_MIN_S`, `RECONNECT_MAX_S`, `SHUTDOWN_PUBLISH_TIMEOUT_S`.
  - `WindowAverager(window_s)` with `add(now: float, watts: float) -> None`, `average(now: float) -> tuple[int, float | None]` (count and mean; `(0, None)` when empty; drops samples that have aged out) and `__len__()`.

- [ ] **Step 1: Ignore the venv and bytecode**

Append to `.gitignore`:

```
# Setpoint server (Python)
server/.venv/
server/**/__pycache__/
```

- [ ] **Step 2: Commit the spec, this plan and the ignore rules**

```bash
git add .gitignore docs/superpowers/specs/2026-09-30-setpoint-server-design.md docs/superpowers/plans/2026-09-30-setpoint-server.md
git commit -m "$(cat <<'EOF'
Add design spec and plan for the setpoint server

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Write the failing averager tests**

Create `server/tests/test_averager.py`:

```python
import unittest

from setpoint_server.averager import WindowAverager


class WindowAveragerTest(unittest.TestCase):
    def setUp(self):
        self.avg = WindowAverager(1)

    def test_empty_window(self):
        self.assertEqual(self.avg.average(5.0), (0, None))

    def test_one_sample(self):
        self.avg.add(10.0, 8000.0)
        self.assertEqual(self.avg.average(10.5), (1, 8000.0))

    def test_mean_of_several_samples(self):
        for t, w in [(10.0, 100.0), (10.2, 200.0), (10.4, 600.0)]:
            self.avg.add(t, w)
        count, mean = self.avg.average(10.6)
        self.assertEqual(count, 3)
        self.assertAlmostEqual(mean, 300.0)

    def test_sample_exactly_one_window_old_is_excluded(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(11.0), (0, None))

    def test_sample_just_under_one_window_old_is_included(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(10.999), (1, 100.0))

    def test_sample_arriving_at_the_tick_is_included(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(10.0), (1, 100.0))

    def test_old_samples_age_out_and_are_dropped(self):
        self.avg.add(10.0, 100.0)
        self.avg.add(10.5, 300.0)
        self.assertEqual(len(self.avg), 2)
        self.assertEqual(self.avg.average(11.2), (1, 300.0))
        self.assertEqual(len(self.avg), 1)

    def test_long_gap_empties_the_window(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(60.0), (0, None))
        self.assertEqual(len(self.avg), 0)

    def test_each_sample_lands_in_exactly_one_tick(self):
        # 4 Hz samples (0.25 s is exact in binary), a tick every 1.0 s.
        counts = []
        for i in range(1, 41):
            t = i * 0.25
            self.avg.add(t, 100.0)
            if t == int(t):
                counts.append(self.avg.average(t)[0])
        self.assertEqual(counts, [4] * 10)

    def test_memory_does_not_grow_without_bound(self):
        for i in range(1, 5001):
            t = i * 0.2
            self.avg.add(t, 100.0)
            if i % 5 == 0:
                self.avg.average(t)
        self.assertLessEqual(len(self.avg), 6)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd server && python3 -m unittest discover -s tests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'setpoint_server'`.

- [ ] **Step 5: Write the package, config and averager**

Create `server/setpoint_server/__init__.py` (one line):

```python
"""Averages the board's CP setpoint over 1 s and republishes it for the Fronius."""
```

Create `server/setpoint_server/config.py`:

```python
"""Constants for the setpoint server, in the style of the firmware's mqtt_config.h.

The broker is Mosquitto on the same computer, so the server reaches it over
loopback; the board reaches the same broker at 192.168.21.137:1883. When the
broker moves, only BROKER_HOST changes (or --host on the command line).

No credentials: stage one is an unauthenticated broker on the laboratory LAN.
"""

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
CLIENT_ID = "evsim-setpoint-server"

TOPIC_CP = "evsim/cp"                # in: the board's telemetry, QoS 0
TOPIC_SETPOINT = "evsim/setpoint"    # out: the averaged setpoint, QoS 1, retained

WINDOW_S = 1                         # averaging window, and the tick period
MAX_SETPOINT_W = 11000               # the charger's 11 kW rating; a larger setpoint_w is dropped, not clamped
KEEPALIVE_S = 10
RECONNECT_MIN_S = 1                  # paho's backoff toward an unreachable broker
RECONNECT_MAX_S = 5
SHUTDOWN_PUBLISH_TIMEOUT_S = 2       # how long a clean stop waits for the broker's ack
```

Create `server/setpoint_server/averager.py`:

```python
"""Sliding-window mean of the setpoints that arrived recently.

Pure logic with no network and no clock of its own: the caller passes the time,
so the tests can drive any timeline. The window is half-open, (now - window, now]:
a sample exactly one window old is out, one that arrived at `now` is in. With a
tick every window, that puts each sample in exactly one tick's average.
"""

from collections import deque


class WindowAverager:
    def __init__(self, window_s):
        self._window_s = window_s
        self._samples = deque()  # (arrival, watts), oldest first

    def add(self, now, watts):
        """Record a sample that arrived at `now`. Arrival times must not go backwards."""
        self._samples.append((now, watts))

    def average(self, now):
        """Return (count, mean) of the samples younger than the window.

        The mean is None when the count is 0. Samples that have aged out are
        dropped, so memory stays bounded.
        """
        while self._samples and now - self._samples[0][0] >= self._window_s:
            self._samples.popleft()
        count = len(self._samples)
        if count == 0:
            return 0, None
        return count, sum(watts for _, watts in self._samples) / count

    def __len__(self):
        return len(self._samples)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd server && python3 -m unittest discover -s tests -v`
Expected: 10 tests, all `ok`.

- [ ] **Step 7: Commit**

```bash
git add server/setpoint_server/__init__.py server/setpoint_server/config.py server/setpoint_server/averager.py server/tests/test_averager.py
git commit -m "$(cat <<'EOF'
Add the setpoint server's config and sliding-window averager

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Message parsing and formatting

**Files:**
- Create: `server/setpoint_server/messages.py`
- Test: `server/tests/test_messages.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure functions).
- Produces:
  - `parse_cp(payload: bytes | str) -> float | None`: the `setpoint_w` of an `evsim/cp` message, or `None` for anything unusable.
  - `format_setpoint(count: int, mean: float | None, window_s: int, now_utc: datetime) -> str`: the output JSON for `ok` (count > 0) or `no_data` (count == 0).
  - `offline_payload(window_s: int) -> str`: the `server_offline` JSON, with no `ts`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_messages.py`:

```python
import json
import unittest
from datetime import datetime, timedelta, timezone

from setpoint_server.messages import format_setpoint, offline_payload, parse_cp

# A real message, as defined in the CP telemetry spec.
REAL_CP = (
    '{"t":123456,"freq":1000.0,"duty":24.30,"adc":0,"cp":"PWM_VALID","state":"C",'
    '"amps":14.58,"watts":10060,"setpoint_w":10060,"status":"live","age_ms":0}'
)
NOW = datetime(2026, 9, 30, 13, 42, 7, 123456, tzinfo=timezone.utc)


class ParseCpTest(unittest.TestCase):
    def test_reads_setpoint_from_a_real_message(self):
        self.assertEqual(parse_cp(REAL_CP), 10060.0)

    def test_accepts_bytes_as_paho_delivers_them(self):
        self.assertEqual(parse_cp(REAL_CP.encode()), 10060.0)

    def test_accepts_zero_and_fractions(self):
        self.assertEqual(parse_cp(b'{"setpoint_w":0}'), 0.0)
        self.assertEqual(parse_cp(b'{"setpoint_w":10060.5}'), 10060.5)

    def test_the_cap_itself_is_accepted(self):
        self.assertEqual(parse_cp(b'{"setpoint_w":11000}'), 11000.0)

    def test_rejects_unusable_messages(self):
        bad = {
            "not json": b"not json",
            "empty": b"",
            "a list": b"[1,2]",
            "no setpoint": b'{"t":1}',
            "null": b'{"setpoint_w":null}',
            "a string": b'{"setpoint_w":"10060"}',
            "a boolean": b'{"setpoint_w":true}',
            "NaN": b'{"setpoint_w":NaN}',
            "Infinity": b'{"setpoint_w":Infinity}',
            "negative": b'{"setpoint_w":-1}',
            "just above the cap": b'{"setpoint_w":11000.5}',
            "far above the cap": b'{"setpoint_w":1000000}',
            "integer too large for a float": b'{"setpoint_w":1' + b"0" * 400 + b"}",
            "not UTF-8": b"\xff\xfe\x00\xff",
            "deeply nested": b"[" * 100000,
        }
        for name, payload in bad.items():
            with self.subTest(name):
                self.assertIsNone(parse_cp(payload))


class FormatSetpointTest(unittest.TestCase):
    def test_ok_message_matches_the_contract_exactly(self):
        self.assertEqual(
            format_setpoint(5, 10060.0, 1, NOW),
            '{"setpoint_w":10060,"samples":5,"window_s":1,"status":"ok",'
            '"ts":"2026-09-30T13:42:07Z"}',
        )

    def test_half_rounds_up(self):
        cases = [(0.5, 1), (1.5, 2), (2.5, 3), (10060.49, 10060), (10060.5, 10061)]
        for mean, expected in cases:
            with self.subTest(mean=mean):
                out = json.loads(format_setpoint(1, mean, 1, NOW))
                self.assertEqual(out["setpoint_w"], expected)

    def test_no_data_message_is_zero(self):
        self.assertEqual(
            format_setpoint(0, None, 1, NOW),
            '{"setpoint_w":0,"samples":0,"window_s":1,"status":"no_data",'
            '"ts":"2026-09-30T13:42:07Z"}',
        )

    def test_ts_is_converted_to_utc(self):
        local = datetime(2026, 9, 30, 15, 42, 7, tzinfo=timezone(timedelta(hours=2)))
        out = json.loads(format_setpoint(1, 100.0, 1, local))
        self.assertEqual(out["ts"], "2026-09-30T13:42:07Z")


class OfflinePayloadTest(unittest.TestCase):
    def test_matches_the_contract_and_has_no_ts(self):
        self.assertEqual(
            offline_payload(1),
            '{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}',
        )
        self.assertNotIn("ts", json.loads(offline_payload(1)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && python3 -m unittest tests.test_messages -v`
Expected: FAIL with `ImportError: cannot import name 'format_setpoint'` (or `ModuleNotFoundError: No module named 'setpoint_server.messages'`).

- [ ] **Step 3: Write the implementation**

Create `server/setpoint_server/messages.py`:

```python
"""Reads the board's evsim/cp messages and writes the evsim/setpoint ones.

Pure functions, no network. See
docs/superpowers/specs/2026-09-30-setpoint-server-design.md for the contract.
"""

import json
import math
from datetime import timezone

from .config import MAX_SETPOINT_W


def parse_cp(payload):
    """Return the `setpoint_w` of an evsim/cp payload as a float, or None.

    None means the message is unusable: not JSON, not an object, no numeric
    `setpoint_w`, or a value that is not finite, is negative, or is above
    MAX_SETPOINT_W. Such a value is dropped, never clamped. It never raises.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("setpoint_w")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or not 0 <= value <= MAX_SETPOINT_W:
            return None
        return float(value)
    except OverflowError:  # an integer too large to convert to a float
        return None


def _dump(fields):
    return json.dumps(fields, separators=(",", ":"))


def format_setpoint(count, mean, window_s, now_utc):
    """The evsim/setpoint message for one tick.

    `ok` with the rounded mean when `count` > 0, otherwise `no_data` and 0 W.
    `now_utc` must be a timezone-aware datetime.
    """
    if count > 0:
        status, setpoint_w = "ok", math.floor(mean + 0.5)  # half rounds up
    else:
        status, setpoint_w = "no_data", 0
    return _dump({
        "setpoint_w": setpoint_w,
        "samples": count,
        "window_s": window_s,
        "status": status,
        "ts": now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def offline_payload(window_s):
    """The `server_offline` message, used as the last-will and on a clean stop.

    It has no `ts`: the last-will is fixed when the client connects.
    """
    return _dump({
        "setpoint_w": 0,
        "samples": 0,
        "window_s": window_s,
        "status": "server_offline",
    })
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server && python3 -m unittest discover -s tests -v`
Expected: all tests in both files `ok` (10 averager, 10 messages).

- [ ] **Step 5: Commit**

```bash
git add server/setpoint_server/messages.py server/tests/test_messages.py
git commit -m "$(cat <<'EOF'
Add the setpoint server's message parsing and formatting

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The MQTT glue, tested with a fake client

**Files:**
- Create: `server/setpoint_server/server.py`
- Test: `server/tests/test_server.py`

`SetpointServer` never imports paho: it takes a client object with the paho methods it uses, so this task needs no venv. (This test file is an addition to the spec's two test files; it pins the connection, shutdown and disconnected-publish rules without a broker.)

**Interfaces:**
- Consumes: `config` constants and `WindowAverager` (Task 1); `parse_cp`, `format_setpoint`, `offline_payload` (Task 2).
- Produces: `SetpointServer(client, clock=time.monotonic, utcnow=<UTC now>)` with:
  - `tick()`: average the window and publish once.
  - `run()`: tick once per `config.WINDOW_S` until `request_stop()`, then `shutdown()`.
  - `request_stop()`: safe to call from a signal handler.
  - `shutdown()`: publish `server_offline` and wait for the ack (only when connected), then `client.disconnect()`.
  - Registers `client.on_connect`, `client.on_message`, `client.on_disconnect` (paho 2.x callback signatures) and calls `client.will_set(...)`.
  - The client must provide `is_connected()`, `will_set()`, `publish()` (returning an object with `.rc` and `.wait_for_publish(timeout)`), `subscribe()` and `disconnect()`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_server.py`:

```python
import logging
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from setpoint_server import config
from setpoint_server.messages import format_setpoint, offline_payload
from setpoint_server.server import SetpointServer

NOW = datetime(2026, 9, 30, 13, 42, 7, tzinfo=timezone.utc)
OK = SimpleNamespace(is_failure=False)
REFUSED = SimpleNamespace(is_failure=True)


class FakeInfo:
    def __init__(self, fail_wait=False):
        self.rc = 0
        self.waited = None
        self._fail_wait = fail_wait

    def wait_for_publish(self, timeout=None):
        self.waited = timeout
        if self._fail_wait:
            raise RuntimeError("Message publish failed")


class FakeClient:
    """Stands in for paho's client: records what the server asks of it."""

    def __init__(self, connected=True, fail_wait=False):
        self.connected = connected
        self.fail_wait = fail_wait
        self.published = []    # (topic, payload, qos, retain)
        self.infos = []
        self.subscribed = []   # (topic, qos)
        self.will = None
        self.disconnected = False
        self.on_connect = self.on_message = self.on_disconnect = None

    def is_connected(self):
        return self.connected

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        info = FakeInfo(self.fail_wait)
        self.infos.append(info)
        return info

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def disconnect(self):
        self.disconnected = True


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def message(payload):
    return SimpleNamespace(topic=config.TOPIC_CP, payload=payload)


class SetpointServerTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.client = FakeClient()
        self.clock = FakeClock()
        self.server = SetpointServer(self.client, clock=self.clock, utcnow=lambda: NOW)

    def feed(self, payload):
        self.client.on_message(self.client, None, message(payload))

    def test_registers_the_last_will(self):
        self.assertEqual(
            self.client.will,
            (config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), 1, True),
        )

    def test_subscribes_on_every_connect(self):
        self.client.on_connect(self.client, None, {}, OK, None)
        self.client.on_connect(self.client, None, {}, OK, None)  # a reconnect
        self.assertEqual(self.client.subscribed, [(config.TOPIC_CP, 0)] * 2)

    def test_a_refused_connect_does_not_subscribe(self):
        self.client.on_connect(self.client, None, {}, REFUSED, None)
        self.assertEqual(self.client.subscribed, [])

    def test_tick_publishes_the_mean_retained_at_qos1(self):
        self.feed(b'{"setpoint_w":1000}')
        self.clock.t = 100.2
        self.feed(b'{"setpoint_w":2000}')
        self.clock.t = 100.4
        self.server.tick()
        self.assertEqual(
            self.client.published,
            [(config.TOPIC_SETPOINT, format_setpoint(2, 1500.0, config.WINDOW_S, NOW), 1, True)],
        )

    def test_tick_with_no_samples_publishes_no_data(self):
        self.server.tick()
        self.assertEqual(
            self.client.published,
            [(config.TOPIC_SETPOINT, format_setpoint(0, None, config.WINDOW_S, NOW), 1, True)],
        )

    def test_old_samples_do_not_leak_into_a_later_tick(self):
        self.feed(b'{"setpoint_w":1000}')
        self.clock.t = 105.0
        self.server.tick()
        self.assertIn('"status":"no_data"', self.client.published[-1][1])

    def test_unusable_messages_are_dropped(self):
        for payload in (b"garbage", b'{"setpoint_w":-5}', b'{"setpoint_w":"x"}',
                        b'{"setpoint_w":null}', b'{"setpoint_w":11001}'):
            self.feed(payload)
        self.feed(b'{"setpoint_w":700}')
        self.server.tick()
        self.assertEqual(
            self.client.published[-1][1],
            format_setpoint(1, 700.0, config.WINDOW_S, NOW),
        )

    def test_tick_publishes_nothing_while_disconnected(self):
        self.client.connected = False
        self.feed(b'{"setpoint_w":1000}')
        self.server.tick()
        self.assertEqual(self.client.published, [])

    def test_shutdown_publishes_offline_waits_and_disconnects(self):
        self.server.shutdown()
        self.assertEqual(
            self.client.published,
            [(config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), 1, True)],
        )
        self.assertEqual(self.client.infos[0].waited, config.SHUTDOWN_PUBLISH_TIMEOUT_S)
        self.assertTrue(self.client.disconnected)

    def test_shutdown_while_disconnected_neither_publishes_nor_waits(self):
        self.client.connected = False
        self.server.shutdown()
        self.assertEqual(self.client.published, [])
        self.assertTrue(self.client.disconnected)

    def test_shutdown_survives_a_failed_publish(self):
        client = FakeClient(fail_wait=True)
        SetpointServer(client, clock=self.clock, utcnow=lambda: NOW).shutdown()
        self.assertTrue(client.disconnected)

    def test_run_stops_when_asked_and_publishes_offline(self):
        thread = threading.Thread(target=self.server.run)
        thread.start()
        self.server.request_stop()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.client.published[-1][1], offline_payload(config.WINDOW_S))
        self.assertTrue(self.client.disconnected)


if __name__ == "__main__":
    unittest.main()
```

`test_run_stops_when_asked_and_publishes_offline` uses `self.server`, built with the fake clock. `run()` waits on a real `threading.Event`, so `request_stop()` ends it at once; the fake clock only feeds `tick()`, which the test never reaches.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && python3 -m unittest tests.test_server -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'setpoint_server.server'`.

- [ ] **Step 3: Write the implementation**

Create `server/setpoint_server/server.py`:

```python
"""The MQTT glue: feed evsim/cp into the averager, publish the mean once a second.

The client is handed in rather than built here, so the tests use a fake and
this module never imports paho. `__main__` builds the real one.

Threads: paho's network thread calls the on_* callbacks; the caller's thread
runs run(). One lock guards the averager, and both sides read the clock inside
it, so a sample is never stamped later than a tick that already ran.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from . import config
from .averager import WindowAverager
from .messages import format_setpoint, offline_payload, parse_cp

log = logging.getLogger("setpoint_server")


def _utcnow():
    return datetime.now(timezone.utc)


class SetpointServer:
    def __init__(self, client, clock=time.monotonic, utcnow=_utcnow):
        self._client = client
        self._clock = clock
        self._utcnow = utcnow
        self._averager = WindowAverager(config.WINDOW_S)
        self._lock = threading.Lock()
        self._stop = threading.Event()

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.will_set(
            config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), qos=1, retain=True
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            return
        # Subscribing here, not once at startup, renews it on every reconnect.
        client.subscribe(config.TOPIC_CP, qos=0)
        log.info("connected, subscribed to %s", config.TOPIC_CP)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        log.warning("disconnected from the broker: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        watts = parse_cp(msg.payload)
        if watts is None:
            log.warning("dropping unusable %s message: %r", msg.topic, bytes(msg.payload[:80]))
            return
        with self._lock:
            self._averager.add(self._clock(), watts)

    def tick(self):
        """Average the window and publish the result once."""
        with self._lock:
            count, mean = self._averager.average(self._clock())
        payload = format_setpoint(count, mean, config.WINDOW_S, self._utcnow())
        if not self._client.is_connected():
            # Publishing now would make paho queue it and replay every stale
            # setpoint in a burst on reconnect.
            log.warning("broker not connected; skipping %s", payload)
            return
        info = self._client.publish(config.TOPIC_SETPOINT, payload, qos=1, retain=True)
        if info.rc != 0:
            log.warning("publish failed (rc=%s): %s", info.rc, payload)
            return
        log.info("published %s", payload)

    def run(self):
        """Tick once per window until request_stop(), then shut down cleanly."""
        period = config.WINDOW_S
        next_tick = self._clock() + period
        while not self._stop.wait(max(0.0, next_tick - self._clock())):
            self.tick()
            next_tick += period  # scheduled, not slept, so a slow publish adds no drift
            now = self._clock()
            if next_tick <= now:  # fell behind: skip the missed ticks, do not burst
                next_tick = now + period
        self.shutdown()

    def request_stop(self):
        """Ask run() to finish. Safe to call from a signal handler."""
        self._stop.set()

    def shutdown(self):
        """Publish server_offline ourselves, then disconnect.

        A broker sends the last-will only after an unclean disconnect, so without
        this a normal stop would leave the last setpoint retained. When the broker
        is unreachable there is nothing to wait for, so do not.
        """
        if self._client.is_connected():
            try:
                info = self._client.publish(
                    config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), qos=1, retain=True
                )
                info.wait_for_publish(timeout=config.SHUTDOWN_PUBLISH_TIMEOUT_S)
                log.info("published server_offline")
            except (RuntimeError, ValueError) as exc:
                log.warning("could not publish server_offline: %s", exc)
        self._client.disconnect()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server && python3 -m unittest discover -s tests -v`
Expected: all tests `ok`, including the 12 in `test_server`.

- [ ] **Step 5: Commit**

```bash
git add server/setpoint_server/server.py server/tests/test_server.py
git commit -m "$(cat <<'EOF'
Add the setpoint server's MQTT glue, tested with a fake client

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: The entry point, the venv and a smoke run

**Files:**
- Create: `server/requirements.txt`
- Create: `server/setpoint_server/__main__.py`
- Create: `server/README.md`

**Interfaces:**
- Consumes: `SetpointServer` (Task 3) and the `config` constants (Task 1).
- Produces: the command `python -m setpoint_server [--host H] [--port P]`, run from `server/`. Exits 0 on Ctrl-C or SIGTERM after publishing `server_offline`.

- [ ] **Step 1: Write the requirements and create the venv**

Create `server/requirements.txt`:

```
paho-mqtt>=2.0,<3
```

Run:

```bash
cd server && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Expected: `Successfully installed paho-mqtt-2.x`. If `pip` cannot build or find a wheel for this Python (3.14 is a release candidate here), stop and report the error instead of pinning another Python silently.

- [ ] **Step 2: Write the entry point**

Create `server/setpoint_server/__main__.py`:

```python
"""Entry point: python -m setpoint_server [--host H] [--port P]"""

import argparse
import logging
import signal
import sys

import paho.mqtt.client as mqtt

from . import config
from .server import SetpointServer


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m setpoint_server",
        description="Average the board's evsim/cp setpoint over 1 s and publish it on evsim/setpoint.",
    )
    parser.add_argument("--host", default=config.BROKER_HOST, help="broker address (default %(default)s)")
    parser.add_argument("--port", type=int, default=config.BROKER_PORT, help="broker port (default %(default)s)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.CLIENT_ID)
    client.reconnect_delay_set(min_delay=config.RECONNECT_MIN_S, max_delay=config.RECONNECT_MAX_S)
    server = SetpointServer(client)

    signal.signal(signal.SIGINT, lambda signum, frame: server.request_stop())
    signal.signal(signal.SIGTERM, lambda signum, frame: server.request_stop())

    # connect_async + loop_start: an unreachable broker at startup is retried in
    # the background instead of raising, and the loop reconnects after a drop.
    client.connect_async(args.host, args.port, keepalive=config.KEEPALIVE_S)
    client.loop_start()
    try:
        server.run()
    finally:
        client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Smoke-test with no broker running**

Run (port 18999 has nothing listening):

```bash
cd server && .venv/bin/python -m setpoint_server --help
log=$(mktemp)
.venv/bin/python -u -m setpoint_server --port 18999 > "$log" 2>&1 &
pid=$!; sleep 3.5; kill -TERM $pid; wait $pid; echo "exit=$?"; cat "$log"; rm "$log"
```

Run the three commands in one shell call, since `$log` and `$pid` do not survive between calls.

Expected: `--help` prints usage. The run prints `exit=0`, and the log shows `broker not connected; skipping {"setpoint_w":0,...,"status":"no_data",...}` about three times and no traceback. That proves an unreachable broker at startup is survived and SIGTERM stops the server cleanly.

- [ ] **Step 4: Write the README**

Create `server/README.md`:

```markdown
# Setpoint server

Subscribes to the board's `evsim/cp`, averages `setpoint_w` over 1 s, and publishes
the result on `evsim/setpoint` (QoS 1, retained) for the Fronius inverter.

Design: `docs/superpowers/specs/2026-09-30-setpoint-server-design.md`.

## Set up (once)

    cd server
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

## Run

Mosquitto must be running with the lab config (`tools/mosquitto-lab.conf`) so the board can reach it.

    cd server
    .venv/bin/python -m setpoint_server            # broker on 127.0.0.1:1883
    .venv/bin/python -m setpoint_server --host 192.168.21.137 --port 1883

Stop it with Ctrl-C. It publishes `server_offline` on the way out.

Watch the output:

    mosquitto_sub -v -t evsim/setpoint

## Test

    cd server
    .venv/bin/python -m unittest discover -s tests -v

The bench tests (`tests/test_bench.py`) start their own Mosquitto on a free port and take about a minute.
They are skipped when `mosquitto` or `paho-mqtt` is missing.
```

- [ ] **Step 5: Commit**

```bash
git add server/requirements.txt server/setpoint_server/__main__.py server/README.md
git commit -m "$(cat <<'EOF'
Add the setpoint server's entry point and README

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Bench tests against a real Mosquitto, and the project note

**Files:**
- Create: `server/tests/test_bench.py`
- Create: `memory/evsim-setpoint-server.md`
- Modify: `memory/MEMORY.md`

**Interfaces:**
- Consumes: the running server from Task 4 (`python -m setpoint_server --port P`), started as a subprocess with the venv's interpreter.
- Produces: an automated version of the spec's bench list. Each test starts a private Mosquitto on a free loopback port, so it never touches the lab broker.

- [ ] **Step 1: Write the bench tests**

Create `server/tests/test_bench.py`:

```python
"""End-to-end checks of the spec's bench list, against a private Mosquitto.

Each test starts its own broker on a free loopback port and its own server as a
subprocess, then fakes the board with a paho publisher on evsim/cp and watches
evsim/setpoint. Skipped when mosquitto or paho-mqtt is not available.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

MOSQUITTO = shutil.which("mosquitto") or (
    "/opt/homebrew/sbin/mosquitto" if os.path.exists("/opt/homebrew/sbin/mosquitto") else None
)
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPIC_CP = "evsim/cp"
TOPIC_SETPOINT = "evsim/setpoint"
READY = "subscribed to evsim/cp"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def new_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    return client


class Watcher:
    """Records every evsim/setpoint message with its arrival time and retain flag."""

    def __init__(self, port):
        self._lock = threading.Lock()
        self._messages = []  # (monotonic time, retain, decoded JSON)
        self._client = new_client()
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(TOPIC_SETPOINT, qos=1)
        self._client.on_message = self._on_message
        self._client.connect_async("127.0.0.1", port)
        self._client.loop_start()

    def _on_message(self, client, userdata, msg):
        with self._lock:
            self._messages.append((time.monotonic(), msg.retain, json.loads(msg.payload)))

    def snapshot(self):
        with self._lock:
            return list(self._messages)

    def last(self):
        messages = self.snapshot()
        return messages[-1][2] if messages else None

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.snapshot()):
                return True
            time.sleep(0.1)
        return predicate(self.snapshot())

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()


class Feeder:
    """Plays the board: publishes evsim/cp messages."""

    def __init__(self, port):
        self._client = new_client()
        self._client.connect_async("127.0.0.1", port)
        self._client.loop_start()

    def send(self, payload):
        self._client.publish(TOPIC_CP, payload, qos=0)

    def feed(self, watts, seconds, hz=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.send(json.dumps({"setpoint_w": watts}))
            time.sleep(1.0 / hz)

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()


@unittest.skipUnless(MOSQUITTO and mqtt, "needs mosquitto and paho-mqtt")
class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.port = free_port()
        self.logpath = os.path.join(self.tmp, "server.log")
        self.broker = self.server = self.watcher = self.feeder = None
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for helper in (self.watcher, self.feeder):
            if helper:
                helper.stop()
        for proc in (self.server, self.broker):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_broker(self):
        conf = os.path.join(self.tmp, "mosquitto.conf")
        with open(conf, "w") as f:
            f.write("listener %d 127.0.0.1\nallow_anonymous true\n" % self.port)
        self.broker = subprocess.Popen(
            [MOSQUITTO, "-c", conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.fail("the private Mosquitto did not start")

    def start_server(self):
        log = open(self.logpath, "a")
        self.addCleanup(log.close)
        self.server = subprocess.Popen(
            [sys.executable, "-u", "-m", "setpoint_server", "--port", str(self.port)],
            cwd=SERVER_DIR, stdout=log, stderr=log,
        )

    def start_clients(self):
        self.watcher = Watcher(self.port)
        self.feeder = Feeder(self.port)

    def log_text(self):
        with open(self.logpath) as f:
            return f.read()

    def wait_for_log(self, text, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.log_text():
                return True
            time.sleep(0.1)
        return text in self.log_text()

    def up(self):
        """Broker, clients and server running, and the server subscribed."""
        self.start_broker()
        self.start_clients()
        self.start_server()
        self.assertTrue(self.wait_for_log(READY, 10), self.log_text())

    def status_is(self, status):
        return lambda messages: bool(messages) and messages[-1][2]["status"] == status

    # -- the spec's bench list ------------------------------------------------

    def test_steady_input_gives_an_ok_average(self):
        self.up()
        self.feeder.feed(10000, 3.5)
        last = self.watcher.last()
        self.assertEqual(last["status"], "ok")
        self.assertEqual(last["setpoint_w"], 10000)
        self.assertTrue(3 <= last["samples"] <= 6, last)

    def test_silence_gives_no_data_within_a_couple_of_seconds(self):
        self.up()
        self.feeder.feed(10000, 2.0)
        self.assertTrue(self.watcher.wait_for(self.status_is("ok"), 3))
        self.assertTrue(self.watcher.wait_for(self.status_is("no_data"), 3.5))
        last = self.watcher.last()
        self.assertEqual((last["setpoint_w"], last["samples"]), (0, 0))

    def test_a_step_to_zero_ramps_down_within_two_seconds(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        step = time.monotonic()
        self.feeder.feed(0, 3.0)
        after = [(t, p["setpoint_w"]) for t, _, p in self.watcher.snapshot() if t > step]
        values = [w for _, w in after]
        self.assertEqual(values[-1], 0)
        self.assertEqual(values, sorted(values, reverse=True))
        first_zero = next(t for t, w in after if w == 0)
        self.assertLess(first_zero - step, 2.5)

    def test_sigkill_makes_the_broker_publish_the_last_will(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        self.server.kill()
        self.assertTrue(self.watcher.wait_for(self.status_is("server_offline"), 3))
        self.assertNotIn("ts", self.watcher.last())

    def test_sigterm_makes_the_server_publish_offline_itself(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        self.server.terminate()
        self.assertEqual(self.server.wait(timeout=5), 0)
        self.assertTrue(self.watcher.wait_for(self.status_is("server_offline"), 3))
        self.assertIn("published server_offline", self.log_text())

    def test_unusable_messages_are_ignored_and_the_server_survives(self):
        self.up()
        bad = [b"garbage", b'{"setpoint_w":-5}', b'{"setpoint_w":"x"}', b'{"setpoint_w":null}',
               b'{"setpoint_w":11001}']
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            for payload in bad:
                self.feeder.send(payload)
            self.feeder.send(json.dumps({"setpoint_w": 10000}))
            time.sleep(0.2)
        last = self.watcher.last()
        self.assertEqual((last["status"], last["setpoint_w"]), ("ok", 10000))
        self.assertIsNone(self.server.poll())

    def test_a_late_subscriber_gets_the_retained_value_at_once(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        late = Watcher(self.port)
        self.addCleanup(late.stop)
        self.assertTrue(late.wait_for(lambda m: len(m) > 0, 2))
        _, retained, payload = late.snapshot()[0]
        self.assertTrue(retained)
        self.assertIn(payload["status"], ("ok", "no_data"))

    def test_the_server_recovers_from_a_broker_restart(self):
        self.up()
        self.feeder.feed(10000, 2.0)
        self.broker.kill()
        self.broker.wait(timeout=5)
        time.sleep(1.5)
        self.start_broker()
        before = len(self.watcher.snapshot())
        deadline = time.monotonic() + 20
        recovered = False
        while time.monotonic() < deadline and not recovered:
            self.feeder.send(json.dumps({"setpoint_w": 10000}))
            time.sleep(0.2)
            recovered = any(
                p["status"] == "ok" and p["setpoint_w"] == 10000
                for _, _, p in self.watcher.snapshot()[before:]
            )
        self.assertTrue(recovered, self.log_text())
        self.assertIsNone(self.server.poll())

    def test_the_server_can_start_before_the_broker_exists(self):
        self.start_server()
        time.sleep(2.0)
        self.assertIsNone(self.server.poll(), self.log_text())
        self.start_broker()
        self.start_clients()
        self.assertTrue(self.wait_for_log(READY, 20), self.log_text())
        self.feeder.feed(10000, 3.0)
        last = self.watcher.last()
        self.assertEqual((last["status"], last["setpoint_w"]), ("ok", 10000))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the bench tests**

Run: `cd server && .venv/bin/python -m unittest tests.test_bench -v`
Expected: 9 tests, all `ok`, in about a minute. If one fails, read the server log it prints (`self.log_text()` is the assertion message where it applies) before changing anything: a failure here is a real finding about the server or a timing margin, and the spec is the arbiter. Do not loosen an assertion to make it pass without saying so.

- [ ] **Step 3: Run the whole suite in the venv**

Run: `cd server && .venv/bin/python -m unittest discover -s tests -v`
Expected: every test `ok` (averager 10, messages 10, server 12, bench 9).

- [ ] **Step 4: Watch it once by hand against the lab broker, then with the board**

This step needs the lab Mosquitto (`tools/mosquitto-lab.conf`) and, for part B, the board and charger on the bench. Skip part B if the hardware is not available, and say so in the report.

Part A, with no board (a fake feed):

```bash
# terminal 1: the server, against the lab broker on 127.0.0.1:1883
cd server && .venv/bin/python -m setpoint_server
# terminal 2: watch the output
mosquitto_sub -v -t evsim/setpoint
# terminal 3: fake the board at 5 Hz for a few seconds
for i in $(seq 1 20); do mosquitto_pub -t evsim/cp -m '{"setpoint_w":10060}'; sleep 0.2; done
```

Expected in terminal 2: one line per second, `ok` with `setpoint_w` 10060 and `samples` near 5 while the feed runs, then `no_data` with 0 within about 2 s after it stops.

Part B, with the board: state C, the charger offering current, the server running with the board publishing `evsim/cp`. Expected: `setpoint_w` follows the AMTRON's offer (duty × the 690 V the board uses for watts), and through the charger's 6–12 s gaps `held` samples keep the average steady instead of dropping to 0.

- [ ] **Step 5: Record the server in the project notes**

Read `memory/MEMORY.md` first and follow its index format. Create `memory/evsim-setpoint-server.md`:

```markdown
---
name: evsim-setpoint-server
description: EVSim setpoint server (server/) - averages evsim/cp setpoint_w over 1 s and publishes JSON on evsim/setpoint for the Fronius; runs on the dev computer
metadata:
  type: project
---

A Python server in `server/` on the development computer (192.168.21.137). It subscribes to the
board's `evsim/cp`, takes the mean of `setpoint_w` over the last 1 s, and publishes
`{"setpoint_w":...,"samples":...,"window_s":1,"status":"ok|no_data|server_offline","ts":...}` on
`evsim/setpoint` (QoS 1, retained) once per second. No data in the window means 0 W, never the last
value. Design: `docs/superpowers/specs/2026-09-30-setpoint-server-design.md`.

**Why:** the board publishes raw samples and leaves smoothing to the consumer
([[evsim-tb-overview]]); this server is that consumer, in the "separate server" form.

**How to apply:** it only puts a setpoint on the broker. Whether the GEN24 needs an MQTT -> Modbus
bridge to act on it is still open ([[evsim-inverter-fronius]]). The averaging is a plain 1 s mean by
design; anything smoother is a later change here, not on the board.
```

Add one line to `memory/MEMORY.md`, in that file's own format:

```
- [EVSim setpoint server](evsim-setpoint-server.md) — server/ averages evsim/cp over 1 s, publishes evsim/setpoint for the Fronius
```

- [ ] **Step 6: Commit**

```bash
git add server/tests/test_bench.py memory/evsim-setpoint-server.md memory/MEMORY.md
git commit -m "$(cat <<'EOF'
Add end-to-end bench tests for the setpoint server and record it in the notes

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review

- **Spec coverage:** contract and status rules (Tasks 2, 3); windowing and its boundary (Task 1); components and layout (Tasks 1–4; `test_server.py` is the one addition to the spec's file list); connection, client ID, clean session, resubscribe on reconnect, last-will and clean shutdown (Tasks 3, 4); every failure-handling bullet (Tasks 2, 3, 5); the spec's bench list and its real-board check (Task 5, steps 2 and 4); out-of-scope items are not built.
- **Placeholders:** none. Every step has its code or exact command.
- **Type consistency:** `WindowAverager.add/average/__len__`, `parse_cp`, `format_setpoint(count, mean, window_s, now_utc)`, `offline_payload(window_s)`, `SetpointServer.tick/run/request_stop/shutdown` and the `config` names are used with the same signatures in every task.
- **Review Focus:** items 1 and 2 are pinned by `test_rejects_unusable_messages`; item 3 by `test_tick_publishes_nothing_while_disconnected`; item 4 by `test_shutdown_while_disconnected_neither_publishes_nor_waits`; item 5 by `test_subscribes_on_every_connect`, `test_the_server_recovers_from_a_broker_restart` and `test_the_server_can_start_before_the_broker_exists`.
