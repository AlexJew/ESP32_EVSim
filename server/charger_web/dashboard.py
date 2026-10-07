"""Owns the lifecycle of the dashboard's two sources and the state command, and gives the web layer its calls."""

from . import config
from .commands import CommandError, StateCommander
from .history import History
from .telemetry import Telemetry, build_client


class Dashboard:
    def __init__(self, telemetry, history, mqtt_client=None, broker=None, commander=None):
        self._telemetry = telemetry
        self._history = history
        self._client = mqtt_client
        self._broker = broker              # (host, port)
        self._commander = commander
        self._started = False

    def telemetry(self):
        """The latest readings: board, setpoint, the server's target and the inverter's measurements."""
        return self._telemetry.snapshot()

    def history(self):
        return self._history.snapshot()

    def set_state(self, state):
        """Command the board's pilot state; raises CommandError (its `status` is the HTTP status)."""
        if self._commander is None:
            raise CommandError(503, "State commands are not available.")
        return self._commander.set_state(state)

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


def build_dashboard(charger=None):
    """The real dashboard from the environment. Raises ValueError for a bad setting; starts nothing.

    `charger` is the charger service: the history reads its cached status for the meter values."""
    mqtt_host, topic = config.mqtt_host(), config.setpoint_topic()
    client = build_client()
    telemetry = Telemetry(client, topic)
    history = History(telemetry, charger)
    commander = StateCommander(client, telemetry)
    return Dashboard(telemetry, history, client, (mqtt_host, config.MQTT_PORT), commander)
