"""The MQTT glue: feed evsim/cp into the averager, publish the mean once a second.

The client is handed in rather than built here, so the tests use a fake and
this module never imports paho. `__main__` builds the real one.

Threads: paho's network thread calls the on_* callbacks; the caller's thread
runs run(). One lock guards the averager, and both sides read the clock inside
it, so a sample is never stamped later than a tick that already ran.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from . import config
from .averager import WindowAverager
from .messages import format_setpoint, offline_payload, parse_cp

log = logging.getLogger("setpoint_server")


def _utcnow():
    return datetime.now(timezone.utc)


class SetpointServer:
    def __init__(self, client, clock=time.monotonic, utcnow=_utcnow):
        self._client = client
        self._clock = clock
        self._utcnow = utcnow
        self._averager = WindowAverager(config.WINDOW_S)
        self._lock = threading.Lock()
        self._stop = threading.Event()

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.will_set(
            config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), qos=1, retain=True
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            return
        # Subscribing here, not once at startup, renews it on every reconnect.
        client.subscribe(config.TOPIC_CP, qos=0)
        log.info("connected, subscribed to %s", config.TOPIC_CP)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        log.warning("disconnected from the broker: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        watts = parse_cp(msg.payload)
        if watts is None:
            log.warning("dropping unusable %s message: %r", msg.topic, bytes(msg.payload[:80]))
            return
        with self._lock:
            self._averager.add(self._clock(), watts)

    def tick(self):
        """Average the window and publish the result once."""
        with self._lock:
            count, mean = self._averager.average(self._clock())
        payload = format_setpoint(count, mean, config.WINDOW_S, self._utcnow())
        if not self._client.is_connected():
            # Publishing now would make paho queue it and replay every stale
            # setpoint in a burst on reconnect.
            log.warning("broker not connected; skipping %s", payload)
            return
        info = self._client.publish(config.TOPIC_SETPOINT, payload, qos=1, retain=True)
        if info.rc != 0:
            log.warning("publish failed (rc=%s): %s", info.rc, payload)
            return
        log.info("published %s", payload)

    def run(self):
        """Tick once per window until request_stop(), then shut down cleanly."""
        period = config.WINDOW_S
        next_tick = self._clock() + period
        while not self._stop.wait(max(0.0, next_tick - self._clock())):
            self.tick()
            next_tick += period  # scheduled, not slept, so a slow publish adds no drift
            now = self._clock()
            if next_tick <= now:  # fell behind: skip the missed ticks, do not burst
                next_tick = now + period
        self.shutdown()

    def request_stop(self):
        """Ask run() to finish. Safe to call from a signal handler."""
        self._stop.set()

    def shutdown(self):
        """Publish server_offline ourselves, then disconnect.

        A broker sends the last-will only after an unclean disconnect, so without
        this a normal stop would leave the last setpoint retained. When the broker
        is unreachable there is nothing to wait for, so do not.
        """
        if self._client.is_connected():
            try:
                info = self._client.publish(
                    config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), qos=1, retain=True
                )
                info.wait_for_publish(timeout=config.SHUTDOWN_PUBLISH_TIMEOUT_S)
                log.info("published server_offline")
            except (RuntimeError, ValueError) as exc:
                log.warning("could not publish server_offline: %s", exc)
        self._client.disconnect()
