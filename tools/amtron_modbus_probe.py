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
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from charger_web.modbus import ModbusError, ModbusTcp  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.21.117")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=255)
    ap.add_argument("--table", choices=("holding", "input", "both"), default="both")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=1200)
    ap.add_argument("--block", type=int, default=10, help="registers per request; a refused block is retried one by one")
    args = ap.parse_args()
    modbus = ModbusTcp(args.host, args.port, args.unit, 3.0)
    for table in ("holding", "input") if args.table == "both" else (args.table,):
        print(f"# {table} registers {args.start}-{args.end} unit {args.unit}")
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
                    except ModbusError:
                        pass
            for address, value in found:
                print(f"{table}\t{address}\t{value}\t0x{value:04x}")


if __name__ == "__main__":
    main()
