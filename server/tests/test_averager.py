import unittest

from setpoint_server.averager import WindowAverager


class WindowAveragerTest(unittest.TestCase):
    def setUp(self):
        self.avg = WindowAverager(1)

    def test_empty_window(self):
        self.assertEqual(self.avg.average(5.0), (0, None))

    def test_one_sample(self):
        self.avg.add(10.0, 8000.0)
        self.assertEqual(self.avg.average(10.5), (1, 8000.0))

    def test_mean_of_several_samples(self):
        for t, w in [(10.0, 100.0), (10.2, 200.0), (10.4, 600.0)]:
            self.avg.add(t, w)
        count, mean = self.avg.average(10.6)
        self.assertEqual(count, 3)
        self.assertAlmostEqual(mean, 300.0)

    def test_sample_exactly_one_window_old_is_excluded(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(11.0), (0, None))

    def test_sample_just_under_one_window_old_is_included(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(10.999), (1, 100.0))

    def test_sample_arriving_at_the_tick_is_included(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(10.0), (1, 100.0))

    def test_old_samples_age_out_and_are_dropped(self):
        self.avg.add(10.0, 100.0)
        self.avg.add(10.5, 300.0)
        self.assertEqual(len(self.avg), 2)
        self.assertEqual(self.avg.average(11.2), (1, 300.0))
        self.assertEqual(len(self.avg), 1)

    def test_long_gap_empties_the_window(self):
        self.avg.add(10.0, 100.0)
        self.assertEqual(self.avg.average(60.0), (0, None))
        self.assertEqual(len(self.avg), 0)

    def test_each_sample_lands_in_exactly_one_tick(self):
        # 4 Hz samples (0.25 s is exact in binary), a tick every 1.0 s.
        counts = []
        for i in range(1, 41):
            t = i * 0.25
            self.avg.add(t, 100.0)
            if t == int(t):
                counts.append(self.avg.average(t)[0])
        self.assertEqual(counts, [4] * 10)

    def test_memory_does_not_grow_without_bound(self):
        for i in range(1, 5001):
            t = i * 0.2
            self.avg.add(t, 100.0)
            if i % 5 == 0:
                self.avg.average(t)
        self.assertLessEqual(len(self.avg), 6)


if __name__ == "__main__":
    unittest.main()
