"""Owns the lifecycle of the dashboard's two read-only sources and gives the web layer two methods to call."""

from . import config
from .history import History
from .telemetry import Telemetry, build_client


class Dashboard:
    def __init__(self, telemetry, history, mqtt_client=None, broker=None):
        self._telemetry = telemetry
        self._history = history
        self._client = mqtt_client
        self._broker = broker              # (host, port)
        self._started = False

    def telemetry(self):
        """The latest readings: board, setpoint, the server's target and the inverter's DC power."""
        return self._telemetry.snapshot()

    def history(self):
        return self._history.snapshot()

    def start(self):
        """Connect the broker and start the sampler. connect_async: a broker that is down is retried, never raised."""
        if self._started:
            return
        self._started = True
        if self._client is not None:
            host, port = self._broker
            self._client.connect_async(host, port, keepalive=config.MQTT_KEEPALIVE_S)
            self._client.loop_start()
        self._history.start()

    def stop(self):
        if not self._started:
            return
        self._started = False
        self._history.stop()
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()


def build_dashboard():
    """The real dashboard from the environment. Raises ValueError for a bad setting; starts nothing."""
    mqtt_host, topic = config.mqtt_host(), config.setpoint_topic()
    client = build_client()
    telemetry = Telemetry(client, topic)
    history = History(telemetry)
    return Dashboard(telemetry, history, client, (mqtt_host, config.MQTT_PORT))
