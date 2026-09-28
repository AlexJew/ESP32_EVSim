#!/usr/bin/env python3
"""Poll the AMTRON's dashboard and log every field it reports.

Purpose: produce a timestamped log of SignaledCurrentLimit_vehicleif so it can be
lined up against the duty cycle the ESP32 measures on the CP line. That validates
the ESP32's duty -> current conversion against the charger's own numbers.

Every run writes a CSV to data/ automatically, named for the moment it started.
The first columns are the stable ones the calibration workflow depends on -- they
share names with the firmware's serial CSV so the two logs merge on time. After
those comes one column per remaining dashboard field, so nothing the charger
reports is thrown away.

Because the trailing columns are discovered from the charger's own field list,
they can differ between charger firmware versions; the leading columns cannot.
Prefer the leading columns for anything you intend to compare across runs.

The charger is a Phoenix Contact CC612 behind a MENNEKES AMTRON Charge Control.
Only the standard library is used, so there is nothing to pip install.

Auth: the web UI sends a plain `Authorization: <uuid>` header (no Bearer prefix).
Grab the token from a browser request and pass it via --token or $EVSIM_TOKEN.
Tokens expire; a 401 means fetch a fresh one.

Examples:
    export EVSIM_TOKEN=b37ce7a5-...
    ./charger_poll.py                          # stream, and log to data/
    ./charger_poll.py --duration 120           # stop after two minutes
    ./charger_poll.py --csv run1.csv           # name the file yourself
    ./charger_poll.py --no-csv                 # terminal only, write nothing
    ./charger_poll.py --sweep 6,8,10,13,16     # WRITES to the charger
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

# Where logs land when --csv is not given. Relative to the repository root, not
# the caller's shell, so the file ends up in the same place wherever it is run
# from. *.csv is gitignored, so nothing here is committed.
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Dashboard fields promoted to friendly column names. These are the ones the
# calibration workflow reads, so their names are a contract: the firmware's
# serial CSV is merged against them on time. Every OTHER dashboard field is
# still logged, under its own raw charger key, after these.
DASH_FIELDS = {
    "SignaledCurrentLimit_vehicleif": "signaled",
    "Type2StateConnector1_vehicleif": "type2_state",
    "OCPPStateConnector1_ocpp": "ocpp_state",
    "OcppMeterCurrent_meter": "meter_current",
}
LIMIT_KEY = "OperatorCurrentLimit_vehicleif"
INSTALL_LIMIT_KEY = "InstallationCurrentLimit_vehicleif"

# The leading, stable columns. The charger's remaining fields are appended after
# these, sorted by key, once the first sample reveals what they are.
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


def default_csv_path(started: float) -> str:
    """data/charger_YYYY-MM-DD_HH-MM-SS.csv, named for when the run started."""
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started))
    return os.path.join(DATA_DIR, f"charger_{stamp}.csv")


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
    """Every field dashboard.json exposes, keyed by its raw charger key.

    Returns the whole set rather than a filtered few: build_row() promotes the
    handful in DASH_FIELDS to friendly names and logs the rest as they are.
    """
    doc = _request(host, "/json/dashboard.json", token)
    out = {}
    for field in _walk_fields(doc):
        key = field.get("key")
        if key:
            out[key] = field.get("value")
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


class CsvSink:
    """Writes rows to a CSV, opening the file on the first row.

    The column set cannot be known up front: the trailing columns come from the
    charger's own field list, which is only visible once a sample has arrived.
    So the header is derived from the first row rather than hard-coded.

    Appending to an existing file reuses that file's header, so a run pointed at
    an earlier log stays readable even if the charger now reports a different
    field set. Fields absent from the header are dropped, and a note is printed
    once rather than every second.
    """

    def __init__(self, path: str):
        self.path = path
        self.rows = 0
        self._handle = None
        self._writer = None
        self._warned = False

    def writerow(self, row: dict) -> None:
        if self._writer is None:
            self._open(row)
        writer = self._writer
        assert writer is not None  # _open() always assigns it

        dropped = [k for k in row if k not in writer.fieldnames]
        if dropped and not self._warned:
            self._warned = True
            print(
                f"  ... {len(dropped)} field(s) not in this file's header are not "
                f"being logged ({', '.join(sorted(dropped)[:3])}). "
                f"Start a new run to capture them.",
                file=sys.stderr,
            )

        writer.writerow(row)
        self.rows += 1

    def _open(self, first_row: dict) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        existing = self._existing_header()
        if existing:
            fieldnames = existing
        else:
            extra = sorted(k for k in first_row if k not in CSV_COLUMNS)
            fieldnames = CSV_COLUMNS + extra

        self._handle = open(self.path, "a", newline="")
        self._writer = csv.DictWriter(
            self._handle, fieldnames=fieldnames, extrasaction="ignore"
        )
        if not existing:
            self._writer.writeheader()

    def _existing_header(self) -> list[str] | None:
        """The header of a non-empty file we are appending to, if there is one."""
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return None
        with open(self.path, newline="") as f:
            header = next(csv.reader(f), None)
        return header or None

    @property
    def columns(self) -> int:
        """How many columns the header ended up with; 0 before the first row."""
        return len(self._writer.fieldnames) if self._writer else 0

    def close(self) -> None:
        if self._handle:
            self._handle.close()


def build_row(sample: dict, limit: int | None, started: float) -> dict:
    """One CSV row: the stable columns first, then every other charger field.

    `sample` is the raw key -> value mapping from read_dashboard().
    """
    named = {name: sample.get(key) for key, name in DASH_FIELDS.items()}
    amps = parse_amps(named.get("signaled"))
    duty = current_to_duty(amps) if amps else None
    now = time.time()
    row = {
        "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "elapsed_s": round(now - started, 2),
        "operator_limit_a": limit if limit is not None else "",
        "signaled_a": amps if amps is not None else "",
        "expected_duty_pct": round(duty, 2) if duty is not None else "",
        "offering": int(bool(amps)),
        "type2_state": clean(named.get("type2_state")),
        "ocpp_state": clean(named.get("ocpp_state")),
        "meter_current": clean(named.get("meter_current")),
    }

    # Everything else the charger reported, under its own key. The four already
    # promoted above are skipped rather than duplicated. Values go through
    # clean() too, since several carry the web UI's embedded <br> tags.
    for key, value in sample.items():
        if key not in DASH_FIELDS:
            row[key] = clean(value) if value is not None else ""
    return row


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
    parser.add_argument(
        "--csv",
        help="write to this path instead of an auto-named file in data/",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="stream to the terminal only; write no file",
    )
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

    sink = None
    if not args.no_csv:
        path = args.csv or default_csv_path(time.time())
        sink = CsvSink(path)
        print(f"logging to {path}", file=sys.stderr)

    try:
        if args.sweep:
            sweep_loop(args, args.token, sink, limits)
        else:
            poll_loop(args, args.token, sink, limits)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if sink:
            sink.close()
            if sink.rows:
                print(
                    f"wrote {sink.path} ({sink.rows} rows, {sink.columns} columns)",
                    file=sys.stderr,
                )
            else:
                print("no rows captured; no file written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
