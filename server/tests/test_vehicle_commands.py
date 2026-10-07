import importlib.util
import threading
import unittest

from fake_mqtt import FakeMqttClient
from charger_web import config
from charger_web.commands import CommandError, StateCommander
from charger_web.telemetry import Telemetry, build_client


class CommanderTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMqttClient()
        self.telemetry = Telemetry(self.client, "evsim/setpoint")
        self.client.connect()
        self.commander = StateCommander(self.client, self.telemetry, timeout_s=0.4)

    def answer_with(self, *lines):
        """The board answers during the publish itself: the reply can arrive before the wait starts."""
        def hook(topic, payload):
            for line in lines:
                self.client.deliver("evsim/reply", line)
        self.client.publish_hook = hook

    def error_of(self, state):
        with self.assertRaises(CommandError) as raised:
            self.commander.set_state(state)
        return raised.exception

    def test_an_accepted_command_publishes_the_letter_and_returns_the_reply(self):
        self.answer_with("state C -> B")
        self.assertEqual(self.commander.set_state("B"), {"success": True, "state": "B", "reply": "state C -> B"})
        # QoS 0 and not retained: paho queues a QoS 1 message it cannot send and replays it after a reconnect, so a
        # command shown as "Not validated" could still be carried out later (an A opens the pilot and stops the charger)
        self.assertEqual(self.client.published, [("evsim/cmd/state", "B", 0, False)])

    def test_already_in_the_requested_state_is_accepted_too(self):
        self.answer_with("already in B")
        self.assertEqual(self.commander.set_state("B")["reply"], "already in B")

    def test_a_refusal_is_a_409_with_the_boards_reason(self):
        self.answer_with("refused A -> C: state B must be asserted first")
        error = self.error_of("C")
        self.assertEqual((error.status, str(error)), (409, "state B must be asserted first"))

    def test_no_answer_is_a_503_after_the_timeout(self):
        error = self.error_of("B")
        self.assertEqual((error.status, str(error)), (503, "The board did not answer."))

    def test_replies_that_are_not_for_this_command_are_ignored(self):
        self.answer_with("state A -> C", "unknown command 'X'", "already in A", "refused B -> A: nope", "state C -> B")
        self.assertEqual(self.commander.set_state("B")["reply"], "state C -> B")

    def test_only_unrelated_replies_end_in_a_timeout(self):
        self.answer_with("state A -> C", "already in A")
        self.assertEqual(self.error_of("B").status, 503)

    def test_a_retained_or_non_ascii_reply_never_answers(self):
        def hook(topic, payload):
            self.client.deliver("evsim/reply", "state C -> B", retain=True)
            self.client.deliver("evsim/reply", b"state C -> B\xff")
        self.client.publish_hook = hook
        self.assertEqual(self.error_of("B").status, 503)

    def test_a_reply_from_before_the_command_does_not_count(self):
        self.client.deliver("evsim/reply", "state C -> B")
        self.assertEqual(self.error_of("B").status, 503)

    def test_with_the_broker_down_nothing_is_published(self):
        self.client.drop()
        error = self.error_of("B")
        self.assertEqual((error.status, str(error)), (503, "Not connected to the MQTT broker."))
        self.assertEqual(self.client.published, [])

    def test_a_publish_that_fails_is_a_503_and_frees_the_lock(self):
        self.client.publish_rc = 4
        error = self.error_of("B")
        self.assertEqual((error.status, str(error)), (503, "The command could not be sent."))
        self.client.publish_rc = 0
        self.answer_with("state C -> B")
        self.assertTrue(self.commander.set_state("B")["success"])

    def test_anything_but_a_b_or_c_is_a_400_and_nothing_is_published(self):
        for bad in ("a", "D", "", "BB", " B", "B\n", None, 1, True, ["B"], {"state": "B"}):
            self.assertEqual(self.error_of(bad).status, 400, repr(bad))
        self.assertEqual(self.client.published, [])

    def test_a_second_command_while_one_waits_is_a_409_and_publishes_nothing(self):
        first = {}
        thread = threading.Thread(target=lambda: first.setdefault("error", self.error_of("B")))
        thread.start()
        while not self.client.published:
            thread.join(0.01)
        second = self.error_of("C")
        self.assertEqual((second.status, str(second)), (409, "A command is already in progress."))
        thread.join(2)
        self.assertEqual(first["error"].status, 503)                  # the first one timed out
        self.assertEqual(len(self.client.published), 1)

    def test_the_lock_is_free_again_after_a_timeout_and_after_a_refusal(self):
        self.assertEqual(self.error_of("B").status, 503)
        self.answer_with("refused A -> C: state B must be asserted first")
        self.assertEqual(self.error_of("C").status, 409)
        self.answer_with("state A -> B")
        self.assertTrue(self.commander.set_state("B")["success"])


@unittest.skipUnless(importlib.util.find_spec("paho"), "paho-mqtt is not installed")
class RealPahoTest(unittest.TestCase):
    """The fake client cannot show what the real one does with a message it cannot send."""

    def test_an_unsent_command_is_not_queued_for_a_later_reconnect(self):
        client = build_client()
        info = client.publish(config.TOPIC_CMD_STATE, "A", qos=0, retain=False)
        self.assertNotEqual(info.rc, 0)                    # not connected: refused
        self.assertEqual(len(client._out_messages), 0)     # and nothing is left to be replayed

    def test_a_qos_1_command_would_have_been_queued(self):
        client = build_client()
        client.publish(config.TOPIC_CMD_STATE, "A", qos=1, retain=False)
        self.assertEqual(len(client._out_messages), 1)     # the reason the commander must not use QoS 1


if __name__ == "__main__":
    unittest.main()
