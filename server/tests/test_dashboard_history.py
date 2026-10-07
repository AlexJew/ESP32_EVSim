import itertools
import time
import unittest

from charger_web.history import History

FRAME_KEYS = ["t", "cp_w", "setpoint_w", "target_dc_w", "dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v",
              "charger_w", "charger_a", "charger_v", "sig_a"]


def rd(value, status="ok"):
    return {"value": value, "status": status, "updated_at": None}


class StubTelemetry:
    def __init__(self):
        self.samples = []
        self.setpoint = rd(4312.0)
        self.target = rd(4312.6)
        self.inverter = {"dc_w": rd(4304.6), "dc_a": rd(8.2), "dc_v": rd(524.7),
                         "ac_w": rd(4181.0), "ac_a": rd(6.0), "ac_v": rd(233.3)}

    def drain_cp_samples(self):
        samples, self.samples = self.samples, []
        return samples

    def snapshot(self):
        return {"setpoint": {"setpoint_w": self.setpoint}, "server": {"target_dc_w": self.target},
                "inverter": self.inverter}


class StubCharger:
    def __init__(self):
        self.calls = 0
        self.power = rd(8245)
        self.current = rd({"L1": 11.785, "L2": 11.593, "L3": 12.078})
        self.voltage = rd({"L1": 232, "L2": 232, "L3": 233})
        self.signalled = rd(12)

    def status(self):
        self.calls += 1
        return {"meter_power_w": self.power, "meter_current_a": self.current, "meter_voltage_v": self.voltage,
                "signalled_current_a": self.signalled}


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.telemetry = StubTelemetry()
        self.charger = StubCharger()
        self.wall = itertools.count(1_700_000_000)
        self.history = History(self.telemetry, self.charger, wall=lambda: next(self.wall))

    def frames(self):
        return self.history.snapshot()["frames"]

    def test_a_frame_has_the_time_and_every_value(self):
        self.telemetry.samples = [4300.0, 4400.0]
        self.history.tick()
        self.assertEqual(self.frames(), [{
            "t": 1_700_000_000, "cp_w": 4350.0, "setpoint_w": 4312.0, "target_dc_w": 4312.6,
            "dc_w": 4304.6, "dc_a": 8.2, "dc_v": 524.7, "ac_w": 4181.0, "ac_a": 6.0, "ac_v": 233.3,
            "charger_w": 8245.0, "charger_a": 11.8, "charger_v": 232.3, "sig_a": 12.0,
        }])
        self.assertEqual(list(self.frames()[0]), FRAME_KEYS)

    def test_the_charger_values_are_the_mean_of_the_phases(self):
        self.charger.current = rd({"L1": 10.0, "L2": 11.0, "L3": 15.0})
        self.charger.voltage = rd({"L1": 230, "L2": 231, "L3": 232})
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["charger_a"], frame["charger_v"]), (12.0, 231.0))

    def test_a_missing_phase_is_left_out_of_the_mean(self):
        self.charger.current = rd({"L1": 10.0, "L3": 14.0})
        self.history.tick()
        self.assertEqual(self.frames()[0]["charger_a"], 12.0)

    def test_no_meter_is_a_gap_not_a_zero(self):
        self.charger.power = rd(None, "unavailable")
        self.charger.current = rd(None, "unavailable")
        self.charger.voltage = rd(None, "unavailable")
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["charger_w"], frame["charger_a"], frame["charger_v"]), (None, None, None))
        self.assertEqual(frame["sig_a"], 12.0)                       # the other charger values still record

    def test_a_stale_charger_reading_is_a_gap(self):
        self.charger.power = rd(8245, "stale")
        self.charger.current = rd({"L1": 11.0, "L2": 11.0, "L3": 11.0}, "stale")
        self.charger.signalled = rd(12, "stale")
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["charger_w"], frame["charger_a"], frame["sig_a"]), (None, None, None))

    def test_without_a_charger_the_charger_keys_are_gaps(self):
        history = History(self.telemetry, wall=lambda: next(self.wall))
        history.tick()
        frame = history.snapshot()["frames"][0]
        self.assertEqual([frame[k] for k in ("charger_w", "charger_a", "charger_v", "sig_a")], [None] * 4)
        self.assertEqual(frame["dc_a"], 8.2)

    def test_the_inverter_values_age_together_and_each_can_be_a_gap(self):
        self.telemetry.inverter = {**self.telemetry.inverter, "dc_a": rd(None, "unavailable"), "ac_v": rd(233.3, "stale")}
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["dc_a"], frame["ac_v"], frame["dc_w"], frame["ac_w"]), (None, None, 4304.6, 4181.0))

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
        self.telemetry.inverter = {**self.telemetry.inverter, "dc_w": rd(4304.6, "stale")}
        self.history.tick()
        frame = self.frames()[0]
        self.assertEqual((frame["setpoint_w"], frame["target_dc_w"], frame["dc_w"]), (None, None, None))

    def test_values_are_rounded_to_a_tenth(self):
        self.telemetry.inverter = {**self.telemetry.inverter, "dc_w": rd(4304.5678)}
        self.history.tick()
        self.assertEqual(self.frames()[0]["dc_w"], 4304.6)

    def test_the_charger_is_read_from_its_cache_once_per_tick(self):
        self.history.tick()
        self.history.tick()
        self.assertEqual(self.charger.calls, 2)

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
        history = History(self.telemetry, self.charger, period_s=0.02)
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
