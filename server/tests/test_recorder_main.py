import contextlib
import io
import os
import shutil
import signal
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from fake_amtron import FakeAmtron
import time

from fake_mqtt import FakeMqttClient
from charger_web import config
from charger_web.service import ChargerService
from evsim_recorder import __main__ as recorder_main
from evsim_recorder import experiment as experiment_module


class ConnectingClient(FakeMqttClient):
    """A client whose broker answers as soon as the network loop starts."""

    def loop_start(self):
        super().loop_start()
        self.connect()


class MainCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "m.sqlite")
        self.client = ConnectingClient()
        for patcher in (
            mock.patch.object(recorder_main, "STABLE_POLLS", 2),
            mock.patch.object(recorder_main, "SETTLE_PAUSE_S", 0.05),
            mock.patch.object(recorder_main, "SETTLE_TIMEOUT_S", 3.0),
            mock.patch.object(recorder_main, "MQTT_WAIT_S", 1.0),
            mock.patch.object(experiment_module, "RESTORE_PAUSE_S", 0.01),
            mock.patch.object(config, "MODBUS_PORT", self.port),
            mock.patch.dict(os.environ, {"EVSIM_CHARGER_HOST": "127.0.0.1"}),
            mock.patch.object(recorder_main, "build_client", return_value=self.client),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *args, confirm=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = recorder_main.main(["run", "--db", self.path, "--charger-period", "1", *args],
                                          confirm=confirm or (lambda prompt: "y"))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def rows(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def writes(self):
        return [value for address, value in self.fake.writes]


class RunTest(MainCase):
    def test_a_run_steps_the_limit_records_it_and_restores_the_original(self):
        code, out, err = self.run_main("--steps", "11,12", "--dwell", "0.3", "--baseline", "0.2", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.writes(), [11, 12, 10])
        self.assertEqual(self.fake.regs[1000], 10)
        steps = self.rows("SELECT step, from_a, to_a FROM limit_writes ORDER BY id")
        self.assertEqual(steps, [(0, 10, 11), (1, 11, 12), (-1, 12, 10)])
        self.assertGreaterEqual(self.rows("SELECT COUNT(*) FROM charger")[0][0], 1)
        session = self.rows("SELECT broker, charger, plan, ended_ns FROM sessions")[0]
        self.assertIn("127.0.0.1", session[1])
        self.assertIn('"steps": [11, 12]', session[2])
        self.assertIsNotNone(session[3])

    def test_a_clean_run_prints_no_sharing_warning(self):
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertNotIn("WARNING", err)

    def test_a_run_whose_charger_polls_mostly_failed_ends_with_the_warning_on_stderr(self):
        from evsim_recorder.charger_log import ChargerLogger as Real

        class Failing(Real):
            def __call__(self, poll):
                super().__call__(poll)
                self.polls += 40                                   # as if many polls had been made and failed
                self.failed += 20

        with mock.patch.object(recorder_main, "ChargerLogger", Failing):
            code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("WARNING", err)
        self.assertIn("stop", err)

    def test_the_mqtt_client_is_connected_for_the_run_and_closed_after_it_in_order(self):
        self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual([c[0] for c in self.client.calls], ["connect_async", "loop_start", "disconnect", "loop_stop"])
        self.assertEqual(self.client.published, [])

    def test_the_plan_is_printed_and_the_answer_is_asked(self):
        asked = []
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", confirm=lambda p: asked.append(p) or "y")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(asked), 1)
        self.assertIn("11", out)
        self.assertIn("127.0.0.1", out)
        self.assertIn("10 A", out)                                  # the limit found on the charger

    def test_declining_writes_nothing(self):
        for answer in ("n", "", "no", "maybe", "N"):
            code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", confirm=lambda p, a=answer: a)
            self.assertEqual(code, 0)
            self.assertEqual(self.writes(), [], answer)
            self.assertIn("Nothing was written", out)

    def test_yes_skips_the_question(self):
        def never(prompt):
            raise AssertionError("asked")
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes", confirm=never)
        self.assertEqual(code, 0, err)

    def test_leave_keeps_the_last_step(self):
        self.run_main("--steps", "11,12", "--dwell", "0.1", "--baseline", "0", "--yes", "--leave")
        self.assertEqual(self.writes(), [11, 12])

    def test_a_stop_signal_during_a_dwell_restores_the_original_limit(self):
        timer = threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGINT))
        timer.start()
        self.addCleanup(timer.cancel)
        code, out, err = self.run_main("--steps", "12,13", "--dwell", "30", "--baseline", "0", "--yes")
        self.assertEqual(self.writes(), [12, 10])
        self.assertEqual(self.fake.regs[1000], 10)
        self.assertEqual(code, 1)
        self.assertIsNotNone(self.rows("SELECT ended_ns FROM sessions")[0][0])

    def test_sigterm_does_the_same(self):
        timer = threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        self.addCleanup(timer.cancel)
        self.run_main("--steps", "12", "--dwell", "30", "--baseline", "0", "--yes")
        self.assertEqual(self.writes(), [12, 10])

    def test_the_signal_handlers_are_put_back_afterwards(self):
        before = signal.getsignal(signal.SIGINT)
        self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_a_charger_in_fallback_at_the_start_does_not_make_0_the_limit_to_restore(self):
        self.fake.fall_back()                                      # the first request reads 0, later ones the real limit
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.writes(), [11, 10])
        self.assertEqual(self.fake.regs[1000], 10)
        self.assertIn("10 A", out)

    def test_the_heartbeat_runs_while_the_confirmation_is_waited_for(self):
        counts = []

        def count():
            return len([e for e in self.fake.log if e[0] == 3 and e[1] == 1000])

        def slow(prompt):
            before = count()
            time.sleep(2.5)
            counts.append(count() - before)
            return "y"

        self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", confirm=slow)
        self.assertGreaterEqual(counts[0], 2)                      # polls kept the charger from falling back

    def test_an_unreadable_limit_refuses_the_run_and_writes_nothing(self):
        self.fake.refuse_reads = {1000}
        with mock.patch.object(recorder_main, "SETTLE_TIMEOUT_S", 0.5):
            code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])
        self.assertIn("limit", (out + err).lower())

    def test_a_refused_write_is_exit_code_3_because_the_restore_failed_too_and_it_says_so_loudly(self):
        self.fake.refuse_writes = True
        code, out, err = self.run_main("--steps", "11,12", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(self.writes()[0], 11)
        self.assertEqual(set(self.writes()[1:]), {10})
        self.assertIn("LEFT AT", err)
        self.assertIn("10 A", err)
        self.assertNotIn("restored", out)

    def test_a_restore_that_works_after_a_failed_step_says_restored_and_is_exit_code_1(self):
        self.fake.ignore_writes = True                             # an unconfirmed step: the restore then reads back fine
        code, out, err = self.run_main("--steps", "11,12", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [11, 10])
        self.assertNotIn("LEFT AT", err)

    def test_a_restore_that_fails_after_good_steps_is_exit_code_3(self):
        fake = self.fake

        class Wrapped(ChargerService):
            def set_limit(inner, value):
                result = super().set_limit(value)
                fake.refuse_writes = True                          # from now on the charger refuses everything
                return result

        with mock.patch.object(recorder_main, "ChargerService", Wrapped):
            code, out, err = self.run_main("--steps", "12", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(self.fake.regs[1000], 12)
        self.assertIn("LEFT AT", err)

    def test_sighup_does_the_same_as_ctrl_c(self):
        timer = threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGHUP))
        timer.start()
        self.addCleanup(timer.cancel)
        self.run_main("--steps", "12", "--dwell", "30", "--baseline", "0", "--yes")
        self.assertEqual(self.writes(), [12, 10])

    def test_a_broker_that_never_answers_stops_the_run_before_anything_is_written(self):
        silent = FakeMqttClient()                                  # loop_start never connects
        with mock.patch.object(recorder_main, "build_client", return_value=silent), \
                mock.patch.object(recorder_main, "MQTT_WAIT_S", 0.3):
            code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])
        self.assertIn("broker", (out + err).lower())


class BadPlanTest(MainCase):
    def test_a_bad_plan_writes_nothing_and_exits_with_a_message(self):
        for steps in ("5", "17", "6,7,99", "6.5", "", "x", "-6", "6,,7"):
            code, out, err = self.run_main("--steps", steps, "--dwell", "0.1", "--yes")
            self.assertNotEqual(code, 0, steps)
            self.assertEqual(self.writes(), [], steps)

    def test_a_step_above_the_installation_limit_is_refused_before_any_write(self):
        self.fake.regs[133] = 13
        code, out, err = self.run_main("--steps", "12,14", "--dwell", "0.1", "--yes")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("14", err + out)

    def test_bad_options_are_refused(self):
        for args in (("--dwell", "0"), ("--dwell", "-5"), ("--dwell", "x"), ("--repeat", "0"), ("--baseline", "-1"),
                     ("--charger-period", "0.5"), ("--charger-period", "11"),
                     ("--dwell", "nan"), ("--dwell", "inf"), ("--dwell", "1e9"), ("--dwell", "3601"),
                     ("--baseline", "nan"), ("--baseline", "inf"), ("--baseline", "3601"), ("--repeat", "100000"),
                     ("--charger-period", "nan"), ("--charger-period", "inf")):
            code, out, err = self.run_main("--steps", "11", "--yes", *args)
            self.assertEqual(code, 2, args)
            self.assertEqual(self.writes(), [], args)

    def test_steps_are_required(self):
        code, out, err = self.run_main("--yes")
        self.assertEqual(code, 2)

    def test_an_unreachable_charger_stops_before_writing_anything(self):
        self.fake.stop()
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])
        self.assertIn("charger", (out + err).lower())

    def test_a_database_that_cannot_be_created_exits_with_a_message(self):
        code, out, err = self.run_main("--steps", "11", "--yes", "--db", "/nonexistent-recorder-folder/x/y.sqlite")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])
        self.assertIn("database", (out + err).lower())

    def test_a_missing_database_folder_is_created(self):
        path = os.path.join(self.dir, "new", "folder", "r.sqlite")
        code, out, err = self.run_main("--steps", "11", "--dwell", "0.1", "--baseline", "0", "--yes", "--db", path)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(path))


class DefaultsTest(unittest.TestCase):
    def test_the_default_database_name_has_the_date_and_lives_in_data_recordings(self):
        path = recorder_main.default_db_path()
        self.assertTrue(path.endswith(".sqlite"))
        self.assertIn(os.path.join("data", "recordings", "evsim-"), path)


if __name__ == "__main__":
    unittest.main()
