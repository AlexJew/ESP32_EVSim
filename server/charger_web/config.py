"""Runtime configuration for the standalone charger web process."""

import os

DEFAULT_CHARGER_HOST = "192.168.21.117"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
MODBUS_PORT = 502

# The Modbus charger service (docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md).
MODBUS_TIMEOUT_S = 3
POLL_PERIOD_S = 2                 # the poller reads this often; it is also the charger's heartbeat
STALE_AFTER_S = 5                 # a reading older than this, or polls failing this long, is stale
FALLBACK_AFTER_S = 30             # no Modbus traffic for this long: the charger drops to its safe current
RESTORE_MIN_INTERVAL_S = 10       # the desired limit is re-written at most this often
MIN_LIMIT_A = 6                   # the lowest non-zero limit the page accepts
MAX_LIMIT_A = 16                  # the highest limit the page accepts


def charger_host():
    host = os.environ.get("EVSIM_CHARGER_HOST", DEFAULT_CHARGER_HOST).strip()
    if not host or any(char in host for char in "/?#@"):
        raise ValueError("EVSIM_CHARGER_HOST must be a host name or address, without a URL path.")
    return host


def modbus_register_map():
    return os.environ.get("EVSIM_CHARGER_REGISTERS") or None
