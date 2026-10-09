import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from evsim_recorder.storage import Store

CP = ('{"t":9912523,"freq":999.9,"duty":10.53,"adc":0,"cp":"PWM_VALID","state":"C","amps":6.32,"watts":4361,'
      '"setpoint_w":4361,"status":"live","age_ms":0}')
SETPOINT = '{"setpoint_w":4312,"samples":5,"window_s":1,"status":"ok","ts":"2026-10-08T10:00:03Z","ts_ms":1791453603000}'
INVERTER = ('{"mode":"capped","target_dc_w":4312.6,"cap_ac_w":4182.7,"dc_w":4304.6,"dc_a":8.2,"dc_v":524.7,"ac_w":4181,'
            '"ac_a":6.0,"ac_v":233.3,"inverter_state":4,"error":null,"ts":"2026-10-08T10:00:04Z",'
            '"read_ts_ms":1791453604120,"cap_ts_ms":1791453601500}')


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "t.sqlite")
        self.store = Store(self.path, batch_seconds=0.05, retry_seconds=0.02)
        self.store.start_session(broker="127.0.0.1:1883", charger="127.0.0.1", plan={"steps": [6, 7]}, argv=["run", "--steps", "6,7"])
        self.store.start()
        self.addCleanup(self.store.stop)

    def query(self, sql, *args):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def message(self, topic, payload, retained=False, t_ns=1_000, mono_ns=2_000):
        self.store.add_message(t_ns, mono_ns, topic, retained, payload.encode() if isinstance(payload, str) else payload)


class SessionAndMessageTest(StoreCase):
    def test_a_session_row_records_how_the_run_was_started(self):
        row = self.query("SELECT broker, charger, plan, argv, ended_ns FROM sessions")[0]
        self.assertEqual(row[:2], ("127.0.0.1:1883", "127.0.0.1"))
        self.assertEqual(json.loads(row[2]), {"steps": [6, 7]})
        self.assertEqual(json.loads(row[3]), ["run", "--steps", "6,7"])
        self.assertIsNone(row[4])
        self.assertEqual(self.store.session_id, 1)

    def test_a_message_is_stored_with_its_times_topic_flag_and_payload(self):
        self.message("evsim/cp", CP, retained=False, t_ns=1_791_453_600_123_456_789, mono_ns=77)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT session_id, t_ns, mono_ns, topic, retained, payload, payload_raw FROM messages")[0]
        self.assertEqual(row, (1, 1_791_453_600_123_456_789, 77, "evsim/cp", 0, CP, None))

    def test_a_retained_flag_is_kept(self):
        self.message("evsim/setpoint", SETPOINT, retained=True)
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT retained FROM messages"), [(1,)])

    def test_bytes_that_are_not_utf8_are_kept_verbatim_next_to_a_readable_text(self):
        self.message("evsim/reply", b"state A \xff\xfe B")
        self.assertTrue(self.store.flush())
        text, raw = self.query("SELECT payload, payload_raw FROM messages")[0]
        self.assertEqual(raw, b"state A \xff\xfe B")
        self.assertIn("state A", text)

    def test_valid_utf8_has_no_raw_copy(self):
        self.message("evsim/state", "C")
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT payload, payload_raw FROM messages"), [("C", None)])

    def test_an_empty_payload_is_stored(self):
        self.message("evsim/reply", b"")
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT payload FROM messages"), [("",)])


class ViewTest(StoreCase):
    def test_the_cp_view_has_the_boards_fields(self):
        self.message("evsim/cp", CP)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT session_id, t_ns, mono_ns, board_ms, freq, duty, adc, cp, state, amps, watts, setpoint_w, "
                         "status, age_ms FROM cp")[0]
        self.assertEqual(row, (1, 1_000, 2_000, 9912523, 999.9, 10.53, 0, "PWM_VALID", "C", 6.32, 4361, 4361, "live", 0))

    def test_the_setpoint_view_has_the_servers_tick_time(self):
        self.message("evsim/setpoint", SETPOINT)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT setpoint_w, samples, window_s, status, server_ts, tick_ms FROM setpoint")[0]
        self.assertEqual(row, (4312, 5, 1, "ok", "2026-10-08T10:00:03Z", 1791453603000))

    def test_the_inverter_view_has_the_measurements_and_the_new_timestamps(self):
        self.message("evsim/inverter", INVERTER)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT mode, target_dc_w, cap_ac_w, dc_w, dc_a, dc_v, ac_w, ac_a, ac_v, inverter_state, error, "
                         "server_ts, read_ts_ms, cap_ts_ms FROM inverter")[0]
        self.assertEqual(row, ("capped", 4312.6, 4182.7, 4304.6, 8.2, 524.7, 4181, 6.0, 233.3, 4, None,
                               "2026-10-08T10:00:04Z", 1791453604120, 1791453601500))

    def test_an_old_message_without_the_new_fields_gives_null_not_an_error(self):
        self.message("evsim/inverter", '{"mode":"capped","dc_w":4304.6}')
        self.assertTrue(self.store.flush())
        row = self.query("SELECT dc_w, dc_a, read_ts_ms, cap_ts_ms FROM inverter")[0]
        self.assertEqual(row, (4304.6, None, None, None))

    def test_hostile_payloads_give_nulls_and_never_break_a_view(self):
        for payload in ("not json", "[1, 2]", "4000", "null", '"text"', "{", "", b"\xff\xfe"):
            self.message("evsim/cp", payload)
            self.message("evsim/setpoint", payload)
            self.message("evsim/inverter", payload)
        self.assertTrue(self.store.flush())
        for view in ("cp", "setpoint", "inverter"):
            rows = self.query("SELECT * FROM %s" % view)
            self.assertEqual(len(rows), 8, view)
        self.assertEqual(self.query("SELECT COUNT(*) FROM cp WHERE setpoint_w IS NOT NULL"), [(0,)])

    def test_a_field_of_an_unexpected_type_is_returned_as_it_was_sent_not_coerced(self):
        self.message("evsim/cp", '{"setpoint_w": "x", "state": 7}')
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT setpoint_w, state FROM cp"), [("x", 7)])

    def test_retained_messages_are_in_messages_but_not_in_the_views(self):
        self.message("evsim/setpoint", SETPOINT, retained=True)
        self.message("evsim/setpoint", SETPOINT, retained=False)
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM setpoint"), [(1,)])

    def test_a_topic_without_a_view_is_still_recorded(self):
        self.message("evsim/electric_car", '{"anything": 1}')
        self.message("evsim/state", "C")
        self.assertTrue(self.store.flush())
        self.assertEqual(sorted(t for (t,) in self.query("SELECT topic FROM messages")), ["evsim/electric_car", "evsim/state"])
        for view in ("cp", "setpoint", "inverter"):
            self.assertEqual(self.query("SELECT COUNT(*) FROM %s" % view), [(0,)])


class OtherTablesTest(StoreCase):
    def test_a_charger_row_has_its_times_and_raw_values(self):
        self.store.add_charger(t_start_ns=10, t_end_ns=25, set_limit_a=14, operator_limit_a=11, installation_limit_a=16,
                               signalled_current_a=11, vehicle_state=3, charger_status=6, i_l1_a=11.8, i_l2_a=11.9,
                               i_l3_a=12.0, v_l1_v=230, v_l2_v=231, v_l3_v=232, power_w=8250)
        self.assertTrue(self.store.flush())
        row = self.query("SELECT session_id, t_start_ns, t_end_ns, set_limit_a, operator_limit_a, installation_limit_a, "
                         "signalled_current_a, vehicle_state, charger_status, i_l1_a, i_l2_a, i_l3_a, v_l1_v, v_l2_v, v_l3_v, "
                         "power_w, error FROM charger")[0]
        self.assertEqual(row, (1, 10, 25, 14, 11, 16, 11, 3, 6, 11.8, 11.9, 12.0, 230, 231, 232, 8250, None))

    def test_a_failed_poll_row_has_the_error_and_null_values(self):
        self.store.add_charger(t_start_ns=10, t_end_ns=25, error="timeout")
        self.assertTrue(self.store.flush())
        self.assertEqual(self.query("SELECT error, power_w, i_l1_a FROM charger"), [("timeout", None, None)])

    def test_a_limit_write_row(self):
        self.store.add_limit_write(run=1, step=0, t_write_ns=100, t_ack_ns=140, from_a=10, to_a=11, readback_a=11, label="10 -> 11 A")
        self.store.add_limit_write(run=1, step=1, t_write_ns=200, t_ack_ns=None, from_a=11, to_a=12, readback_a=None,
                                   label="11 -> 12 A", error="refused")
        self.assertTrue(self.store.flush())
        rows = self.query("SELECT run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error FROM limit_writes ORDER BY id")
        self.assertEqual(rows, [(1, 0, 100, 140, 10, 11, 11, "10 -> 11 A", None), (1, 1, 200, None, 11, 12, None, "11 -> 12 A", "refused")])

    def test_an_event_has_a_time_a_kind_and_a_detail(self):
        self.store.add_event("connected", "broker 127.0.0.1", t_ns=555)
        self.store.add_event("charger_error", "timeout")
        self.assertTrue(self.store.flush())
        rows = self.query("SELECT t_ns, kind, detail FROM recorder_events ORDER BY id")
        self.assertEqual(rows[0], (555, "connected", "broker 127.0.0.1"))
        self.assertEqual((rows[1][1], rows[1][2]), ("charger_error", "timeout"))
        self.assertGreater(rows[1][0], 1_700_000_000_000_000_000)           # defaulted to the wall clock


class WriterTest(StoreCase):
    def test_rows_are_committed_by_time_without_a_flush(self):
        self.message("evsim/cp", CP)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.query("SELECT 1 FROM messages"):
            time.sleep(0.02)
        self.assertEqual(len(self.query("SELECT 1 FROM messages")), 1)

    def test_a_large_burst_is_committed_in_batches_and_in_order(self):
        for index in range(1000):
            self.message("evsim/cp", '{"t": %d}' % index, t_ns=index)
        self.assertTrue(self.store.flush(timeout=10))
        rows = self.query("SELECT t_ns FROM messages ORDER BY id")
        self.assertEqual([r[0] for r in rows], list(range(1000)))

    def test_stop_flushes_everything_and_marks_the_session_ended(self):
        for index in range(50):
            self.message("evsim/cp", CP, t_ns=index)
        self.store.stop()
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(50,)])
        self.assertIsNotNone(self.query("SELECT ended_ns FROM sessions")[0][0])
        self.store.stop()                                                   # idempotent

    def test_adding_never_blocks_while_the_writer_is_stuck(self):
        gate = threading.Event()
        real = self.store._transaction

        def stuck(rows):
            gate.wait(5)
            return real(rows)

        with mock.patch.object(self.store, "_transaction", side_effect=stuck):
            self.message("evsim/cp", CP)                                    # the writer takes it and sticks
            time.sleep(0.2)
            started = time.monotonic()
            for index in range(2000):
                self.message("evsim/cp", CP, t_ns=index)
            self.assertLess(time.monotonic() - started, 1.0)
            gate.set()
        self.assertTrue(self.store.flush(timeout=10))
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2001,)])

    def test_a_full_queue_drops_the_oldest_rows_and_says_how_many(self):
        small = Store(os.path.join(self.dir, "small.sqlite"), queue_max=50, batch_seconds=0.05, retry_seconds=0.02)
        small.start_session("b", "c", {}, [])
        self.addCleanup(small.stop)
        gate = threading.Event()
        real = small._transaction

        def stuck(rows):
            gate.wait(5)
            return real(rows)

        with mock.patch.object(small, "_transaction", side_effect=stuck):
            small.start()
            small.add_message(0, 0, "evsim/cp", False, b"first")            # taken by the writer, which then sticks
            time.sleep(0.2)
            for index in range(1, 121):
                small.add_message(index, index, "evsim/cp", False, b"x")
            gate.set()
        self.assertTrue(small.flush(timeout=10))
        conn = sqlite3.connect(os.path.join(self.dir, "small.sqlite"))
        kept = [r[0] for r in conn.execute("SELECT t_ns FROM messages ORDER BY t_ns")]
        dropped = conn.execute("SELECT detail FROM recorder_events WHERE kind='dropped'").fetchall()
        conn.close()
        self.assertEqual(len(kept), 51)                                     # the first one plus the newest 50
        self.assertEqual(kept[-1], 120)
        self.assertEqual(kept[1], 71)                                       # 1 to 70 were dropped
        self.assertEqual(len(dropped), 1)
        self.assertIn("70", dropped[0][0])

    def test_a_failing_write_is_retried_and_recorded_and_nothing_is_lost(self):
        real = self.store._transaction
        attempts = []

        def flaky(rows):
            attempts.append(1)
            if len(attempts) <= 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(rows)

        with mock.patch.object(self.store, "_transaction", side_effect=flaky):
            self.message("evsim/cp", CP)
            self.assertTrue(self.store.flush(timeout=10))
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(1,)])
        kinds = [k for (k,) in self.query("SELECT kind FROM recorder_events")]
        self.assertIn("write_error", kinds)

    def test_a_second_connection_can_read_while_the_recorder_writes(self):
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        for index in range(300):
            self.message("evsim/cp", CP, t_ns=index)
            if index % 50 == 0:
                reader.execute("SELECT COUNT(*) FROM messages").fetchone()
        self.assertTrue(self.store.flush(timeout=10))
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM messages").fetchone(), (300,))


class OpeningTest(unittest.TestCase):
    def test_a_file_that_cannot_be_created_raises_at_once(self):
        with self.assertRaises(sqlite3.OperationalError):
            Store("/nonexistent-folder-for-the-recorder-test/x.sqlite")

    def test_reopening_a_file_keeps_its_data_and_starts_a_new_session(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "again.sqlite")
        for _ in range(2):
            store = Store(path, batch_seconds=0.05)
            store.start_session("b", "c", {}, [])
            store.start()
            store.add_message(1, 1, "evsim/state", False, b"C")
            store.stop()
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone(), (2,))
        self.assertEqual(conn.execute("SELECT DISTINCT session_id FROM messages ORDER BY 1").fetchall(), [(1,), (2,)])


if __name__ == "__main__":
    unittest.main()
