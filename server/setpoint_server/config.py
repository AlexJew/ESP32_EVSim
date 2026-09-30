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
TOPIC_SETPOINT = "evsim/setpoint"    # out: the averaged setpoint, QoS 1, retained

WINDOW_S = 1                         # averaging window, and the tick period
MAX_SETPOINT_W = 11000               # the charger's 11 kW rating; a larger setpoint_w is dropped, not clamped
KEEPALIVE_S = 10
RECONNECT_MIN_S = 1                  # paho's backoff toward an unreachable broker
RECONNECT_MAX_S = 5
SHUTDOWN_PUBLISH_TIMEOUT_S = 2       # how long a clean stop waits for the broker's ack
