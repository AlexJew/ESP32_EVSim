#!/usr/bin/env python3
"""Capture the firmware's CSV over serial, and compare two captures' jitter.

This answers the one question the WiFi work exists to settle: does an associated,
idle radio disturb the Control Pilot interrupt enough to matter? See
docs/superpowers/specs/2026-09-25-wifi-static-ip-design.md.

The comparison must be pre-WiFi firmware against WiFi-connected firmware. An
"AP powered off" run is NOT a valid baseline: with no AP the radio scans
continuously and the Arduino event task fires on every failure, so that run can
easily be noisier than the connected one and hide the real answer.

Examples:
    # baseline, from a build with no WiFi at all (see the plan's step 12)
    ~/.platformio/penv/bin/python tools/cp_jitter.py capture \\
        --out baseline.csv --seconds 60

    # then the WiFi build, same charger limit and same pilot state
    ~/.platformio/penv/bin/python tools/cp_jitter.py capture \\
        --out wifi_on.csv --seconds 60

    # compare (plain python3 is fine here, no pyserial needed)
    python3 tools/cp_jitter.py compare baseline.csv wifi_on.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import statistics
import sys
import time

# The acceptance criteria from the design. duty_pct is the one that matters: the
# reader claims ~0.1% duty resolution, so a spread growing by more than that is
# the radio eating into the measurement rather than rounding noise.
MAX_DUTY_SD_GROWTH_PCT = 0.1
NOMINAL_FREQ_HZ = 1000.0
MAX_FREQ_ERROR_HZ = 1.0

HEADER_PREFIX = "millis,"


def default_port() -> str | None:
    """The ESP32's USB serial port, if exactly one looks like a candidate."""
    candidates = sorted(
        p
        for pattern in ("/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*",
                        "/dev/cu.wchusbserial*", "/dev/ttyUSB*", "/dev/ttyACM*")
        for p in glob.glob(pattern)
    )
    return candidates[0] if len(candidates) == 1 else None


def capture(args: argparse.Namespace) -> int:
    try:
        import serial  # type: ignore
    except ImportError:
        sys.exit(
            "pyserial is not installed for this interpreter.\n"
            "Run this with PlatformIO's python, which bundles it:\n"
            "  ~/.platformio/penv/bin/python tools/cp_jitter.py capture ..."
        )

    port = args.port or default_port()
    if not port:
        sys.exit("could not identify the serial port; pass --port explicitly")

    print(f"# opening {port} at {args.baud} (this resets the board)")
    with serial.Serial(port, args.baud, timeout=1) as ser:
        # Opening the port toggles DTR/RTS, which resets the ESP32, so we always
        # start from a known state: pilot state A, fresh banner, fresh CSV header.
        header = wait_for_header(ser, args.boot_timeout)

        if args.state:
            # State is A after reset and the firmware refuses A -> C, so walk it.
            for letter in ("B", "C")[: ("B", "C").index(args.state) + 1]:
                print(f"# sending {letter}")
                ser.write(f"{letter}\n".encode())
                ser.flush()
                time.sleep(0.5)

        print(f"# settling {args.settle:.1f} s, then logging {args.seconds:.0f} s")
        drain_for(ser, args.settle)

        rows = collect_rows(ser, args.seconds)

    if not rows:
        sys.exit("no CSV rows captured — is the firmware running and printing?")

    # The first measurement window after boot straddles WiFi/PHY init, during
    # which both CPUs stall and CP edges are lost: window_us includes the stall
    # but high_us does not, so that row under-reports duty and frequency. The
    # settle period above normally skips it; dropping one more is cheap.
    rows = rows[1:]

    with open(args.out, "w", newline="") as f:
        f.write(header + "\n")
        for row in rows:
            f.write(row + "\n")

    valid = sum(1 for r in rows if ",PWM_VALID," in r)
    print(f"# wrote {args.out}: {len(rows)} rows, {valid} PWM_VALID")
    if valid == 0:
        print("# WARNING: no PWM_VALID rows. Is the charger signalling, and is "
              "the board in state C?", file=sys.stderr)
    return 0


def wait_for_header(ser, timeout: float) -> str:
    """Reads past the '#' banner lines to the CSV header the firmware prints."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = ser.readline().decode("utf-8", "replace").strip()
        if line.startswith("#"):
            print(f"  {line}")
        elif line.startswith(HEADER_PREFIX):
            return line
    sys.exit(f"no CSV header seen within {timeout:.0f} s — wrong port or baud?")


def drain_for(ser, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = ser.readline().decode("utf-8", "replace").strip()
        if line.startswith("#"):
            print(f"  {line}")


def collect_rows(ser, seconds: float) -> list[str]:
    """Data lines only. '#' lines are echoed so link transitions stay visible."""
    rows: list[str] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = ser.readline().decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("#"):
            print(f"  {line}")  # link transitions, state changes
        elif not line.startswith(HEADER_PREFIX):
            rows.append(line)
    return rows


def stats_for(path: str) -> dict[str, tuple[int, float, float]]:
    with open(path) as f:
        rows = [r for r in csv.DictReader(f) if r.get("state") == "PWM_VALID"]
    if not rows:
        sys.exit(f"{path}: no PWM_VALID rows to analyse")

    out = {}
    for col in ("freq_hz", "duty_pct"):
        vals = [float(r[col]) for r in rows if r.get(col)]
        out[col] = (len(vals), statistics.mean(vals), statistics.pstdev(vals))
    return out


def compare(args: argparse.Namespace) -> int:
    base = stats_for(args.baseline)
    test = stats_for(args.wifi)

    print(f"{'':12s} {'n':>6s} {'mean':>10s} {'sd':>8s}")
    for label, st in ((args.baseline, base), (args.wifi, test)):
        for col in ("freq_hz", "duty_pct"):
            n, mean, sd = st[col]
            print(f"{col:12s} {n:6d} {mean:10.3f} {sd:8.3f}   {label}")

    duty_growth = test["duty_pct"][2] - base["duty_pct"][2]
    freq_error = abs(test["freq_hz"][1] - NOMINAL_FREQ_HZ)

    print()
    print(f"duty_pct sd growth: {duty_growth:+.3f} pp "
          f"(limit {MAX_DUTY_SD_GROWTH_PCT:+.3f})")
    print(f"freq_hz mean error: {freq_error:.3f} Hz from {NOMINAL_FREQ_HZ:.0f} "
          f"(limit {MAX_FREQ_ERROR_HZ:.3f})")

    failures = []
    if duty_growth > MAX_DUTY_SD_GROWTH_PCT:
        failures.append("duty_pct spread grew beyond the reader's resolution")
    if freq_error > MAX_FREQ_ERROR_HZ:
        failures.append("freq_hz mean drifted too far from 1 kHz")

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print("\nDo not patch around this. The escalation path, in order: gate "
              "net_wifi_poll() on a 100 ms millis() check; then a core-0 "
              "FreeRTOS task; then the MCPWM capture unit.")
        return 1

    print("PASS: WiFi does not measurably disturb CP sampling.")
    print("Record these numbers in memory/evsim-network.md either way — a null "
          "result is what the firmware-layout note was waiting for.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="log the firmware's CSV to a file")
    cap.add_argument("--out", required=True, help="CSV file to write")
    cap.add_argument("--port", default=None,
                     help="serial port; auto-detected when unambiguous")
    cap.add_argument("--baud", type=int, default=115200)
    cap.add_argument("--seconds", type=float, default=60,
                     help="how long to log after settling (default 60)")
    cap.add_argument("--settle", type=float, default=3,
                     help="seconds to discard after boot (default 3)")
    cap.add_argument("--state", choices=("B", "C"), default="C",
                     help="pilot state to command before logging; "
                          "pass nothing to leave the board in A")
    cap.add_argument("--boot-timeout", type=float, default=15,
                     help="seconds to wait for the CSV header")
    cap.set_defaults(func=capture)

    cmp_ = sub.add_parser("compare", help="compare a baseline against a WiFi log")
    cmp_.add_argument("baseline", help="log from firmware with no WiFi")
    cmp_.add_argument("wifi", help="log from WiFi firmware, connected")
    cmp_.set_defaults(func=compare)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
