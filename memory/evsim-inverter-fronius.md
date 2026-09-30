---
name: evsim-inverter-fronius
description: The EVSim load-side device is a Fronius Symo GEN24 12.0 Plus SC at 192.168.21.120, fed from the charger through a rectifier on MPPT 1, controlled over Modbus TCP (SunSpec model 123), not MQTT
metadata:
  type: project
---

## What it is and how it is wired

The inverter in the EVSim chain is a **Fronius Symo GEN24 12.0 Plus SC** (firmware 1.41.11-1, **12 000 W AC
rated**), at **`192.168.21.120`**. Read-only probe on 2026-09-30. The earlier version of this note said
"GEN24 6.0 Plus at .122"; that is not the unit the charger feeds. Address it by IP, never by hostname
([[evsim-network]]).

The power path: **AMTRON AC out -> rectifier -> DC -> inverter MPPT 1 -> inverter -> AC grid.** There is **no
battery**. The inverter emulates the power side of an EV: it draws DC power from the charger side and exports
it to the grid like PV feed-in. Whatever the AMTRON delivers is the "PV" the inverter tracks.

**Use MPPT 1, not MPPT 2.** The charger side was first on MPPT 2, which has a **14 A** maximum usable input
current (MPPT 1: **28 A**; DC range 80-1000 V, datasheet). Measured on 2026-09-30: on MPPT 2 the current sat
flat at 13.58 A (515 V, 6 990 W DC, 6 825 W AC) while the board reported 12.2 A (8.4 kW) offered. After moving
to MPPT 1: 19.9 A, 508 V, 10 108 W DC, 9 748 W AC.

## The uncontrolled inverter overdraws the charger

With no limit set, the inverter takes whatever the source gives. On MPPT 1 it drew **about 9.7 kW AC against
7.0 kW offered** (10.15 A on the CP), roughly 40 % over, with the pilot still in state C. A real EV would never
do this; it is exactly what the setpoint loop has to prevent. Until the cap works, keep the offer low or set a
static power limit in the inverter's web UI, since an overdrawn offer can eventually trip the charger's or the
installation's protection.

## Control interface: Modbus TCP, not MQTT

The GEN24 does not subscribe to MQTT. It is a **Modbus TCP server: port 502, unit ID 1**. Something must connect
as a Modbus client and write registers. Decision (2026-09-30): the Modbus writer lives **inside the setpoint
server** ([[evsim-setpoint-server]]), so everything is centralized and moves with it to a separate server.

Web UI settings seen: mode TCP Server, port 502, SunSpec model type **int + SF** (so inverter model 103, not
113), **Allow Control on** (required, writes fail otherwise), **Restrict Control off** (any LAN host may control;
it can be limited to the server's IP), meter address offset 200. "Reset Modbus API Control" is the fallback to
clear control state.

SunSpec map (base address 40000, "SunS"; discover the model chain at runtime, do not hardcode; community
projects disagree on addresses). Models present: 1, 103, 120, 121, 122, **123**, 160, 124 (124 is storage, unused
without a battery). `WMax` = 12 000 W.

**Model 123, Immediate Controls, first data address 40229** (0-based wire addresses):

| Register | Address | Value on 2026-09-30 | Meaning |
|---|---|---|---|
| `Conn` | 40231 | 1 | 1 = grid feed on, 0 = standby |
| `WMaxLimPct` | 40232 | 10000 | % of `WMax`, scale factor -2 (10000 = 100.00 %) |
| `WMaxLimPct_RvrtTms` | 40234 | 0 | revert timer, seconds; 0 = none set |
| `WMaxLim_Ena` | 40236 | 0 | 1 turns the cap on |
| `WMaxLimPct_SF` | 40250 | -2 | scale factor |

The lever is `WMaxLimPct` with `WMaxLim_Ena`: a **ceiling on AC output, not a target**. Export is the smaller of
the cap and the DC power the charger delivers. Register value = setpoint_W / 12 000 x 10 000 (about 1.2 W per
step). The charger sees the export plus conversion losses (measured 2-4 %: 7 268 W DC for 7 047 W AC), so a
calibration factor may be needed later.

**Fail-safe caveat:** when a revert timer expires, the cap is removed and the inverter then draws everything it
can from the charger. The hardware timer is therefore not a safe fallback here. On `no_data` or
`server_offline` the writer must **write an explicit stop itself**, and keep rewriting the cap as a
heartbeat. See "Stopping" below.

Useful read-only registers: model 103 `W` (AC power, SF 0) at 40083; model 160 modules at 40263 (MPPT 1) and
40283 (MPPT 2), 20 registers each (`DCA` +9, `DCV` +10, `DCW` +11, scale factors in the first three registers
of model 160 at 40255). A value of 65535 or -3276.8 means "not implemented".

**How to apply:** treat this as the design basis for the Modbus writer. Verify every write behaviour (0 %
versus standby, response time, the revert timer) on the unit first.

## First write, 2026-09-30: it works

Wrote `WMaxLimPct` = 5875 (58.75 % of 12 000 W = 7 050 W) then `WMaxLim_Ena` = 1 with Modbus function code 6, unit
ID 1; both read back correctly. AC output fell from 9 773 W to **7 047 W within about 2 s** and held there for the
60 s watched (7 045-7 049 W, i.e. within 5 W of the 7 050 W asked for). DC input settled after about 4 s at
about 7 270 W, 14.1 A, 515 V. The pilot stayed in state C with a valid PWM throughout. So model 123 gives an
accurate, fast AC ceiling; no revert timer was set (`RvrtTms` still 0).

**State left on the inverter:** the cap is still ENABLED at 58.75 %. The charger offer was raised to about 12.3 A
(8.5 kW) after the write, so the inverter is now held below the offer. Remove it by writing `WMaxLim_Ena` = 0
(register 40236) and `WMaxLimPct` = 10000 (40232), or press "Reset Modbus API Control" in the web UI.

## How a new cap takes effect: write the value, then write `WMaxLim_Ena` = 1 again

**Writing `WMaxLimPct` alone changes nothing.** The register reads back the new value, but the inverter keeps
its old effective limit until `WMaxLim_Ena` = 1 is written again (a "commit"). Seen twice: 16 consecutive
`WMaxLimPct` writes (1 000 to 7 300 W, and 0 %) left AC at 7 047 W; then `WMaxLimPct` = 5000 followed by
`WMaxLim_Ena` = 1 gave **5 998 W AC** within seconds (6 000 W asked). The first write worked only because it
wrote the value and then enabled. **Protocol for every change: write `WMaxLimPct`, then write `WMaxLim_Ena` = 1.**
An earlier version of this note claimed "0 % is silently ignored"; that was wrong, the value had not been
committed.

Accuracy with commit: across 1 000-7 300 W the AC output settles **2-3 W below the cap**.

## Stopping (tested 2026-09-30)

- **Near zero: 0 % plus commit.** Smallest caps: 1 % (120 W) gives 118 W AC / 156 W DC; 0.1 % gives 59 W AC /
  95 W DC; **0 % gives 58 W AC / 94 W DC (0.17 A)** and the inverter stays in MPPT. So the inverter never quite
  reaches zero (about 1 % of the 8.5 kW offered) but recovers instantly when a higher cap is committed.
- **Hard stop: `Conn` = 0 (standby)** brings AC and DC to 0 W in about 1.6 s (state 8 STANDBY, DC voltage
  rises to about 558 V open circuit). **Coming back takes about 57 s:** `Conn` = 1, then state 1 OFF for about
  24 s, then 3 STARTING for about 24 s, then MPPT, with the AC output at the cap at about 57 s. Use only for rare
  events (leaving state C, long link loss), never for short gaps; the board's held setpoint covers the charger's
  6-12 s gaps.
- **The charger does not react to a low or zero draw:** over about 2 minutes including about 50 s at zero draw
  and long stretches at about 100 W, the pilot stayed in state C with a valid PWM, still offering about 8.5 kW.
- **Scale factors change at run time.** During start-up the model 160 DC power register read 10x too large
  (63 944 for about 6 394 W) because its scale factor changed. Re-read the scale factors with every read, never
  cache them.

## Calibration: the setpoint is DC input power, the cap is on AC output

Sweep 2026-09-30 (each cap committed and held 14 s, last 7 s averaged, inverter's own DC measurement):

| AC W | DC W | AC/DC |
|---|---|---|
| 997 | 1 032 | 0.967 |
| 1 998 | 2 083 | 0.959 |
| 2 997 | 3 088 | 0.971 |
| 3 997 | 4 116 | 0.971 |
| 4 997 | 5 147 | 0.971 |
| 5 997 | 6 176 | 0.971 |
| 6 997 | 7 220 | 0.969 |
| 7 296 | 7 535 | 0.968 |

Least-squares fit: **DC = 1.0303 x AC + 6.3 W** (worst residual 19 W = 0.9 % at 2 kW, elsewhere under 0.25 %).
To take a wanted DC power P_dc from the charger: **AC cap = (P_dc - 6.3) / 1.0303 + 3 W** (the 3 W offsets the
settling error), then register = cap / 12 000 x 10 000. Examples: 4 000 W DC -> 3 879 W AC -> register 3233;
7 000 W DC -> 6 791 W AC -> register 5659.

Caveats: the DC power is the inverter's own MPPT measurement, so its accuracy is unknown; validate against the
AMTRON's meter or a power analyser. Measured at 515-545 V DC only. A closed-loop trim (read the DC power over
Modbus and correct the cap) is possible later.

## Extended sweep, 2026-09-30 (7 to 10.3 kW AC, charger offering 16.2 A)

Same method as above (each cap committed, held 14 s, last 7 s averaged), stopped short of the charger's offer.

| Cap AC W | AC W | DC W | DC A | DC V | AC/DC |
|---|---|---|---|---|---|
| 7 000 | 6 997 | 7 220 | 13.91 | 519.0 | 0.969 (matches the first sweep exactly) |
| 8 000 | 7 997 | 8 263 | 16.01 | 516.1 | 0.968 |
| 9 000 | 8 978 | 9 284 | 18.08 | 513.3 | 0.967 |
| 10 000 | **9 385** | 9 780 | **19.10** | 512.0 | not following the cap |
| 10 300 | **9 555** | 9 773 | **19.10** | 511.7 | not following the cap |

- **The calibration holds to 9 kW.** The old fit `DC = 1.0303 x AC + 6.3` is off by 17 W (0.2 %) at 8 kW and 28 W
  (0.3 %) at 9 kW. A refit over all 11 valid points (linear `1.0326 x AC - 1.2`, or quadratic) has an RMS of 7 to
  11 W against 13 W for the old one, all far inside the 100 W dead-band, so the constants were left as they are.
- **Above about 9.4 kW AC the cap stops binding.** The DC current is pinned at **19.1 A** (about 9.78 kW DC), whatever the
  cap. It is the source, not the inverter: 19.1 A DC is about 15.6 A per phase through a three-phase bridge (factor
  1.2247), close to the AMTRON's 16 A limit. The earlier uncontrolled draw (19.9 A DC at a 10.15 A offer) hit the same
  ceiling, so the AMTRON does not enforce the *offered* current, only a hard limit near 16 A.
- **Consequence for the setpoint:** the board turns the offered current into watts as amps x 690 V (16.2 A -> 11 190 W),
  but the rectified DC voltage sags to about 510 V under load, so the same current is only about 9.8 kW of DC power
  (about 630 W per offered amp, not 690). Following a watts setpoint of 11.2 kW exactly would need 21.8 A DC, about
  17.8 A per phase, 10 % over the 16.2 A offered. At lower offers the same 690 V conversion overshoots the offered
  current by about 7 to 10 %. Open question: convert amps to a DC target using the measured DC voltage (or a
  factor near 630 W/A), or regulate the DC current instead of the power.

## Tool: `tools/fronius_modbus.py`

One standard-library script for all of the above: `probe`, `status`, `watch` (read-only), `set WATTS`,
`set --dc WATTS` (uses the calibration), `off` and `conn 0|1`. It discovers the model addresses at run time,
re-reads the scale factors every time, writes only `WMaxLimPct`, `WMaxLim_Ena` and `Conn` (range-checked and
read back), and always commits a cap with the `Ena` write. Tests run against a fake inverter, never the real
one: `cd server && .venv/bin/python -m unittest discover -s tests -p 'test_fronius*.py'`. Default host
192.168.21.120. The code lives in `server/setpoint_server/fronius.py` and `fronius_cli.py`; the tool is a shim.

## Live check of the server driving the inverter, 2026-09-30

`python -m setpoint_server --inverter 192.168.21.120` against the lab broker, the real board and the real inverter,
with the AMTRON offering 10 to 12 A (7 to 8.5 kW).

- **It tracks.** Target 7 751 W: DC input 7 742-7 776 W (within 0.3 %). Later 8 452 W target: 8 480 W DC (+28 W).
  Counting the ramps after each step, 87 % of samples were within 100 W; the median error was +4 W.
- **Steps settle in 2 to 3 s** (DC within 100 W of the new target) for every change of 500 W or more, including
  the operator's limit changes (about 7.9 -> 8.5 -> 6.4 -> 8.5 kW).
- **The dead-band keeps the writes calm:** 8 real cap changes (50 W or more) in 3 minutes; the rest was the 10 s
  refresh moving the cap by about 1 W. No errors and no `unreachable` status anywhere.
- **Board outage:** the server went to `zero` (0 % cap) within 1 s of the last board message. The board reboots into
  pilot state A, which is a *valid* 0 W target, so the server stayed `capped` at 0 W (not an outage, no standby).
  Not isolated: the charger drops its contactor when the board's pilot opens, so the DC also vanished for that reason.
- **Resume:** after `B` then `C` on `evsim/cmd/state` the server re-applied the cap at once, while the inverter was
  still asleep. The inverter then restarted by itself, SLEEPING (about 50 s), OFF (about 25 s), STARTING (about 30 s),
  MPPT, and was at full power about **two minutes** after the `C` command. Any loss of DC (a limit change that drops
  the contactor, a board reboot) costs this minute or two.
- **Ctrl-C:** `server_offline` is published, then the 0 % cap is written, and the server exits in about 2 s; the
  inverter then reads `WMaxLimPct=0 Ena=1`.
- **Write rate:** 60 cap commits at 1 Hz (alternating 5 000 and 5 100 W) gave **0 errors**, so the inverter tolerates
  the once-a-second commit the writer can produce.

## Still open

- The 120 s standby fallback and the 0 % cap on no data were not exercised live (bench tests and the earlier
  floor test cover them); the outage was 13 s and coincided with the contactor opening.
- Timing below 1 s (the status is once a second), and what the inverter does when the *offered* power falls below a cap
  that is still higher than the offer.
- How `RvrtTms` behaves, and an independent check of the DC power (AMTRON meter or analyser).
- **Amps to watts:** the board turns offered current into watts as amps x 690 V, but the rectified DC voltage sags to
  about 510 V, so the same current is about 630 W per amp of DC power. Following the 690 V figure exactly would draw
  7 to 10 % more current than offered. Decide: convert with the measured DC voltage (or about 630 W/A), or regulate
  the DC current instead of the power.
