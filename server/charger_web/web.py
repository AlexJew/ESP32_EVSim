"""Loopback-only HTTP API and static page server."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .charger import ChargerClient, ChargerError, LimitValidationError
from .config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT

log = logging.getLogger("charger_web.http")
STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 1024


class _LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(charger=None, host=DEFAULT_WEB_HOST, port=DEFAULT_WEB_PORT):
    if host != DEFAULT_WEB_HOST:
        raise ValueError("The charger page must bind to 127.0.0.1.")
    client = charger or ChargerClient()

    class Handler(BaseHTTPRequestHandler):
        server_version = "EVSimChargerWeb/1"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _host_matches(self):
            expected = "127.0.0.1" if self.server.server_port == 80 else f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host", "").lower() == expected

        def _send(self, code, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; style-src 'self'; "
                "script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, body):
            self._send(code, json.dumps(body, separators=(",", ":"), allow_nan=False))

        def do_GET(self):
            if not self._host_matches():
                self._json(400, {"error": "Use the local http://127.0.0.1 URL."})
                return
            path = urlsplit(self.path).path
            if path == "/api/charger/status":
                self._json(200, client.status())
                return
            asset = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
            }.get(path)
            if asset is None:
                self._json(404, {"error": "Not found."})
                return
            filename, mime = asset
            try:
                content = (STATIC_DIR / filename).read_bytes()
            except OSError:
                self._json(500, {"error": "Page assets are unavailable."})
                return
            self._send(200, content, mime)

        def do_POST(self):
            if not self._host_matches():
                self._json(400, {"error": "Use the local http://127.0.0.1 URL."})
                return
            expected_origin = (
                "http://127.0.0.1"
                if self.server.server_port == 80
                else f"http://127.0.0.1:{self.server.server_port}"
            )
            if self.headers.get("Origin") != expected_origin:
                self._json(403, {"error": "Cross-origin writes are not allowed."})
                return
            path = urlsplit(self.path).path
            if path == "/api/charger/reconnect":
                self._json(200, client.reconnect())
                return
            if path != "/api/charger/operator-limit":
                self._json(404, {"error": "Not found."})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "Expected application/json."})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(413, {"error": "Request body is missing or too large."})
                return
            try:
                payload = json.loads(self.rfile.read(length), object_pairs_hook=_unique_object)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._json(400, {"error": "Request body must be valid JSON."})
                return
            if not isinstance(payload, dict) or set(payload) != {"current_limit_a"}:
                self._json(400, {"error": "Expected current_limit_a."})
                return
            try:
                result = client.set_operator_limit(payload["current_limit_a"])
            except LimitValidationError as exc:
                self._json(400, {"success": False, "error": str(exc)})
                return
            except ChargerError as exc:
                self._json(503, {"success": False, "error": str(exc)})
                return
            self._json(200, result)

        def do_OPTIONS(self):
            self._json(405, {"error": "Method not allowed."})

    return _LocalHTTPServer((host, port), Handler)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result
