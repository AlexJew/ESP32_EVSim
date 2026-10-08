"""The MQTT side of the recorder: stamp every message of the chain the moment it arrives and queue it.

Subscribing happens in on_connect, so it is renewed on every reconnect. The callbacks do nothing but stamp, hand the
message to the store's queue and return; an exception here would kill paho's network thread, so none escapes.
"""

import logging
import secrets
import threading
import time

log = logging.getLogger("evsim_recorder.capture")

TOPIC = "evsim/#"
KEEPALIVE_S = 10
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 5


def build_client():
    """The real paho client: a unique id (equal ids kick each other off the broker), a short reconnect delay."""
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"evsim-recorder-{secrets.token_hex(3)}")
    client.reconnect_delay_set(min_delay=RECONNECT_MIN_S, max_delay=RECONNECT_MAX_S)
    return client


class Capture:
    def __init__(self, client, store, clock_ns=time.time_ns, mono_ns=time.monotonic_ns):
        self._store = store
        self._clock_ns = clock_ns
        self._mono_ns = mono_ns
        self._connected = threading.Event()
        self._unreachable = False             # a streak of failed connection attempts: one event, not one per retry
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail

    def wait_connected(self, timeout_s):
        """True once the broker has accepted the connection (and is still connected), False after timeout_s."""
        return self._connected.wait(timeout_s)

    def _event(self, kind, detail):
        try:
            self._store.add_event(kind, detail)
        except Exception:
            log.exception("could not record a %s event", kind)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            self._event("disconnected", f"refused: {reason_code}")
            return
        client.subscribe(TOPIC, qos=0)
        log.info("connected, subscribed to %s", TOPIC)
        self._unreachable = False
        self._connected.set()
        self._event("connected", TOPIC)

    def _on_connect_fail(self, client, userdata):
        if not self._unreachable:
            self._unreachable = True
            log.warning("cannot reach the broker: messages are not being recorded")
            self._event("disconnected", "could not reach the broker")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        log.warning("disconnected from the broker: %s", reason_code)
        self._connected.clear()
        self._unreachable = True                # the retries that follow are the same streak
        self._event("disconnected", str(reason_code))

    def _on_message(self, client, userdata, msg):
        t_ns = self._clock_ns()                 # first thing: the arrival time
        mono_ns = self._mono_ns()
        try:
            self._store.add_message(t_ns, mono_ns, msg.topic, msg.retain, msg.payload)
        except Exception:
            log.exception("could not queue a message from %s", msg.topic)
