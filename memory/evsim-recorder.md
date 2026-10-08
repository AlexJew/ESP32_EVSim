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
- **Stop the page (and any other Modbus client) before a run:** with a second client a third of the charger polls fail (the AMTRON drops connections); the page's service also re-writes the limit it last applied.
- **Additive timestamps** added for it: `ts_ms` in `evsim/setpoint`; `read_ts_ms` and `cap_ts_ms` in `evsim/inverter`.
- **Limits of the timing:** the board's `evsim/cp` crosses the WiFi (unknown transit); the setpoint server averages over 1 s and
  publishes at 1 Hz; the inverter writer reads at 1 Hz; the charger is polled at 1 s. Nothing here samples faster on purpose:
  it characterizes the system as it is.

Related: [[evsim-dashboard]], [[evsim-setpoint-server]], [[evsim-inverter-fronius]], [[evsim-amtron-modbus]].

## Live check, 2026-10-08

Setpoint server restarted from the branch; charger at 15 A (operator limit 16); steps `14,15,14`, all 1 A, dwell 45 s,
baseline 10 s. (The rig sits near the inverter's DC current ceiling at 14 to 15 A, about 19 A DC and 9.8 kW, where a 1 A step
moves the inverter little: steady state 9 753 W DC at 14 A and 9 932 W at 15 A against 10 380 and 10 496 W at the charger.
Lower currents are better for characterizing the chain.)

- **Rates:** `cp` 5.01 a second with `board_ms` steps of exactly 200 ms; `setpoint` 1.01; `inverter` 0.96 (a few skipped);
  the server's new timestamps are present on every row and arrive within about 2 ms of the recorder's clock.
- **Writes:** acknowledged in 32 to 83 ms (once 178 ms); the original 15 A was restored each time (the page's 2 s cache showed
  14 A for a moment after the run, the charger itself read 15).
- **First change after each write (three 1 A steps):** `cp` 0.14 to 0.26 s, `setpoint` 0.35 to 0.54 s, `inverter.dc_w` about
  0.87 s (the second step is lost in the saturated inverter's noise), charger signalled current 1 to 3 s (its poll cycle).
- **Two Modbus clients break the charger polls:** with the page running, 32 of 99 polls failed (reset every ~4.5 s), at 1 s and
  at 2 s alike; stopped, 0 of 27, 0 of 15 and, in a full run, 0 of 91. A poll of 13 requests takes 0.7 s, so the cycle at
  `--charger-period 1` is about 1.7 s. The recorder now warns at the end of a run if more than 5 % of polls failed.
- **Fixed by the check:** the spec and README had said the page and the recorder could poll side by side.
- **Not checked:** jumps larger than 1 A (left to the user's own runs); the page's 8080 process was restarted fresh (it had
  remembered 15 A from an Apply, which would have fought the steps).
