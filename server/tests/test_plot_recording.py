import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import plot_recording as pr                                    # noqa: E402  (the tool lives in tools/)
from evsim_recorder.storage import Store                       # noqa: E402

S = 1_000_000_000


class CrossingTest(unittest.TestCase):
    def rows(self, values, start=0.0, step=1.0):
        return [(int((start + i * step) * S), v) for i, v in enumerate(values)]

    def test_a_rising_step_is_found_when_it_has_gone_half_way(self):
        rows = self.rows([100, 100, 100, 400, 700, 700, 700])
        self.assertEqual(pr.crossing_time(rows, 0, 100, 700), 4 * S)       # 400 is exactly half: 300 of 600 is reached at 400? no: needs >= 300 above base
        self.assertEqual(pr.crossing_time(self.rows([100, 100, 500, 700, 700]), 0, 100, 700), 2 * S)

    def test_a_falling_step_is_found_too(self):
        rows = self.rows([700, 700, 300, 100, 100])
        self.assertEqual(pr.crossing_time(rows, 0, 700, 100), 2 * S)

    def test_one_noisy_sample_does_not_count(self):
        rows = self.rows([100, 100, 600, 100, 100, 100, 700, 700])
        self.assertEqual(pr.crossing_time(rows, 0, 100, 700), 6 * S)

    def test_rows_before_the_start_are_ignored(self):
        rows = self.rows([700, 700, 100, 100, 700, 700])
        self.assertEqual(pr.crossing_time(rows, 3 * S, 100, 700), 4 * S)

    def test_a_step_that_never_happens_gives_none(self):
        self.assertIsNone(pr.crossing_time(self.rows([100, 120, 90, 110]), 0, 100, 700))
        self.assertIsNone(pr.crossing_time(self.rows([100, 100]), 0, 100, 100))
        self.assertIsNone(pr.crossing_time([], 0, 100, 700))

    def test_a_crossing_in_the_very_last_row_counts(self):
        self.assertEqual(pr.crossing_time(self.rows([100, 100, 700]), 0, 100, 700), 2 * S)

    def test_the_median_of_a_window(self):
        rows = self.rows([1, 2, 3, 100, 5])
        self.assertEqual(pr.level(rows, 0, 5 * S), 3)
        self.assertEqual(pr.level(rows, 1 * S, 3 * S), 2.5)
        self.assertIsNone(pr.level(rows, 10 * S, 20 * S))


class DipTest(unittest.TestCase):
    def test_the_window_stops_at_the_first_reading_far_from_the_first_half(self):
        rows = [(i * S, v) for i, v in enumerate([100, 100, 100, 100, 100, 100, 100, 700, 100, 100])]
        self.assertEqual(pr.dip_start(rows, 0, 10 * S), 7 * S)

    def test_a_steady_window_is_not_cut(self):
        rows = [(i * S, 100 + i % 3) for i in range(10)]
        self.assertEqual(pr.dip_start(rows, 0, 10 * S), 10 * S)
        self.assertEqual(pr.dip_start([], 0, 10 * S), 10 * S)


class SyntheticRecording(unittest.TestCase):
    """Two writes 20 s apart. The first changes everything with known delays: the board sees it 0.2 s after the write, the
    server publishes it at 1.0 s, the inverter's reading changes at 2.1 s. The second changes the board and the server but not
    the inverter (as when it is saturated), so it must be skipped."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "r.sqlite")
        store = Store(self.path, batch_seconds=0.05)
        store.start_session("b", "c", {"steps": [7, 8], "dwell_s": 20}, ["run"])
        store.start()
        t0 = 100 * S

        def message(t_ns, topic, payload):
            store.add_message(t_ns, t_ns, topic, False, json.dumps(payload).encode())

        for i in range(0, 251):                                            # 90 s to 140 s, every 0.2 s
            t = 90 * S + i * S // 5
            level = 4000 if t < t0 + S // 5 else 4700 if t < t0 + 20 * S + S // 5 else 5400
            message(t, "evsim/cp", {"t": i * 200, "setpoint_w": level})
        for second in range(90, 141):
            t = second * S
            level = 4000 if second < 101 else 4700 if second < 121 else 5400
            message(t, "evsim/setpoint", {"setpoint_w": level, "ts_ms": second * 1000})
            read_ms = second * 1000 + 100
            dc = 4000 if read_ms < 102_100 else 4700
            message(read_ms * 1_000_000 + 2_000_000, "evsim/inverter",
                    {"dc_w": dc, "dc_a": dc / 500, "ac_w": dc - 100, "read_ts_ms": read_ms, "cap_ts_ms": None})
        for second in range(90, 141):
            store.add_charger(t_start_ns=second * S, t_end_ns=second * S + 700_000_000, power_w=dc_for(second) + 300)
        store.add_limit_write(1, 0, t0, t0 + 30_000_000, 6, 7, 7, "6 -> 7 A")
        store.add_limit_write(1, 1, t0 + 20 * S, t0 + 20 * S + 30_000_000, 7, 8, 8, "7 -> 8 A")
        store.stop()

    def test_the_hops_of_the_good_step_are_the_known_delays(self):
        result = pr.analyse(self.path)
        self.assertEqual(len(result["hops"]), 1)
        hop = result["hops"][0]
        self.assertEqual((hop["from_a"], hop["to_a"], hop["kind"]), (6, 7, "small"))
        self.assertAlmostEqual(hop["hop1"], 0.2, places=3)                 # write to the board's first changed sample
        self.assertAlmostEqual(hop["hop2"], 0.8, places=3)                 # board to the server's published setpoint
        self.assertAlmostEqual(hop["hop3"], 1.1, places=3)                 # server to the inverter's changed reading
        self.assertAlmostEqual(hop["total"], 2.1, places=3)

    def test_a_step_the_inverter_did_not_follow_is_skipped_with_a_reason(self):
        result = pr.analyse(self.path)
        self.assertEqual([step for step, reason in result["skipped"]], [1])
        self.assertIn("inverter", result["skipped"][0][1])

    def test_the_inverter_time_is_the_read_time_not_the_arrival_time(self):
        hop = pr.analyse(self.path)["hops"][0]
        self.assertAlmostEqual(hop["total"], 2.1, places=3)                # arrival is 2 ms later: that is not counted

    def test_a_large_jump_is_told_apart_from_a_small_step(self):
        self.assertEqual(pr.kind_of(6, 7), "small")
        self.assertEqual(pr.kind_of(7, 6), "small")
        self.assertEqual(pr.kind_of(15, 6), "large")
        self.assertEqual(pr.kind_of(6, 16), "large")

    def test_the_steady_state_powers_are_the_means_of_the_end_of_each_step(self):
        powers = pr.analyse(self.path)["powers"]
        first = powers[0]
        self.assertEqual(first["to_a"], 7)
        self.assertAlmostEqual(first["charger_w"], 5000, delta=1)
        self.assertAlmostEqual(first["dc_w"], 4700, delta=1)
        self.assertAlmostEqual(first["ac_w"], 4600, delta=1)
        self.assertAlmostEqual(first["setpoint_w"], 4700, delta=1)
        self.assertEqual(len(powers), 2)

    def test_a_recording_with_no_writes_gives_empty_results_not_an_error(self):
        path = os.path.join(self.dir, "empty.sqlite")
        store = Store(path)
        store.start_session("b", "c", {}, [])
        store.start()
        store.stop()
        result = pr.analyse(path)
        self.assertEqual((result["hops"], result["skipped"], result["powers"]), ([], [], []))

    def test_the_latest_recording_is_found_by_its_modification_time(self):
        older = os.path.join(self.dir, "evsim-old.sqlite")
        shutil.copy(self.path, older)
        os.utime(older, (1, 1))
        self.assertEqual(os.path.basename(pr.latest_db(self.dir)), "r.sqlite")


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib is not installed")
class PlotTest(SyntheticRecording):
    def test_both_figures_are_written(self):
        result = pr.analyse(self.path)
        for function, name in ((pr.plot_waterfall, "w.png"), (pr.plot_power, "p.png")):
            out = os.path.join(self.dir, name)
            function(result, out)
            self.assertGreater(os.path.getsize(out), 5000)


def dc_for(second):
    return 4000 if second < 102 else 4700


if __name__ == "__main__":
    unittest.main()
