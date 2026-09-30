import unittest

from setpoint_server.inverter_control import Action, Controller


def cap(watts):
    return Action("cap", watts)


STANDBY = Action("standby", None)
RESUME = Action("resume", None)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.c = Controller(deadband_w=100, refresh_s=10, standby_after_s=120)

    def to_standby(self):
        self.c.step(0, None)
        self.c.step(120, None)
        self.assertEqual(self.c.mode, "standby")

    def test_the_first_valid_target_is_written(self):
        self.assertEqual(self.c.step(0, 4000.0), [cap(4000.0)])
        self.assertEqual((self.c.mode, self.c.last_target_w), ("capped", 4000.0))

    def test_starting_without_data_asserts_a_zero_cap(self):
        self.assertEqual(self.c.step(0, None), [cap(0.0)])
        self.assertEqual(self.c.mode, "zero")

    def test_no_data_after_a_valid_target_caps_at_zero_at_once(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, None), [cap(0.0)])
        self.assertEqual(self.c.mode, "zero")

    def test_inside_the_dead_band_nothing_is_written(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4099.9), [])
        self.assertEqual(self.c.step(2, 3900.1), [])

    def test_at_the_dead_band_a_write_happens(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4100.0), [cap(4100.0)])

    def test_drift_is_measured_from_the_last_written_target(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4060.0), [])
        self.assertEqual(self.c.step(2, 4110.0), [cap(4110.0)])

    def test_zero_versus_non_zero_always_writes(self):
        self.c.step(0, 0.0)
        self.assertEqual(self.c.step(1, 50.0), [cap(50.0)])
        self.assertEqual(self.c.step(2, 0.0), [cap(0.0)])

    def test_the_cap_is_re_committed_every_refresh_period(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(9.9, 4000.0), [])
        self.assertEqual(self.c.step(10.0, 4000.0), [cap(4000.0)])
        self.assertEqual(self.c.step(19.9, 4000.0), [])
        self.assertEqual(self.c.step(20.0, 4000.0), [cap(4000.0)])

    def test_the_zero_cap_is_re_committed_during_an_outage(self):
        self.c.step(0, None)
        self.assertEqual(self.c.step(9.9, None), [])
        self.assertEqual(self.c.step(10.0, None), [cap(0.0)])

    def test_standby_comes_only_after_the_outage_time(self):
        self.c.step(0, None)
        for t in range(1, 120):
            self.assertNotIn(STANDBY, self.c.step(t, None))
        self.assertEqual(self.c.step(120, None)[-1], STANDBY)
        self.assertEqual(self.c.mode, "standby")

    def test_standby_is_silent(self):
        self.to_standby()
        self.assertEqual(self.c.step(121, None), [])
        self.assertEqual(self.c.step(500, None), [])

    def test_data_returning_from_standby_resumes_then_caps(self):
        self.to_standby()
        self.assertEqual(self.c.step(200, 4000.0), [RESUME, cap(4000.0)])
        self.assertEqual(self.c.mode, "capped")

    def test_a_valid_zero_is_not_an_outage(self):
        self.c.step(0, 0.0)
        for t in range(1, 400):
            self.assertNotIn(STANDBY, self.c.step(t, 0.0))
        self.assertEqual(self.c.mode, "capped")

    def test_the_outage_clock_restarts_when_data_returns(self):
        self.c.step(0, None)
        self.c.step(100, None)
        self.c.step(101, 4000.0)
        self.c.step(102, None)                       # a new outage starts here
        self.assertNotIn(STANDBY, self.c.step(200, None))   # only 98 s since 102
        self.assertIn(STANDBY, self.c.step(222, None))

    def test_force_writes_even_inside_the_dead_band(self):
        self.c.step(0, 4000.0)
        self.assertEqual(self.c.step(1, 4000.0, force=True), [cap(4000.0)])

    def test_reset_makes_the_next_step_re_assert(self):
        self.c.step(0, 4000.0)
        self.c.reset()
        self.assertEqual(self.c.mode, "unknown")
        self.assertEqual(self.c.step(1, 4000.0), [cap(4000.0)])

    def test_reset_into_standby_resumes_when_data_arrives(self):
        self.c.reset(standby=True)
        self.assertEqual(self.c.step(0, None), [])
        self.assertEqual(self.c.step(1, 3000.0), [RESUME, cap(3000.0)])

    def test_reset_keeps_the_outage_clock(self):
        self.c.step(0, None)
        self.c.step(60, None)
        self.c.reset()
        self.assertIn(STANDBY, self.c.step(120, None))


if __name__ == "__main__":
    unittest.main()
