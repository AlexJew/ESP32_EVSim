#!/usr/bin/env python3
"""Read and cap the Fronius GEN24 over Modbus TCP. Run `fronius_modbus.py --help`.

The implementation lives in server/setpoint_server/fronius.py and fronius_cli.py, so the setpoint
server and this tool share one copy of the register logic. Commands: probe, status, watch (read-only),
set, off, conn. Background: memory/evsim-inverter-fronius.md.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from setpoint_server.fronius_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
