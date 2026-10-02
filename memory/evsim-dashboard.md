---
name: evsim-dashboard
description: The local web page (server/charger_web) also shows the inverter's DC power, evsim/cp, the setpoint topic and the server's DC target, with a 10-minute chart; read-only
metadata:
  type: project
---

The charger page became an EVSim dashboard on 2026-10-01 (spec `docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md`,
plan `docs/superpowers/plans/2026-10-01-evsim-dashboard.md`). Same process, same port (8080), still loopback only.

- **Inverter:** the page never touches it. The DC power is `dc_w` in the setpoint server's `evsim/inverter` message (MPPT 1, read by the server's writer once a second), so it exists only while the server runs with `--inverter`. A direct read-only Modbus reader was designed first and dropped (commit 2d4d134); it would be the way back if an independent view is needed.
- **MQTT:** `charger_web/telemetry.py` subscribes (client id `evsim-charger-web`, never the setpoint server's id) to
  `evsim/cp`, the setpoint topic and `evsim/inverter`; parsers in `parsers.py`. Retained messages are shown stale.
- **History:** `charger_web/history.py`, 600 frames at 1 Hz in memory; the chart is `static/chart.js`.
- **For the car emulation:** the setpoint topic is `EVSIM_SETPOINT_TOPIC` (default `evsim/setpoint`). When
  `evsim/electric_car` exists, set it and adapt `parse_setpoint` if the payload differs.

**Why:** the loop only proves itself by comparing the board's setpoint, the average, the DC target and the DC power
the inverter really takes; before this they lived in `mosquitto_sub` and the inverter tool.

**How to apply:** see [[evsim-inverter-fronius]] for the SunSpec details, [[evsim-setpoint-server]] for the topics
and [[evsim-amtron-modbus]] for the charger half of the page.

## Live check, 2026-10-02

Page on port 8081 against the real broker, the real board and the setpoint server driving the inverter (`--inverter`),
AMTRON offering about 8 A. Nothing was written to the inverter or the broker.

- **The page's inverter DC power equals the `evsim/inverter` message** (5 673.4 W both) and agrees with
  `tools/fronius_modbus.py status` (MPPT 1 5 673 W, 10.87 A, 522 V): the number the writer publishes is the inverter's own.
- **History:** 121 frames after about two minutes, no gaps in any of the four series, no errors or tracebacks in the page log.
- **Broker unreachable at start-up** (`EVSIM_MQTT_HOST=127.0.0.2`): the page still starts and serves; `mqtt.connected` is
  false, the board and inverter readings are unavailable, and the charger part (link ok, set limit read) works.
- **Not checked live:** the rendering in a browser (chart, legend clicks, greyed cards) and the setpoint server being
  stopped while the page runs (it would have left the inverter at a 0 % cap); both are covered by unit tests only.
- The old page on port 8080 was left running; it is the pre-dashboard code until restarted.
