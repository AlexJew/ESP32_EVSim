#!/usr/bin/env python3
"""Read and cap a Fronius GEN24 inverter over Modbus TCP (SunSpec), for the EVSim bench.

The inverter takes the power the AMTRON delivers (through a rectifier, on MPPT 1) and
exports it to the grid. The lever is SunSpec model 123 "Immediate Controls": a ceiling on
the AC output, set with WMaxLimPct and switched on with WMaxLim_Ena. Findings and the
calibration behind this tool: memory/evsim-inverter-fronius.md.

Only the standard library is used, so there is nothing to pip install.

Reading never writes anything. Writing is limited to exactly three registers of model 123
(WMaxLimPct, WMaxLim_Ena, Conn), each range-checked and read back, and no other register
can be written through this tool.

How a cap takes effect (seen on the unit): writing WMaxLimPct alone changes nothing. The
value is applied when WMaxLim_Ena = 1 is written again, so `set` always writes the value
and then commits with Ena = 1.

Commands (all accept --host, --port, --unit, --timeout):
    probe                 read-only: identity, rating, SunSpec model chain, model 123, power
    status                read-only: one line with the control registers and the power
    watch [--seconds N]   read-only: AC output and each MPPT input once a second
    set WATTS             cap the AC output at WATTS (0 to WMax); 0 leaves about 94 W of DC draw
    set --dc WATTS        cap it so the DC input from the charger is about WATTS (calibrated)
    off                   remove the cap (Ena = 0, WMaxLimPct = 100 %)
    conn 0|1              standby (0) or grid feed (1); after 1 the inverter needs about 57 s
                          (OFF, then STARTING) to be back at its cap

Examples:
    ./fronius_modbus.py probe
    ./fronius_modbus.py status
    ./fronius_modbus.py set 7050
    ./fronius_modbus.py set --dc 7000
    ./fronius_modbus.py off
"""

import argparse
import socket
import struct
import sys
import time

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

# Calibration, fitted on 2026-09-30 (8 caps from 1 to 7.3 kW): the DC power the inverter
# takes from the charger side is DC = DC_PER_AC * AC + DC_OFFSET_W, within 0.25 % except
# 0.9 % at 2 kW. The AC output also settles about 3 W below the cap.
DC_PER_AC = 1.0303
DC_OFFSET_W = 6.3
AC_SETTLE_W = 3.0

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


# -- command line ---------------------------------------------------------------------------

def mppt_inputs(p):
    """The MPPT inputs that carry data (the battery ports StCha/StDisCha are left out)."""
    return [m for m in p["mppt"] if m["dc_w"] is not None and m["name"].startswith("MPPT")]


def status_line(c, p):
    mppt = "  ".join("%s %s W %s A %s V" % (
        m["name"], *("-" if m[k] is None else fmt % m[k] for k, fmt in (("dc_w", "%.0f"), ("dc_a", "%.2f"), ("dc_v", "%.1f"))))
        for m in mppt_inputs(p))
    return ("Conn=%d  WMaxLimPct=%d (%.2f %%)  Ena=%d  RvrtTms=%d | AC %s W | %s | St=%d %s"
            % (c["conn"], c["pct_raw"], c["pct"], c["ena"], c["rvrt_s"],
               "-" if p["ac_w"] is None else "%.0f" % p["ac_w"], mppt or "no MPPT input active",
               p["state"], STATES.get(p["state"], "?")))


def cmd_probe(inv, args):
    ident = inv.identity()
    print("%s %s  firmware %s  WMax %.0f W" % (ident["manufacturer"], ident["model"], ident["version"], inv.wmax()))
    print("SunSpec models (id: first data address, length):")
    for mid, (addr, length) in inv.models().items():
        print("  model %-4d %d, %d" % (mid, addr, length))
    addr, _ = inv.models()[123]
    print("model 123 (Immediate Controls): Conn %d, WMaxLimPct %d, WMaxLimPct_RvrtTms %d, WMaxLim_Ena %d, "
          "WMaxLimPct_SF %d" % (addr + M123_CONN, addr + M123_PCT, addr + M123_RVRT, addr + M123_ENA, addr + M123_PCT_SF))
    print(status_line(inv.controls(), inv.power()))


def cmd_status(inv, args):
    print(status_line(inv.controls(), inv.power()))


def cmd_watch(inv, args):
    start = time.monotonic()
    try:
        while time.monotonic() - start < args.seconds:
            p = inv.power()
            print("t=%3.0fs  AC %s W | %s | St=%d %s" % (
                time.monotonic() - start, "-" if p["ac_w"] is None else "%.0f" % p["ac_w"],
                "  ".join("%s %.0f W %.2f A %.1f V" % (m["name"], m["dc_w"], m["dc_a"], m["dc_v"])
                          for m in mppt_inputs(p)),
                p["state"], STATES.get(p["state"], "?")))
            time.sleep(max(0.0, 1.0 - (time.monotonic() - start) % 1.0))
    except KeyboardInterrupt:
        pass


def cmd_set(inv, args):
    if args.dc:
        ac = ac_cap_for_dc(args.watts)
        c = inv.set_dc_target(args.watts)
        print("wanted DC input %.0f W -> AC cap %.0f W" % (args.watts, ac))
    else:
        c = inv.set_ac_cap(args.watts)
    print("cap on: WMaxLimPct=%d (%.2f %%), Ena=%d" % (c["pct_raw"], c["pct"], c["ena"]))


def cmd_off(inv, args):
    c = inv.clear_cap()
    print("cap off: WMaxLimPct=%d (%.2f %%), Ena=%d" % (c["pct_raw"], c["pct"], c["ena"]))


def cmd_conn(inv, args):
    inv.set_conn(args.value)
    print("Conn=%d written%s" % (args.value, "; the inverter needs about 57 s to be back at its cap"
                                 if args.value else "; the inverter goes to standby"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read and cap a Fronius GEN24 over Modbus TCP.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--unit", type=int, default=DEFAULT_UNIT)
    parser.add_argument("--timeout", type=float, default=5.0)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe").set_defaults(fn=cmd_probe)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    watch = sub.add_parser("watch")
    watch.add_argument("--seconds", type=float, default=30)
    watch.set_defaults(fn=cmd_watch)
    setp = sub.add_parser("set")
    setp.add_argument("watts", type=float)
    setp.add_argument("--dc", action="store_true", help="WATTS is the wanted DC input power")
    setp.set_defaults(fn=cmd_set)
    sub.add_parser("off").set_defaults(fn=cmd_off)
    conn = sub.add_parser("conn")
    conn.add_argument("value", type=int, choices=(0, 1))
    conn.set_defaults(fn=cmd_conn)
    args = parser.parse_args(argv)

    try:
        modbus = ModbusTcp(args.host, args.port, args.unit, args.timeout)
    except OSError as exc:
        print("error: cannot reach %s:%d: %s" % (args.host, args.port, exc), file=sys.stderr)
        return 1
    try:
        args.fn(Inverter(modbus), args)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        modbus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
