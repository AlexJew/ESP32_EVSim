import json
import logging
import socket
import threading
import time
import unittest
from datetime import datetime, timezone

from fake_inverter import FakeInverter, M103, M123
from setpoint_server import config
from setpoint_server.inverter_writer import InverterWriter

NOW = datetime(2026, 9, 30, 13, 42, 7, tzinfo=timezone.utc)
CONN, PCT, ENA = M123 + 2, M123 + 3, M123 + 7
ST = M103 + 36           # SunSpec state register: 4 = MPPT, 3 = STARTING


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class WriterTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.fake = FakeInverter()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.clock = FakeClock()
        self.status = []
        self.writer = InverterWriter("127.0.0.1", self.port, timeout_s=1, publish=self.status.append,
                                     clock=self.clock, utcnow=lambda: NOW)
        self.addCleanup(self.writer.stop)

    def last(self):
        return json.loads(self.status[-1])

    def test_a_target_is_written_committed_and_reported(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(PCT, 3233), (ENA, 1)])
        s = self.last()
        self.assertEqual((s["mode"], s["target_dc_w"], s["cap_ac_w"]), ("capped", 4000.0, 3879.2))
        self.assertEqual((s["dc_w"], s["ac_w"], s["inverter_state"], s["error"]), (7272, 7047, 4, None))
        self.assertEqual(list(s), ["mode", "target_dc_w", "cap_ac_w", "dc_w", "ac_w", "inverter_state", "error", "ts"])
        self.assertEqual(s["ts"], "2026-09-30T13:42:07Z")

    def test_starting_with_no_data_asserts_a_zero_cap(self):
        self.writer.step_once()            # nothing was ever set
        self.assertEqual(self.fake.writes, [(PCT, 0), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "zero")

    def test_a_target_inside_the_dead_band_is_not_written(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        del self.fake.writes[:]
        self.clock.t += 1
        self.writer.set_target(4050.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [])

    def test_a_stale_target_counts_as_no_data(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        del self.fake.writes[:]
        self.clock.t += config.MAILBOX_STALE_S + 1     # the server stopped refreshing it
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(PCT, 0), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "zero")

    def test_nan_infinite_and_negative_targets_count_as_no_data(self):
        for bad in (float("nan"), float("inf"), -5.0):
            with self.subTest(bad=bad):
                del self.fake.writes[:]
                self.writer.set_target(4000.0)
                self.writer.step_once()
                self.clock.t += 1
                self.writer.set_target(bad)
                self.writer.step_once()
                self.assertEqual(self.fake.writes[-2:], [(PCT, 0), (ENA, 1)])
                self.clock.t += 1

    def test_a_dead_inverter_is_reported_and_the_cap_re_asserted_when_it_returns(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.fake.stop()                                # closes the connection too
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        s = self.last()
        self.assertEqual(s["mode"], "unreachable")
        self.assertIsNotNone(s["error"])
        # it comes back on the same port with its cap gone (as after a reset)
        fake2 = FakeInverter()
        fake2.start(port=self.port)
        self.addCleanup(fake2.stop)
        self.clock.t += 2                               # past the backoff
        self.writer.step_once()
        self.assertEqual(fake2.writes, [(PCT, 3233), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "capped")

    def test_a_failed_commit_is_re_asserted_after_reconnecting(self):
        self.fake.refuse = {ENA}                        # the value is written, the commit refused
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.last()["mode"], "unreachable")
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (3233, 0))
        self.fake.refuse = set()
        self.clock.t += 2
        self.writer.step_once()
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (3233, 1))
        self.assertEqual(self.last()["mode"], "capped")

    def test_an_inverter_found_in_standby_is_resumed_when_data_arrives(self):
        self.fake.regs[CONN] = 0
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(CONN, 1), (PCT, 3233), (ENA, 1)])
        self.assertEqual(self.last()["mode"], "capped")

    def test_a_cap_is_forced_every_step_while_the_inverter_is_starting(self):
        self.fake.regs[CONN] = 0
        self.fake.put(ST, [3])                          # STARTING, as for about 24 s after Conn = 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.last()["mode"], "starting")
        del self.fake.writes[:]
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [(PCT, 3233), (ENA, 1)])   # forced, and no second resume
        self.assertEqual(self.last()["mode"], "starting")
        self.fake.put(ST, [4])                          # the inverter is running
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.last()["mode"], "capped")
        del self.fake.writes[:]
        self.clock.t += 1
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual(self.fake.writes, [])          # back to the dead-band rule

    def test_an_outage_ends_in_standby_and_data_resumes(self):
        self.writer.step_once()                         # no data: zero cap
        self.clock.t += config.STANDBY_AFTER_S
        self.writer.step_once()
        self.assertEqual(self.fake.regs[CONN], 0)
        self.assertEqual(self.last()["mode"], "standby")
        self.clock.t += 5
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.assertEqual((self.fake.regs[CONN], self.fake.regs[PCT]), (1, 3233))

    def test_stop_writes_a_zero_cap(self):
        self.writer.set_target(4000.0)
        self.writer.step_once()
        self.writer.stop()
        self.assertEqual(self.fake.writes[-2:], [(PCT, 0), (ENA, 1)])

    def test_a_stalled_inverter_does_not_block_the_caller(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        status = []
        writer = InverterWriter("127.0.0.1", listener.getsockname()[1], timeout_s=1, publish=status.append)
        thread = threading.Thread(target=writer.step_once)   # accepted by the backlog, never answered
        thread.start()
        time.sleep(0.2)
        started = time.monotonic()
        writer.set_target(1000.0)
        self.assertLess(time.monotonic() - started, 0.1)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(json.loads(status[-1])["mode"], "unreachable")

    def test_the_thread_applies_targets_and_stops_with_a_zero_cap(self):
        writer = InverterWriter("127.0.0.1", self.port, timeout_s=1, publish=self.status.append, period_s=0.05)
        writer.set_target(4000.0)
        writer.start()
        deadline = time.monotonic() + 3
        while self.fake.regs[PCT] != 3233 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (3233, 1))
        writer.stop()
        self.assertEqual((self.fake.regs[PCT], self.fake.regs[ENA]), (0, 1))


if __name__ == "__main__":
    unittest.main()
