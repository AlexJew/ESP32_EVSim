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
import sys
import time

from .fronius import (
    DEFAULT_HOST, DEFAULT_PORT, DEFAULT_UNIT, M123_CONN, M123_ENA, M123_PCT, M123_PCT_SF,
    M123_RVRT, STATES, Inverter, ModbusTcp, ac_cap_for_dc,
)


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
