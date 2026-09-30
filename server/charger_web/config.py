"""Runtime configuration for the standalone charger web process."""

import os

DEFAULT_CHARGER_HOST = "192.168.21.117"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
HTTP_TIMEOUT_S = 3
SETTINGS_CACHE_S = 30
DASHBOARD_CACHE_S = 2
LOGIN_RETRY_DELAY_S = 5
IDLE_RELEASE_S = 10


def charger_host():
    host = os.environ.get("EVSIM_CHARGER_HOST", DEFAULT_CHARGER_HOST).strip()
    if not host or any(char in host for char in "/?#@"):
        raise ValueError("EVSIM_CHARGER_HOST must be a host name or address, without a URL path.")
    return host


def charger_credentials():
    return (
        os.environ.get("EVSIM_CHARGER_USERNAME", ""),
        os.environ.get("EVSIM_CHARGER_PASSWORD", ""),
    )


MODBUS_PORT = 502


def modbus_register_map():
    return os.environ.get("EVSIM_CHARGER_REGISTERS") or None
