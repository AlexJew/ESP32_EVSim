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
