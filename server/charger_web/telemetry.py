"""The MQTT side of the dashboard: the latest parsed value of each topic, with its receive time.

The client is handed in, so the tests use a fake; `build_client` makes the real paho one. Subscribing happens
in on_connect, so it is renewed on every reconnect. This module sets no will and never publishes: the state
command (commands.py) publishes through the same client and waits here for the board's reply.
paho's network thread calls the on_* callbacks; one lock guards the state they and the readers share.
"""

import logging
import threading
import time
from collections import deque

from . import config
from .parsers import parse_cp, parse_inverter, parse_reply, parse_setpoint
from .reading import reading

log = logging.getLogger("charger_web.telemetry")

INVERTER_FIELDS = ("dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v")


def build_client():
    """The real paho client: its own id, a short reconnect delay, not yet connected."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise ValueError("the dashboard needs paho-mqtt; run: pip install -r requirements.txt") from exc
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.MQTT_CLIENT_ID)
    client.reconnect_delay_set(min_delay=config.MQTT_RECONNECT_MIN_S, max_delay=config.MQTT_RECONNECT_MAX_S)
    return client


class Telemetry:
    def __init__(self, client, setpoint_topic, clock=time.monotonic, wall=time.time):
        self._setpoint_topic = setpoint_topic
        self._clock = clock
        self._wall = wall
        self._routes = {
            config.TOPIC_CP: ("cp", parse_cp),
            setpoint_topic: ("setpoint", parse_setpoint),
            config.TOPIC_INVERTER: ("server", parse_inverter),
            config.TOPIC_REPLY: ("reply", parse_reply),
        }
        self._lock = threading.Lock()
        self._reply_arrived = threading.Condition(self._lock)
        self._connected = False
        self._latest = {}               # kind -> {"data": parsed dict, "at": receive time, "retained": bool (shown stale)}
        self._cp_samples = []           # raw evsim/cp setpoint_w values since the last drain
        self._replies = deque(maxlen=16)    # (sequence number, text) of the board's recent replies
        self._reply_seq = 0
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

    # -- paho's callbacks --------------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            return
        for topic in self._routes:
            client.subscribe(topic, qos=0)
        with self._lock:
            self._connected = True
        log.info("connected, subscribed to %s", ", ".join(self._routes))

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._lock:
            self._connected = False
        log.warning("disconnected from the broker: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        route = self._routes.get(msg.topic)
        if route is None:
            return
        kind, parse = route
        data = parse(msg.payload)
        if data is None:
            log.warning("dropping unusable %s message: %r", msg.topic, bytes(msg.payload[:80]))
            return
        retained = bool(msg.retain)
        if kind == "reply":
            if retained:                # a reply is an event, never a state: a retained copy is old news
                return
            with self._reply_arrived:
                self._reply_seq += 1
                self._replies.append((self._reply_seq, data["text"]))
                self._reply_arrived.notify_all()
            return
        # The setpoint server's last word is "I am gone". A connected subscriber gets it live, not retained, but
        # it is the last known state all the same: whatever setpoint it carries, it is no longer a fresh value.
        gone = kind == "setpoint" and data.get("status") == "server_offline"
        now = self._clock()
        with self._lock:
            current = self._latest.get(kind)
            if retained and current is not None and not current["retained"] \
                    and now - current["at"] < config.MQTT_STALE_AFTER_S:
                return                  # a retained copy never replaces a fresh live value
            self._latest[kind] = {"data": data, "at": now, "retained": retained or gone}
            if kind == "cp" and not retained:
                self._cp_samples.append(data["setpoint_w"])

    # -- what the page, the history and the state command read ---------------------------------------

    def drain_cp_samples(self):
        """The raw evsim/cp setpoint_w values received since the last call, oldest first."""
        with self._lock:
            samples, self._cp_samples = self._cp_samples, []
        return samples

    def reply_marker(self):
        """A marker for wait_reply: replies after it are the ones a command published now can be answered by."""
        with self._lock:
            return self._reply_seq

    def wait_reply(self, since, accept, timeout_s):
        """The first board reply after marker `since` for which accept(text) is true, or None after timeout_s."""
        end = time.monotonic() + timeout_s
        with self._reply_arrived:
            while True:
                for seq, text in self._replies:
                    if seq > since and accept(text):
                        return text
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self._reply_arrived.wait(remaining)

    def snapshot(self):
        with self._lock:
            now = self._clock()
            latest = dict(self._latest)
            connected = self._connected

        def value(kind, field):
            entry = latest.get(kind)
            if entry is None:
                return reading(None, None, now, config.MQTT_STALE_AFTER_S)
            return reading(entry["data"].get(field), entry["at"], now, config.MQTT_STALE_AFTER_S,
                           force_stale=entry["retained"], wall=self._wall)

        def text(kind, field):
            entry = latest.get(kind)
            return None if entry is None else entry["data"].get(field)

        return {
            "mqtt": {"connected": connected},
            "board": {"setpoint_w": value("cp", "setpoint_w"), "state": text("cp", "state"), "status": text("cp", "status")},
            "setpoint": {"topic": self._setpoint_topic, "setpoint_w": value("setpoint", "setpoint_w"),
                         "status": text("setpoint", "status")},
            "server": {"target_dc_w": value("server", "target_dc_w"), "mode": text("server", "mode"),
                       "inverter_state": text("server", "inverter_state")},
            "inverter": {name: value("server", name) for name in INVERTER_FIELDS},   # one message: they age together
        }
