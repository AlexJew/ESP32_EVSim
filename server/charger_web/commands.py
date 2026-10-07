"""The state command: publish A, B or C to the board and wait for its one-line answer.

This is the page's only write path to MQTT. Replies carry no command id, so a reply is matched by content: a line
naming the requested state is the answer, any other line is somebody else's and is ignored. One command at a time.
"""

import logging
import re
import threading

from . import config

log = logging.getLogger("charger_web.commands")

STATES = ("A", "B", "C")


class CommandError(RuntimeError):
    """A command that did not succeed; `status` is the HTTP status to answer with."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class StateCommander:
    def __init__(self, client, telemetry, timeout_s=config.STATE_REPLY_TIMEOUT_S):
        self._client = client
        self._telemetry = telemetry
        self._timeout = timeout_s
        self._busy = threading.Lock()

    def set_state(self, state):
        if type(state) is not str or state not in STATES:
            raise CommandError(400, "State must be A, B or C.")
        if not self._telemetry.snapshot()["mqtt"]["connected"]:
            raise CommandError(503, "Not connected to the MQTT broker.")
        if not self._busy.acquire(blocking=False):
            raise CommandError(409, "A command is already in progress.")
        try:
            accepted = re.compile(r"^(state [ABC] -> %s|already in %s)$" % (state, state))
            refused = re.compile(r"^refused [ABC] -> %s(?:: (.*))?$" % state)
            since = self._telemetry.reply_marker()            # taken before publishing: a fast reply is not missed
            info = self._client.publish(config.TOPIC_CMD_STATE, state, qos=1, retain=False)
            if info.rc != 0:
                raise CommandError(503, "The command could not be sent.")
            reply = self._telemetry.wait_reply(
                since, lambda text: bool(accepted.match(text) or refused.match(text)), self._timeout)
        finally:
            self._busy.release()
        if reply is None:
            raise CommandError(503, "The board did not answer.")
        refusal = refused.match(reply)
        if refusal:
            raise CommandError(409, refusal.group(1) or reply)
        log.info("state %s accepted: %s", state, reply)
        return {"success": True, "state": state, "reply": reply}
