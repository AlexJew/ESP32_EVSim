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
value. A `setpoint_w` above 11 000 W (the charger's rating) is dropped as a corrupt message, not
clamped. Design: `docs/superpowers/specs/2026-09-30-setpoint-server-design.md`.

**Why:** the board publishes raw samples and leaves smoothing to the consumer
([[evsim-tb-overview]]); this server is that consumer, in the "separate server" form.

**How to apply:** it only puts a setpoint on the broker. Whether the GEN24 needs an MQTT -> Modbus
bridge to act on it is still open ([[evsim-inverter-fronius]]). The averaging is a plain 1 s mean by
design; anything smoother is a later change here, not on the board. Run it with
`cd server && .venv/bin/python -m setpoint_server`; see `server/README.md`.

**Watch the cap:** the board's watts come from amps x 690 V (3 x 230 V), so a full 16 A offer is about
11 040 W, just over the 11 000 W cap, and would be dropped (0 W, `no_data`). Fine at the 14 A bench
setting (about 9.8 kW); revisit `MAX_SETPOINT_W` in `server/setpoint_server/config.py` before offering 16 A.
