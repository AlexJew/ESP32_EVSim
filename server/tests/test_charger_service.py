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
from charger_web.modbus import ModbusTransportError
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


class WriteTest(ServiceTest):
    def test_set_limit_writes_register_1000_and_confirms_it(self):
        self.service.poll_once()
        result = self.service.set_limit(8)
        self.assertEqual((result["success"], result["set_limit_a"]), (True, 8))
        self.assertEqual(self.fake.writes, [(1000, 8)])
        self.assertEqual(self.fake.regs[1000], 8)
        s = self.service.status()
        self.assertEqual((s["set_limit_a"]["value"], s["desired_limit_a"]), (8, 8))

    def test_zero_is_accepted(self):
        self.service.poll_once()
        self.assertEqual(self.service.set_limit(0)["set_limit_a"], 0)
        self.assertEqual(self.fake.writes, [(1000, 0)])

    def test_invalid_limits_are_rejected_before_any_write(self):
        self.service.poll_once()
        for bad in (5, 1, -1, 17, 7.5, 8.0, True, False, "8", None):
            with self.subTest(bad=bad):
                with self.assertRaises(LimitValidationError):
                    self.service.set_limit(bad)
        self.assertEqual(self.fake.writes, [])

    def test_the_installation_limit_caps_the_choice_and_defaults_to_16(self):
        self.fake.regs[133] = 10
        self.service.poll_once()
        with self.assertRaises(LimitValidationError) as cm:
            self.service.set_limit(12)
        self.assertIn("installation limit of 10 A", str(cm.exception))
        self.assertEqual(self.fake.writes, [])
        self.fake.refuse_reads = {133}                 # unreadable: the ceiling is 16
        self.service.poll_once()
        self.assertEqual(self.service.set_limit(12)["set_limit_a"], 12)

    def test_a_refused_write_is_an_error_and_sets_no_desired_limit(self):
        self.service.poll_once()
        self.fake.refuse_writes = True
        with self.assertRaises(ChargerError):
            self.service.set_limit(8)
        self.assertIsNone(self.service.status()["desired_limit_a"])

    def test_an_unconfirmed_write_is_reported_and_sets_no_desired_limit(self):
        self.service.poll_once()
        self.fake.ignore_writes = True                 # the charger echoes the write but keeps the old value
        with self.assertRaises(ChargerError) as cm:
            self.service.set_limit(8)
        self.assertIn("not confirmed", str(cm.exception))
        self.assertIsNone(self.service.status()["desired_limit_a"])

    def test_an_unreachable_charger_during_a_write_is_an_error(self):
        self.service.poll_once()
        self.fake.stop()
        with self.assertRaises(ChargerError):
            self.service.set_limit(8)
        self.assertIsNone(self.service.status()["desired_limit_a"])


class RestoreTest(ServiceTest):
    def test_it_never_writes_on_its_own_at_start(self):
        self.fake.regs[1000] = 0                       # e.g. the charger fell back to 0 A
        for _ in range(3):
            self.clock.t += 20
            self.service.poll_once()
        self.assertEqual(self.fake.writes, [])
        self.assertIsNone(self.service.status()["desired_limit_a"])

    def test_a_value_it_merely_read_never_becomes_the_desired_limit(self):
        self.service.poll_once()
        self.assertIsNone(self.service.status()["desired_limit_a"])
        self.assertEqual(self.fake.writes, [])

    def test_a_lost_limit_is_restored_at_most_once_per_interval(self):
        self.service.poll_once()
        self.service.set_limit(8)
        self.fake.regs[1000] = 0                       # a charger reboot, or another Modbus master
        self.clock.t += 1
        self.service.poll_once()
        self.assertEqual(self.fake.writes, [(1000, 8), (1000, 8)])
        self.assertIsNotNone(self.service.status()["restored_at"])
        self.fake.regs[1000] = 0
        self.clock.t += 1
        self.service.poll_once()                       # within 10 s: no new write
        self.assertEqual(len(self.fake.writes), 2)
        self.clock.t += config.RESTORE_MIN_INTERVAL_S
        self.service.poll_once()
        self.assertEqual(len(self.fake.writes), 3)

    def test_nothing_is_restored_when_the_limit_already_matches(self):
        self.service.poll_once()
        self.service.set_limit(8)
        for _ in range(3):
            self.clock.t += 20
            self.service.poll_once()
        self.assertEqual(self.fake.writes, [(1000, 8)])

    def test_the_fallback_cycle_ends_with_the_limit_back_in_force(self):
        self.service.poll_once()
        self.service.set_limit(10)
        self.fake.fall_back()                          # 30 s of silence: the charger signals 0 A
        self.clock.t += 31
        self.service.poll_once()                       # the first request sees 0 and puts the limit back
        self.assertEqual(self.fake.regs[1000], 10)
        self.assertEqual(self.fake.regs[706], 10)
        self.assertEqual(self.fake.writes[-1], (1000, 10))
        self.assertIsNotNone(self.service.status()["restored_at"])

    def test_a_refused_restore_write_does_not_raise_or_spin(self):
        self.service.poll_once()
        self.service.set_limit(8)
        self.fake.refuse_writes = True
        self.fake.regs[1000] = 0
        for _ in range(3):
            self.clock.t += 1
            self.service.poll_once()                   # logs and carries on
        self.assertEqual(len([w for w in self.fake.writes if w == (1000, 8)]), 2)   # the Apply and one attempt


    def test_an_unconfirmed_write_is_not_silently_reverted_by_the_restore(self):
        self.service.poll_once()
        self.service.set_limit(10)
        self.fake.ignore_writes = False
        original = self.service._read_register

        def fail_readback(name):
            if name == "set_limit_a" and fail_readback.armed:
                raise ModbusTransportError("timeout")
            return original(name)

        fail_readback.armed = True
        with mock.patch.object(self.service, "_read_register", fail_readback):
            with self.assertRaises(ChargerError):
                self.service.set_limit(12)               # sent, but the read-back never arrived
        fail_readback.armed = False
        self.assertIsNone(self.service.status()["desired_limit_a"])
        self.clock.t += 20
        self.service.poll_once()
        self.assertEqual(self.fake.writes, [(1000, 10), (1000, 12)])   # nothing is written back to 10
        self.assertEqual(self.service.status()["set_limit_a"]["value"], 12)

    def test_a_mismatched_read_back_is_not_silently_reverted_either(self):
        self.service.poll_once()
        self.service.set_limit(10)
        self.fake.ignore_writes = True
        with self.assertRaises(ChargerError):
            self.service.set_limit(12)
        self.fake.ignore_writes = False
        self.assertIsNone(self.service.status()["desired_limit_a"])
        self.clock.t += 20
        self.service.poll_once()
        self.assertEqual(self.fake.writes, [(1000, 10), (1000, 12)])

    def test_a_refused_write_keeps_the_earlier_desired_limit(self):
        self.service.poll_once()
        self.service.set_limit(10)
        self.fake.refuse_writes = True
        with self.assertRaises(ChargerError):
            self.service.set_limit(12)                   # refused: the charger certainly still holds 10
        self.assertEqual(self.service.status()["desired_limit_a"], 10)

    def test_nothing_is_restored_once_the_service_is_stopping(self):
        self.service.poll_once()
        self.service.set_limit(8)
        self.fake.regs[1000] = 0
        self.service._stop.set()
        self.clock.t += 20
        self.service.poll_once()
        self.assertEqual(self.fake.writes, [(1000, 8)])

    def test_the_paused_warning_waits_for_a_poll_after_an_apply(self):
        self.fake.regs[1000] = 0
        self.fake.regs[706] = 0
        self.service.poll_once()
        self.fake.ignore_signal_updates = True           # the real unit takes about 3 s to follow
        self.service.set_limit(10)
        self.assertFalse(self.service.status()["paused"])


class RegisterMapSafetyTest(unittest.TestCase):
    def _map(self, address):
        m = load_map()
        m["registers"]["set_limit_a"]["address"] = address
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(m, f)
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_the_writable_register_must_be_1000(self):
        with self.assertRaises(ValueError) as cm:
            load_map(self._map(134))
        self.assertIn("1000", str(cm.exception))

    def test_a_missing_map_file_is_a_value_error(self):
        with self.assertRaises(ValueError):
            load_map("/nonexistent/registers.json")


class ThreadTest(ServiceTest):
    def test_the_poller_thread_polls_repeatedly_and_stops(self):
        service = ChargerService("127.0.0.1", self.port, period_s=0.05)
        self.addCleanup(service.stop)
        service.start()
        deadline = time.monotonic() + 3
        while len([e for e in self.fake.log if e[1] == 1000]) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len([e for e in self.fake.log if e[1] == 1000]), 4)
        service.stop()
        polls = len(self.fake.log)
        time.sleep(0.2)
        self.assertEqual(len(self.fake.log), polls)

    def test_the_poller_thread_survives_an_unexpected_exception(self):
        service = ChargerService("127.0.0.1", self.port, period_s=0.05)
        self.addCleanup(service.stop)
        real = service.poll_once
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("something unexpected")
            real()

        service.poll_once = flaky
        service.start()
        deadline = time.monotonic() + 3
        while len(calls) < 5 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(calls), 5)         # it kept polling after the exception
        self.assertEqual(service.status()["link"], "ok")


if __name__ == "__main__":
    unittest.main()
