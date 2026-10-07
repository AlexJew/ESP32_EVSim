# EVSim

Bachelor thesis project (Travail de bachelor, HES). An **ESP32 board emulates an electric vehicle** toward a
MENNEKES AMTRON Charge Control 11 kW wallbox, and the power the charger offers is turned into a **real load on a
Fronius GEN24 inverter**. The point is to close the loop from a current limit on the charger to a measurable load,
without a physical EV.

## How it works

```
 web page ──Modbus TCP──▶ AMTRON charger ──CP pilot (PWM)──▶ ESP32 (EVSim board)
 (set current limit)                                              │ evsim/cp  (5 Hz, MQTT)
                                                                  ▼
                                                    setpoint server (averages 1 s)
                                                     │ evsim/setpoint (MQTT)
                                                     │ Modbus TCP: cap the inverter
                                                     ▼
 AMTRON AC out ──rectifier──▶ DC ──▶ Fronius GEN24 (MPPT 1) ──▶ AC grid
```

1. The **web page** sets the charger's maximum current (Modbus register 1000) and shows what the charger reports.
2. The charger encodes that limit as the duty cycle of the **CP pilot** signal. The **ESP32** measures it, converts
   duty to amps to watts, and publishes the result on `evsim/cp` over MQTT. It also fakes the vehicle's pilot states
   (A, B, C) with two MOSFETs, driven from `evsim/cmd/state`.
3. The **setpoint server** averages `setpoint_w` over 1 s, publishes it on `evsim/setpoint`, and (with `--inverter`)
   caps the inverter over Modbus TCP so that its DC input from the charger side equals the setpoint. It reports the
   inverter's measured power on `evsim/inverter`.

The inverter is the load: it draws DC power from the charger side (through a rectifier, on MPPT 1) and exports it to the
grid. There is no battery.

## Repository layout

| Path | What it is |
|---|---|
| `EVSim/` | ESP32 firmware (PlatformIO, Arduino framework): CP measurement, pilot-state control, WiFi, MQTT |
| `server/setpoint_server/` | The setpoint server (Python): MQTT averaging and the Fronius writer |
| `server/charger_web/` | The local page (Python): Modbus TCP to the AMTRON, plus a dashboard from MQTT (`paho-mqtt`) that can command the board's pilot state |
| `server/tests/` | Tests for both servers, with fake chargers and inverters |
| `tools/` | Scripts: Mosquitto config, Fronius and AMTRON Modbus tools, charger and jitter loggers |
| `data/` | Calibration and measurement logs |
| `docs/superpowers/` | Design specs and implementation plans |
| `memory/` | Working notes: network, registers, calibration, open questions |

## Network

All devices share the laboratory subnet `192.168.21.0/24` (SSID `hems`, no DHCP, no internet). Everything is addressed
by IP.

| Device | Address |
|---|---|
| Development computer (Mosquitto broker, servers) | `192.168.21.137` |
| ESP32 (EVSim board) | `192.168.21.138` |
| AMTRON charger | `192.168.21.117` |
| Fronius GEN24 12.0 Plus SC | `192.168.21.120` |

## Set up (once)

    cd server
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

You also need Mosquitto (`brew install mosquitto`) and PlatformIO for the firmware. PlatformIO is not on the `PATH`;
it lives at `~/.platformio/penv/bin/pio`.

On the AMTRON's web configuration, enable the Modbus TCP server once: "Modbus TCP Server for energy management
systems" On, port 502, register address set MENNEKES, "Allow Start/Stop Transaction" Off, and set the operator limit
to 16 A.

## Run

Start these in this order, each in its own terminal.

### 1. The MQTT broker

From the repository root. The config makes Mosquitto listen on the LAN (it listens on localhost only by default),
which the board needs:

    /opt/homebrew/sbin/mosquitto -c tools/mosquitto-lab.conf -v

### 2. The ESP32 firmware

Only when the firmware changed. Plug the board in over USB:

    cd EVSim
    ~/.platformio/penv/bin/pio run -e esp32dev -t upload
    ~/.platformio/penv/bin/pio device monitor

Use `-e esp32dev-lowvoltage` for the interim bench setup (ADC-based reader). The board joins the WiFi and publishes on
the broker by itself.

### 3. The setpoint server

    cd server
    .venv/bin/python -m setpoint_server                                  # publishes evsim/setpoint only
    .venv/bin/python -m setpoint_server --inverter 192.168.21.120        # also drives the Fronius inverter

The broker is `127.0.0.1:1883` by default (`--host`, `--port` to change). Stop it with Ctrl-C: it publishes
`server_offline` and, with `--inverter`, leaves the inverter at a 0 % cap. Without `--inverter` the inverter is never
touched.

### 4. The web page (charger and dashboard)

    cd server
    .venv/bin/python -m charger_web

Open <http://127.0.0.1:8080/>. It binds to loopback only. `EVSIM_CHARGER_HOST` overrides the charger address and
`--port` the page's port. While it runs it also keeps the charger alive: after 30 s without Modbus traffic the AMTRON
drops to 0 A.

Below the charger controls the page shows the board's `evsim/cp`, the setpoint topic (`EVSIM_SETPOINT_TOPIC`),
the server's DC target and the inverter's DC power, with a 10-minute chart. The DC target and DC power come from
`evsim/inverter`, so they appear only while the setpoint server runs with `--inverter`. `EVSIM_MQTT_HOST` changes the
broker. See `server/README.md`.

## Watch and control over MQTT

    mosquitto_sub -v -t 'evsim/#'                    # everything
    mosquitto_sub -v -t evsim/setpoint               # the averaged setpoint
    mosquitto_sub -v -t evsim/inverter               # the inverter's measured DC and AC power

| Topic | From | Content |
|---|---|---|
| `evsim/cp` | board | CP measurement and the held `setpoint_w`, 5 Hz |
| `evsim/setpoint` | server | 1 s average of `setpoint_w`, retained |
| `evsim/inverter` | server (`--inverter`) | DC target, cap, measured DC and AC power, inverter state |
| `evsim/state`, `evsim/status`, `evsim/reply` | board | pilot state, online/offline, replies |
| `evsim/cmd/state` | you | command the pilot state |

Command the vehicle state by publishing to the **command** topic (`evsim/state` is where the board reports it):

    mosquitto_pub -h 127.0.0.1 -t evsim/cmd/state -m B     # vehicle connected
    mosquitto_pub -h 127.0.0.1 -t evsim/cmd/state -m C     # ready to charge
    mosquitto_pub -h 127.0.0.1 -t evsim/cmd/state -m A     # unplugged

## Command-line tools

    server/.venv/bin/python tools/fronius_modbus.py --help        # probe, status, watch, set, off, conn
    server/.venv/bin/python tools/fronius_modbus.py status        # read-only
    server/.venv/bin/python tools/amtron_modbus_probe.py --help   # read-only scan of the AMTRON's registers

The AMTRON restores its last limit on the first Modbus request after a fallback, even a read, so any read can resume
charging.

## Tests

    cd server
    .venv/bin/python -m unittest discover -s tests          # servers, about two minutes
    cd ../EVSim
    ~/.platformio/penv/bin/pio test -e native               # firmware logic, on the computer

The bench tests in `server/tests/test_bench.py` start their own Mosquitto and are skipped when it is missing.

## More

- `server/README.md`: the servers in detail (inverter behaviour, the charger page's design, what to know).
- `docs/superpowers/specs/`: design records. `docs/superpowers/plans/`: implementation plans.
- `memory/`: notes on the network, the AMTRON and Fronius Modbus registers, calibration, and open questions.

## Safety

Modbus and the lab MQTT broker have no authentication: anything on the laboratory network can set the charger's
current or cap the inverter. The inverter can draw more than the charger offers if left uncapped, so keep the offer
low or a cap in place when testing.
