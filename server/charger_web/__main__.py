"""Run the independent AMTRON charger web interface (Modbus TCP)."""

import argparse
import logging
from http.server import ThreadingHTTPServer

from .config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT
from .service import ChargerService
from .web import create_server


def _port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be from 1 through 65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m charger_web",
        description="Serve the local AMTRON charger page over Modbus TCP, without starting the MQTT setpoint server.",
    )
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help="must be 127.0.0.1")
    parser.add_argument("--port", type=_port, default=DEFAULT_WEB_PORT, help="local page port (default %(default)s)")
    args = parser.parse_args(argv)
    if args.host != DEFAULT_WEB_HOST:
        parser.error("the charger web interface must bind to 127.0.0.1")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("charger_web")
    server: ThreadingHTTPServer | None = None
    service = None
    try:
        try:
            service = ChargerService()
        except ValueError as exc:
            parser.error(str(exc))
        service.start()          # the background poller is also the charger's heartbeat
        server = create_server(charger=service, host=args.host, port=args.port)
        log.info("charger page listening at http://127.0.0.1:%d/", args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping charger page")
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
