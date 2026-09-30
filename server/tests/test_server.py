import logging
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from setpoint_server import config
from setpoint_server.messages import format_setpoint, offline_payload
from setpoint_server.server import SetpointServer

NOW = datetime(2026, 9, 30, 13, 42, 7, tzinfo=timezone.utc)
OK = SimpleNamespace(is_failure=False)
REFUSED = SimpleNamespace(is_failure=True)


class FakeInfo:
    def __init__(self, fail_wait=False):
        self.rc = 0
        self.waited = None
        self._fail_wait = fail_wait

    def wait_for_publish(self, timeout=None):
        self.waited = timeout
        if self._fail_wait:
            raise RuntimeError("Message publish failed")


class FakeClient:
    """Stands in for paho's client: records what the server asks of it."""

    def __init__(self, connected=True, fail_wait=False):
        self.connected = connected
        self.fail_wait = fail_wait
        self.published = []    # (topic, payload, qos, retain)
        self.infos = []
        self.subscribed = []   # (topic, qos)
        self.will = None
        self.disconnected = False
        self.on_connect = self.on_message = self.on_disconnect = None

    def is_connected(self):
        return self.connected

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        info = FakeInfo(self.fail_wait)
        self.infos.append(info)
        return info

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def disconnect(self):
        self.disconnected = True


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def message(payload, retain=False):
    return SimpleNamespace(topic=config.TOPIC_CP, payload=payload, retain=retain)


class SetpointServerTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.client = FakeClient()
        self.clock = FakeClock()
        self.server = SetpointServer(self.client, clock=self.clock, utcnow=lambda: NOW)

    def feed(self, payload):
        self.client.on_message(self.client, None, message(payload))

    def test_registers_the_last_will(self):
        self.assertEqual(
            self.client.will,
            (config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), 1, True),
        )

    def test_subscribes_on_every_connect(self):
        self.client.on_connect(self.client, None, {}, OK, None)
        self.client.on_connect(self.client, None, {}, OK, None)  # a reconnect
        self.assertEqual(self.client.subscribed, [(config.TOPIC_CP, 0)] * 2)

    def test_a_refused_connect_does_not_subscribe(self):
        self.client.on_connect(self.client, None, {}, REFUSED, None)
        self.assertEqual(self.client.subscribed, [])

    def test_tick_publishes_the_mean_retained_at_qos0(self):
        self.feed(b'{"setpoint_w":1000}')
        self.clock.t = 100.2
        self.feed(b'{"setpoint_w":2000}')
        self.clock.t = 100.4
        self.server.tick()
        self.assertEqual(
            self.client.published,
            [(config.TOPIC_SETPOINT, format_setpoint(2, 1500.0, config.WINDOW_S, NOW), 0, True)],
        )

    def test_tick_with_no_samples_publishes_no_data(self):
        self.server.tick()
        self.assertEqual(
            self.client.published,
            [(config.TOPIC_SETPOINT, format_setpoint(0, None, config.WINDOW_S, NOW), 0, True)],
        )

    def test_old_samples_do_not_leak_into_a_later_tick(self):
        self.feed(b'{"setpoint_w":1000}')
        self.clock.t = 105.0
        self.server.tick()
        self.assertIn('"status":"no_data"', self.client.published[-1][1])

    def test_unusable_messages_are_dropped(self):
        for payload in (b"garbage", b'{"setpoint_w":-5}', b'{"setpoint_w":"x"}',
                        b'{"setpoint_w":null}', b'{"setpoint_w":12001}'):
            self.feed(payload)
        self.feed(b'{"setpoint_w":700}')
        self.server.tick()
        self.assertEqual(
            self.client.published[-1][1],
            format_setpoint(1, 700.0, config.WINDOW_S, NOW),
        )

    def test_a_retained_cp_message_is_not_a_live_sample(self):
        # The board never retains evsim/cp, so a retained copy delivered on
        # subscribe is stale by definition (e.g. a bench `mosquitto_pub -r`).
        self.client.on_message(self.client, None, message(b'{"setpoint_w":10000}', retain=True))
        self.server.tick()
        self.assertIn('"status":"no_data"', self.client.published[-1][1])

    def test_tick_publishes_nothing_while_disconnected(self):
        self.client.connected = False
        self.feed(b'{"setpoint_w":1000}')
        self.server.tick()
        self.assertEqual(self.client.published, [])

    def test_shutdown_publishes_offline_waits_and_disconnects(self):
        self.server.shutdown()
        self.assertEqual(
            self.client.published,
            [(config.TOPIC_SETPOINT, offline_payload(config.WINDOW_S), 1, True)],
        )
        self.assertEqual(self.client.infos[0].waited, config.SHUTDOWN_PUBLISH_TIMEOUT_S)
        self.assertTrue(self.client.disconnected)

    def test_shutdown_while_disconnected_neither_publishes_nor_waits(self):
        self.client.connected = False
        self.server.shutdown()
        self.assertEqual(self.client.published, [])
        self.assertTrue(self.client.disconnected)

    def test_shutdown_survives_a_failed_publish(self):
        client = FakeClient(fail_wait=True)
        SetpointServer(client, clock=self.clock, utcnow=lambda: NOW).shutdown()
        self.assertTrue(client.disconnected)

    def test_run_stops_when_asked_and_publishes_offline(self):
        thread = threading.Thread(target=self.server.run)
        thread.start()
        self.server.request_stop()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.client.published[-1][1], offline_payload(config.WINDOW_S))
        self.assertTrue(self.client.disconnected)


class FakeWriter:
    """Stands in for the InverterWriter: records what the server hands it."""

    def __init__(self, client):
        self._client = client
        self.targets = []
        self.published_when_stopped = None
        self.disconnected_when_stopped = None

    def set_target(self, watts):
        self.targets.append(watts)

    def stop(self):
        self.published_when_stopped = list(self._client.published)
        self.disconnected_when_stopped = self._client.disconnected


class WriterIntegrationTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.client = FakeClient()
        self.clock = FakeClock()
        self.writer = FakeWriter(self.client)
        self.server = SetpointServer(self.client, clock=self.clock, utcnow=lambda: NOW, writer=self.writer)

    def feed(self, payload):
        self.client.on_message(self.client, None, message(payload))

    def test_tick_hands_the_mean_to_the_writer(self):
        self.feed(b'{"setpoint_w":1000}')
        self.clock.t = 100.2
        self.feed(b'{"setpoint_w":2000}')
        self.clock.t = 100.4
        self.server.tick()
        self.assertEqual(self.writer.targets, [1500.0])

    def test_no_data_hands_none_to_the_writer(self):
        self.server.tick()
        self.assertEqual(self.writer.targets, [None])

    def test_the_writer_is_fed_even_when_mqtt_is_down(self):
        self.client.connected = False
        self.feed(b'{"setpoint_w":1000}')
        self.server.tick()
        self.assertEqual(self.writer.targets, [1000.0])
        self.assertEqual(self.client.published, [])

    def test_shutdown_stops_the_writer_after_the_offline_message_and_before_disconnecting(self):
        self.server.shutdown()
        self.assertEqual(self.writer.published_when_stopped[-1][1], offline_payload(config.WINDOW_S))
        self.assertFalse(self.writer.disconnected_when_stopped)
        self.assertTrue(self.client.disconnected)

    def test_shutdown_stops_the_writer_even_when_mqtt_is_down(self):
        self.client.connected = False
        self.server.shutdown()
        self.assertEqual(self.writer.published_when_stopped, [])
        self.assertTrue(self.client.disconnected)


if __name__ == "__main__":
    unittest.main()
