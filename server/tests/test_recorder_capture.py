import importlib.util
import os
import shutil
import sqlite3
import tempfile
import unittest

from fake_mqtt import FakeMqttClient, REFUSED
from evsim_recorder.capture import TOPIC, Capture, build_client
from evsim_recorder.storage import Store


class FakeClocks:
    def __init__(self):
        self.wall = 1_791_453_600_000_000_000
        self.mono = 5_000_000_000

    def now(self):
        self.wall += 1_000
        return self.wall

    def tick(self):
        self.mono += 7
        return self.mono


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "c.sqlite")
        self.store = Store(self.path, batch_seconds=0.05)
        self.store.start_session("b", "c", {}, [])
        self.store.start()
        self.addCleanup(self.store.stop)
        self.client = FakeMqttClient()
        self.clocks = FakeClocks()
        self.capture = Capture(self.client, self.store, clock_ns=self.clocks.now, mono_ns=self.clocks.tick)

    def query(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def settle(self):
        self.assertTrue(self.store.flush())

    def test_it_subscribes_to_everything_under_evsim_and_again_after_a_reconnect(self):
        self.client.connect()
        self.assertEqual(self.client.subscribed, [("evsim/#", 0)])
        self.assertEqual(TOPIC, "evsim/#")
        self.client.drop()
        self.client.connect()
        self.assertEqual(self.client.subscribed, [("evsim/#", 0)] * 2)

    def test_a_message_is_stamped_in_the_callback_and_stored_verbatim(self):
        self.client.connect()
        self.client.deliver("evsim/cp", '{"setpoint_w": 4361}')
        self.client.deliver("evsim/state", "C", retain=True)
        self.settle()
        rows = self.query("SELECT t_ns, mono_ns, topic, retained, payload FROM messages ORDER BY id")
        self.assertEqual(rows[0], (1_791_453_600_000_001_000 + 0, 5_000_000_007, "evsim/cp", 0, '{"setpoint_w": 4361}'))
        self.assertEqual(rows[1][1:], (5_000_000_014, "evsim/state", 1, "C"))
        self.assertLess(rows[0][0], rows[1][0])

    def test_connect_and_disconnect_are_recorded_as_events(self):
        self.client.connect()
        self.client.drop()
        self.client.connect()
        self.settle()
        kinds = [k for (k,) in self.query("SELECT kind FROM recorder_events ORDER BY id")]
        self.assertEqual(kinds, ["connected", "disconnected", "connected"])

    def test_a_refused_connection_is_a_disconnected_event_with_the_reason_and_no_subscription(self):
        self.client.connect(REFUSED)
        self.settle()
        self.assertEqual(self.client.subscribed, [])
        kind, detail = self.query("SELECT kind, detail FROM recorder_events")[0]
        self.assertEqual(kind, "disconnected")
        self.assertIn("refused", detail)

    def test_it_never_publishes(self):
        self.client.connect()
        self.client.deliver("evsim/reply", "state C -> B")
        self.client.drop()
        self.assertEqual(self.client.published, [])

    def test_a_failing_store_never_raises_into_the_mqtt_thread(self):
        class Broken:
            session_id = 1

            def add_message(self, *args):
                raise RuntimeError("boom")

            def add_event(self, *args, **kwargs):
                raise RuntimeError("boom")

        client = FakeMqttClient()
        Capture(client, Broken())
        client.connect()
        client.deliver("evsim/cp", "{}")
        client.drop()

    def test_any_payload_is_accepted(self):
        self.client.connect()
        for payload in (b"", b"\xff\xfe", b"{", b"x" * 100_000):
            self.client.deliver("evsim/anything", payload)
        self.settle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(4,)])


@unittest.skipUnless(importlib.util.find_spec("paho"), "paho-mqtt is not installed")
class BuildClientTest(unittest.TestCase):
    def test_the_client_has_its_own_unique_id_and_never_connects_by_itself(self):
        ids = {build_client()._client_id for _ in range(20)}
        self.assertEqual(len(ids), 20)
        self.assertTrue(all(i.startswith(b"evsim-recorder-") for i in ids))
        self.assertFalse(build_client().is_connected())


if __name__ == "__main__":
    unittest.main()
