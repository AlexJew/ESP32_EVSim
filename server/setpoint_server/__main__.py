"""Entry point: python -m setpoint_server [--host H] [--port P] [--inverter HOST]"""

import argparse
import logging
import signal
import sys

import paho.mqtt.client as mqtt

from . import config
from .inverter_control import Controller
from .inverter_writer import InverterWriter
from .server import SetpointServer


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m setpoint_server",
        description="Average the board's evsim/cp setpoint over 1 s and publish it on evsim/setpoint.",
    )
    parser.add_argument("--host", default=config.BROKER_HOST, help="broker address (default %(default)s)")
    parser.add_argument("--port", type=int, default=config.BROKER_PORT, help="broker port (default %(default)s)")
    parser.add_argument("--inverter", metavar="HOST",
                        help="also drive the Fronius inverter at HOST over Modbus TCP; without this option "
                             "the inverter is never touched")
    parser.add_argument("--inverter-port", type=int, default=config.INVERTER_PORT,
                        help="inverter Modbus port (default %(default)s)")
    parser.add_argument("--inverter-unit", type=int, default=config.INVERTER_UNIT,
                        help="inverter Modbus unit ID (default %(default)s)")
    parser.add_argument("--standby-after", type=float, default=config.STANDBY_AFTER_S,
                        help="seconds without data before the inverter goes to standby (default %(default)s)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.CLIENT_ID)
    client.reconnect_delay_set(min_delay=config.RECONNECT_MIN_S, max_delay=config.RECONNECT_MAX_S)

    writer = None
    if args.inverter:
        def publish_status(payload):
            if client.is_connected():
                client.publish(config.INVERTER_TOPIC, payload, qos=0)

        writer = InverterWriter(
            args.inverter, args.inverter_port, args.inverter_unit, publish=publish_status,
            controller=Controller(config.DEADBAND_W, config.REFRESH_S, args.standby_after),
        )
    server = SetpointServer(client, writer=writer)

    signal.signal(signal.SIGINT, lambda signum, frame: server.request_stop())
    signal.signal(signal.SIGTERM, lambda signum, frame: server.request_stop())

    # connect_async + loop_start: an unreachable broker at startup is retried in
    # the background instead of raising, and the loop reconnects after a drop.
    client.connect_async(args.host, args.port, keepalive=config.KEEPALIVE_S)
    client.loop_start()
    if writer is not None:
        writer.start()
    try:
        server.run()
    finally:
        if writer is not None:
            writer.stop()             # a no-op when run() already stopped it
        client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
