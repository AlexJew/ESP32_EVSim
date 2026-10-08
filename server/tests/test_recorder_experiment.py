import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from unittest import mock

from fake_amtron import FakeAmtron
from charger_web.service import ChargerError, ChargerService
from evsim_recorder import experiment as experiment_module
from evsim_recorder.charger_log import ChargerLogger, sharing_warning
from evsim_recorder.experiment import Experiment, PlanError, describe, parse_steps, validate_steps
from evsim_recorder.storage import Store


class ParseStepsTest(unittest.TestCase):
    def test_a_comma_separated_list_of_whole_numbers(self):
        self.assertEqual(parse_steps("6,7, 12 ,16,0"), [6, 7, 12, 16, 0])

    def test_anything_else_is_refused(self):
        for bad in ("", " ", ",", "6,,7", "6;7", "six", "6.5", "-6", "+6", "6,x", "1e1", "6 7", "0x10"):
            with self.assertRaises(PlanError, msg=repr(bad)):
                parse_steps(bad)


class ValidateStepsTest(unittest.TestCase):
    def test_zero_and_six_to_sixteen_are_allowed(self):
        validate_steps([0, 6, 7, 16], 16)

    def test_everything_else_is_refused(self):
        for bad in (1, 5, 17, 20, 100):
            with self.assertRaises(PlanError, msg=bad):
                validate_steps([10, bad], 16)

    def test_the_installation_limit_is_a_ceiling(self):
        validate_steps([13], 13)
        with self.assertRaises(PlanError):
            validate_steps([14], 13)

    def test_an_unknown_installation_limit_means_16(self):
        validate_steps([16], None)
        with self.assertRaises(PlanError):
            validate_steps([17], None)

    def test_an_empty_plan_is_refused(self):
        with self.assertRaises(PlanError):
            validate_steps([], 16)


class DescribeTest(unittest.TestCase):
    def test_the_plan_text_names_the_steps_the_time_the_charger_and_the_current_limit(self):
        text = describe([10, 11, 10], 45, 2, 10, 14, "192.168.21.117")
        for needle in ("10", "11", "45", "192.168.21.117", "14 A", "restore"):
            self.assertIn(needle, text)
        self.assertIn("6 writes", text)                       # 3 steps x 2 runs
        self.assertIn("280 s", text)                          # 10 baseline + 6 x 45
        self.assertNotIn("pause", text)

    def test_a_zero_step_is_flagged(self):
        self.assertIn("pause", describe([10, 0, 10], 30, 1, 0, 10, "h"))

    def test_leaving_the_last_step_is_said(self):
        self.assertIn("stay", describe([10, 11], 30, 1, 0, 10, "h", leave=True))


class RigCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "x.sqlite")
        self.store = Store(self.path, batch_seconds=0.05)
        self.store.start_session("b", "c", {}, [])
        self.store.start()
        self.addCleanup(self.store.stop)
        patcher = mock.patch.object(experiment_module, "RESTORE_PAUSE_S", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = ChargerService("127.0.0.1", self.port, on_poll=ChargerLogger(self.store), period_s=0.05)
        self.addCleanup(self.service.stop)
        self.service.poll_once()

    def query(self, sql):
        self.assertTrue(self.store.flush())
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def experiment(self, steps, dwell=0.05, **kwargs):
        return Experiment(self.service, self.store, steps, dwell, **kwargs)


class ChargerLoggerTest(RigCase):
    def test_each_poll_is_a_row_with_the_raw_phase_values(self):
        row = self.query("SELECT t_start_ns, t_end_ns, set_limit_a, operator_limit_a, installation_limit_a, "
                         "signalled_current_a, i_l1_a, i_l2_a, i_l3_a, v_l1_v, v_l2_v, v_l3_v, power_w, error FROM charger")[0]
        self.assertLess(row[0], row[1])
        self.assertEqual(row[2:], (10, 16, 16, 10, 0.874, 0.651, 0.808, 231, 232, 233, 600, None))

    def test_a_failing_charger_is_one_error_event_not_one_per_poll(self):
        self.fake.stop()
        for _ in range(3):
            self.service.poll_once()
        events = self.query("SELECT kind, detail FROM recorder_events ORDER BY id")
        self.assertEqual([e[0] for e in events], ["charger_error"])
        self.assertTrue(events[0][1])
        self.assertEqual(self.query("SELECT COUNT(*) FROM charger WHERE error IS NOT NULL"), [(3,)])

    def test_the_end_of_a_run_of_failures_is_one_recovery_event(self):
        logger = ChargerLogger(self.store)
        ok = {"t_start_ns": 1, "t_end_ns": 2, "values": {}, "error": None}
        bad = {"t_start_ns": 3, "t_end_ns": 4, "values": {}, "error": "timeout"}
        for poll in (ok, bad, bad, bad, ok, ok, bad, ok):
            logger(poll)
        events = [e[0] for e in self.query("SELECT kind FROM recorder_events ORDER BY id")]
        self.assertEqual(events, ["charger_error", "charger_ok", "charger_error", "charger_ok"])


class SharingWarningTest(unittest.TestCase):
    """Seen live on 2026-10-08: with a second Modbus client (the web page) polling the AMTRON, about a third of the
    recorder's polls failed; with the page stopped, none did."""

    def test_a_clean_run_has_no_warning(self):
        self.assertIsNone(sharing_warning(150, 0))
        self.assertIsNone(sharing_warning(150, 7))                  # 4.7 %: noise

    def test_a_run_with_many_failed_polls_says_what_probably_happened_and_what_to_do(self):
        text = sharing_warning(99, 32)
        for needle in ("32 of 99", "32 %", "web page", "stop", "MQTT"):
            self.assertIn(needle, text)

    def test_too_few_polls_to_judge_gives_no_warning(self):
        self.assertIsNone(sharing_warning(5, 5))
        self.assertIsNone(sharing_warning(0, 0))


class ChargerLoggerCountTest(RigCase):
    def test_the_logger_counts_polls_and_failures(self):
        logger = ChargerLogger(self.store)
        ok = {"t_start_ns": 1, "t_end_ns": 2, "values": {}, "error": None}
        bad = {"t_start_ns": 3, "t_end_ns": 4, "values": {}, "error": "reset"}
        for poll in (ok, bad, ok, bad, bad):
            logger(poll)
        self.assertEqual((logger.polls, logger.failed), (5, 3))


class ExperimentTest(RigCase):
    def writes(self):
        return [value for address, value in self.fake.writes]

    def test_the_steps_are_written_in_order_each_logged_with_its_times_and_readback(self):
        self.assertTrue(self.experiment([11, 12, 11]).run())
        rows = self.query("SELECT run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error FROM limit_writes "
                          "WHERE step >= 0 ORDER BY id")
        self.assertEqual([(r[0], r[1], r[4], r[5], r[6], r[7], r[8]) for r in rows],
                         [(1, 0, 10, 11, 11, "10 -> 11 A", None), (1, 1, 11, 12, 12, "11 -> 12 A", None),
                          (1, 2, 12, 11, 11, "12 -> 11 A", None)])
        for row in rows:
            self.assertLess(row[2], row[3])
            self.assertGreater(row[2], 1_700_000_000_000_000_000)

    def test_only_register_1000_is_ever_written(self):
        self.experiment([11, 0, 16]).run()
        self.assertTrue(self.fake.writes)
        self.assertEqual({address for address, value in self.fake.writes}, {1000})

    def test_the_limit_found_at_the_start_is_written_back_at_the_end(self):
        self.experiment([12, 13]).run()
        self.assertEqual(self.writes(), [12, 13, 10])
        self.assertEqual(self.fake.regs[1000], 10)
        restore = self.query("SELECT run, step, from_a, to_a, label FROM limit_writes ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(restore, (0, -1, 13, 10, "restore"))

    def test_with_leave_the_last_step_stays(self):
        self.experiment([12, 13], leave=True).run()
        self.assertEqual(self.writes(), [12, 13])
        self.assertEqual(self.fake.regs[1000], 13)

    def test_a_run_that_already_ends_on_the_original_limit_writes_no_restore(self):
        self.experiment([12, 10]).run()
        self.assertEqual(self.writes(), [12, 10])

    def test_repeat_runs_the_list_again_with_the_run_number(self):
        self.experiment([11, 12], repeat=2).run()
        self.assertEqual(self.writes(), [11, 12, 11, 12, 10])
        rows = self.query("SELECT run, step FROM limit_writes WHERE step >= 0 ORDER BY id")
        self.assertEqual(rows, [(1, 0), (1, 1), (2, 0), (2, 1)])

    def test_each_step_is_held_for_the_dwell(self):
        started = time.monotonic()
        self.experiment([11, 12], dwell=0.3).run()
        self.assertGreaterEqual(time.monotonic() - started, 0.6)

    def test_the_baseline_passes_before_the_first_write(self):
        started = time.monotonic()
        experiment = self.experiment([11], dwell=0.05, baseline_s=0.4)
        first = []
        original = self.service.set_limit

        def spy(value):
            first.append(time.monotonic() - started)
            return original(value)

        self.service.set_limit = spy
        experiment.run()
        self.assertGreaterEqual(first[0], 0.4)

    def test_a_step_of_zero_is_allowed_and_written(self):
        self.experiment([0, 12]).run()
        self.assertEqual(self.writes()[:2], [0, 12])

    def test_a_refused_write_is_logged_with_its_error_stops_the_run_and_still_tries_the_restore(self):
        self.fake.refuse_writes = True
        self.assertFalse(self.experiment([11, 12]).run())
        rows = self.query("SELECT step, error FROM limit_writes ORDER BY id")
        self.assertEqual(rows[0][0], 0)
        self.assertTrue(rows[0][1])
        self.assertEqual(len([r for r in rows if r[0] >= 0]), 1)               # the second step was never tried
        self.assertEqual(rows[-1][0], -1)                                       # the restore was attempted (and refused too)
        self.assertTrue(rows[-1][1])

    def test_an_unconfirmed_write_is_an_error_too_and_the_original_is_written_again_to_be_safe(self):
        self.fake.ignore_writes = True
        self.assertFalse(self.experiment([11, 12]).run())
        self.assertTrue(self.query("SELECT error FROM limit_writes ORDER BY id")[0][0])
        self.assertEqual(self.writes(), [11, 10])

    def test_a_failed_write_is_restored_even_with_leave(self):
        self.fake.refuse_writes = True
        self.assertFalse(self.experiment([11, 12], leave=True).run())
        self.assertEqual(self.writes()[0], 11)
        self.assertEqual(set(self.writes()[1:]), {10})                          # the restore, tried again and again

    def test_a_stop_during_a_dwell_ends_the_run_and_restores_the_original_limit(self):
        experiment = self.experiment([12, 13, 14], dwell=5)
        threading.Timer(0.3, experiment.request_stop).start()
        started = time.monotonic()
        self.assertFalse(experiment.run())
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.writes(), [12, 10])
        self.assertEqual(self.fake.regs[1000], 10)

    def test_a_stop_during_the_baseline_writes_nothing_at_all(self):
        experiment = self.experiment([12], baseline_s=5)
        threading.Timer(0.2, experiment.request_stop).start()
        self.assertFalse(experiment.run())
        self.assertEqual(self.writes(), [])

    def test_an_exception_in_the_middle_still_restores_the_limit(self):
        experiment = self.experiment([12, 13], dwell=0.05)
        original = self.service.set_limit
        calls = []

        def explode(value):
            calls.append(value)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return original(value)

        self.service.set_limit = explode
        with self.assertRaises(KeyboardInterrupt):
            experiment.run()
        self.assertEqual(calls, [12, 13, 10])                                   # the restore was still attempted
        self.assertEqual(self.fake.regs[1000], 10)

    def test_the_poll_keeps_running_through_the_run_so_the_charger_never_falls_back(self):
        self.service.start()
        self.experiment([11, 12], dwell=0.3).run()
        reads = [e for e in self.fake.log if e[0] == 3 and e[1] == 1000]
        self.assertGreater(len(reads), 5)
        self.assertFalse(self.fake.in_fallback)

    def test_an_unknown_original_limit_refuses_the_run_before_writing_anything(self):
        self.service._values["set_limit_a"] = None
        experiment = self.experiment([12])
        self.assertFalse(experiment.run())
        self.assertEqual(self.writes(), [])
        self.assertIn("refused", [k for (k,) in self.query("SELECT kind FROM recorder_events")])

    def test_an_unknown_original_limit_is_fine_with_leave_because_nothing_is_restored(self):
        self.service._values["set_limit_a"] = None
        self.assertTrue(self.experiment([12], leave=True).run())
        self.assertEqual(self.writes(), [12])

    def test_the_original_given_by_the_caller_is_restored_not_a_new_read_of_the_charger(self):
        self.experiment([12], original=7).run()
        self.assertEqual(self.writes(), [12, 7])

    def test_a_restore_that_never_works_is_retried_a_bounded_number_of_times_and_reported_as_failed(self):
        original = self.service.set_limit

        def one_good_write_then_refuse(value):
            result = original(value)
            self.fake.refuse_writes = True
            return result

        self.service.set_limit = one_good_write_then_refuse
        experiment = self.experiment([12])
        experiment.run()
        self.assertIs(experiment.restore_ok, False)
        errors = [e for (e,) in self.query("SELECT error FROM limit_writes WHERE step = -1")]
        self.assertEqual(len(errors), experiment_module.RESTORE_ATTEMPTS)
        self.assertTrue(all(errors))
        self.assertEqual(self.fake.regs[1000], 12)                               # the charger really was left there
        self.assertEqual((experiment.original, experiment.last_tried), (10, 12))

    def test_a_restore_that_works_on_the_third_try_is_reported_as_ok(self):
        original = self.service.set_limit
        calls = []

        def flaky(value):
            calls.append(value)
            if len(calls) in (2, 3):
                raise ChargerError("busy")
            return original(value)

        self.service.set_limit = flaky
        experiment = self.experiment([12])
        experiment.run()
        self.assertIs(experiment.restore_ok, True)
        self.assertEqual(calls, [12, 10, 10, 10])
        self.assertEqual(self.fake.regs[1000], 10)

    def test_nothing_to_restore_counts_as_ok(self):
        experiment = self.experiment([12, 10])
        experiment.run()
        self.assertIs(experiment.restore_ok, True)


if __name__ == "__main__":
    unittest.main()
