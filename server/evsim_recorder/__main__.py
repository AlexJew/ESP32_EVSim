"""python -m evsim_recorder run --steps 6,7,6,12 --dwell 60: step the charger's limit and record the chain to SQLite."""

import argparse
import logging
import math
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

from charger_web import config as charger_config
from charger_web.service import ChargerService

from .capture import KEEPALIVE_S, Capture, build_client
from .charger_log import ChargerLogger, sharing_warning
from .experiment import Experiment, PlanError, describe, parse_steps, validate_steps
from .storage import Store

log = logging.getLogger("evsim_recorder")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# The AMTRON falls back to 0 A after 30 s without Modbus traffic and the first request after that reads 0 although the
# stored limit is still there, so the limit to restore is only believed once several polls in a row agree.
STABLE_POLLS = 4
SETTLE_PAUSE_S = None             # between those polls; None means the charger poll period
SETTLE_TIMEOUT_S = 30.0
MQTT_WAIT_S = 5.0                 # how long to wait for the broker before the first write
MAX_SECONDS = 3600                # the longest dwell or baseline accepted
MAX_REPEAT = 1000


def default_db_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return os.path.join(REPO_ROOT, "data", "recordings", f"evsim-{stamp}.sqlite")


def _number(kind, minimum, maximum, strict=True):
    """A finite number in range: nan and inf are refused (nan would skip every hold, inf would never end)."""
    def convert(text):
        try:
            value = kind(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
        if not math.isfinite(value):
            raise argparse.ArgumentTypeError("must be a finite number")
        if (value <= minimum) if strict else (value < minimum):
            raise argparse.ArgumentTypeError(f"must be {'above' if strict else 'at least'} {minimum}")
        if value > maximum:
            raise argparse.ArgumentTypeError(f"must be at most {maximum}")
        return value
    return convert


def _period(text):
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
    if not math.isfinite(value) or not 1 <= value <= 10:
        raise argparse.ArgumentTypeError("must be from 1 to 10 seconds")
    return value


def _steps(text):
    try:
        return parse_steps(text)
    except PlanError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m evsim_recorder",
        description="Step the AMTRON's maximum current and record the charger, the board, the setpoint server and the "
                    "inverter into one SQLite file, to measure propagation delays and power at each current.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an experiment")
    run.add_argument("--steps", required=True, type=_steps, help="maximum currents to write, in order: 0 or 6 to 16, e.g. 6,7,6,12")
    run.add_argument("--dwell", type=_number(float, 0, MAX_SECONDS), default=60.0,
                     help="seconds to hold each step, up to 3600 (default %(default)s)")
    run.add_argument("--repeat", type=_number(int, 0, MAX_REPEAT), default=1, help="how many times to run the list (default %(default)s)")
    run.add_argument("--baseline", type=_number(float, 0, MAX_SECONDS, strict=False), default=10.0,
                     help="seconds of recording before the first write, up to 3600 (default %(default)s)")
    run.add_argument("--db", default=None, help="SQLite file (default data/recordings/evsim-<UTC time>.sqlite)")
    run.add_argument("--host", default="127.0.0.1", help="MQTT broker (default %(default)s)")
    run.add_argument("--port", type=int, default=1883, help="MQTT broker port (default %(default)s)")
    run.add_argument("--charger-period", type=_period, default=1.0, help="seconds between charger polls, 1 to 10 (default %(default)s)")
    run.add_argument("--leave", action="store_true", help="keep the last step on the charger instead of restoring the first limit")
    run.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


def read_original(service, period_s):
    """The limit set on the charger, or None if it cannot be read reliably.

    Polls until the last STABLE_POLLS polls (about 3 s) all read the same value. The first request after a fallback reads 0
    and the real limit is back a moment later, so a single read, or two reads taken together, cannot be trusted. These polls
    are Modbus traffic too, so the charger is not left to fall back while this runs.
    """
    pause = period_s if SETTLE_PAUSE_S is None else SETTLE_PAUSE_S
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    seen = []
    while True:
        service.poll_once()
        status = service.status()
        seen.append(status["set_limit_a"]["value"] if status["status"] == "ok" else None)
        recent = seen[-STABLE_POLLS:]
        if len(recent) == STABLE_POLLS and recent[0] is not None and all(value == recent[0] for value in recent):
            return recent[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(pause)


def main(argv=None, confirm=input):
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        host = charger_config.charger_host()
    except ValueError as exc:
        parser.error(str(exc))

    path = args.db or default_db_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        store = Store(path)
    except (OSError, sqlite3.Error) as exc:
        print(f"Cannot create the database {path}: {exc}", file=sys.stderr)
        return 1
    stop = threading.Event()
    service = client = None
    started = False
    old_handlers = {}
    try:
        plan = {"steps": args.steps, "dwell_s": args.dwell, "repeat": args.repeat, "baseline_s": args.baseline,
                "charger_period_s": args.charger_period, "leave": args.leave}
        store.start_session(f"{args.host}:{args.port}", host, plan, sys.argv if argv is None else ["evsim_recorder", *argv])
        store.start()
        logger = ChargerLogger(store)
        service = ChargerService(host, period_s=args.charger_period, on_poll=logger)
        service.poll_once()
        status = service.status()
        if status["status"] != "ok":
            print(f"Cannot reach the charger at {host}: {status['error'] or 'no answer'}. Nothing was written.", file=sys.stderr)
            return 1
        current = read_original(service, args.charger_period)
        if current is None:
            print(f"Cannot read the charger's current limit reliably at {host}. Nothing was written.", file=sys.stderr)
            return 1
        try:
            validate_steps(args.steps, service.status()["installation_limit_a"]["value"])
        except PlanError as exc:
            print(f"{exc} Nothing was written.", file=sys.stderr)
            return 1
        service.start()                                           # the heartbeat from here to the end, prompt included
        print(describe(args.steps, args.dwell, args.repeat, args.baseline, current, host, args.leave))
        if not args.yes and str(confirm("Run this experiment on the charger? [y/N] ")).strip().lower() not in ("y", "yes"):
            print("Nothing was written.")
            return 0

        client = build_client()
        capture = Capture(client, store)
        client.connect_async(args.host, args.port, keepalive=KEEPALIVE_S)
        client.loop_start()
        started = True
        if not capture.wait_connected(MQTT_WAIT_S):
            print(f"Cannot reach the MQTT broker at {args.host}:{args.port}: the board, the setpoint server and the inverter "
                  "would not be recorded. Nothing was written.", file=sys.stderr)
            return 1
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is not None:
                old_handlers[sig] = signal.signal(sig, lambda signum, frame: stop.set())
        experiment = Experiment(service, store, args.steps, args.dwell, args.repeat, args.baseline, args.leave, stop,
                                original=current)
        done = experiment.run()
        warning = sharing_warning(logger.polls, logger.failed)
        if warning:
            print(warning, file=sys.stderr)
        if experiment.restore_ok is False:
            print(f"CHARGER LEFT AT {experiment.last_tried} A (maybe): the limit set at the start was {current} A and "
                  f"could not be written back. Set it back by hand.", file=sys.stderr)
            return 3
        print("Done." if done else "Stopped before the end." + ("" if args.leave else " The limit was restored."))
        return 0 if done else 1
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if service is not None:
            service.stop()
        if client is not None and started:
            client.disconnect()
            client.loop_stop()
        store.stop()
        print(f"Recorded to {path}")


if __name__ == "__main__":
    sys.exit(main())
