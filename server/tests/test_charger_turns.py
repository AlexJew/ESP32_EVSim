import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from charger_web import charger as charger_module
from charger_web.charger import ChargerClient

SETTINGS = {"groups": [{"fields": [
    {"key": "OperatorCurrentLimit_vehicleif", "value": 10},
    {"key": "InstallationCurrentLimit_vehicleif", "value": 16}]}]}
DASHBOARD = {"groups": [{"fields": [
    {"key": "SignaledCurrentLimit_vehicleif", "value": 10},
    {"key": "OcppMeterCurrent_meter", "value": 0}]}]}


class FakeCharger:
    """Single-session charger: each login invalidates the previous session."""

    def __init__(self):
        self.session = None
        self.logins = 0
        self.operator = 10


def make_server(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/v1/login":
                return self._send(200, {"token": "abc"})
            if self.headers.get("Authorization") != fake.session or fake.session is None:
                return self._send(401, {})
            if self.path == "/json/settings.json":
                doc = json.loads(json.dumps(SETTINGS))
                doc["groups"][0]["fields"][0]["value"] = fake.operator
                return self._send(200, doc)
            self._send(200, DASHBOARD)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/login":
                fake.logins += 1
                fake.session = f"s{fake.logins}"
                return self._send(200, {"logged_in": True, "session": {"id": fake.session}})
            if self.headers.get("Authorization") != fake.session:
                return self._send(401, {})
            fake.operator = body["params"][0]["value"]
            self._send(200, {})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TurnTakingTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCharger()
        self.server = make_server(self.fake)
        self.now = [0.0]
        self.addCleanup(self.server.shutdown)
        self.client = ChargerClient(
            host=f"127.0.0.1:{self.server.server_port}", username="u", password="p",
            clock=lambda: self.now[0])

    def foreign_login(self):
        self.fake.logins += 1
        self.fake.session = f"s{self.fake.logins}"

    def test_yields_instead_of_relogging_in(self):
        self.assertEqual(self.client.status()["status"], "ok")
        self.foreign_login()
        logins = self.fake.logins
        self.now[0] += 5
        status = self.client.status()
        self.assertEqual(status["status"], "session_taken")
        self.assertEqual(status["operator_limit_a"]["value"], 10)
        self.now[0] += 5
        self.client.status()
        self.assertEqual(self.fake.logins, logins)  # never evicted the other client

    def test_reconnect_takes_session_back(self):
        self.client.status()
        self.foreign_login()
        self.now[0] += 5
        self.assertEqual(self.client.status()["status"], "session_taken")
        self.assertEqual(self.client.reconnect()["status"], "ok")

    def test_write_takes_session(self):
        self.client.status()
        self.foreign_login()
        self.now[0] += 5
        self.assertEqual(self.client.status()["status"], "session_taken")
        self.assertEqual(self.client.set_operator_limit(8)["operator_limit_a"], 8)
        self.now[0] += 3
        self.assertEqual(self.client.status()["status"], "ok")

    def test_idle_release_starts_fresh_turn(self):
        self.client.status()
        self.foreign_login()
        self.now[0] += 5
        self.assertEqual(self.client.status()["status"], "session_taken")
        self.now[0] += charger_module.config.IDLE_RELEASE_S + 1
        self.assertEqual(self.client.status()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
