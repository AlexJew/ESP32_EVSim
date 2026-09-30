# Setpoint Server — Design

**Date:** 2026-09-30
**Status:** awaiting review
**Scope:** a small server that runs on the development computer (192.168.21.137), subscribes to the
board's `evsim/cp` telemetry, averages the power setpoint over a 1 s window, and publishes the result
on MQTT as the setpoint for the Fronius inverter. Builds on
[2026-09-29-mqtt-cp-telemetry-design.md](2026-09-29-mqtt-cp-telemetry-design.md), which left the
consumer of the stream undecided; this design is that consumer, in the "separate server" form.

## Purpose

The thesis loop is AMTRON → ESP32 → Fronius GEN24. The board publishes one `evsim/cp` message every
200 ms, carrying a held setpoint (`setpoint_w`) that already survives the charger's periodic gaps. The
board does no smoothing: it publishes unfiltered samples and leaves averaging to the consumer, where it
can change without reflashing.

This server does the averaging and republishes a single setpoint value on its own topic. The setpoint
means the **load, in watts, that the inverter should take out**.

First implementation: a plain average over the last 1 s (about five samples at the board's 5 Hz) and
nothing else. No filtering beyond that, no ramp limiting, no Modbus or REST toward the Fronius. The
GEN24 is not known to accept an MQTT setpoint (see `memory/evsim-inverter-fronius.md`), so whatever
consumes `evsim/setpoint` on the inverter side is a later step. This server only puts a well-defined
value on the broker.

## Message contract

**Input** — `evsim/cp`, QoS 0, as defined in the telemetry spec. The server reads only `setpoint_w`.
The board's `status`, `t` and the other fields are ignored: the board has already applied its hold
rules to `setpoint_w`, and `t` is uptime, not a wall clock.

A `setpoint_w` above **12 000 W** is unusable. The cap is the inverter's 12 kW rating (it started at the
charger's 11 kW, but a full 16 A offer is about 11.2 kW on the board's 690 V conversion, which that cap would
have dropped), so a larger value is treated as a corrupt or foreign message. The server drops it (see Failure
handling) rather than clamping it to 12 000 W, so it never publishes a load that no one offered. The cap is a
named constant in `config.py`.

**Output** — topic `evsim/setpoint`, **QoS 0**, **retained**, one message per second. QoS 0 is
deliberate, and was QoS 1 in the first draft: paho queues a QoS 1 publish it cannot send yet, or one
still unacknowledged when the link drops, and replays it after reconnecting. With the retained flag
that puts a stale pre-outage setpoint on the topic, the one thing this server must never do. paho
drops QoS 0 instead, and the next tick replaces the value within a second. The `server_offline`
message (last-will and clean stop) stays at QoS 1, since it is sent once and must arrive.

```json
{"setpoint_w":10060,"samples":5,"window_s":1,"status":"ok","ts":"2026-09-30T13:42:07Z"}
```

| Field | Meaning |
|---|---|
| `setpoint_w` | The mean of the `setpoint_w` values that arrived in the window, rounded to a whole watt (half rounds up). 0 when `status` is not `ok`. |
| `samples` | How many input samples were averaged. 0 when there were none. |
| `window_s` | The averaging window, 1. |
| `status` | `ok`, `no_data` or `server_offline`. |
| `ts` | Absent from `server_offline` messages, which are fixed in advance. Otherwise the computer's UTC time when the message was built, ISO 8601 with a `Z` suffix and whole seconds. The board has no wall clock, so this is the only real timestamp in the chain. A consumer can compare it with its own clock to spot a stale retained message. |

### Status rules

- **`ok`** — at least one sample arrived in the window. `setpoint_w` is their mean.
- **`no_data`** — no sample arrived in the window (board off, Wi-Fi down, `evsim/cp` silent, broker
  just reconnected). `setpoint_w` is 0. A dead upstream must never leave the inverter loading at the
  last value.
- **`server_offline`** — published by the server as it exits, and by the broker as the last-will if
  the server vanishes. `setpoint_w` is 0 and `samples` 0.

### Windowing

The window is the 1.0 s before each tick, in **server arrival time** on a monotonic clock. A sample
counts if `now - arrival < 1.0 s`. Arrival time is used because the board's `t` is only uptime, and
the network jitter between board and server is small against a 1 s window.

The mean is the plain arithmetic mean of the samples in the window. `held` and `zero` samples are
included exactly as the board sends them. Consequence: when the pilot leaves state C the board drops
to 0 W at once, and the average follows within one window, so the output ramps to 0 W over up to 1 s
instead of switching instantly. The output also lags the input by about 0.5 s on average.

## Design

### Components

Layout, under a new `server/` folder next to `EVSim/` and `tools/`:

```
server/
  requirements.txt              # paho-mqtt
  setpoint_server/
    __init__.py
    config.py                   # broker host/port, topics, window length
    averager.py                 # pure: the sliding-window mean
    messages.py                 # pure: parse an input message, format the output
    server.py                   # MQTT glue and the 1 s tick
    __main__.py                 # entry point: python -m setpoint_server
  tests/
    test_averager.py
    test_messages.py
```

- **`averager.py`** — pure logic, no network and no clock of its own. `add(now, watts)` records a
  sample; `average(now)` returns the count and mean of the samples younger than the window, and drops
  older ones. The caller passes the time, so tests can drive any timeline.
- **`messages.py`** — pure. `parse_cp(payload)` returns the `setpoint_w` as a number, or `None` for
  anything unusable. `format_setpoint(...)` builds the output JSON. Both are tested without a broker.
- **`config.py`** — constants, in the style of the firmware's `mqtt_config.h`: broker host and port,
  the topics, the window length, the client ID.
- **`server.py`** — connects, subscribes, feeds the averager from the MQTT thread, and publishes from
  a once-per-second tick. A lock guards the averager, since paho's network thread and the tick thread
  both touch it.

Only `paho-mqtt` is a dependency. It goes in a venv; nothing else in the repo needs to change. The
tests use the standard library's `unittest`, so they add no dependency of their own.

### Data flow

1. The MQTT thread receives an `evsim/cp` message, `parse_cp` extracts `setpoint_w`, and the sample is
   added to the averager with the arrival time.
2. Every second the tick asks the averager for the count and mean, builds the message with the right
   status, and publishes it retained at QoS 0.
3. The tick is scheduled against a monotonic clock, so a slow publish does not accumulate drift.

### Connection

The server connects to Mosquitto on the same computer at `127.0.0.1:1883`; `--host` and `--port`
override this for when the broker moves. The board reaches the same broker at `192.168.21.137:1883`,
which needs the lab config (`listener 1883`, `allow_anonymous true`) as described in the state-commands
spec. The server needs no extra listener setup.

- Client ID `evsim-setpoint-server`, fixed. A second copy started by mistake would take over the
  session and the first would drop off; that is acceptable, and preferable to two servers publishing
  different setpoints.
- Clean session. The subscription to `evsim/cp` is made in the connect callback, so it is renewed on
  every reconnect. paho's loop reconnects automatically.
- **Last-will:** on `evsim/setpoint`, retained, QoS 1, payload
  `{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}` with no `ts`, since the will is
  fixed when the client connects.
- **Clean shutdown** (Ctrl-C or SIGTERM): the server publishes the same `server_offline` message itself
  and then disconnects. This is needed because a broker sends the last-will only after an *unclean*
  disconnect; without it a normal stop would leave the last setpoint retained.

### Failure handling

- **Invalid JSON, `setpoint_w` missing, `null`, non-numeric, a boolean, not finite, negative, or above
  12 000 W:** the
  message is logged and dropped. It does not enter the average and does not stop the loop. A window
  made only of such messages is `no_data`.
- **A retained `evsim/cp` message** is ignored the same way. The board never retains it, so a retained
  copy (which the broker delivers on every subscribe, for example after a bench `mosquitto_pub -r`) is
  stale by definition and must not count as a live sample.
- **No samples in the window:** `no_data`, `setpoint_w` 0 (see the status rules).
- **Broker down or unreachable:** the tick keeps running; publishes that cannot be sent are logged and
  skipped, and paho retries the connection. On return the first tick is `no_data` until samples flow
  again.
- **A retained value from an earlier run** stays on the broker until the first tick after startup. It
  is at worst the previous run's last-will (a zero) or its last clean value; its `ts` shows its age.

## Testing

- **Unit tests, `averager`, on a fake clock:** an empty window; one sample; several samples; a sample
  exactly 1.0 s old (excluded) and one just under (included); old samples aging out and being dropped;
  a long gap emptying the window.
- **Unit tests, `messages`:** `parse_cp` accepts a real `evsim/cp` message from the telemetry spec and
  rejects each bad case in the failure list; `format_setpoint` produces every field, rounds half up,
  and gives the exact JSON for `ok`, `no_data` and `server_offline`; `ts` has the `Z` form.
- **Bench, with `mosquitto_pub` faking `evsim/cp` and `mosquitto_sub -v -t evsim/setpoint` watching:**
  steady input gives an `ok` average close to the input; stopping the input gives `no_data` and 0
  within a second; a step from 10 000 W to 0 ramps over about 1 s; killing the server with SIGKILL
  makes the broker publish `server_offline`, and Ctrl-C makes the server publish it itself; restarting
  Mosquitto and then the input recovers without restarting the server. Finally, with the real board:
  the values follow the AMTRON's offer, and `held` samples keep the average steady through the
  charger's 6–12 s gaps.

## Out of scope

- Any smoothing beyond the 1 s mean, ramp limiting, or a minimum-change filter. The board publishes the
  raw stream so this can be changed here without reflashing; a later design can add it.
- Modbus TCP or REST toward the Fronius, and any inverter-side consumer of `evsim/setpoint`. Whether
  the GEN24 needs an MQTT→Modbus bridge is still open.
- Authentication and TLS, as in the earlier designs. Revisit when the broker leaves the computer.
- Running the server as a service. It is started by hand (`python -m setpoint_server` from `server/`);
  a `launchd` job can come later if wanted.
- Compensating the low-voltage reader's small constant duty offset (recorded in the reader design).
