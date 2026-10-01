import socket
import struct
import threading
import unittest

from charger_web.modbus import ModbusError, ModbusTcp, ModbusTransportError


class OneShotServer:
    """Accepts one connection, reads one request and answers with the given raw bytes (built from the request)."""

    def __init__(self, reply):
        self.reply = reply
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        conn, _ = self.sock.accept()
        with conn:
            request = conn.recv(64)
            conn.sendall(self.reply(request))
            conn.settimeout(1)
            try:
                conn.recv(1)
            except OSError:
                pass

    def close(self):
        self.sock.close()


def read_reply(request, tid=None, length=None, payload=b"\x03\x02\x00\x0a"):
    t = struct.unpack(">H", request[:2])[0] if tid is None else tid
    n = len(payload) + 1 if length is None else length
    return struct.pack(">HHHB", t, 0, n, request[6]) + payload


class MalformedFrameTest(unittest.TestCase):
    def _client(self, reply):
        server = OneShotServer(reply)
        self.addCleanup(server.close)
        client = ModbusTcp("127.0.0.1", server.port, 255, 1.0)
        self.addCleanup(client.close)
        return client

    def test_a_good_reply_still_reads(self):
        self.assertEqual(self._client(read_reply).read(1000, 1), [10])

    def test_a_zero_length_field_is_a_transport_error(self):
        client = self._client(lambda r: read_reply(r, length=0, payload=b""))
        with self.assertRaises(ModbusTransportError):
            client.read(1000, 1)

    def test_a_short_payload_is_a_transport_error(self):
        client = self._client(lambda r: read_reply(r, payload=b"\x03\x02\x00"))
        with self.assertRaises(ModbusError):
            client.read(1000, 1)

    def test_an_empty_data_section_is_a_modbus_error(self):
        client = self._client(lambda r: read_reply(r, payload=b"\x03"))
        with self.assertRaises(ModbusError):
            client.read(1000, 1)

    def test_a_reply_for_another_transaction_is_rejected_and_closes_the_connection(self):
        client = self._client(lambda r: read_reply(r, tid=0x7777))
        with self.assertRaises(ModbusTransportError):
            client.read(1000, 1)
        self.assertIsNone(client._sock)


if __name__ == "__main__":
    unittest.main()
