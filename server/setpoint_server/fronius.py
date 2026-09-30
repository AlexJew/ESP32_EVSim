"""Modbus TCP client for the Fronius GEN24 (SunSpec): reads, and the writes that cap the export.

Standard library only. The only registers ever written are WMaxLimPct, WMaxLim_Ena and Conn of
model 123, each range-checked. A cap is always committed with an Ena write, because the inverter
applies a new WMaxLimPct only then. Scale factors are re-read with every read. Background and
calibration: memory/evsim-inverter-fronius.md.
"""

import socket
import struct

from .config import AC_SETTLE_W, DC_OFFSET_W, DC_PER_AC

DEFAULT_HOST = "192.168.21.120"   # GEN24 12.0 Plus SC on the lab network
DEFAULT_PORT = 502
DEFAULT_UNIT = 1                  # the unit ID of the inverter over TCP is always 1

SUNSPEC_BASE = 40000              # wire address of the "SunS" marker
MAX_READ = 60                     # registers per request; longer reads are split

# Offsets, in registers, from the first data register of each model (SunSpec).
M1_MN, M1_MD, M1_VR = 0, 16, 40                            # common: manufacturer, model, version
M103_W, M103_W_SF, M103_ST = 12, 13, 36                    # inverter (integer): AC power, its SF, state
M121_WMAX, M121_WMAX_SF = 0, 20                            # basic settings
M123_CONN, M123_PCT, M123_RVRT, M123_ENA, M123_PCT_SF = 2, 3, 5, 7, 21   # immediate controls
M160_SF_A, M160_SF_V, M160_SF_W, M160_N, M160_MODULES = 0, 1, 2, 6, 8    # MPPT extension
M160_BLOCK = 20                                            # registers per MPPT module

STATES = {1: "OFF", 2: "SLEEPING", 3: "STARTING", 4: "MPPT", 5: "THROTTLED",
          6: "SHUTTING_DOWN", 7: "FAULT", 8: "STANDBY"}


def s16(v):
    return v - 65536 if v >= 32768 else v


def scaled(raw, sf):
    """A register value times 10^sf, where sf is the raw scale-factor register."""
    return raw * 10 ** s16(sf)


def text(regs):
    raw = b"".join(struct.pack(">H", r) for r in regs)
    return raw.split(b"\0")[0].decode("ascii", "replace").strip()


def pct_register(watts, wmax_w, sf):
    """The WMaxLimPct register value that caps the AC output at `watts`."""
    if not 0 <= watts <= wmax_w:
        raise ValueError("%s W is outside 0-%s W" % (watts, wmax_w))
    if sf > 0:
        raise ValueError("unexpected WMaxLimPct scale factor %d" % sf)
    return int(watts / wmax_w * 100 * 10 ** (-sf) + 0.5)


def ac_cap_for_dc(dc_w):
    """The AC cap that makes the DC input from the charger about `dc_w` (calibrated)."""
    return max(0.0, (dc_w - DC_OFFSET_W) / DC_PER_AC + AC_SETTLE_W)


class ModbusTcp:
    """Minimal Modbus TCP client: read holding registers (3) and write one register (6)."""

    def __init__(self, host, port=DEFAULT_PORT, unit=DEFAULT_UNIT, timeout=5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._unit = unit
        self._tid = 0

    def close(self):
        self._sock.close()

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise IOError("connection closed by the inverter")
            buf += chunk
        return buf

    def _request(self, fc, payload):
        self._tid = (self._tid + 1) & 0xFFFF
        self._sock.sendall(struct.pack(">HHHBB", self._tid, 0, 2 + len(payload), self._unit, fc) + payload)
        _, _, length, _ = struct.unpack(">HHHB", self._recv(7))
        body = self._recv(length - 1)
        if body[0] == fc | 0x80:
            raise IOError("modbus exception %d (function %d)" % (body[1], fc))
        if body[0] != fc:
            raise IOError("unexpected modbus function %d" % body[0])
        return body[1:]

    def read(self, addr, count):
        """Holding registers addr .. addr+count-1 as a list of uint16."""
        out = []
        while count > 0:
            n = min(count, MAX_READ)
            data = self._request(3, struct.pack(">HH", addr, n))
            if data[0] != 2 * n:
                raise IOError("short read at %d" % addr)
            out += list(struct.unpack(">%dH" % n, data[1:]))
            addr += n
            count -= n
        return out

    def write_single(self, addr, value):
        data = self._request(6, struct.pack(">HH", addr, value))
        if struct.unpack(">HH", data) != (addr, value):
            raise IOError("unexpected echo writing %d" % addr)


class Inverter:
    def __init__(self, modbus):
        self._m = modbus
        self._models = None

    def close(self):
        self._m.close()

    def models(self):
        """SunSpec model chain as {model id: (first data address, length)}."""
        if self._models is None:
            if self._m.read(SUNSPEC_BASE, 2) != [0x5375, 0x6E53]:
                raise IOError("no SunSpec marker at %d" % SUNSPEC_BASE)
            found, addr = {}, SUNSPEC_BASE + 2
            while addr < SUNSPEC_BASE + 4000:
                mid, length = self._m.read(addr, 2)
                if mid == 0xFFFF:
                    break
                found[mid] = (addr + 2, length)
                addr += 2 + length
            self._models = found
        return self._models

    def _model(self, mid, what):
        try:
            return self.models()[mid]
        except KeyError:
            raise IOError("model %d (%s) is not present on this inverter" % (mid, what)) from None

    def identity(self):
        addr, _ = self._model(1, "Common")
        d = self._m.read(addr, 48)
        return {"manufacturer": text(d[M1_MN:M1_MN + 16]), "model": text(d[M1_MD:M1_MD + 16]),
                "version": text(d[M1_VR:M1_VR + 8])}

    def wmax(self):
        addr, _ = self._model(121, "Basic Settings")
        d = self._m.read(addr, M121_WMAX_SF + 1)
        return d[M121_WMAX] * 10 ** s16(d[M121_WMAX_SF])

    def controls(self):
        addr, length = self._model(123, "Immediate Controls")
        d = self._m.read(addr, length)
        sf = s16(d[M123_PCT_SF])
        return {"conn": d[M123_CONN], "pct_raw": d[M123_PCT], "pct": d[M123_PCT] * 10 ** sf,
                "rvrt_s": d[M123_RVRT], "ena": d[M123_ENA], "sf": sf}

    def power(self):
        """AC power, state and every MPPT input. Scale factors are read with the values,
        every time: the inverter changes them at run time."""
        for mid in (101, 102, 103):
            if mid in self.models():
                addr, length = self.models()[mid]
                break
        else:
            raise IOError("no integer inverter model (101-103); the float models are not supported")
        d = self._m.read(addr, length)
        ac = None if d[M103_W] == 0x8000 else scaled(s16(d[M103_W]), d[M103_W_SF])
        mppt = []
        if 160 in self.models():
            a160, l160 = self.models()[160]
            m = self._m.read(a160, l160)
            f = lambda raw, sf: None if raw == 65535 else scaled(raw, sf)
            for i in range(min(m[M160_N], (l160 - M160_MODULES) // M160_BLOCK)):
                b = m[M160_MODULES + M160_BLOCK * i: M160_MODULES + M160_BLOCK * (i + 1)]
                mppt.append({"id": b[0], "name": text(b[1:9]),
                             "dc_a": f(b[9], m[M160_SF_A]), "dc_v": f(b[10], m[M160_SF_V]),
                             "dc_w": f(b[11], m[M160_SF_W])})
        return {"ac_w": ac, "state": d[M103_ST], "mppt": mppt}

    # -- writing: the only three registers this tool can touch --------------------------

    def _write(self, kind, value):
        addr123, _ = self._model(123, "Immediate Controls")
        offsets = {"conn": M123_CONN, "pct": M123_PCT, "ena": M123_ENA}
        if kind not in offsets:
            raise ValueError("this tool cannot write %r" % kind)
        if kind in ("conn", "ena") and value not in (0, 1):
            raise ValueError("%s takes only 0 or 1, not %r" % (kind, value))
        if kind == "pct" and not 0 <= value <= 100 * 10 ** (-self.controls()["sf"]):
            raise ValueError("WMaxLimPct %r is outside 0-100 %%" % value)
        self._m.write_single(addr123 + offsets[kind], value)

    def set_ac_cap(self, watts):
        """Cap the AC output: write WMaxLimPct, then commit with WMaxLim_Ena = 1, and read back."""
        raw = pct_register(watts, self.wmax(), self.controls()["sf"])
        self._write("pct", raw)
        self._write("ena", 1)
        c = self.controls()
        if (c["pct_raw"], c["ena"]) != (raw, 1):
            raise IOError("read back WMaxLimPct=%d Ena=%d, expected %d and 1" % (c["pct_raw"], c["ena"], raw))
        return c

    def set_dc_target(self, dc_w):
        return self.set_ac_cap(ac_cap_for_dc(dc_w))

    def clear_cap(self):
        full = int(100 * 10 ** (-self.controls()["sf"]))
        self._write("ena", 0)
        self._write("pct", full)
        c = self.controls()
        if (c["pct_raw"], c["ena"]) != (full, 0):
            raise IOError("read back WMaxLimPct=%d Ena=%d, expected %d and 0" % (c["pct_raw"], c["ena"], full))
        return c

    def set_conn(self, value):
        self._write("conn", value)
        conn = self.controls()["conn"]
        if conn != value:
            raise IOError("read back Conn=%d, expected %d" % (conn, value))
