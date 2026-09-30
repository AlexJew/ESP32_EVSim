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
`server_offline` the writer must **write an explicit low value itself** (0 % or `Conn` = 0, to be tested on the
unit), and keep rewriting the cap as a heartbeat.

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

Still to test before relying on it: what 0 % does versus `Conn` = 0, the response time when the cap is raised,
and how the cap behaves when the offered power drops below it.
