"""End-to-end checks of the spec's bench list, against a private Mosquitto.

Each test starts its own broker on a free loopback port and its own server as a
subprocess, then fakes the board with a paho publisher on evsim/cp and watches
evsim/setpoint. Skipped when mosquitto or paho-mqtt is not available.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

MOSQUITTO = shutil.which("mosquitto") or (
    "/opt/homebrew/sbin/mosquitto" if os.path.exists("/opt/homebrew/sbin/mosquitto") else None
)
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPIC_CP = "evsim/cp"
TOPIC_SETPOINT = "evsim/setpoint"
READY = "subscribed to evsim/cp"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def new_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    return client


class Watcher:
    """Records every evsim/setpoint message with its arrival time and retain flag."""

    def __init__(self, port):
        self._lock = threading.Lock()
        self._messages = []  # (monotonic time, retain, decoded JSON)
        self._client = new_client()
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(TOPIC_SETPOINT, qos=1)
        self._client.on_message = self._on_message
        self._client.connect_async("127.0.0.1", port)
        self._client.loop_start()

    def _on_message(self, client, userdata, msg):
        with self._lock:
            self._messages.append((time.monotonic(), msg.retain, json.loads(msg.payload)))

    def snapshot(self):
        with self._lock:
            return list(self._messages)

    def last(self):
        messages = self.snapshot()
        return messages[-1][2] if messages else None

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.snapshot()):
                return True
            time.sleep(0.1)
        return predicate(self.snapshot())

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()


class Feeder:
    """Plays the board: publishes evsim/cp messages."""

    def __init__(self, port):
        self._client = new_client()
        self._client.connect_async("127.0.0.1", port)
        self._client.loop_start()

    def send(self, payload):
        self._client.publish(TOPIC_CP, payload, qos=0)

    def feed(self, watts, seconds, hz=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.send(json.dumps({"setpoint_w": watts}))
            time.sleep(1.0 / hz)

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()


@unittest.skipUnless(MOSQUITTO and mqtt, "needs mosquitto and paho-mqtt")
class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.port = free_port()
        self.logpath = os.path.join(self.tmp, "server.log")
        self.broker = self.server = self.watcher = self.feeder = None
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for helper in (self.watcher, self.feeder):
            if helper:
                helper.stop()
        for proc in (self.server, self.broker):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_broker(self):
        conf = os.path.join(self.tmp, "mosquitto.conf")
        with open(conf, "w") as f:
            f.write("listener %d 127.0.0.1\nallow_anonymous true\n" % self.port)
        self.broker = subprocess.Popen(
            [MOSQUITTO, "-c", conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.fail("the private Mosquitto did not start")

    def start_server(self):
        log = open(self.logpath, "a")
        self.addCleanup(log.close)
        self.server = subprocess.Popen(
            [sys.executable, "-u", "-m", "setpoint_server", "--port", str(self.port)],
            cwd=SERVER_DIR, stdout=log, stderr=log,
        )

    def start_clients(self):
        self.watcher = Watcher(self.port)
        self.feeder = Feeder(self.port)

    def log_text(self):
        with open(self.logpath) as f:
            return f.read()

    def wait_for_log(self, text, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.log_text():
                return True
            time.sleep(0.1)
        return text in self.log_text()

    def up(self):
        """Broker, clients and server running, and the server subscribed."""
        self.start_broker()
        self.start_clients()
        self.start_server()
        self.assertTrue(self.wait_for_log(READY, 10), self.log_text())

    def status_is(self, status):
        return lambda messages: bool(messages) and messages[-1][2]["status"] == status

    # -- the spec's bench list ------------------------------------------------

    def test_steady_input_gives_an_ok_average(self):
        self.up()
        self.feeder.feed(10000, 3.5)
        last = self.watcher.last()
        self.assertEqual(last["status"], "ok")
        self.assertEqual(last["setpoint_w"], 10000)
        self.assertTrue(3 <= last["samples"] <= 6, last)

    def test_silence_gives_no_data_within_a_couple_of_seconds(self):
        self.up()
        self.feeder.feed(10000, 2.0)
        self.assertTrue(self.watcher.wait_for(self.status_is("ok"), 3))
        self.assertTrue(self.watcher.wait_for(self.status_is("no_data"), 3.5))
        last = self.watcher.last()
        self.assertEqual((last["setpoint_w"], last["samples"]), (0, 0))

    def test_a_step_to_zero_ramps_down_within_two_seconds(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        step = time.monotonic()
        self.feeder.feed(0, 3.0)
        after = [(t, p["setpoint_w"]) for t, _, p in self.watcher.snapshot() if t > step]
        values = [w for _, w in after]
        self.assertEqual(values[-1], 0)
        self.assertEqual(values, sorted(values, reverse=True))
        first_zero = next(t for t, w in after if w == 0)
        self.assertLess(first_zero - step, 2.5)

    def test_sigkill_makes_the_broker_publish_the_last_will(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        self.server.kill()
        self.assertTrue(self.watcher.wait_for(self.status_is("server_offline"), 3))
        self.assertNotIn("ts", self.watcher.last())

    def test_sigterm_makes_the_server_publish_offline_itself(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        self.server.terminate()
        self.assertEqual(self.server.wait(timeout=5), 0)
        self.assertTrue(self.watcher.wait_for(self.status_is("server_offline"), 3))
        self.assertIn("published server_offline", self.log_text())

    def test_unusable_messages_are_ignored_and_the_server_survives(self):
        self.up()
        bad = [b"garbage", b'{"setpoint_w":-5}', b'{"setpoint_w":"x"}', b'{"setpoint_w":null}',
               b'{"setpoint_w":11001}']
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            for payload in bad:
                self.feeder.send(payload)
            self.feeder.send(json.dumps({"setpoint_w": 10000}))
            time.sleep(0.2)
        last = self.watcher.last()
        self.assertEqual((last["status"], last["setpoint_w"]), ("ok", 10000))
        self.assertIsNone(self.server.poll())

    def test_a_late_subscriber_gets_the_retained_value_at_once(self):
        self.up()
        self.feeder.feed(10000, 2.5)
        late = Watcher(self.port)
        self.addCleanup(late.stop)
        self.assertTrue(late.wait_for(lambda m: len(m) > 0, 2))
        _, retained, payload = late.snapshot()[0]
        self.assertTrue(retained)
        self.assertIn(payload["status"], ("ok", "no_data"))

    def test_the_server_recovers_from_a_broker_restart(self):
        self.up()
        self.feeder.feed(10000, 2.0)
        self.broker.kill()
        self.broker.wait(timeout=5)
        time.sleep(1.5)
        self.start_broker()
        before = len(self.watcher.snapshot())
        deadline = time.monotonic() + 20
        recovered = False
        while time.monotonic() < deadline and not recovered:
            self.feeder.send(json.dumps({"setpoint_w": 10000}))
            time.sleep(0.2)
            recovered = any(
                p["status"] == "ok" and p["setpoint_w"] == 10000
                for _, _, p in self.watcher.snapshot()[before:]
            )
        self.assertTrue(recovered, self.log_text())
        self.assertIsNone(self.server.poll())

    def test_the_server_can_start_before_the_broker_exists(self):
        self.start_server()
        time.sleep(2.0)
        self.assertIsNone(self.server.poll(), self.log_text())
        self.start_broker()
        self.start_clients()
        self.assertTrue(self.wait_for_log(READY, 20), self.log_text())
        self.feeder.feed(10000, 3.0)
        last = self.watcher.last()
        self.assertEqual((last["status"], last["setpoint_w"]), ("ok", 10000))


if __name__ == "__main__":
    unittest.main()
