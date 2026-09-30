# Inverter Writer — Design

**Date:** 2026-09-30
**Status:** awaiting review
**Scope:** make the setpoint server drive the Fronius GEN24 inverter over Modbus TCP: turn the averaged
setpoint into a cap on the inverter's AC output, so the DC power it takes from the charger equals the
setpoint. Builds on [2026-09-30-setpoint-server-design.md](2026-09-30-setpoint-server-design.md), which
produces the setpoint, and on the bench findings in `memory/evsim-inverter-fronius.md`.

## Purpose

The thesis loop is AMTRON → ESP32 → Fronius. The board publishes the CP measurement, the setpoint server
averages it into a setpoint, and this design closes the loop: the inverter must take from the charger about
the power the setpoint names, as an EV would.

The physical setup fixes what "setpoint" means. The charger's AC output goes through a rectifier to the
inverter's **MPPT 1** DC input (a 12.0 kW GEN24 Plus SC, 192.168.21.120, no battery), and the inverter
exports it to the grid. Left alone it draws whatever the source gives, which measured about 9.7 kW AC
against 7.0 kW offered. The setpoint is therefore the **DC input power** to take, and the lever is SunSpec
model 123: a ceiling on the AC output, `WMaxLimPct`, switched on with `WMaxLim_Ena`.

Facts from the bench (2026-09-30) that shape everything below:

1. **A new cap takes effect only when `WMaxLim_Ena` = 1 is written again** after the value. The value
   alone reads back but changes nothing. Every change is "write `WMaxLimPct`, then write `Ena` = 1".
2. **The AC output follows the cap to within 3 W** (it settles about 3 W below), in about 2 s.
3. **The DC input is `1.0303 x AC + 6.3 W`** across 1 to 7.3 kW (worst residual 0.9 % at 2 kW, else under
   0.25 %). The DC figure is the inverter's own measurement.
4. **A 0 % cap leaves about 94 W of DC draw and 58 W AC**; it never reaches zero but recovers instantly.
5. **`Conn` = 0 (standby) gives 0 W in about 1.6 s, but `Conn` = 1 needs about 57 s** (OFF, STARTING, then
   MPPT) before the AC output is back at its cap.
6. **When a revert timer expires the cap is removed**, so the inverter would draw everything it can. The
   hardware timer is therefore not a fail-safe here, and the software must stop the inverter explicitly.
7. **Scale factors change at run time** (the DC power register read 10x too large during start-up), so
   they are re-read with every read.
8. The charger does not react to a low or zero draw: the pilot stayed in state C offering about 8.5 kW.

## Behaviour

The writer receives one **target** per second: the DC power to take, in watts, or **none** when the
averager has no data (`no_data`). A target of 0 is a valid value (the board is in state A or B), and is
not an outage.

### Modes and actions

| Situation | Action | Mode |
|---|---|---|
| Target, at least 100 W from the last written target (or 0 versus non-0) | Cap the AC output at `(P_dc - 6.3) / 1.0303 + 3 W`: write `WMaxLimPct`, then `Ena` = 1, and read back | `capped` |
| Target, within 100 W of the last written one | Nothing | `capped` |
| No data | Write a **0 % cap** at once (value, then `Ena` = 1) | `zero` |
| No data for **120 s** | Write `Conn` = 0 | `standby` |
| Data returns while in `standby` | Write `Conn` = 1, then the cap, and keep re-committing until the read-back confirms it | `capped` |
| Server starts with no data | Assert the 0 % cap | `zero` |
| Clean shutdown | Write the 0 % cap, then close | |

A valid target of 0 W caps at 0 % and stays `capped`; it never escalates to standby, because it is not an
outage. Only `no_data` runs the 120 s clock.

### Write rules

- **Dead-band 100 W.** A new target is written only if it differs from the last *written* target by at
  least 100 W, or if one of the two is exactly 0. This absorbs the electronics' jitter and keeps write
  traffic low. It compares against the last written value, so a slow drift still triggers a write once it
  adds up to 100 W. Consequence, accepted: the DC draw can sit up to about 100 W from the setpoint.
- **Refresh every 10 s.** The current cap is re-committed (value, then `Ena` = 1) every 10 s even without a
  change, so an inverter reset or a press of "Reset Modbus API Control" cannot silently drop it.
- **Read back every write.** A failed write or a read-back mismatch closes the connection, is reported in
  the status and the error field, and is re-asserted on reconnecting (after a 1 to 5 s backoff), which
  also repairs a write that died half way.
- **Range.** The cap is limited to 0 to `WMax` (12 000 W, read from the inverter). The server's own input
  cap of 12 000 W keeps a valid target under it: a 12 000 W DC target needs only about an 11 650 W AC cap.

### Failure handling

- **Inverter unreachable, or a Modbus error:** the writer closes the connection, retries with a backoff of
  1 to 5 s, and on reconnect re-reads the controls and re-asserts the current cap. The status shows
  `unreachable` with the error.
- **Standby is watched.** While in standby the writer reads `Conn` every step. If the inverter left standby on
  its own (a reboot, or a press of "Reset Modbus API Control", which also clears the cap), the writer stops it
  again: 0 % cap, then `Conn` = 0. `Conn` writes are read back like the others.
- **Forcing after a resume is bounded.** After `Conn` = 1 the cap is forced every step, but only while the target
  is above 0 W and only for 180 s. An inverter that never starts (no DC because the charger's contactor is open)
  must not be written to every second for hours; after 180 s the status returns to `capped`.
- **A stop is not lost on a slow link.** Once a stop is requested the writer skips the rest of its step and
  writes the 0 % cap; `stop()` waits for that (up to nine round-trip timeouts) and logs a warning if it could not
  confirm it.
- **Stale mailbox:** if the once-a-second target has not been refreshed for 3 s (a stalled server tick),
  the writer treats it as no data.
- **Known limit, accepted:** if the link to the inverter dies while it holds a cap, the cap stays. A dead
  server cannot force zero, and the revert timer would make things worse (fact 6). If the charger's offer
  then drops below the cap, the inverter can overdraw. The status and the log flag the outage; there is no
  hardware fallback in this version.
- **Slow inverter:** all Modbus work runs on its own thread, so a slow or dead inverter never delays the
  once-a-second MQTT publish.

## Design

### Components

Under `server/setpoint_server/`:

- **`fronius.py`** — the Modbus client, moved from `tools/fronius_modbus.py`: `ModbusTcp`, `Inverter`
  (model discovery, reads with fresh scale factors, and writes limited to `WMaxLimPct`, `WMaxLim_Ena` and
  `Conn`, range-checked and read back), and the pure functions `pct_register` and `ac_cap_for_dc`. Standard
  library only. The calibration constants move to `config.py`.
- **`fronius_cli.py`** — the command-line commands (`probe`, `status`, `watch`, `set`, `set --dc`, `off`,
  `conn`), moved from the tool. `tools/fronius_modbus.py` stays as a thin shim that calls it, so the
  documented command keeps working and there is one copy of the register logic.
- **`inverter_control.py`** — pure logic, no I/O and no clock of its own: the state machine of the table
  above. `Controller.step(now, target)` returns the list of actions to perform (`cap(target_w)`,
  `standby()`, `resume()`) and tracks the mode, the last written target, the last write time and the start
  of the outage. Tested on a fake timeline.
- **`inverter_writer.py`** — the writer thread. It owns the connection, holds a one-slot mailbox for the
  latest target (`set_target(value_or_None)`), runs the controller once a second, performs the actions
  through `fronius.Inverter`, reads the inverter's power and state, and reports status. It reconnects with
  backoff and writes the 0 % cap on `stop()`.
- **`server.py`** — the once-a-second tick also calls `writer.set_target(mean)` (the DC watts, or none for
  `no_data`). `shutdown()` stops the writer after publishing `server_offline`. Modbus is optional: without a
  writer nothing changes.
- **`__main__.py`** — new `--inverter HOST` (off unless given; the server never controls the inverter
  without it), with `--inverter-port` (default 502) and `--inverter-unit` (default 1).

`config.py` gains: `INVERTER_TOPIC = "evsim/inverter"`, `INVERTER_TIMEOUT_S = 3`, `DEADBAND_W = 100`,
`REFRESH_S = 10`, `STANDBY_AFTER_S = 120`, `MAILBOX_STALE_S = 3`, and the calibration constants
`DC_PER_AC = 1.0303`, `DC_OFFSET_W = 6.3`, `AC_SETTLE_W = 3.0`, with the date and range of the fit.

### Data flow

1. The tick averages the window, publishes `evsim/setpoint` as today, and hands the mean (or none) to the
   writer's mailbox.
2. Once a second the writer takes the mailbox value and asks the controller for actions. It performs them
   in order, each read back.
3. It then reads the inverter's power and publishes the status on `evsim/inverter`.

### Status message

Topic `evsim/inverter`, QoS 0, not retained, once per second, so the actual draw can be compared with the
setpoint:

```json
{"mode":"capped","target_dc_w":7000,"cap_ac_w":6791,"dc_w":7012,"ac_w":6788,"inverter_state":4,"error":null,"ts":"2026-09-30T13:42:07Z"}
```

| Field | Meaning |
|---|---|
| `mode` | `capped`, `zero`, `standby`, `starting` (after `Conn` = 1, until the cap reads back and the inverter reports MPPT) or `unreachable` |
| `target_dc_w` | The last written target, or `null` |
| `cap_ac_w` | The AC cap currently written, or `null` |
| `dc_w`, `ac_w` | The inverter's measured MPPT 1 DC power and AC power, or `null` if unreadable |
| `inverter_state` | The SunSpec state (4 = MPPT, 8 = STANDBY, 3 = STARTING, 1 = OFF), or `null` |
| `error` | The last error text, or `null` |
| `ts` | Server UTC time, ISO 8601 |

## Testing

- **Controller, pure, fake timeline:** every row of the mode table; the dead-band at 99 W and 100 W;
  0 versus non-0; drift adding up across writes; the 120 s boundary; a valid 0 W never reaching standby;
  refresh every 10 s; recovery from standby; stale mailbox.
- **`fronius` client:** the tests from `tools/test_fronius_modbus.py`, moved, against the fake inverter.
- **Writer thread against a fake inverter:** the write order (value, then `Ena`), read-back and retry,
  reconnect and re-assert after the fake drops, the 0 % cap on `stop()`, and a stalled inverter not blocking
  the caller.
- **Bench, private Mosquitto plus the fake inverter:** feeding `evsim/cp` moves the fake's `WMaxLimPct`
  register to the expected value; silence gives a 0 % cap, then `Conn` = 0 after the outage time (the
  threshold shortened for the test); data returning resumes; a status message arrives each second.
- **Live, guarded, once, with the real inverter and the board:** cap tracking through the loop, then a
  1 Hz write test to find the write rate the inverter tolerates, since that is still unmeasured.

## Out of scope

- **A hardware fallback** for a dead link (see the known limit).
- **Closed-loop trimming** from the measured DC power, and any smoothing beyond the 1 s mean.
- **An independent check of the DC power** against the AMTRON's meter or an analyser (recommended
  separately; the calibration uses the inverter's own measurement).
- **The battery models**, and any other inverter control (power factor, reactive power).
- **Authentication and TLS**, as in the earlier designs. The Modbus link is unauthenticated on the lab LAN;
  "Restrict Control" in the inverter's web UI can limit it to the server's IP.
- **Running as a service.**

## Still to measure before relying on it

- The write rate the inverter tolerates (the plan re-commits at most once per second, and only on change or
  every 10 s).
- Whether a cap write is honoured while the inverter is in STARTING after `Conn` = 1 (the writer keeps
  re-committing until the read-back confirms, so this decides only how long recovery takes).
- What the inverter does when the offered power falls below the cap.
