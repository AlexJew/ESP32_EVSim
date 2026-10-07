# Setpoint server

Subscribes to the board's `evsim/cp`, averages `setpoint_w` over 1 s, and publishes
the result on `evsim/setpoint` (QoS 0, retained) for the Fronius inverter.

Design: `docs/superpowers/specs/2026-09-30-setpoint-server-design.md`.

## Set up (once)

    cd server
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

## Run

Mosquitto must be running with the lab config (`tools/mosquitto-lab.conf`) so the board can reach it.

    cd server
    .venv/bin/python -m setpoint_server            # broker on 127.0.0.1:1883
    .venv/bin/python -m setpoint_server --host 192.168.21.137 --port 1883

Stop it with Ctrl-C. It publishes `server_offline` on the way out.

Watch the output:

    mosquitto_sub -v -t evsim/setpoint

## Local AMTRON charger page

A small page, run as a separate process from the MQTT averaging server, to read the AMTRON's current and set its
limit over **Modbus TCP**. It also shows the Fronius inverter's DC power, the board's `evsim/cp`, the
setpoint topic and the server's DC target (all from MQTT), with a 10-minute chart (see "The dashboard" below). It binds to
`127.0.0.1`, so it is available only on the development computer. It needs `paho-mqtt` (see `requirements.txt`).

Enable the charger's Modbus server once, in the AMTRON web config: "Modbus TCP Server for energy management
systems" On, base port 502, register address set MENNEKES, "Allow Start/Stop Transaction" Off. Set the operator
limit on the AMTRON page to 16 A (the ceiling that Modbus works below). No username or password is needed.

    cd server
    .venv/bin/python -m charger_web

Open `http://127.0.0.1:8080/`. `EVSIM_CHARGER_HOST` overrides the charger address (default `192.168.21.117`) and
`--port` the page's port. Stop it with Ctrl-C.

How it works, and what to know:

- A background poller reads the charger every 2 s. That is also the heartbeat the charger needs: after 30 s without
  Modbus traffic it drops to 0 A, so if this process stops, charging stops within 30 s.
- "Maximum current" writes register 1000 (0 A pauses; otherwise 6 to 16 A). The AMTRON's own operator limit
  (register 134, set on its page) is a ceiling: the effective limit is the lower of the two.
- After you press Apply the page keeps that limit: if the charger loses it (a reboot, another Modbus master), the
  service puts it back, at most once every 10 s. This also overrides another Modbus master that pauses the charger
  (for example by writing 0 A) while this page is running; the page only forgets its limit when you Apply a new one,
  when an Apply is not confirmed (it then adopts whatever the charger holds), or when it restarts.
- The service never writes on its own at start. But the charger remembers the last limit written and restores it on
  the first Modbus request after a fallback, so starting this page can resume charging at that limit.
- Modbus has no login: anything on the lab network can set the charger's current. Keep the page on loopback.

Design: `docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md`. The read-only scanner
`tools/amtron_modbus_probe.py` lists registers (any read can resume charging after a fallback).

### The dashboard

The same page shows the whole chain. It never writes to the inverter. Its only MQTT write is the vehicle-state command
(see "Vehicle state" below); everything else on the page is read-only.

- **Inverter:** MPPT 1's DC power is the `dc_w` field of `evsim/inverter`, which the setpoint server's inverter writer
  publishes once a second. The page never connects to the inverter itself, so the DC power and the DC target appear
  only while the setpoint server runs with `--inverter`.
- **MQTT:** the page subscribes (client id `evsim-charger-web-` plus a random suffix, so a second page never kicks it off) to `evsim/cp`, the setpoint topic and
  `evsim/inverter`, on the broker at `127.0.0.1:1883`. `EVSIM_MQTT_HOST` changes the broker. A retained message, such as `server_offline`, is shown as the last known state and flagged stale.
- **The setpoint topic** is `evsim/setpoint`. Set `EVSIM_SETPOINT_TOPIC` (for example `evsim/electric_car`) to follow
  another topic. Its message must carry a numeric `setpoint_w`; `charger_web/parsers.py` (`parse_setpoint`) is the
  one place to change if the format differs. A `setpoint_w` outside 0 to 12 000 W is dropped, not clamped.
- **The chart** keeps the last 10 minutes in memory (a restart empties it): the board's raw setpoint (the mean of the
  5 Hz samples in each second), the 1 s average, the DC target and the inverter's DC power. A value that is stale or
  missing leaves a gap. Click a legend entry to hide or show a line.
  Scroll or pinch over the chart to zoom along the time axis (down to 20 s, around the cursor), drag to pan, double-click or
  "Back to live" to follow the latest data again; a zoomed view stays where you put it, shows clock times and a marker on
  each 1 s frame when narrow. Frames are 1 s apart, so delays can be read to about 1 s.
- **Measurements:** the Car charger, DC input and AC output rows each read "current / voltage / power". The
  **A**, **V** and **W** bubbles in the card's header switch each part on or off for all three rows (all three may be off; the
  choice is kept in the browser). Amps always show one decimal. The charger's
  values come from the AMTRON's meter (current and voltage are the mean of the three phases; total power from register
  220). The inverter's come from `evsim/inverter`: `dc_a`, `dc_v`, `dc_w` (MPPT 1) and `ac_a`, `ac_v` (means of the
  three phases), `ac_w`. A setpoint server older than 2026-10-07 does not publish the currents and voltages: they show
  dashes. The chart switches between Power, Current and Voltage; all three draw the same server-side history.
- **Vehicle state:** the dropdown shows the state the board presents and, when you pick A, B or C, publishes it to
  `evsim/cmd/state` (QoS 0, so an unconfirmed command is never replayed after a reconnect) and waits up to 3 s for the board's answer on `evsim/reply`. "Validated" means the board accepted it;
  "Not validated" gives the board's reason (for example A to C is refused until B). This is the page's only write to
  MQTT. Going to A opens the pilot: the charger stops and the inverter loses its DC input for about two minutes.
- Each source fails alone: with the broker down the charger part still works, and the reverse.

Design: `docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md`.

## Test

    cd server
    .venv/bin/python -m unittest discover -s tests -v

The bench tests (`tests/test_bench.py`) start their own Mosquitto on a free port and take about a minute.
They are skipped when `mosquitto` or `paho-mqtt` is missing.

## Driving the inverter

By default the server only publishes the setpoint. To also drive the Fronius GEN24 over Modbus TCP:

    .venv/bin/python -m setpoint_server --inverter 192.168.21.120

The averaged setpoint is the DC power to take from the charger. The server caps the inverter's AC output so
that it takes about that much (calibration in `memory/evsim-inverter-fronius.md`), and publishes the inverter's
measured DC and AC power once a second on `evsim/inverter`.

- New targets are written only when they move by 100 W or more, and the cap is re-committed every 10 s.
- With no data the cap goes to 0 % at once (about 94 W of DC draw remains). After 120 s of no data
  (`--standby-after`) the inverter is put in standby (`Conn` = 0); coming back from standby takes about 57 s.
- On a clean stop (Ctrl-C) the server leaves the inverter at a 0 % cap.
- Known limit: if the link to the inverter dies while it holds a cap, the cap stays; the status shows
  `unreachable`. Design: `docs/superpowers/specs/2026-09-30-inverter-writer-design.md`.

Read or cap the inverter by hand with `tools/fronius_modbus.py` (`--help`).
