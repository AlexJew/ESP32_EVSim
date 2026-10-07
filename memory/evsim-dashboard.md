---
name: evsim-dashboard
description: The local web page (server/charger_web) also shows the inverter's DC power, evsim/cp, the setpoint topic and the server's DC target, with a 10-minute chart; read-only
metadata:
  type: project
---

The charger page became an EVSim dashboard on 2026-10-01 (spec `docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md`,
plan `docs/superpowers/plans/2026-10-01-evsim-dashboard.md`). Same process, same port (8080), still loopback only.

- **Inverter:** the page never touches it. The DC power is `dc_w` in the setpoint server's `evsim/inverter` message (MPPT 1, read by the server's writer once a second), so it exists only while the server runs with `--inverter`. A direct read-only Modbus reader was designed first and dropped (commit 2d4d134); it would be the way back if an independent view is needed.
- **MQTT:** `charger_web/telemetry.py` subscribes (client id `evsim-charger-web-<random>`, never the setpoint server's id) to
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

## Measurements and state control (2026-10-07)

Spec `docs/superpowers/specs/2026-10-07-evsim-measurements-and-state-control-design.md`, plan
`docs/superpowers/plans/2026-10-07-evsim-measurements-and-state-control.md`.

- **Every measurement is A / V / W.** Charger: mean of the three phase currents (212, 214, 216) and voltages (222, 224,
  226), total power from 220 (see [[evsim-amtron-modbus]]). Inverter: `evsim/inverter` now also carries `dc_a`, `dc_v`,
  `ac_a`, `ac_v` (AC values are the mean of the three phases); the page still never reads the inverter.
- **History frames** carry all of them (`charger_w/a/v`, `sig_a`, `dc_a/v`, `ac_w/a/v`), so the power, current and
  voltage charts all draw the server's 10 minutes.
- **The page now publishes once:** `POST /api/vehicle/state` sends A, B or C to `evsim/cmd/state` (QoS 0, not retained: paho queues QoS 1 and replays it after a reconnect, so a command shown as failed could run later)
  and waits 3 s for the matching line on `evsim/reply`. Replies have no command id, so they are matched by content; one
  command at a time (409 otherwise). "Validated" = the board accepted; the charger is not waited for.

## Live check of the measurements and state control, 2026-10-07

Page on port 8081 against the real broker, board, AMTRON and inverter, with the setpoint server restarted from the branch
(`--inverter`); charger offering about 11 A, 8.2 kW.

- **The numbers agree.** Inverter DC input 15.1 A / 513 V / about 7 760 W: the same in the page, in `evsim/inverter` and in
  `tools/fronius_modbus.py status` (15.12 A, 512.8 V, 7 756 W). AC output 10.8 A / 232.4 V / 7 515 W (7 515 / 3 / 232.4 = 10.8 A).
  Charger: phase powers 2 765 + 2 713 + 2 775 = 8 253 W against register 220 = 8 254 W, phase voltages 230-231 V,
  currents 11.8 to 12.1 A. The inferred meter registers (206-210, 220, 222-226) are confirmed by this arithmetic again.
- **History:** 125 frames after two minutes, every key present; the only gaps were the first seconds (page started before the
  first inverter message) and 13 seconds without a raw board sample (`cp_w`), which is the board's own publishing gaps.
- **State commands (B, C, C again, never A):** `{"state":"B"}` gave 200 `state C -> B`, `C` gave 200 `state B -> C`, `C` again gave 200
  `already in C`; a lower-case letter gave 400, a foreign Origin 403. The board's lines appeared on `evsim/reply`.
- **A brief B then C cost about 55 s:** the charger sat at 150-210 W (0.8 A) with the inverter's DC input at 0 W, then returned
  to 8.2 kW and the inverter followed. Much shorter than the two minutes after a longer state A.
- **Bug found by the check, fixed:** the first run said "The board did not answer" (the board had in fact accepted B and was
  left in B; restored with `mosquitto_pub`) and then "Not connected". Cause: the page on port 8080 started earlier had the same
  MQTT client id, and two clients with one id kick each other off the broker (85 disconnects in a minute). Each page process now
  adds a random suffix to its client id; with the old page still running, 0 disconnects. Test: two clients never share an id.
- **Not checked:** the page in a browser (dropdown, rows, three chart quantities); a refusal such as A to C (it needs state A,
  which was never commanded).

## Chart zoom (2026-10-07, ported onto the new layout)

Wheel or pinch zooms the time axis around the cursor (20 s to 10 min), drag pans, double-click or "Back to live" resets; a
zoomed view is frozen on absolute times (`dashboardState.zoom`, `chart.js` `zoomView`/`panView`/`clampView`/`timeTicks`).
Only the zoomed chart changes: clock-time ticks, a vertical scale that follows what is in view, clipping and a marker on
each 1 s frame when 60 s or narrower. The chart that is not zoomed is drawn exactly as before (tests pin it). Frames stay
1 Hz, so delays read to about 1 s. The first version, on the old layout, was branch `feat/chart-zoom`.
