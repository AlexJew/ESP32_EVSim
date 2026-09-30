"""Constants for the setpoint server, in the style of the firmware's mqtt_config.h.

The broker is Mosquitto on the same computer, so the server reaches it over
loopback; the board reaches the same broker at 192.168.21.137:1883. When the
broker moves, only BROKER_HOST changes (or --host on the command line).

No credentials: stage one is an unauthenticated broker on the laboratory LAN.
"""

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
CLIENT_ID = "evsim-setpoint-server"

TOPIC_CP = "evsim/cp"                # in: the board's telemetry, QoS 0
TOPIC_SETPOINT = "evsim/setpoint"    # out: the averaged setpoint, QoS 0, retained

WINDOW_S = 1                         # averaging window, and the tick period
MAX_SETPOINT_W = 11000               # the charger's 11 kW rating; a larger setpoint_w is dropped, not clamped
KEEPALIVE_S = 10
RECONNECT_MIN_S = 1                  # paho's backoff toward an unreachable broker
RECONNECT_MAX_S = 5
SHUTDOWN_PUBLISH_TIMEOUT_S = 2       # how long a clean stop waits for the broker's ack


# The inverter writer (see docs/superpowers/specs/2026-09-30-inverter-writer-design.md).
INVERTER_TOPIC = "evsim/inverter"    # out: inverter status, QoS 0, not retained
INVERTER_PORT = 502                  # Modbus TCP
INVERTER_UNIT = 1
INVERTER_TIMEOUT_S = 3               # connect and read timeout toward the inverter
DEADBAND_W = 100                     # a new DC target is written only if it moved this much
REFRESH_S = 10                       # re-commit the current cap this often
STANDBY_AFTER_S = 120                # no data for this long: Conn = 0
MAILBOX_STALE_S = 3                  # a target older than this counts as no data
WRITER_PERIOD_S = 1                  # the writer's step
BACKOFF_MIN_S = 1                    # retry delay toward an unreachable inverter
BACKOFF_MAX_S = 5

# Calibration of the inverter, fitted on 2026-09-30 from 8 AC caps between 1 and 7.3 kW: the DC power it
# takes from the charger side is DC = DC_PER_AC * AC + DC_OFFSET_W (worst residual 0.9 % at 2 kW, else
# under 0.25 %). The AC output also settles about AC_SETTLE_W below the cap.
# See memory/evsim-inverter-fronius.md.
DC_PER_AC = 1.0303
DC_OFFSET_W = 6.3
AC_SETTLE_W = 3.0
