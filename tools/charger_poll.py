#!/usr/bin/env python3
"""Poll the AMTRON's dashboard and log the current it signals on CP.

Purpose: produce a timestamped log of SignaledCurrentLimit_vehicleif so it can be
lined up against the duty cycle the ESP32 measures on the CP line. That validates
the ESP32's duty -> current conversion against the charger's own numbers.

The charger is a Phoenix Contact CC612 behind a MENNEKES AMTRON Charge Control.
Only the standard library is used, so there is nothing to pip install.

Auth: the web UI sends a plain `Authorization: <uuid>` header (no Bearer prefix).
Grab the token from a browser request and pass it via --token or $EVSIM_TOKEN.
Tokens expire; a 401 means fetch a fresh one.

Examples:
    export EVSIM_TOKEN=b37ce7a5-...
    ./charger_poll.py                          # stream to the terminal
    ./charger_poll.py --csv run1.csv           # ...and record it
    ./charger_poll.py --duration 120           # stop after two minutes
    ./charger_poll.py --sweep 6,8,10,13,16 --csv sweep.csv   # WRITES to charger
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "192.168.21.117"

# Fields pulled out of dashboard.json. Everything else in the response is ignored.
DASH_FIELDS = {
    "SignaledCurrentLimit_vehicleif": "signaled",
    "Type2StateConnector1_vehicleif": "type2_state",
    "OCPPStateConnector1_ocpp": "ocpp_state",
    "OcppMeterCurrent_meter": "meter_current",
}
LIMIT_KEY = "OperatorCurrentLimit_vehicleif"
INSTALL_LIMIT_KEY = "InstallationCurrentLimit_vehicleif"

CSV_COLUMNS = [
    "iso_time",
    "elapsed_s",
    "operator_limit_a",
    "signaled_a",
    "expected_duty_pct",
    "offering",
    "type2_state",
    "ocpp_state",
    "meter_current",
]


class AuthError(RuntimeError):
    """The charger rejected the token."""


# --- charger HTTP -----------------------------------------------------------


def _request(host: str, path: str, token: str, payload: dict | None = None) -> dict:
    headers = {
        "Authorization": token,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json;charset=UTF-8"

    req = urllib.request.Request(f"http://{host}{path}", data=data, headers=headers)
    try:
        raw = urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError(
                "charger returned 401 - the token has expired.\n"
                "Log in to the web UI, copy a fresh Authorization header, and retry."
            ) from exc
        raise
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _walk_fields(doc: dict):
    """dashboard.json nests fields under groups; settings.json adds sub_groups."""
    for group in doc.get("groups", []):
        yield from group.get("fields", []) or []
        for sub in group.get("sub_groups", []) or []:
            yield from sub.get("fields", []) or []


def read_dashboard(host: str, token: str) -> dict:
    doc = _request(host, "/json/dashboard.json", token)
    out = {}
    for field in _walk_fields(doc):
        name = DASH_FIELDS.get(field.get("key"))
        if name:
            out[name] = field.get("value")
    return out


def read_limits(host: str, token: str) -> dict:
    """Returns the operator and installation current limits, in amps."""
    doc = _request(host, "/json/settings.json", token)
    out = {}
    for field in _walk_fields(doc):
        if field.get("key") == LIMIT_KEY:
            out["operator"] = field.get("value")
        elif field.get("key") == INSTALL_LIMIT_KEY:
            out["installation"] = field.get("value")
    return out


def set_operator_limit(host: str, token: str, amps: int) -> dict:
    """Writes the operator current limit. This changes charger state."""
    return _request(
        host,
        "/json/settings.json",
        token,
        {"params": [{"key": LIMIT_KEY, "value": amps}]},
    )


# --- conversion -------------------------------------------------------------


def current_to_duty(amps: float) -> float | None:
    """Invert the IEC 61851 duty->current rule (see evsim.gonium.net).

    6-51 A:  I = 0.6 * duty      ->  duty = I / 0.6
    51-80 A: I = 2.5 * (duty-64) ->  duty = I / 2.5 + 64

    Returns None outside the signalable range, including the 0 A the charger
    reports between offers when nothing is drawing load.
    """
    if amps is None or amps < 6:
        return None
    if amps <= 51:
        return amps / 0.6
    if amps <= 80:
        return amps / 2.5 + 64
    return None


def parse_amps(value) -> float | None:
    """SignaledCurrentLimit_vehicleif arrives as a string like '10 A'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    token = str(value).strip().split()[0] if str(value).strip() else ""
    try:
        return float(token)
    except ValueError:
        return None


def clean(text) -> str:
    """Strip the HTML the web UI embeds in some state strings."""
    return " ".join(str(text).replace("<br>", " ").split())


# --- polling ----------------------------------------------------------------


def build_row(sample: dict, limit: int | None, started: float) -> dict:
    amps = parse_amps(sample.get("signaled"))
    duty = current_to_duty(amps) if amps else None
    now = time.time()
    return {
        "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "elapsed_s": round(now - started, 2),
        "operator_limit_a": limit if limit is not None else "",
        "signaled_a": amps if amps is not None else "",
        "expected_duty_pct": round(duty, 2) if duty is not None else "",
        "offering": int(bool(amps)),
        "type2_state": clean(sample.get("type2_state")),
        "ocpp_state": clean(sample.get("ocpp_state")),
        "meter_current": clean(sample.get("meter_current")),
    }


def format_row(row: dict) -> str:
    duty = f"{row['expected_duty_pct']:>5}%" if row["expected_duty_pct"] != "" else "    - "
    amps = f"{row['signaled_a']:>4} A" if row["signaled_a"] != "" else "   -  "
    mark = "OFFER" if row["offering"] else "  -  "
    return (
        f"{row['elapsed_s']:>7.1f}s  {amps}  duty {duty}  {mark}  "
        f"{row['ocpp_state'][:22]:<22} {row['type2_state'][:30]}"
    )


def poll_loop(args, token: str, writer, limit_holder: dict) -> None:
    started = time.time()
    consecutive_errors = 0
    print(
        f"{'elapsed':>8}  {'signal':>6}  {'expected':>10}  state  "
        f"{'ocpp':<22} type2",
        file=sys.stderr,
    )
    while True:
        if args.duration and time.time() - started >= args.duration:
            return
        try:
            sample = read_dashboard(args.host, token)
            consecutive_errors = 0
        except AuthError:
            raise
        except Exception as exc:  # transient: the CC612 drops connections briefly
            consecutive_errors += 1
            print(f"  ... poll failed ({exc}); retrying", file=sys.stderr)
            if consecutive_errors >= 10:
                raise RuntimeError("10 consecutive poll failures; giving up") from exc
            time.sleep(args.interval)
            continue

        row = build_row(sample, limit_holder.get("operator"), started)
        print(format_row(row), file=sys.stderr)
        if writer:
            writer.writerow(row)
        time.sleep(args.interval)


def sweep_loop(args, token: str, writer, limit_holder: dict) -> None:
    """Step the operator limit through a list, dwelling at each. WRITES."""
    started = time.time()
    steps = [int(s) for s in args.sweep.split(",") if s.strip()]
    ceiling = limit_holder.get("installation")
    for amps in steps:
        if ceiling and amps > ceiling:
            print(
                f"  !! skipping {amps} A - above the {ceiling} A installation limit",
                file=sys.stderr,
            )
            continue
        print(f"\n=== setting operator limit to {amps} A ===", file=sys.stderr)
        set_operator_limit(args.host, token, amps)
        limit_holder["operator"] = amps
        expected = current_to_duty(amps)
        if expected:
            print(f"    expect ~{expected:.1f}% duty on CP", file=sys.stderr)

        dwell_start = time.time()
        while time.time() - dwell_start < args.dwell:
            try:
                sample = read_dashboard(args.host, token)
            except AuthError:
                raise
            except Exception as exc:
                print(f"  ... poll failed ({exc})", file=sys.stderr)
                time.sleep(args.interval)
                continue
            row = build_row(sample, amps, started)
            print(format_row(row), file=sys.stderr)
            if writer:
                writer.writerow(row)
            time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll the AMTRON dashboard and log the signaled CP current.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[-1],
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"default {DEFAULT_HOST}")
    parser.add_argument(
        "--token",
        default=os.environ.get("EVSIM_TOKEN"),
        help="Authorization token; defaults to $EVSIM_TOKEN",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    parser.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = forever)")
    parser.add_argument("--csv", help="append samples to this CSV file")
    parser.add_argument(
        "--sweep",
        help="comma-separated amps to step through, e.g. 6,8,10,13,16. WRITES to the charger.",
    )
    parser.add_argument("--dwell", type=float, default=20, help="seconds to hold each sweep step")
    args = parser.parse_args()

    if not args.token:
        parser.error("no token: pass --token or set EVSIM_TOKEN")

    try:
        limits = read_limits(args.host, args.token)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: cannot reach the charger at {args.host}: {exc}", file=sys.stderr)
        return 2

    print(
        f"charger {args.host}  operator limit {limits.get('operator')} A  "
        f"(installation ceiling {limits.get('installation')} A)",
        file=sys.stderr,
    )

    handle = writer = None
    if args.csv:
        new_file = not os.path.exists(args.csv)
        handle = open(args.csv, "a", newline="")
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()

    try:
        if args.sweep:
            sweep_loop(args, args.token, writer, limits)
        else:
            poll_loop(args, args.token, writer, limits)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if handle:
            handle.close()
            print(f"wrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
