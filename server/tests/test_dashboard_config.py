import os
import unittest
from unittest import mock

from charger_web import config
from charger_web.reading import reading

NAMES = ("EVSIM_MQTT_HOST", "EVSIM_SETPOINT_TOPIC")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in NAMES:
            os.environ.pop(name, None)

    def test_the_defaults_are_the_lab_values(self):
        self.assertEqual(config.mqtt_host(), "127.0.0.1")
        self.assertEqual(config.setpoint_topic(), "evsim/setpoint")

    def test_the_environment_overrides_them(self):
        os.environ["EVSIM_MQTT_HOST"] = " broker.lab "
        os.environ["EVSIM_SETPOINT_TOPIC"] = "evsim/electric_car"
        self.assertEqual((config.mqtt_host(), config.setpoint_topic()), ("broker.lab", "evsim/electric_car"))

    def test_a_host_with_a_url_or_nothing_is_refused(self):
        for bad in ("http://x", "a/b", "", "   ", "user@host", "h?x", "h#x"):
            os.environ["EVSIM_MQTT_HOST"] = bad
            with self.assertRaises(ValueError, msg=bad):
                config.mqtt_host()

    def test_a_setpoint_topic_with_a_wildcard_or_whitespace_is_refused(self):
        for bad in ("", "  ", "evsim/#", "evsim/+/x", "#", "a b", "evsim/\tx", "x" * 129):
            os.environ["EVSIM_SETPOINT_TOPIC"] = bad
            with self.assertRaises(ValueError, msg=bad):
                config.setpoint_topic()

    def test_the_charger_host_check_is_unchanged(self):
        os.environ["EVSIM_CHARGER_HOST"] = "http://x"
        with self.assertRaisesRegex(ValueError, "EVSIM_CHARGER_HOST must be a host name"):
            config.charger_host()


class ReadingTest(unittest.TestCase):
    def test_no_value_is_unavailable(self):
        self.assertEqual(reading(None, 10.0, 11.0, 5), {"value": None, "status": "unavailable", "updated_at": None})

    def test_a_young_value_is_ok_and_carries_its_time(self):
        result = reading(7000, 10.0, 12.0, 5, wall=lambda: 1000.0)
        self.assertEqual((result["value"], result["status"]), (7000, "ok"))
        self.assertEqual(result["updated_at"], "1970-01-01T00:16:38+00:00")    # 1000 s minus 2 s old

    def test_an_old_value_is_stale(self):
        self.assertEqual(reading(7000, 10.0, 16.0, 5)["status"], "stale")

    def test_the_limit_itself_is_stale(self):
        self.assertEqual(reading(7000, 10.0, 14.999, 5)["status"], "ok")
        self.assertEqual(reading(7000, 10.0, 15.0, 5)["status"], "stale")

    def test_a_forced_stale_value_keeps_its_value(self):
        result = reading(7000, 10.0, 11.0, 5, force_stale=True)
        self.assertEqual((result["value"], result["status"]), (7000, "stale"))

    def test_a_value_with_no_time_is_stale_with_no_timestamp(self):
        result = reading(0.0, None, 11.0, 5)
        self.assertEqual((result["value"], result["status"], result["updated_at"]), (0.0, "stale", None))

    def test_a_clock_that_ran_backwards_does_not_give_a_future_time(self):
        result = reading(1, 20.0, 10.0, 5, wall=lambda: 1000.0)
        self.assertEqual(result["updated_at"], "1970-01-01T00:16:40+00:00")


if __name__ == "__main__":
    unittest.main()
