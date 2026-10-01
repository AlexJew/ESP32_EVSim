import contextlib
import http.client
import io
import json
import threading
import unittest

from fake_amtron import FakeAmtron
from charger_web import __main__ as charger_main
from charger_web.service import ChargerService
from charger_web.web import create_server


class WebTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.service = ChargerService("127.0.0.1", port)
        self.addCleanup(self.service.stop)
        self.service.poll_once()
        self.http = create_server(charger=self.service, port=0)
        self.web_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def request(self, method, path, body=None, origin=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=5)
        headers = {"Host": host or "127.0.0.1:%d" % self.web_port}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if method == "POST" and origin:
            headers["Origin"] = "http://127.0.0.1:%d" % self.web_port
        conn.request(method, path, data, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        kind = response.getheader("Content-Type", "")
        return response.status, (json.loads(raw) if kind.startswith("application/json") else raw)

    def test_status_comes_from_the_cache_and_never_touches_the_charger(self):
        before = len(self.fake.log)
        status, body = self.request("GET", "/api/charger/status")
        self.assertEqual(status, 200)
        self.assertEqual((body["link"], body["set_limit_a"]["value"], body["vehicle_state"]["value"]), ("ok", 10, "C"))
        self.assertEqual(len(self.fake.log), before)

    def test_apply_writes_register_1000(self):
        status, body = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8})
        self.assertEqual((status, body["success"], body["set_limit_a"]), (200, True, 8))
        self.assertEqual(self.fake.writes, [(1000, 8)])

    def test_invalid_values_are_rejected_without_a_write(self):
        for bad in (5, 17, 7.5, True, "8", None):
            status, _ = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": bad})
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.fake.writes, [])

    def test_other_bodies_are_rejected(self):
        self.assertEqual(self.request("POST", "/api/charger/operator-limit", {"limit": 8})[0], 400)
        self.assertEqual(self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8, "x": 1})[0], 400)
        self.assertEqual(self.fake.writes, [])

    def test_a_cross_origin_post_is_refused(self):
        status, _ = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8}, origin=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.fake.writes, [])

    def test_a_wrong_host_header_is_refused(self):
        self.assertEqual(self.request("GET", "/api/charger/status", host="evil.example")[0], 400)

    def test_a_refused_write_is_a_503_with_the_reason(self):
        self.fake.refuse_writes = True
        status, body = self.request("POST", "/api/charger/operator-limit", {"current_limit_a": 8})
        self.assertEqual((status, body["success"]), (503, False))
        self.assertTrue(body["error"])

    def test_the_rest_reconnect_route_is_gone(self):
        self.assertEqual(self.request("POST", "/api/charger/reconnect", {})[0], 404)

    def test_the_page_is_served(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"AMTRON charger", body)

    def test_only_loopback_is_allowed(self):
        with self.assertRaises(ValueError):
            create_server(charger=self.service, host="0.0.0.0", port=0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            charger_main.main(["--host", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
