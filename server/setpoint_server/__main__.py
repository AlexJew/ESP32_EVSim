"""Entry point: python -m setpoint_server [--host H] [--port P]"""

import argparse
import logging
import signal
import sys

import paho.mqtt.client as mqtt

from . import config
from .server import SetpointServer


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m setpoint_server",
        description="Average the board's evsim/cp setpoint over 1 s and publish it on evsim/setpoint.",
    )
    parser.add_argument("--host", default=config.BROKER_HOST, help="broker address (default %(default)s)")
    parser.add_argument("--port", type=int, default=config.BROKER_PORT, help="broker port (default %(default)s)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.CLIENT_ID)
    client.reconnect_delay_set(min_delay=config.RECONNECT_MIN_S, max_delay=config.RECONNECT_MAX_S)
    server = SetpointServer(client)

    signal.signal(signal.SIGINT, lambda signum, frame: server.request_stop())
    signal.signal(signal.SIGTERM, lambda signum, frame: server.request_stop())

    # connect_async + loop_start: an unreachable broker at startup is retried in
    # the background instead of raising, and the loop reconnects after a drop.
    client.connect_async(args.host, args.port, keepalive=config.KEEPALIVE_S)
    client.loop_start()
    try:
        server.run()
    finally:
        client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
