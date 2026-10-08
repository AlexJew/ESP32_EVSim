# EVSim Recorder: Stepping the Charger's Limit and Logging the Chain to SQLite — Design

**Date:** 2026-10-08
**Status:** draft, awaiting review
**Scope:** one command, `python -m evsim_recorder run`, that **runs a characterization experiment itself**: it steps the AMTRON's maximum current through a list of values, and records, with one clock and into one SQLite file, everything the chain does in response: the charger's own measurements (current, voltage, power), the board's `evsim/cp`, the setpoint server's `evsim/setpoint` and the inverter's `evsim/inverter`. The data answers two questions for the system **as it is**: how long does a change of the maximum current take to propagate along the chain (small 1 A steps, and large jumps up and down), and what are the power differentials along the chain at each current. Builds on
[2026-10-01-charger-modbus-backend-design.md](2026-10-01-charger-modbus-backend-design.md) (the charger service, reused),
[2026-09-29-mqtt-cp-telemetry-design.md](2026-09-29-mqtt-cp-telemetry-design.md),
[2026-09-30-setpoint-server-design.md](2026-09-30-setpoint-server-design.md) and
[2026-09-30-inverter-writer-design.md](2026-09-30-inverter-writer-design.md).

## Why

The thesis chain turns a current limit written to the AMTRON into a load on the inverter: charger (pilot PWM) to board (`evsim/cp`, 5 Hz) to setpoint server (1 s average, `evsim/setpoint`) to inverter writer (cap, `evsim/inverter`, 1 Hz). How long each hop takes, and how much power is lost or gained between the hops at each setpoint, is what other users of this setup need to know. Today those numbers scroll past in `mosquitto_sub` and on the web page. One command that steps the limit and records the lot makes the experiment reproducible.

## Decisions (from the user, 2026-10-08)

- **Time zero is the Modbus write** of the new maximum current to the charger. The recorder makes that write itself, with the same Modbus code as the web page, and stores its exact time. No marker topic.
- **The recorder runs the experiment** with one command. There is no separate `set-limit` command.
- **Full-resolution events**, **SQLite**, one file per run.
- **The charger's measurements are logged too** (signalled current, phase currents, phase voltages, total power), so power differentials can be assessed as well as delays.
- **Characterize the current setup, do not improve it:** no sampling faster than 1 Hz anywhere (the charger poll is **1 s in the recorder**, aligned with the 1 Hz setpoint and inverter messages, instead of the page's 2 s), no fast inverter reader, no change to the firmware or the web page.
- **Cheap changes are welcome:** the setpoint server and the inverter writer get **additive timestamp fields** (below); the board's own uptime is already in every `evsim/cp` sample.

## Design

### The command

```
python -m evsim_recorder run --steps 6,7,6,12,16,6 --dwell 60 [--repeat 1] [--baseline 10] [--db PATH] [--host H] [--port P] [--charger-period 1] [--leave] [--yes]
```

- **`--steps`**: the maximum currents to write, in order, each **0 or a whole number from 6 to 16** and not above the charger's installation limit. They are all validated before anything is written; one bad value stops the command with a message and nothing is written.
- **`--dwell`**: seconds to hold each step (default 60), so each response settles and the steady state can be averaged. **`--repeat`**: how many times to run the whole list (default 1). **`--baseline`**: seconds of recording before the first write (default 10), so the value before the first step is measured too.
- **`--db`**: the SQLite file; default `data/recordings/evsim-<UTC date and time>.sqlite`. **`--host`, `--port`**: the MQTT broker (default `127.0.0.1:1883`). The charger's address is `EVSIM_CHARGER_HOST`, as for the page. **`--charger-period`**: seconds between charger polls, 1 to 10 (default 1).
- **Safety:** the command prints the plan (the steps, the dwell, the total time, the charger's address) and the limit currently set, and **asks for confirmation** (`--yes` skips the question). At the end, or on Ctrl-C or SIGTERM, it **writes back the limit that was set when it started** (best effort, logged as a final step); `--leave` keeps the last step instead. A step of 0 A pauses charging; it is allowed when listed, and the plan says so.
- **Only register 1000 is ever written**, with the same safety rules as the page (`WRITABLE_ADDRESS`; the operator limit, register 134, is never written; the values above).

It runs, in one process: the MQTT recorder, the charger poller and the step runner.

### The MQTT recorder

- Subscribes to **`evsim/#`** at QoS 0, with a client id of `evsim-recorder-` plus a random suffix (equal ids kick each other off the broker). Subscribing happens in `on_connect`, so it is renewed on every reconnect; the connection is asynchronous, so an absent broker never blocks start-up. It publishes nothing.
- **Stamps each message in the callback**, before anything else: `t_ns` (`time.time_ns()`, the computer's wall clock) and `mono_ns` (`time.monotonic_ns()`, immune to clock steps). Then it queues the message and returns.

### The charger poller

The recorder reuses the page's `ChargerService` (`server/charger_web/service.py`): one Modbus connection, a read of every register **every 1 s** in the recorder (`ChargerService` already takes a poll period; the page keeps its 2 s default, so the page does not change), which is also the heartbeat the charger needs (after 30 s without Modbus traffic it falls back to 0 A, so the recorder must keep polling for the whole run). Two additive changes to the service, invisible to the page:

- an optional **`on_poll` callback**, called after every poll with the poll's start and end times (`time.time_ns()`), the raw register values (each phase current, each phase voltage, total power, signalled current, the limits, the states) or the error;
- `set_limit` additionally returns **`write_sent_ns`** and **`write_acked_ns`**: the wall clock just before the write request and just after its reply.

The AMTRON's meter registers (220 power, 222 to 226 voltages) are the ones the page reads; their meaning was inferred and checked on 2026-10-07.

### The step runner

For each step it calls `ChargerService.set_limit(value)` (write register 1000, read it back, validate) and stores one **`limit_writes`** row with `t_write_ns` (`write_sent_ns`: **time zero**), `t_ack_ns`, `from_a`, `to_a`, the read-back value, the run and step indices, a label and any error. Then it waits `--dwell` seconds. A failed write is stored with its error and stops the experiment (after restoring the original limit).

### The writer and what is stored

Messages go through a queue to a **writer thread** that writes SQLite in batches (every 0.5 s or 200 rows, one transaction), in WAL mode, so a slow disk never delays the MQTT callback or the poller and the file can be read while a run is in progress. The queue holds 100 000 rows; if it is ever full the oldest are dropped and counted. A write error is logged, counted and retried with the next batch.

| Table | Columns |
|---|---|
| `sessions` | `id`, `started_ns`, `ended_ns` (set on a clean stop), `broker`, `charger`, `plan` (the steps, dwell and repeat as JSON), `argv`, `version` |
| `messages` | `id`, `session_id`, `t_ns`, `mono_ns`, `topic`, `retained`, `payload` (the bytes, verbatim) |
| `charger` | `id`, `session_id`, `t_start_ns`, `t_end_ns`, `set_limit_a`, `operator_limit_a`, `installation_limit_a`, `signalled_current_a`, `vehicle_state`, `charger_status`, `i_l1_a`, `i_l2_a`, `i_l3_a`, `v_l1_v`, `v_l2_v`, `v_l3_v`, `power_w`, `error` (one row per poll, a failed poll has the error and `NULL` values) |
| `limit_writes` | `id`, `session_id`, `run`, `step`, `t_write_ns`, `t_ack_ns`, `from_a`, `to_a`, `readback_a`, `label`, `error` (the final restore is `step = -1`, label `restore`) |
| `recorder_events` | `id`, `session_id`, `t_ns`, `kind` (`connected`, `disconnected`, `dropped`, `write_error`, `charger_error`, `charger_ok`, `restore_skipped`), `detail` |

`recorder_events` matters for validity: a period when the recorder lost the broker or the charger has no data, and the analysis must be able to tell that from a quiet system.

Views over `messages` (SQLite JSON functions), all with `session_id`, `t_ns`, `mono_ns`, retained messages excluded; invalid JSON or a missing field gives `NULL`, never an error:

- `cp`: `board_ms` (the board's uptime), `freq`, `duty`, `adc`, `cp`, `state`, `amps`, `watts`, `setpoint_w`, `status`, `age_ms`.
- `setpoint`: `setpoint_w`, `samples`, `window_s`, `status`, `server_ts`, `tick_ms`.
- `inverter`: `mode`, `target_dc_w`, `cap_ac_w`, `dc_w`, `dc_a`, `dc_v`, `ac_w`, `ac_a`, `ac_v`, `inverter_state`, `error`, `server_ts`, `read_ts_ms`, `cap_ts_ms`.

Topics without a view (`evsim/state`, `evsim/reply`, `evsim/status`, a future `evsim/electric_car`) are still in `messages`.

### The timestamp fields (setpoint server and inverter writer)

Additive keys in the JSON the two already publish, in epoch milliseconds from the computer's clock; nothing else about their behaviour changes:

- **`evsim/setpoint`**: `ts_ms`, the time of the server's tick that produced the message.
- **`evsim/inverter`**: `read_ts_ms`, the time the inverter's power was read (the measurement time, not the publish time), and `cap_ts_ms`, the time of the last successful cap write (`null` before the first one).

The inverter-writer spec's field table and the page's parsers (which ignore unknown fields) are not otherwise affected.

### What the timestamps mean, and their limits

All times come from one computer's clock, so delays are exact for what is local and carry an error for the rest. This is documented in the README, not corrected for:

- `evsim/setpoint` and `evsim/inverter` come from the setpoint server on the same computer: arrival is accurate to about a millisecond, and the new `ts_ms`, `read_ts_ms` and `cap_ts_ms` give the server's own times. Their content is sampled: the server averages `evsim/cp` over 1 s and publishes once a second; the inverter writer reads and publishes once a second.
- `evsim/cp` crosses the WiFi from the board: its arrival time includes a transit time that is not known and varies. The board's uptime (`board_ms`) shows its sampling regularity.
- The charger's own readings (`charger` table) are one poll every **1 s** (`--charger-period`, 1 to 10 s), each poll of 13 reads a few tens of milliseconds long: the signalled current and the meter resolve the charger's side of a step to about 1 s, the same rhythm as the setpoint and inverter messages. They suit the **steady-state** comparisons (power at each current) well. The poll takes about 0.7 s, so with the period added a row comes about every 1.7 s (see "Running with the web page stopped").
- Time zero (`t_write_ns`) is exact: the same process writes the register and reads the clock.

### Running with the web page stopped

**Stop the web page, and any other program polling the AMTRON, before a run.** Measured on the rig on 2026-10-08: with the page (a second Modbus client) running, about a third of the recorder's charger polls failed ("connection closed by the charger", "connection reset by peer", roughly every 4.5 s), at a 1 s and at a 2 s period alike, while the page itself showed no errors; with the page stopped none of 91 polls failed. The recorder counts its failed polls and ends with a warning on stderr if more than 5 % failed. The page's service also **re-writes the limit last set with Apply** if the charger reports a different one, which would undo a run's steps, so the page must not be used during a run anyway.

**The charger rows come about every 1.7 s, not every second.** A poll is 13 Modbus requests and takes about 0.7 s (the AMTRON answers a request in about 50 ms), and the service waits the poll period after each poll, so the cycle is the duration plus the period. `--charger-period 1` therefore gives a row about every 1.7 s.

### Files and housekeeping

`data/recordings/` is git-ignored (`*.sqlite`, `*.sqlite-wal`, `*.sqlite-shm`). About 7 messages a second plus one charger row every second is roughly 150 MB a day; a run is meant to last the length of an experiment. `server/README.md` gains a section: the command, the safety behaviour, the tables and views, the limits above, and two example queries with pandas: the delay from a `limit_writes.t_write_ns` to the first `cp`, `setpoint` and `inverter` rows that differ from their value before the write by more than a threshold, and the mean charger, DC and AC power over the last 20 s of each step.

## Error handling

| Situation | Behaviour |
|---|---|
| A bad or out-of-range step, or an unreadable plan | Refused at start, nothing written, a clear message. |
| Confirmation declined | Nothing written. |
| Charger unreachable at start, or its limit cannot be read reliably | The command stops with a message before writing anything. The limit to restore is the value that 4 consecutive polls (about 3 s) agree on: the first request after the charger's fallback reads 0 although the stored limit is intact. |
| MQTT broker not reachable before the first write | The command stops with a message, nothing written (otherwise the chain would not be recorded). Drops during a run are events. |
| The first limit cannot be written back | Up to 5 tries; then `restore_failed` is recorded, `CHARGER LEFT AT ... A` is printed to stderr and the exit code is 3. SIGHUP is handled like SIGINT and SIGTERM. |
| Charger unreachable mid-run | `charger_error` events and error rows; the run stops after a failed write; the original limit is restored if the charger comes back. |
| Broker down at start-up or during the run | The recorder starts and keeps retrying; `disconnected` and `connected` events bracket the gap; the step runner and the charger poll carry on. |
| Slow or full disk | Writes retry with the next batch; after 100 000 queued rows the oldest are dropped and a `dropped` event records how many. Callbacks never block. |
| Ctrl-C, SIGTERM | The original limit is written back (unless `--leave`), the queue is flushed, the session's `ended_ns` is set. |
| Malformed message | Stored verbatim in `messages`; its view columns are `NULL`. |
| Database file cannot be created | The process exits at start with a clear message. |

## Testing

All with the standard `unittest`, the existing `FakeAmtron` and `FakeMqttClient`, injected clocks and a temporary SQLite file.

- **Plan and safety:** steps outside 0 or 6 to 16, above the installation limit or not whole numbers are refused before any write; declining the confirmation writes nothing; the fake records only writes to register 1000 and never to 134; a step of 0 is allowed and flagged in the plan.
- **Steps:** the steps are written in order, repeated `--repeat` times, each held for the dwell; each `limit_writes` row has `t_write_ns` before `t_ack_ns`, the right `from_a`, `to_a` and read-back; a refused or unconfirmed write is stored with its error and stops the run.
- **Restore:** the original limit is written back at the end, on Ctrl-C and on SIGTERM (`step = -1`), and not with `--leave`.
- **Charger poll:** each poll becomes a `charger` row with the raw phase values, `t_start_ns` before `t_end_ns`; a failed poll is an error row plus a `charger_error` event; the poll keeps running during a dwell (the fake's 30 s fallback is never reached).
- **MQTT capture:** a message is stored with the injected `t_ns` and `mono_ns`, topic, `retained` flag and exact bytes; the subscription to `evsim/#` is renewed on a second `on_connect`; the client id is unique; nothing is ever published.
- **Views:** `cp`, `setpoint` and `inverter` give the typed columns from real payloads, including the new `ts_ms`, `read_ts_ms` and `cap_ts_ms`; invalid JSON, a missing field and a non-object give `NULL`; retained messages are in `messages` only.
- **Writer:** batches commit on size and on time; a stop flushes everything; the callback stays fast while the writer is blocked; overflow drops the oldest and records a `dropped` event with the count; a failing write is retried and recorded, and no thread raises.
- **Validity events:** disconnect and reconnect produce `recorder_events` rows; `ended_ns` is set on a clean stop; a second connection can read the file while it is written (WAL).
- **Setpoint server and inverter writer:** `evsim/setpoint` carries `ts_ms` (epoch milliseconds of the tick, from the injected clock); `evsim/inverter` carries `read_ts_ms` after the read and `cap_ts_ms` after a successful cap write, `null` before the first; every existing field and test is unchanged.
- **Charger service:** `on_poll` receives the timings and the values; `set_limit` returns the two write times; the page's existing tests pass unchanged.
- **Live check** (after the suite, on the real rig): restart the setpoint server from the branch; run the recorder with a short plan, for example `--steps 10,11,10 --dwell 45 --yes`, a **1 A step only**, nothing larger, and confirm that the original limit is back at the end; check the tables fill at the expected rates (`cp` about 5 a second with `board_ms` steps near 200, `setpoint` and `inverter` about 1 a second, `charger` about 1 a second with no `charger_error` events at that rate), that the new timestamps are present, and run the example delay query on one step. Larger jumps are left to the user's own runs.

## Not in scope

Any change to the firmware or the web page; any sampling faster than 1 Hz; a fast inverter reader; computing the delays and differentials (the data and example queries are provided); InfluxDB or any other database; a user interface; clock synchronization across machines; stepping the pilot state; writing any register other than 1000.
