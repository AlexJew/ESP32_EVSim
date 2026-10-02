import unittest

from charger_web.parsers import parse_cp, parse_inverter, parse_setpoint

HOSTILE_WATTS = [
    b'{"setpoint_w": null}', b'{"setpoint_w": true}', b'{"setpoint_w": "4000"}', b'{"setpoint_w": [1]}',
    b'{"setpoint_w": NaN}', b'{"setpoint_w": Infinity}', b'{"setpoint_w": -Infinity}',
    b'{"setpoint_w": -1}', b'{"setpoint_w": 12000.5}', b'{"setpoint_w": 12001}',
    b'{"setpoint_w": 1' + b"0" * 400 + b"}", b"{}",
]
NOT_OBJECTS = [None, b"", b"not json", b"[1]", b"4000", b"null", b'"text"', b"\xff\xfe", b"{" * 2000]


class CpTest(unittest.TestCase):
    def test_a_normal_sample(self):
        payload = b'{"t":1,"freq":1000.0,"duty":10.5,"cp":"PWM_VALID","state":"C","amps":6.3,"watts":4361,"setpoint_w":4361,"status":"live","age_ms":0}'
        self.assertEqual(parse_cp(payload), {"setpoint_w": 4361.0, "state": "C", "status": "live"})

    def test_a_string_payload_is_accepted(self):
        self.assertEqual(parse_cp('{"setpoint_w": 5}')["setpoint_w"], 5.0)

    def test_the_bounds_are_inclusive(self):
        self.assertEqual(parse_cp(b'{"setpoint_w": 0}')["setpoint_w"], 0.0)
        self.assertEqual(parse_cp(b'{"setpoint_w": 12000}')["setpoint_w"], 12000.0)

    def test_hostile_values_are_dropped(self):
        for payload in HOSTILE_WATTS:
            self.assertIsNone(parse_cp(payload), payload[:40])

    def test_things_that_are_not_objects_are_dropped(self):
        for payload in NOT_OBJECTS:
            self.assertIsNone(parse_cp(payload), payload)

    def test_missing_or_oversized_text_does_not_drop_the_sample(self):
        self.assertEqual(parse_cp(b'{"setpoint_w": 5}'), {"setpoint_w": 5.0, "state": None, "status": None})
        long_text = ('{"setpoint_w": 5, "state": "%s", "status": 7}' % ("x" * 33)).encode()
        self.assertEqual(parse_cp(long_text), {"setpoint_w": 5.0, "state": None, "status": None})


class SetpointTest(unittest.TestCase):
    def test_the_servers_message(self):
        payload = b'{"setpoint_w":4312,"samples":5,"window_s":1,"status":"ok","ts":"2026-10-01T15:13:03Z"}'
        self.assertEqual(parse_setpoint(payload), {"setpoint_w": 4312.0, "status": "ok"})

    def test_the_offline_message_is_a_valid_zero(self):
        payload = b'{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}'
        self.assertEqual(parse_setpoint(payload), {"setpoint_w": 0.0, "status": "server_offline"})

    def test_hostile_values_and_non_objects_are_dropped(self):
        for payload in HOSTILE_WATTS + NOT_OBJECTS:
            self.assertIsNone(parse_setpoint(payload), payload)


class InverterTest(unittest.TestCase):
    def test_the_servers_status(self):
        payload = b'{"mode":"capped","target_dc_w":4312.6,"cap_ac_w":4182.7,"dc_w":4304.6,"ac_w":4181,"inverter_state":4,"error":null,"ts":"2026-10-01T15:13:04Z"}'
        self.assertEqual(parse_inverter(payload), {"target_dc_w": 4312.6, "dc_w": 4304.6, "mode": "capped", "inverter_state": 4})

    def test_null_values_are_kept_as_none(self):
        parsed = parse_inverter(b'{"mode":"unreachable","target_dc_w":null,"dc_w":null,"inverter_state":null}')
        self.assertEqual(parsed, {"target_dc_w": None, "dc_w": None, "mode": "unreachable", "inverter_state": None})

    def test_missing_values_are_none_too(self):
        self.assertEqual(parse_inverter(b'{"mode":"zero"}'),
                         {"target_dc_w": None, "dc_w": None, "mode": "zero", "inverter_state": None})

    def test_a_target_that_is_not_a_good_power_drops_the_message(self):
        for bad in (b'"x"', b"true", b"-5", b"NaN", b"12001", b"[]"):
            self.assertIsNone(parse_inverter(b'{"target_dc_w": ' + bad + b"}"), bad)

    def test_a_measured_power_can_exceed_the_setpoint_cap(self):
        self.assertEqual(parse_inverter(b'{"dc_w": 13000}')["dc_w"], 13000.0)
        self.assertEqual(parse_inverter(b'{"dc_w": 30000}')["dc_w"], 30000.0)
        self.assertEqual(parse_inverter(b'{"dc_w": 0}')["dc_w"], 0.0)

    def test_a_bad_measured_power_becomes_none_and_keeps_the_target(self):
        for bad in (b'"x"', b"true", b"-1", b"NaN", b"Infinity", b"30001", b"[]", b"1" + b"0" * 400):
            parsed = parse_inverter(b'{"target_dc_w": 4300, "dc_w": ' + bad + b"}")
            self.assertEqual((parsed["target_dc_w"], parsed["dc_w"]), (4300.0, None), bad)

    def test_a_bad_inverter_state_is_none_but_the_message_stays(self):
        for bad in (b'"4"', b"true", b"-1", b"256", b"4.5", b"null"):
            parsed = parse_inverter(b'{"target_dc_w": 100, "inverter_state": ' + bad + b"}")
            self.assertEqual(parsed["inverter_state"], None, bad)

    def test_non_objects_are_dropped(self):
        for payload in NOT_OBJECTS:
            self.assertIsNone(parse_inverter(payload), payload)

if __name__ == "__main__":
    unittest.main()
