import itertools
import time
import unittest

from charger_web.history import History


def rd(value, status="ok"):
    return {"value": value, "status": status, "updated_at": None}


class StubTelemetry:
    def __init__(self):
        self.samples = []
        self.setpoint = rd(4312.0)
        self.target = rd(4312.6)
        self.dc = rd(4304.6)

    def drain_cp_samples(self):
        samples, self.samples = self.samples, []
        return samples

    def snapshot(self):
        return {"setpoint": {"setpoint_w": self.setpoint}, "server": {"target_dc_w": self.target},
                "inverter": {"dc_w": self.dc}}


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.telemetry = StubTelemetry()
        self.wall = itertools.count(1_700_000_000)
        self.history = History(self.telemetry, wall=lambda: next(self.wall))

    def frames(self):
        return self.history.snapshot()["frames"]

    def test_a_frame_has_the_time_and_the_four_values(self):
        self.telemetry.samples = [4300.0, 4400.0]
        self.history.tick()
        self.assertEqual(self.frames(), [
            {"t": 1_700_000_000, "cp_w": 4350.0, "setpoint_w": 4312.0, "target_dc_w": 4312.6, "dc_w": 4304.6},
        ])

    def test_the_raw_value_is_the_mean_of_the_samples_since_the_last_tick(self):
        self.telemetry.samples = [100.0, 200.0, 300.0]
        self.history.tick()
        self.telemetry.samples = [1000.0]
        self.history.tick()
        self.assertEqual([f["cp_w"] for f in self.frames()], [200.0, 1000.0])

    def test_no_samples_is_a_gap(self):
        self.history.tick()
        self.assertIsNone(self.frames()[0]["cp_w"])

    def test_a_stale_or_unavailable_source_is_a_gap(self):
        self.telemetry.setpoint = rd(4312.0, "stale")
        self.telemetry.target = rd(None, "unavailable")
        self.telemetry.dc = rd(4304.6, "stale")
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["setpoint_w"], frame["target_dc_w"], frame["dc_w"]), (None, None, None))

    def test_values_are_rounded_to_a_tenth_of_a_watt(self):
        self.telemetry.dc = rd(4304.5678)
        self.history.tick()
        self.assertEqual(self.frames()[0]["dc_w"], 4304.6)

    def test_the_ring_keeps_600_frames_and_drops_the_oldest(self):
        for _ in range(601):
            self.history.tick()
        frames = self.frames()
        self.assertEqual(len(frames), 600)
        self.assertEqual(frames[0]["t"], 1_700_000_001)
        self.assertEqual(frames[-1]["t"], 1_700_000_600)

    def test_the_snapshot_describes_itself_and_returns_a_copy(self):
        self.history.tick()
        snap = self.history.snapshot()
        self.assertEqual((snap["interval_s"], snap["window_s"]), (1, 600))
        snap["frames"].clear()
        self.assertEqual(len(self.frames()), 1)

    def test_the_thread_samples_and_stops_and_start_is_idempotent(self):
        history = History(self.telemetry, period_s=0.02)
        history.start()
        thread = history._thread
        history.start()
        self.assertIs(history._thread, thread)
        deadline = time.monotonic() + 3
        while len(history.snapshot()["frames"]) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        history.stop()
        self.assertGreaterEqual(len(history.snapshot()["frames"]), 3)
        self.assertFalse(thread.is_alive())

    def test_stop_before_start_does_nothing(self):
        self.history.stop()


if __name__ == "__main__":
    unittest.main()
