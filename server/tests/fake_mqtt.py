"""Stands in for paho's client: records what the code under test asks of it and lets a test drive the callbacks."""

from types import SimpleNamespace

OK = SimpleNamespace(is_failure=False)
REFUSED = SimpleNamespace(is_failure=True)


class FakeMqttClient:
    def __init__(self):
        self.subscribed = []           # (topic, qos), in order
        self.published = []            # (topic, payload, qos, retain), in order
        self.publish_rc = 0            # what publish() reports; 4 is paho's "not connected"
        self.publish_hook = None       # called with (topic, payload) while publishing: a test's way to answer at once
        self.calls = []                # ("connect_async", host, port), ("loop_start",), ("disconnect",), ("loop_stop",)
        self.on_connect = self.on_message = self.on_disconnect = None

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        if self.publish_hook is not None:
            self.publish_hook(topic, payload)
        return SimpleNamespace(rc=self.publish_rc)

    def connect_async(self, host, port, keepalive=60):
        self.calls.append(("connect_async", host, port))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    # -- what the broker would do ---------------------------------------------------------------

    def connect(self, reason=OK):
        self.on_connect(self, None, None, reason, None)

    def drop(self):
        self.on_disconnect(self, None, None, None, None)

    def deliver(self, topic, payload, retain=False):
        data = payload.encode() if isinstance(payload, str) else payload
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=data, retain=retain))
