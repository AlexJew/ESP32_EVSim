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
