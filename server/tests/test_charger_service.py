import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from fake_amtron import FakeAmtron
from charger_web import config
from charger_web.registers import load_map
from charger_web.service import ChargerError, ChargerService, LimitValidationError


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.clock = FakeClock()
        self.service = ChargerService("127.0.0.1", self.port, clock=self.clock)
        self.addCleanup(self.service.stop)


class RegisterMapTest(unittest.TestCase):
    def test_the_shipped_map_matches_the_documented_registers(self):
        m = load_map()
        self.assertEqual(m["unit"], 255)
        addresses = {name: spec["address"] for name, spec in m["registers"].items()}
        self.assertEqual(addresses, {
            "set_limit_a": 1000, "operator_limit_a": 134, "installation_limit_a": 133,
            "signalled_current_a": 706, "vehicle_state": 122, "charger_status": 104,
            "meter_current_l1_a": 212, "meter_current_l2_a": 214, "meter_current_l3_a": 216,
        })
        self.assertEqual(m["registers"]["meter_current_l1_a"], {"address": 212, "size": 2, "scale": 0.001})

    def test_a_map_missing_a_register_is_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"unit": 255, "registers": {"set_limit_a": {"address": 1000}}}, f)
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ValueError) as cm:
            load_map(f.name)
        self.assertIn("operator_limit_a", str(cm.exception))


class ReadTest(ServiceTest):
    def test_before_the_first_poll_everything_is_unavailable_and_nothing_is_sent(self):
        s = self.service.status()
        self.assertEqual((s["status"], s["link"]), ("unavailable", "unavailable"))
        self.assertEqual(s["set_limit_a"]["status"], "unavailable")
        self.assertEqual(self.fake.log, [])

    def test_a_poll_fills_the_status(self):
        self.service.poll_once()
        s = self.service.status()
        self.assertEqual((s["status"], s["link"], s["error"]), ("ok", "ok", None))
        self.assertEqual(s["set_limit_a"]["value"], 10)
        self.assertEqual(s["operator_limit_a"]["value"], 16)
        self.assertEqual(s["installation_limit_a"]["value"], 16)
        self.assertEqual(s["signalled_current_a"]["value"], 10)
        self.assertEqual(s["vehicle_state"]["value"], "C")
        self.assertEqual(s["charger_status"]["value"], "Occupied")
        self.assertEqual(s["meter_current_a"]["value"], {"L1": 0.874, "L2": 0.651, "L3": 0.808})
        self.assertEqual(s["set_limit_a"]["status"], "ok")
        self.assertIsNotNone(s["set_limit_a"]["updated_at"])
        self.assertEqual((s["paused"], s["operator_below_set"], s["desired_limit_a"]), (False, False, None))

    def test_32_bit_currents_are_read_as_two_registers_high_word_first(self):
        self.fake.set_u32(212, 70_000)                 # 70.000 A needs both words
        self.service.poll_once()
        self.assertEqual(self.service.status()["meter_current_a"]["value"]["L1"], 70.0)
        self.assertIn((3, 212, 2), self.fake.log)

    def test_a_missing_meter_is_unavailable_and_a_real_zero_is_zero(self):
        self.fake.set_u32(212, 0xFFFFFFFF)
        self.service.poll_once()
        self.assertEqual(sorted(self.service.status()["meter_current_a"]["value"]), ["L2", "L3"])
        for address in (212, 214, 216):
            self.fake.set_u32(address, 0xFFFFFFFF)
        self.service.poll_once()
        meter = self.service.status()["meter_current_a"]
        self.assertEqual((meter["value"], meter["status"]), (None, "unavailable"))
        for address in (212, 214, 216):
            self.fake.set_u32(address, 0)
        self.service.poll_once()
        self.assertEqual(self.service.status()["meter_current_a"]["value"], {"L1": 0.0, "L2": 0.0, "L3": 0.0})

    def test_zero_is_a_reading_not_missing_data(self):
        self.fake.regs[706] = 0
        self.service.poll_once()
        r = self.service.status()["signalled_current_a"]
        self.assertEqual((r["value"], r["status"]), (0, "ok"))

    def test_only_holding_registers_are_used_and_nothing_is_written(self):
        for _ in range(3):
            self.service.poll_once()
        self.assertEqual({entry[0] for entry in self.fake.log}, {3})
        self.assertEqual(self.fake.writes, [])

    def test_one_refused_register_does_not_fail_the_others(self):
        self.fake.refuse_reads = {133}
        self.service.poll_once()
        s = self.service.status()
        self.assertEqual(s["installation_limit_a"]["status"], "unavailable")
        self.assertEqual(s["set_limit_a"]["value"], 10)
        self.assertEqual(s["link"], "ok")

    def test_paused_and_operator_below_set_flags(self):
        self.fake.regs[706] = 0
        self.service.poll_once()
        self.assertTrue(self.service.status()["paused"])
        self.fake.regs[706] = 6
        self.fake.regs[134] = 6
        self.service.poll_once()
        s = self.service.status()
        self.assertFalse(s["paused"])
        self.assertTrue(s["operator_below_set"])

    def test_link_states_follow_the_time_since_the_last_good_poll(self):
        self.service.poll_once()
        self.fake.stop()
        self.clock.t += 4
        self.service.poll_once()                        # fails, but the last good poll is only 4 s old
        self.assertEqual(self.service.status()["link"], "ok")
        self.clock.t += 2                               # 6 s
        s = self.service.status()
        self.assertEqual((s["link"], s["status"]), ("stale", "stale"))
        self.assertIsNotNone(s["error"])
        self.assertEqual((s["set_limit_a"]["value"], s["set_limit_a"]["status"]), (10, "stale"))
        self.clock.t += 25                              # 31 s
        self.assertEqual(self.service.status()["link"], "fallback_expected")

    def test_a_charger_that_never_answers_does_not_block_status(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        with mock.patch.object(config, "MODBUS_TIMEOUT_S", 1):
            stalled = ChargerService("127.0.0.1", listener.getsockname()[1])
            self.addCleanup(stalled.stop)
            poll = threading.Thread(target=stalled.poll_once)    # accepted by the backlog, never answered
            poll.start()
            time.sleep(0.2)
            started = time.monotonic()
            status = stalled.status()
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(status["link"], "unavailable")
            poll.join(timeout=5)
            self.assertFalse(poll.is_alive())
        self.assertIsNotNone(stalled.status()["error"])


if __name__ == "__main__":
    unittest.main()
