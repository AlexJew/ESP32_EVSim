#!/usr/bin/env python3
"""Read-only scan of the AMTRON's Modbus TCP registers, to find the current-limit registers.

Only function codes 3 and 4 (reads) are sent. Nothing is ever written.

Change the operator limit on the charger's web page between two runs and diff the
outputs to find which holding register is the operator limit.

Examples:
    ./amtron_modbus_probe.py --unit 255 --start 0 --end 1200
    ./amtron_modbus_probe.py --unit 1 --table input --start 100 --end 800 > input_a.txt
"""

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from charger_web.modbus import ModbusError, ModbusTcp  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.21.117")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", default="255", help="unit ID, or a comma-separated list to try (e.g. 0,1,255)")
    ap.add_argument("--table", choices=("holding", "input", "both"), default="both")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=1200)
    ap.add_argument("--block", type=int, default=10, help="registers per request; a refused block is retried one by one")
    args = ap.parse_args()
    try:
        socket.create_connection((args.host, args.port), timeout=3).close()
    except OSError as exc:
        sys.exit(f"Cannot open TCP {args.host}:{args.port}: {exc}\n"
                 "Check that the Modbus TCP server is On, the port matches, and the charger was restarted if it asked.")
    for unit in (int(u) for u in args.unit.split(",")):
        modbus = ModbusTcp(args.host, args.port, unit, 3.0)
        scan(modbus, args, unit)


def scan(modbus, args, unit):
    total, errors = 0, {}
    for table in ("holding", "input") if args.table == "both" else (args.table,):
        print(f"# {table} registers {args.start}-{args.end} unit {unit}")
        for base in range(args.start, args.end + 1, args.block):
            count = min(args.block, args.end + 1 - base)
            try:
                values = modbus.read(base, count, table)
                found = list(enumerate(values, base))
            except ModbusError:
                found = []
                for address in range(base, base + count):
                    try:
                        found.append((address, modbus.read(address, 1, table)[0]))
                    except ModbusError as exc:
                        errors[str(exc)] = errors.get(str(exc), 0) + 1
            for address, value in found:
                total += 1
                print(f"{table}\t{address}\t{value}\t0x{value:04x}")
    print(f"# unit {unit}: {total} registers answered", file=sys.stderr)
    for message, n in sorted(errors.items(), key=lambda item: -item[1])[:3]:
        print(f"#   {n}x {message}", file=sys.stderr)


if __name__ == "__main__":
    main()
