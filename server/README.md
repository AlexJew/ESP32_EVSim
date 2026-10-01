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
limit over **Modbus TCP**. It uses only the Python standard library and binds to `127.0.0.1`, so it is available
only on the development computer.

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
  service puts it back, at most once every 10 s.
- The service never writes on its own at start. But the charger remembers the last limit written and restores it on
  the first Modbus request after a fallback, so starting this page can resume charging at that limit.
- Modbus has no login: anything on the lab network can set the charger's current. Keep the page on loopback.

Design: `docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md`. The read-only scanner
`tools/amtron_modbus_probe.py` lists registers (any read can resume charging after a fallback).

## Test

    cd server
    .venv/bin/python -m unittest discover -s tests -v

The bench tests (`tests/test_bench.py`) start their own Mosquitto on a free port and take about a minute.
They are skipped when `mosquitto` or `paho-mqtt` is missing.
