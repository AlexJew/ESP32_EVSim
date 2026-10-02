import http.client
import threading
import unittest

from charger_web.web import create_server


class AssetsTest(unittest.TestCase):
    def setUp(self):
        self.server = create_server(charger=object(), port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, response.getheader("Content-Type"), body

    def test_the_new_scripts_are_served_as_javascript(self):
        for path in ("/chart.js", "/dashboard.js", "/app.js"):
            status, kind, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(kind.startswith("text/javascript"), (path, kind))
            self.assertTrue(body, path)

    def test_the_page_names_the_scripts_it_loads(self):
        _, _, body = self.get("/")
        for path in (b"/chart.js", b"/dashboard.js", b"/app.js"):
            self.assertIn(path, body)


if __name__ == "__main__":
    unittest.main()
