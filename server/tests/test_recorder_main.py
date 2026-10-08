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
from fake_mqtt import FakeMqttClient
from charger_web import config
from evsim_recorder import __main__ as recorder_main


class MainCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAmtron()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "m.sqlite")
        self.client = FakeMqttClient()
        for patcher in (
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

    def test_a_refused_write_is_exit_code_1_and_the_restore_is_attempted(self):
        self.fake.refuse_writes = True
        code, out, err = self.run_main("--steps", "11,12", "--dwell", "0.1", "--baseline", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [11, 10])                  # the failed step, then the attempted restore


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
                     ("--charger-period", "0.5"), ("--charger-period", "11")):
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
