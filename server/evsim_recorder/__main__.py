"""python -m evsim_recorder run --steps 6,7,6,12 --dwell 60: step the charger's limit and record the chain to SQLite."""

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone

from charger_web import config as charger_config
from charger_web.service import ChargerService

from .capture import KEEPALIVE_S, Capture, build_client
from .charger_log import ChargerLogger
from .experiment import Experiment, PlanError, describe, parse_steps, validate_steps
from .storage import Store

log = logging.getLogger("evsim_recorder")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def default_db_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return os.path.join(REPO_ROOT, "data", "recordings", f"evsim-{stamp}.sqlite")


def _positive(kind, minimum=0, strict=True):
    def convert(text):
        try:
            value = kind(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
        if (value <= minimum) if strict else (value < minimum):
            raise argparse.ArgumentTypeError(f"must be {'above' if strict else 'at least'} {minimum}")
        return value
    return convert


def _period(text):
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
    if not 1 <= value <= 10:
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
    run.add_argument("--dwell", type=_positive(float), default=60.0, help="seconds to hold each step (default %(default)s)")
    run.add_argument("--repeat", type=_positive(int), default=1, help="how many times to run the list (default %(default)s)")
    run.add_argument("--baseline", type=_positive(float, 0, strict=False), default=10.0,
                     help="seconds of recording before the first write (default %(default)s)")
    run.add_argument("--db", default=None, help="SQLite file (default data/recordings/evsim-<UTC time>.sqlite)")
    run.add_argument("--host", default="127.0.0.1", help="MQTT broker (default %(default)s)")
    run.add_argument("--port", type=int, default=1883, help="MQTT broker port (default %(default)s)")
    run.add_argument("--charger-period", type=_period, default=1.0, help="seconds between charger polls, 1 to 10 (default %(default)s)")
    run.add_argument("--leave", action="store_true", help="keep the last step on the charger instead of restoring the first limit")
    run.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


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
        service = ChargerService(host, period_s=args.charger_period, on_poll=ChargerLogger(store))
        service.poll_once()
        status = service.status()
        if status["status"] != "ok":
            print(f"Cannot reach the charger at {host}: {status['error'] or 'no answer'}. Nothing was written.", file=sys.stderr)
            return 1
        current = status["set_limit_a"]["value"]
        try:
            validate_steps(args.steps, status["installation_limit_a"]["value"])
        except PlanError as exc:
            print(f"{exc} Nothing was written.", file=sys.stderr)
            return 1
        print(describe(args.steps, args.dwell, args.repeat, args.baseline, current, host, args.leave))
        if not args.yes and str(confirm("Run this experiment on the charger? [y/N] ")).strip().lower() not in ("y", "yes"):
            print("Nothing was written.")
            return 0

        client = build_client()
        Capture(client, store)
        client.connect_async(args.host, args.port, keepalive=KEEPALIVE_S)
        client.loop_start()
        started = True
        service.start()                                           # the heartbeat, until the restore is done
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, lambda signum, frame: stop.set())
        experiment = Experiment(service, store, args.steps, args.dwell, args.repeat, args.baseline, args.leave, stop)
        done = experiment.run()
        print("Done." if done else "Stopped before the end: the limit was restored." if not args.leave else "Stopped before the end.")
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
