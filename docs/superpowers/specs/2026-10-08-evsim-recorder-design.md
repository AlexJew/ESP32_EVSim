# EVSim Recorder: Logging the Chain's Messages to SQLite — Design

**Date:** 2026-10-08
**Status:** draft, awaiting review
**Scope:** a standalone process that records every message of the EVSim MQTT chain, with arrival times to the nanosecond, into a SQLite file, so that the **propagation delay from a change of the charger's maximum current to the change of the current and power along the chain** (charger, board, setpoint server, inverter) can be characterized afterwards, for the system **as it is**. It adds no sampling, changes no existing component and has no interface. Builds on
[2026-09-29-mqtt-cp-telemetry-design.md](2026-09-29-mqtt-cp-telemetry-design.md) (`evsim/cp`),
[2026-09-30-setpoint-server-design.md](2026-09-30-setpoint-server-design.md) (`evsim/setpoint`) and
[2026-09-30-inverter-writer-design.md](2026-09-30-inverter-writer-design.md) (`evsim/inverter`).

## Why

The thesis chain turns a current limit written to the AMTRON into a load on the inverter: charger (pilot PWM) to board (`evsim/cp`, 5 Hz) to setpoint server (1 s average, `evsim/setpoint`) to inverter writer (cap, `evsim/inverter`, 1 Hz). How long each hop takes, for a 1 A step and for a large jump up or down, is what other users of this setup need to know. Today those messages only scroll past in `mosquitto_sub`. A recorder keeps them with one clock, so the delays can be read out of the data later.

## Decisions (from the user, 2026-10-08)

- **Time zero is the Modbus write** of the new maximum current to the charger. The user's **stepping script**, not the recorder, makes that write, and it **marks** it (below). The recorder never writes to the charger.
- **Full-resolution events**, not the graph's 1 Hz frames.
- **SQLite**, one file per run.
- **Characterize the current setup, do not improve it:** no faster sampling, no fast inverter reader, **no change to the setpoint server, the inverter writer, the board or the web page**.
- The recorder opens **no Modbus connection** and publishes nothing. The charger-side hops (the limit write to the charger's reaction, the AMTRON's own meter) are not measured by it; the first thing it sees move is the board's `evsim/cp`.

## Design

### The recorder

A new package `server/evsim_recorder/`, started with `python -m evsim_recorder --db PATH` (default `data/recordings/evsim-<UTC date and time>.sqlite`). It uses `paho-mqtt` and the standard library's `sqlite3`.

- **Subscribes to `evsim/#`** at QoS 0, on the broker at `127.0.0.1:1883` (`--host`, `--port`), with a client id of `evsim-recorder-` plus a random suffix (equal ids kick each other off the broker). Subscribing happens in `on_connect`, so it is renewed on every reconnect; the connection is asynchronous, so an absent broker never blocks start-up.
- **Stamps each message in the callback**, before anything else: `t_ns` (`time.time_ns()`, the computer's wall clock) and `mono_ns` (`time.monotonic_ns()`, immune to clock steps, for intervals within a run). Then it puts the message on a queue and returns.
- **A writer thread** takes messages from the queue and writes them to SQLite in batches (every 0.5 s or 200 messages, one transaction each), so a slow disk never delays the MQTT callback. SQLite is opened in WAL mode, so the file can be read while a recording runs.
- **Overflow:** the queue holds 100 000 messages. If it is ever full, the oldest are dropped and counted, and the count is stored (see `recorder_events`). A write error is logged and counted and retried with the next batch; it never raises into the MQTT thread.
- **Stop** (Ctrl-C or SIGTERM) flushes the queue and closes the file cleanly.

### What is stored

Lossless first, convenient second: every message goes into one table, and views give the typed columns.

| Table | Columns |
|---|---|
| `sessions` | `id`, `started_ns`, `ended_ns` (set on a clean stop), `broker`, `argv`, `version` |
| `messages` | `id`, `session_id`, `t_ns`, `mono_ns`, `topic`, `retained` (0 or 1), `payload` (the bytes, verbatim) |
| `recorder_events` | `id`, `session_id`, `t_ns`, `kind` (`connected`, `disconnected`, `dropped`, `write_error`), `detail` |

`recorder_events` matters for validity: a period when the recorder was disconnected from the broker has **no messages**, and the analysis must be able to tell that from a quiet system.

Views (SQLite JSON functions), all with `session_id`, `t_ns` and `mono_ns`, retained messages excluded:

- `cp`: the board's raw samples: `board_ms` (its uptime `t`), `freq`, `duty`, `adc`, `cp`, `state`, `amps`, `watts`, `setpoint_w`, `status`, `age_ms`.
- `setpoint`: `setpoint_w`, `samples`, `window_s`, `status`, `server_ts` (the server's own, whole seconds).
- `inverter`: `mode`, `target_dc_w`, `cap_ac_w`, `dc_w`, `dc_a`, `dc_v`, `ac_w`, `ac_a`, `ac_v`, `inverter_state`, `error`, `server_ts`.
- `markers`: the stimulus markers, `label`, `event`, `t_write_ns`, `t_ack_ns`, `from_a`, `to_a`, `run` and the raw `payload`.

A payload that is not valid JSON, or lacks a field, gives `NULL` in the view, never an error. Topics without a view (`evsim/state`, `evsim/reply`, `evsim/status`, and any future topic such as `evsim/electric_car`) are still in `messages`.

### The stimulus marker

The stepping script publishes one JSON object to **`evsim/marker`** around each write to the charger's limit register 1000:

```json
{"event":"limit_write","t_write_ns":1791369214123456789,"t_ack_ns":1791369214131000000,"from_a":6,"to_a":7,"run":3,"label":"1 A step up"}
```

`t_write_ns` is the script's `time.time_ns()` taken just **before** the Modbus write and `t_ack_ns` just after the reply: **`t_write_ns` is time zero** of the delay. The keys are a recommendation: the recorder stores the payload verbatim and the `markers` view reads these names. The script must run on the **same computer** (one clock); from another machine, its clock offset to this one would have to be measured and removed.

### What the timestamps mean, and their limits

All delays are measured with arrival times on one computer, so they are exact for what is local and carry an error for the rest. This is documented in the README, not corrected for:

- `evsim/setpoint` and `evsim/inverter` come from the setpoint server on the same computer: their arrival time is accurate to about a millisecond, but their content is itself sampled (the server averages `evsim/cp` over 1 s and publishes once a second; the inverter writer reads and publishes once a second).
- `evsim/cp` crosses the WiFi from the board: its arrival time includes a transit time that is not known and varies. The board's own uptime clock (`board_ms`, kept in the `cp` view) shows the board's sampling regularity (the 200 ms spacing), so jitter in the arrival times can be told from the board's own.
- The charger's reaction to the register write (the pilot duty changing) is inside the first delay: the recorder sees it only as the first `evsim/cp` sample that differs.

### Files and housekeeping

`data/recordings/` is git-ignored (`*.sqlite`, `*.sqlite-wal`, `*.sqlite-shm`). At about 7 messages a second a run produces roughly 150 MB a day, so it is meant to run for the length of an experiment, one file per run. `server/README.md` gains a section: how to start it, the marker format with a short Python snippet for the stepping script, the views, and an example query (delay from a marker to the first `cp` sample whose `setpoint_w` differs by more than a threshold) with pandas.

## Error handling

| Situation | Behaviour |
|---|---|
| Broker down at start-up | The recorder starts, records a `disconnected`/`connected` event pair when it comes up, and keeps retrying. |
| Broker drops during a run | `disconnected` and `connected` events bracket the gap; paho reconnects. |
| Slow or full disk | Writes retry with the next batch; after 100 000 queued messages the oldest are dropped and a `dropped` event records how many. The callback never blocks. |
| Database file cannot be created | The process exits at start with a clear message. |
| Malformed message | Stored verbatim in `messages`; its view columns are `NULL`. |
| Marker with unexpected keys | Stored verbatim; the `markers` view fills what it finds. |
| Recorder stopped mid-batch | The queue is flushed first; a killed process loses at most the last 0.5 s batch, and WAL keeps the file consistent. |

## Testing

All with the standard `unittest`, the existing `FakeMqttClient` and a temporary SQLite file.

- **Capture:** a message delivered to the callback is stored with the injected clock's `t_ns` and `mono_ns`, its topic, its `retained` flag and its exact bytes; subscription to `evsim/#` is renewed on a second `on_connect`; the client id is unique.
- **Views:** `cp`, `setpoint`, `inverter` and `markers` give the right typed columns from real payloads (including the new `dc_a`, `dc_v`, `ac_a`, `ac_v`); invalid JSON, a missing field and a non-object give `NULL`, not an error; retained messages are left out of the views but kept in `messages`; an unknown topic is in `messages` only.
- **Writer:** batches commit on size and on time; a stop flushes everything; the callback stays fast while the writer is blocked; overflow drops the oldest and records a `dropped` event with the count; a failing write is retried and recorded as `write_error`, and the callback thread never raises.
- **Validity events:** disconnect and reconnect produce `recorder_events` rows; a session row gets `ended_ns` on a clean stop.
- **Concurrent read:** a second connection can read the file while the recorder writes (WAL).
- **Read-only:** the recorder publishes nothing and opens no Modbus connection (the fake records every publish; the test asserts none).
- **Entry point:** a database path that cannot be created exits with a message; `--db` and `--host` are honoured.
- **Live check** (after the suite, against the real rig, changing nothing): run the recorder for about two minutes; publish two test markers with `mosquitto_pub`; check that `cp` rows arrive at about 5 per second with `board_ms` steps of about 200, that `setpoint` and `inverter` arrive about once a second, that the `markers` view reads the test markers, and run the example delay query on one marker. A step on the rig is **not** part of the check (the user's script does that later).

## Not in scope

Any change to the setpoint server, the inverter writer, the firmware or the web page; faster or additional sampling of the charger or the inverter; reading the AMTRON's register or meter; the stepping script; computing the delays (the data and an example query are provided); InfluxDB or any other database; a user interface; clock synchronization across machines.
