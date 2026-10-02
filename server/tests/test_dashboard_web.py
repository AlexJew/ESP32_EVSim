import contextlib
import http.client
import io
import json
import os
import socket
import threading
import unittest
from unittest import mock

from fake_mqtt import FakeMqttClient
from charger_web import __main__ as charger_main
from charger_web.dashboard import Dashboard
from charger_web.history import History
from charger_web.telemetry import Telemetry
from charger_web.web import create_server

CP = '{"setpoint_w":4361,"state":"C","status":"live"}'
INVERTER = '{"mode":"capped","target_dc_w":4312.6,"dc_w":4304.6,"inverter_state":4}'


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMqttClient()
        self.telemetry = Telemetry(self.client, "evsim/setpoint")
        self.history = History(self.telemetry)
        self.dashboard = Dashboard(self.telemetry, self.history, self.client, ("127.0.0.1", 1883))
        self.addCleanup(self.dashboard.stop)

    def test_before_anything_arrives_everything_is_unavailable_and_serializes(self):
        data = self.dashboard.telemetry()
        self.assertFalse(data["mqtt"]["connected"])
        self.assertEqual(data["inverter"]["dc_w"]["status"], "unavailable")
        self.assertEqual(data["board"]["setpoint_w"]["status"], "unavailable")
        json.dumps(data, allow_nan=False)
        json.dumps(self.dashboard.history(), allow_nan=False)

    def test_telemetry_carries_the_mqtt_values_including_the_inverters_dc_power(self):
        self.client.connect()
        self.client.deliver("evsim/cp", CP)
        self.client.deliver("evsim/inverter", INVERTER)
        data = self.dashboard.telemetry()
        self.assertEqual(data["board"]["setpoint_w"]["value"], 4361.0)
        self.assertEqual((data["inverter"]["dc_w"]["value"], data["server"]["target_dc_w"]["value"]), (4304.6, 4312.6))
        self.assertTrue(data["mqtt"]["connected"])

    def test_history_returns_what_the_sampler_took(self):
        self.client.deliver("evsim/inverter", INVERTER)
        self.history.tick()
        frames = self.dashboard.history()["frames"]
        self.assertEqual((len(frames), frames[0]["dc_w"], frames[0]["target_dc_w"]), (1, 4304.6, 4312.6))

    def test_start_connects_the_broker_and_stop_undoes_it_in_order(self):
        self.dashboard.start()
        self.assertEqual(self.client.calls, [("connect_async", "127.0.0.1", 1883), ("loop_start",)])
        self.dashboard.start()                                  # idempotent
        self.assertEqual(len(self.client.calls), 2)
        self.dashboard.stop()
        self.assertEqual(self.client.calls[2:], [("disconnect",), ("loop_stop",)])
        self.dashboard.stop()                                   # idempotent
        self.assertEqual(len(self.client.calls), 4)

    def test_stop_before_start_touches_nothing(self):
        self.dashboard.stop()
        self.assertEqual(self.client.calls, [])

class StubDashboard:
    def __init__(self):
        self.fail = False

    def telemetry(self):
        if self.fail:
            raise RuntimeError("boom")
        return {"mqtt": {"connected": True}, "inverter": {"dc_w": {"value": 7272, "status": "ok", "updated_at": None}}}

    def history(self):
        return {"interval_s": 1, "window_s": 600, "frames": [{"t": 1, "dc_w": 5.0}]}


class RoutesTest(unittest.TestCase):
    def serve(self, dashboard):
        server = create_server(charger=object(), dashboard=dashboard, port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def get(self, port, path, host=None, method="GET", origin=False):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host or "127.0.0.1:%d" % port}
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % port
        conn.request(method, path, None, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        kind = response.getheader("Content-Type", "")
        return response.status, (json.loads(raw) if kind.startswith("application/json") else raw), response

    def setUp(self):
        self.stub = StubDashboard()
        self.port = self.serve(self.stub)

    def test_telemetry_and_history_are_served_as_json(self):
        status, body, response = self.get(self.port, "/api/telemetry")
        self.assertEqual((status, body["inverter"]["dc_w"]["value"]), (200, 7272))
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        status, body, _ = self.get(self.port, "/api/history")
        self.assertEqual((status, body["window_s"], body["frames"][0]["dc_w"]), (200, 600, 5.0))

    def test_a_wrong_host_header_is_refused(self):
        for path in ("/api/telemetry", "/api/history"):
            self.assertEqual(self.get(self.port, path, host="evil.example")[0], 400)

    def test_the_dashboard_routes_do_not_take_posts(self):
        self.assertEqual(self.get(self.port, "/api/telemetry", method="POST", origin=True)[0], 404)

    def test_a_failing_dashboard_is_a_500_not_a_dropped_connection(self):
        self.stub.fail = True
        status, body, _ = self.get(self.port, "/api/telemetry")
        self.assertEqual(status, 500)
        self.assertTrue(body["error"])

    def test_without_a_dashboard_the_routes_are_not_found(self):
        port = self.serve(None)
        self.assertEqual(self.get(port, "/api/telemetry")[0], 404)
        self.assertEqual(self.get(port, "/api/history")[0], 404)


class MainTest(unittest.TestCase):
    def test_a_bad_setpoint_topic_stops_the_start_with_a_message(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"EVSIM_SETPOINT_TOPIC": "evsim/#"}), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            charger_main.main(["--port", "8099"])      # never bound: the setting is checked first
        self.assertIn("EVSIM_SETPOINT_TOPIC", stderr.getvalue())

    def test_a_taken_port_starts_no_dashboard(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        self.addCleanup(blocker.close)
        dashboard = mock.Mock()
        with mock.patch.object(charger_main, "build_dashboard", return_value=dashboard), self.assertRaises(OSError):
            charger_main.main(["--port", str(blocker.getsockname()[1])])
        dashboard.start.assert_not_called()
        dashboard.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
