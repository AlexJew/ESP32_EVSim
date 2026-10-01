"""Minimal Modbus TCP client: read holding (3) / input (4) registers, write one register (6)."""

from __future__ import annotations

import socket
import struct

MAX_READ = 60


class ModbusError(IOError):
    """Communication failure or Modbus exception response."""


class ModbusTransportError(ModbusError):
    """No usable reply: timeout, reset or refused connection (not a Modbus exception)."""


class ModbusTcp:
    """One TCP connection, opened on first use and reopened after any failure.

    Not thread-safe; callers serialize access.
    """

    def __init__(self, host, port=502, unit=1, timeout=3.0):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self._sock = None
        self._tid = 0

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ModbusTransportError("connection closed by the charger")
            buf += chunk
        return buf

    def _request(self, fc, payload):
        try:
            if self._sock is None:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self._sock.settimeout(self.timeout)
            self._tid = (self._tid + 1) & 0xFFFF
            frame = struct.pack(">HHHBB", self._tid, 0, 2 + len(payload), self.unit, fc) + payload
            self._sock.sendall(frame)
            _, _, length, _ = struct.unpack(">HHHB", self._recv(7))
            body = self._recv(length - 1)
        except (OSError, struct.error) as exc:
            self.close()
            raise ModbusTransportError(f"Could not reach the charger over Modbus: {exc}") from exc
        if body[0] == fc | 0x80:
            raise ModbusError(f"Modbus exception {body[1]} (function {fc})")
        if body[0] != fc:
            self.close()
            raise ModbusError(f"unexpected Modbus function {body[0]}")
        return body[1:]

    def read(self, address, count=1, table="holding"):
        fc = {"holding": 3, "input": 4}[table]
        out = []
        while count > 0:
            n = min(count, MAX_READ)
            data = self._request(fc, struct.pack(">HH", address, n))
            if data[0] != 2 * n:
                raise ModbusError(f"short read at {address}")
            out += struct.unpack(f">{n}H", data[1:])
            address += n
            count -= n
        return out

    def write_single(self, address, value):
        data = self._request(6, struct.pack(">HH", address, value))
        if struct.unpack(">HH", data) != (address, value):
            raise ModbusError(f"unexpected echo writing register {address}")
