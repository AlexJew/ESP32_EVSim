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

## Local AMTRON charger page

The charger page runs as a separate process from the MQTT averaging server. It
uses only the Python standard library, binds to `127.0.0.1`, and is available
only on the development computer. The setpoint server does not need to be
changed or restarted to use the page.

Set the charger account in the shell that will run the page. Do not put the
credentials in project files:

    export EVSIM_CHARGER_USERNAME='your charger username'
    export EVSIM_CHARGER_PASSWORD='your charger password'

Start the two processes in separate terminals from `server/`:

    .venv/bin/python -m charger_web

    .venv/bin/python -m setpoint_server

Open `http://127.0.0.1:8080/`. Set `EVSIM_CHARGER_HOST` to override the default
charger address `192.168.21.117`, or pass `--port` to use another local page
port. Stop either process with Ctrl-C; the other process continues running.

The web process obtains a temporary REST session through the CC612 challenge
login and keeps the session ID in memory. The login endpoint is present on the
lab unit, but the configured account's access and the write/readback behavior
still need verification on this firmware. The AMTRON currently serves plain
HTTP, so the page process must remain bound to loopback.

Watch the output:

    mosquitto_sub -v -t evsim/setpoint

## Test

    cd server
    .venv/bin/python -m unittest discover -s tests -v

The bench tests (`tests/test_bench.py`) start their own Mosquitto on a free port and take about a minute.
They are skipped when `mosquitto` or `paho-mqtt` is missing.
