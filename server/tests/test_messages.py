import json
import unittest
from datetime import datetime, timedelta, timezone

from setpoint_server.messages import format_setpoint, offline_payload, parse_cp

# A real message, as defined in the CP telemetry spec.
REAL_CP = (
    '{"t":123456,"freq":1000.0,"duty":24.30,"adc":0,"cp":"PWM_VALID","state":"C",'
    '"amps":14.58,"watts":10060,"setpoint_w":10060,"status":"live","age_ms":0}'
)
NOW = datetime(2026, 9, 30, 13, 42, 7, 123456, tzinfo=timezone.utc)
NOW_MS = 1790775727123          # the same instant in epoch milliseconds


class ParseCpTest(unittest.TestCase):
    def test_reads_setpoint_from_a_real_message(self):
        self.assertEqual(parse_cp(REAL_CP), 10060.0)

    def test_accepts_bytes_as_paho_delivers_them(self):
        self.assertEqual(parse_cp(REAL_CP.encode()), 10060.0)

    def test_accepts_zero_and_fractions(self):
        self.assertEqual(parse_cp(b'{"setpoint_w":0}'), 0.0)
        self.assertEqual(parse_cp(b'{"setpoint_w":10060.5}'), 10060.5)

    def test_the_cap_itself_is_accepted(self):
        self.assertEqual(parse_cp(b'{"setpoint_w":12000}'), 12000.0)

    def test_a_full_16a_offer_is_accepted(self):
        # 16.2 A x 690 V = 11 190 W, what the board reports at the charger's 16 A limit
        self.assertEqual(parse_cp(b'{"setpoint_w":11190}'), 11190.0)

    def test_rejects_unusable_messages(self):
        bad = {
            "not json": b"not json",
            "empty": b"",
            "a list": b"[1,2]",
            "no setpoint": b'{"t":1}',
            "null": b'{"setpoint_w":null}',
            "a string": b'{"setpoint_w":"10060"}',
            "a boolean": b'{"setpoint_w":true}',
            "NaN": b'{"setpoint_w":NaN}',
            "Infinity": b'{"setpoint_w":Infinity}',
            "negative": b'{"setpoint_w":-1}',
            "just above the cap": b'{"setpoint_w":12000.5}',
            "far above the cap": b'{"setpoint_w":1000000}',
            "integer too large for a float": b'{"setpoint_w":1' + b"0" * 400 + b"}",
            "not UTF-8": b"\xff\xfe\x00\xff",
            "deeply nested": b"[" * 100000,
        }
        for name, payload in bad.items():
            with self.subTest(name):
                self.assertIsNone(parse_cp(payload))


class FormatSetpointTest(unittest.TestCase):
    def test_ok_message_matches_the_contract_exactly(self):
        self.assertEqual(
            format_setpoint(5, 10060.0, 1, NOW),
            '{"setpoint_w":10060,"samples":5,"window_s":1,"status":"ok",'
            '"ts":"2026-09-30T13:42:07Z","ts_ms":%d}' % NOW_MS,
        )

    def test_half_rounds_up(self):
        cases = [(0.5, 1), (1.5, 2), (2.5, 3), (10060.49, 10060), (10060.5, 10061)]
        for mean, expected in cases:
            with self.subTest(mean=mean):
                out = json.loads(format_setpoint(1, mean, 1, NOW))
                self.assertEqual(out["setpoint_w"], expected)

    def test_no_data_message_is_zero(self):
        self.assertEqual(
            format_setpoint(0, None, 1, NOW),
            '{"setpoint_w":0,"samples":0,"window_s":1,"status":"no_data",'
            '"ts":"2026-09-30T13:42:07Z","ts_ms":%d}' % NOW_MS,
        )

    def test_ts_ms_is_the_epoch_in_milliseconds_whatever_the_timezone(self):
        local = datetime(2026, 9, 30, 15, 42, 7, 123456, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(json.loads(format_setpoint(1, 100.0, 1, local))["ts_ms"], NOW_MS)
        self.assertEqual(json.loads(format_setpoint(1, 100.0, 1, NOW))["ts_ms"], NOW_MS)

    def test_the_offline_message_has_no_timestamp(self):
        self.assertNotIn("ts_ms", json.loads(offline_payload(1)))

    def test_ts_is_converted_to_utc(self):
        local = datetime(2026, 9, 30, 15, 42, 7, tzinfo=timezone(timedelta(hours=2)))
        out = json.loads(format_setpoint(1, 100.0, 1, local))
        self.assertEqual(out["ts"], "2026-09-30T13:42:07Z")


class OfflinePayloadTest(unittest.TestCase):
    def test_matches_the_contract_and_has_no_ts(self):
        self.assertEqual(
            offline_payload(1),
            '{"setpoint_w":0,"samples":0,"window_s":1,"status":"server_offline"}',
        )
        self.assertNotIn("ts", json.loads(offline_payload(1)))


if __name__ == "__main__":
    unittest.main()
