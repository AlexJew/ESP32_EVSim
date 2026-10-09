"""Runtime configuration for the standalone charger web process."""

import os

DEFAULT_CHARGER_HOST = "192.168.21.117"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
MODBUS_PORT = 502

# The Modbus charger service (docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md).
MODBUS_TIMEOUT_S = 3
POLL_PERIOD_S = 1                 # a poll starts this often (start to start); it is also the charger's heartbeat
STALE_AFTER_S = 5                 # a reading older than this, or polls failing this long, is stale
FALLBACK_AFTER_S = 30             # no Modbus traffic for this long: the charger drops to its safe current
RESTORE_MIN_INTERVAL_S = 10       # the desired limit is re-written at most this often
SIGNALLED_SETTLE_S = 6            # after an Apply the old signalled current stays stale this long if it never changes
MIN_LIMIT_A = 6                   # the lowest non-zero limit the page accepts
MAX_LIMIT_A = 16                  # the highest limit the page accepts

# The dashboard (docs/superpowers/specs/2026-10-01-evsim-dashboard-design.md). Read-only, except the state command.
DEFAULT_MQTT_HOST = "127.0.0.1"   # Mosquitto on the same computer
MQTT_PORT = 1883
MQTT_CLIENT_ID = "evsim-charger-web"   # a prefix: each process adds a random suffix, since equal ids kick each other off
MQTT_KEEPALIVE_S = 10
MQTT_RECONNECT_MIN_S = 1
MQTT_RECONNECT_MAX_S = 5
MQTT_STALE_AFTER_S = 3            # an MQTT value older than this is stale (the slowest topic publishes every 1 s)
TOPIC_CP = "evsim/cp"             # the board's telemetry, 5 Hz
DEFAULT_SETPOINT_TOPIC = "evsim/setpoint"   # becomes evsim/electric_car with the car emulation
TOPIC_INVERTER = "evsim/inverter"           # the setpoint server's inverter status, 1 Hz
MAX_SETPOINT_W = 12000            # the inverter's rating; a larger setpoint_w is dropped, not clamped
MAX_DC_W = 30000                  # a measured DC power above this is not believed (the inverter is a 12 kW unit)
MAX_A = 100                       # likewise for a measured current ...
MAX_V = 1000                      # ... and a measured voltage (the DC side runs near 520 V)
TOPIC_CMD_STATE = "evsim/cmd/state"   # the page publishes A, B or C here (QoS 0, not retained)
TOPIC_REPLY = "evsim/reply"           # the board's one-line answer to every command
STATE_REPLY_TIMEOUT_S = 3             # how long a state command waits for the board's answer

HISTORY_WINDOW_S = 600            # the chart shows the last 10 minutes
HISTORY_PERIOD_S = 1              # one frame per second


def _host(variable, default):
    host = os.environ.get(variable, default).strip()
    if not host or any(char in host for char in "/?#@"):
        raise ValueError(f"{variable} must be a host name or address, without a URL path.")
    return host


def charger_host():
    return _host("EVSIM_CHARGER_HOST", DEFAULT_CHARGER_HOST)


def mqtt_host():
    return _host("EVSIM_MQTT_HOST", DEFAULT_MQTT_HOST)


def setpoint_topic():
    topic = os.environ.get("EVSIM_SETPOINT_TOPIC", DEFAULT_SETPOINT_TOPIC).strip()
    if not topic or len(topic) > 128 or any(char in topic for char in "+#") or any(char.isspace() for char in topic):
        raise ValueError("EVSIM_SETPOINT_TOPIC must be one MQTT topic: no wildcards (+ #) or spaces, at most 128 characters.")
    return topic


def modbus_register_map():
    return os.environ.get("EVSIM_CHARGER_REGISTERS") or None
