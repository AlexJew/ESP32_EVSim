import socketserver
import struct
import threading
import unittest

from charger_web.charger import ChargerError, LimitValidationError
from charger_web.charger_modbus import ModbusChargerClient

REGISTER_MAP = {
    "unit": 255,
    "operator_limit": {"table": "holding", "address": 1000, "scale": 1},
    "installation_limit": {"table": "input", "address": 200, "scale": 1},
    "signalled_current": {"table": "input", "address": 201, "scale": 1},
    "meter_current": {"table": "input", "addresses": {"L1": 300, "L2": 301, "L3": None}, "scale": 0.5},
}


class FakeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        regs = self.server.regs
        while True:
            head = self.request.recv(7)
            if len(head) < 7:
                return
            tid, _, length, unit = struct.unpack(">HHHB", head)
            body = self.request.recv(length - 1)
            fc = body[0]
            addr, arg = struct.unpack(">HH", body[1:5])
            self.server.log.append((fc, addr, arg))
            if fc in (3, 4):
                table = regs["holding" if fc == 3 else "input"]
                if any(a not in table for a in range(addr, addr + arg)):
                    out = bytes([fc | 0x80, 2])
                else:
                    out = bytes([fc, 2 * arg]) + struct.pack(f">{arg}H", *(table[a] for a in range(addr, addr + arg)))
            elif fc == 6:
                if self.server.refuse_writes:
                    out = bytes([fc | 0x80, 3])
                else:
                    regs["holding"][addr] = arg
                    out = body
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(out) + 1, unit) + out)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ModbusBackendTest(unittest.TestCase):
    def setUp(self):
        self.server = Server(("127.0.0.1", 0), FakeHandler)
        self.server.regs = {"holding": {1000: 10}, "input": {200: 16, 201: 10, 300: 20, 301: 22}}
        self.server.log = []
        self.server.refuse_writes = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.now = [0.0]
        self.client = ModbusChargerClient("127.0.0.1", self.server.server_address[1], REGISTER_MAP, clock=lambda: self.now[0])
        self.addCleanup(self.client._modbus.close)

    def test_status_reads_scaled_values(self):
        status = self.client.status()
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["operator_limit_a"]["value"], 10)
        self.assertEqual(status["installation_limit_a"]["value"], 16)
        self.assertEqual(status["meter_current_a"]["value"], {"L1": 10.0, "L2": 11.0})

    def test_write_is_read_back(self):
        self.assertEqual(self.client.set_operator_limit(8)["operator_limit_a"], 8)
        self.assertEqual(self.server.regs["holding"][1000], 8)

    def test_invalid_limit_is_not_written(self):
        for bad in (5, 17, 7.5):
            with self.assertRaises(LimitValidationError):
                self.client.set_operator_limit(bad)
        self.assertFalse([entry for entry in self.server.log if entry[0] == 6])

    def test_refused_write_reports_error(self):
        self.server.refuse_writes = True
        with self.assertRaises(ChargerError):
            self.client.set_operator_limit(8)

    def test_unreachable_charger_shows_stale_values(self):
        self.client.status()
        self.server.shutdown()
        self.server.server_close()
        self.client._modbus.close()
        self.now[0] += 5
        status = self.client.status()
        self.assertEqual(status["status"], "stale")
        self.assertEqual(status["operator_limit_a"]["value"], 10)

    def test_empty_map_is_not_configured_and_refuses_writes(self):
        empty = {"unit": 255, "operator_limit": {"address": None}}
        client = ModbusChargerClient("127.0.0.1", self.server.server_address[1], empty)
        self.assertEqual(client.status()["status"], "not_configured")
        with self.assertRaises(ChargerError):
            client.set_operator_limit(8)
        self.assertFalse(self.server.log)


if __name__ == "__main__":
    unittest.main()
