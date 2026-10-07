import http.client
import json
import threading
import unittest

from fake_mqtt import FakeMqttClient
from charger_web.commands import StateCommander
from charger_web.dashboard import Dashboard
from charger_web.history import History
from charger_web.telemetry import Telemetry
from charger_web.web import create_server


class VehicleRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMqttClient()
        self.telemetry = Telemetry(self.client, "evsim/setpoint")
        self.client.connect()
        commander = StateCommander(self.client, self.telemetry, timeout_s=0.3)
        self.dashboard = Dashboard(self.telemetry, History(self.telemetry), self.client, ("127.0.0.1", 1883), commander)
        self.port = self.serve(self.dashboard)

    def serve(self, dashboard):
        server = create_server(charger=object(), dashboard=dashboard, port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def post(self, body, port=None, path="/api/vehicle/state", origin=True, host=None, content_type="application/json"):
        port = port or self.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host or "127.0.0.1:%d" % port, "Content-Type": content_type}
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % port
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        conn.request("POST", path, data, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, (json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw)

    def answer_with(self, line):
        self.client.publish_hook = lambda topic, payload: self.client.deliver("evsim/reply", line)

    def test_an_accepted_command_is_a_200(self):
        self.answer_with("state C -> B")
        status, body = self.post({"state": "B"})
        self.assertEqual((status, body), (200, {"success": True, "state": "B", "reply": "state C -> B"}))
        self.assertEqual(self.client.published, [("evsim/cmd/state", "B", 0, False)])

    def test_a_refusal_is_a_409_with_the_reason(self):
        self.answer_with("refused A -> C: state B must be asserted first")
        status, body = self.post({"state": "C"})
        self.assertEqual((status, body), (409, {"success": False, "error": "state B must be asserted first"}))

    def test_no_answer_is_a_503(self):
        status, body = self.post({"state": "B"})
        self.assertEqual((status, body["success"], body["error"]), (503, False, "The board did not answer."))

    def test_the_broker_down_is_a_503_and_publishes_nothing(self):
        self.client.drop()
        self.assertEqual(self.post({"state": "B"})[0], 503)
        self.assertEqual(self.client.published, [])

    def test_a_bad_letter_is_a_400(self):
        for bad in ("b", "D", "", "BB", None, 1):
            self.assertEqual(self.post({"state": bad})[0], 400, repr(bad))
        self.assertEqual(self.client.published, [])

    def test_a_request_that_must_publish_nothing(self):
        cases = {
            "other key": lambda: self.post({"current_limit_a": 8}),
            "extra key": lambda: self.post({"state": "B", "x": 1}),
            "not an object": lambda: self.post("[\"B\"]"),
            "invalid json": lambda: self.post("{nope"),
            "duplicate key": lambda: self.post('{"state": "B", "state": "C"}'),
            "empty body": lambda: self.post(""),
            "too large": lambda: self.post(json.dumps({"state": "B", "pad": "x" * 2000})),
            "wrong type": lambda: self.post("state=B", content_type="text/plain"),
            "foreign origin": lambda: self.post({"state": "B"}, origin=False),
            "wrong host": lambda: self.post({"state": "B"}, host="evil.example"),
        }
        for name, request in cases.items():
            status, _ = request()
            self.assertIn(status, (400, 403, 413, 415), name)
        self.assertEqual(self.client.published, [], "nothing may be published for a rejected request")

    def test_without_a_dashboard_the_route_is_not_found(self):
        port = self.serve(None)
        self.assertEqual(self.post({"state": "B"}, port=port)[0], 404)

    def test_without_a_commander_the_route_answers_503(self):
        bare = Dashboard(self.telemetry, History(self.telemetry), self.client, ("127.0.0.1", 1883))
        self.assertEqual(self.post({"state": "B"}, port=self.serve(bare))[0], 503)

    def test_the_limit_route_is_unchanged(self):
        status, _ = self.post({"state": "B"}, path="/api/charger/operator-limit")
        self.assertEqual(status, 400)                               # expects current_limit_a, not state


if __name__ == "__main__":
    unittest.main()
