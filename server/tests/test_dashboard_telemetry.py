import importlib.util
import json
import unittest

from fake_mqtt import FakeMqttClient, REFUSED
from charger_web import config
from charger_web.telemetry import Telemetry, build_client

CP = '{"t":1,"state":"C","status":"live","setpoint_w":4361,"watts":4361}'
SETPOINT = '{"setpoint_w":4312,"samples":5,"window_s":1,"status":"ok","ts":"2026-10-01T15:13:03Z"}'
OFFLINE = '{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}'
INVERTER = '{"mode":"capped","target_dc_w":4312.6,"cap_ac_w":4182.7,"dc_w":4304.6,"ac_w":4181,"inverter_state":4,"error":null}'


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMqttClient()
        self.clock = FakeClock()
        self.telemetry = Telemetry(self.client, "evsim/setpoint", clock=self.clock, wall=lambda: 1000.0)
        self.client.connect()

    def snapshot(self):
        return self.telemetry.snapshot()

    def test_it_subscribes_to_the_three_topics_and_again_after_a_reconnect(self):
        expected = [("evsim/cp", 0), ("evsim/setpoint", 0), ("evsim/inverter", 0)]
        self.assertEqual(self.client.subscribed, expected)
        self.client.drop()
        self.client.connect()
        self.assertEqual(self.client.subscribed, expected * 2)

    def test_a_refused_connection_subscribes_to_nothing_and_is_not_connected(self):
        client = FakeMqttClient()
        telemetry = Telemetry(client, "evsim/setpoint")
        client.connect(REFUSED)
        self.assertEqual(client.subscribed, [])
        self.assertFalse(telemetry.snapshot()["mqtt"]["connected"])

    def test_the_connection_flag_follows_the_broker(self):
        self.assertTrue(self.snapshot()["mqtt"]["connected"])
        self.client.drop()
        self.assertFalse(self.snapshot()["mqtt"]["connected"])

    def test_nothing_received_is_unavailable_and_serializes(self):
        snap = self.snapshot()
        self.assertEqual(snap["board"]["setpoint_w"]["status"], "unavailable")
        self.assertEqual(snap["setpoint"]["setpoint_w"]["status"], "unavailable")
        self.assertEqual(snap["server"]["target_dc_w"]["status"], "unavailable")
        self.assertEqual(snap["inverter"]["dc_w"]["status"], "unavailable")
        self.assertEqual((snap["board"]["state"], snap["setpoint"]["status"], snap["server"]["mode"]), (None, None, None))
        json.dumps(snap, allow_nan=False)

    def test_the_three_topics_fill_their_cards(self):
        self.client.deliver("evsim/cp", CP)
        self.client.deliver("evsim/setpoint", SETPOINT)
        self.client.deliver("evsim/inverter", INVERTER)
        snap = self.snapshot()
        self.assertEqual((snap["board"]["setpoint_w"]["value"], snap["board"]["setpoint_w"]["status"]), (4361.0, "ok"))
        self.assertEqual((snap["board"]["state"], snap["board"]["status"]), ("C", "live"))
        self.assertEqual((snap["setpoint"]["setpoint_w"]["value"], snap["setpoint"]["status"]), (4312.0, "ok"))
        self.assertEqual(snap["server"]["target_dc_w"]["value"], 4312.6)
        self.assertEqual((snap["inverter"]["dc_w"]["value"], snap["inverter"]["dc_w"]["status"]), (4304.6, "ok"))
        self.assertEqual((snap["server"]["mode"], snap["server"]["inverter_state"]), ("capped", 4))
        json.dumps(snap, allow_nan=False)

    def test_a_null_target_is_unavailable_but_the_mode_shows(self):
        self.client.deliver("evsim/inverter", '{"mode":"zero","target_dc_w":null,"inverter_state":4}')
        server = self.snapshot()["server"]
        self.assertEqual((server["target_dc_w"]["status"], server["mode"]), ("unavailable", "zero"))

    def test_an_unreachable_inverter_makes_the_dc_card_unavailable_and_shows_the_mode(self):
        self.client.deliver("evsim/inverter", '{"mode":"unreachable","target_dc_w":null,"dc_w":null,"inverter_state":null}')
        snap = self.snapshot()
        self.assertEqual(
            (snap["inverter"]["dc_w"]["status"], snap["server"]["target_dc_w"]["status"], snap["server"]["mode"]),
            ("unavailable", "unavailable", "unreachable"))

    def test_the_dc_power_and_the_target_go_stale_together(self):
        self.client.deliver("evsim/inverter", INVERTER)
        self.clock.t += config.MQTT_STALE_AFTER_S
        snap = self.snapshot()
        self.assertEqual((snap["inverter"]["dc_w"]["status"], snap["server"]["target_dc_w"]["status"]), ("stale", "stale"))

    def test_a_retained_inverter_message_is_stale(self):
        self.client.deliver("evsim/inverter", INVERTER, retain=True)
        self.assertEqual(self.snapshot()["inverter"]["dc_w"]["status"], "stale")

    def test_a_malformed_message_is_dropped_and_the_old_value_stays(self):
        self.client.deliver("evsim/cp", "not json")
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "unavailable")
        self.client.deliver("evsim/cp", CP)
        self.client.deliver("evsim/cp", '{"setpoint_w": NaN}')
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["value"], 4361.0)

    def test_an_unknown_topic_is_ignored(self):
        self.client.deliver("evsim/other", CP)
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "unavailable")

    def test_a_value_goes_stale_at_the_limit(self):
        self.client.deliver("evsim/cp", CP)
        self.clock.t += config.MQTT_STALE_AFTER_S - 0.1
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "ok")
        self.clock.t += 0.1
        self.assertEqual(self.snapshot()["board"]["setpoint_w"]["status"], "stale")

    def test_a_retained_message_is_the_last_known_state_but_stale(self):
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["value"], snap["setpoint_w"]["status"], snap["status"]), (0.0, "stale", "server_offline"))

    def test_a_retained_copy_never_overwrites_a_fresh_live_value(self):
        self.client.deliver("evsim/setpoint", SETPOINT)
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["value"], snap["setpoint_w"]["status"], snap["status"]), (4312.0, "ok", "ok"))

    def test_a_retained_copy_replaces_a_stale_live_value(self):
        self.client.deliver("evsim/setpoint", SETPOINT)
        self.clock.t += config.MQTT_STALE_AFTER_S
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        self.assertEqual(self.snapshot()["setpoint"]["status"], "server_offline")

    def test_a_live_message_replaces_a_retained_one(self):
        self.client.deliver("evsim/setpoint", OFFLINE, retain=True)
        self.client.deliver("evsim/setpoint", SETPOINT)
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["status"], snap["status"]), ("ok", "ok"))

    def test_a_live_server_offline_message_is_stale_not_a_fresh_zero(self):
        self.client.deliver("evsim/setpoint", SETPOINT)
        self.client.deliver("evsim/setpoint", OFFLINE)          # not retained: a connected subscriber gets it live
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["value"], snap["setpoint_w"]["status"], snap["status"]),
                         (0.0, "stale", "server_offline"))

    def test_a_live_message_after_server_offline_is_fresh_again(self):
        self.client.deliver("evsim/setpoint", OFFLINE)
        self.client.deliver("evsim/setpoint", SETPOINT)
        snap = self.snapshot()["setpoint"]
        self.assertEqual((snap["setpoint_w"]["status"], snap["status"]), ("ok", "ok"))

    def test_raw_samples_are_drained_once_and_never_include_retained_ones(self):
        self.client.deliver("evsim/cp", CP, retain=True)
        for watts in (4300, 4350, 4400):
            self.client.deliver("evsim/cp", '{"setpoint_w": %d}' % watts)
        self.assertEqual(self.telemetry.drain_cp_samples(), [4300.0, 4350.0, 4400.0])
        self.assertEqual(self.telemetry.drain_cp_samples(), [])

    def test_dropped_messages_do_not_become_samples(self):
        self.client.deliver("evsim/cp", '{"setpoint_w": 99999}')
        self.assertEqual(self.telemetry.drain_cp_samples(), [])


class ConfigurableTopicTest(unittest.TestCase):
    def test_the_setpoint_topic_is_a_setting(self):
        client = FakeMqttClient()
        telemetry = Telemetry(client, "evsim/electric_car")
        client.connect()
        self.assertIn(("evsim/electric_car", 0), client.subscribed)
        self.assertNotIn(("evsim/setpoint", 0), client.subscribed)
        client.deliver("evsim/setpoint", SETPOINT)
        self.assertEqual(telemetry.snapshot()["setpoint"]["setpoint_w"]["status"], "unavailable")
        client.deliver("evsim/electric_car", SETPOINT)
        snap = telemetry.snapshot()["setpoint"]
        self.assertEqual((snap["topic"], snap["setpoint_w"]["value"]), ("evsim/electric_car", 4312.0))


@unittest.skipUnless(importlib.util.find_spec("paho"), "paho-mqtt is not installed")
class BuildClientTest(unittest.TestCase):
    def test_the_client_has_its_own_id_and_never_connects_by_itself(self):
        client = build_client()
        self.assertEqual(client._client_id, b"evsim-charger-web")
        self.assertFalse(client.is_connected())


if __name__ == "__main__":
    unittest.main()
