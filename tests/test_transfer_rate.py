import unittest

from roseblox.backend.transfer_rate import TransferRate, format_duration, format_speed, progress_text

MIB = 1024 * 1024


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TransferRateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.rate = TransferRate(window=5.0, clock=self.clock)

    def feed(self, *steps):
        downloaded = 0
        for seconds, received in steps:
            self.clock.now += seconds
            downloaded += received
            self.rate.add(downloaded)
        return downloaded

    def test_no_estimate_until_a_second_of_data(self):
        self.rate.add(0)
        self.assertIsNone(self.rate.speed())
        self.clock.now += 0.5
        self.rate.add(MIB)
        self.assertIsNone(self.rate.speed())
        self.assertIsNone(self.rate.remaining(MIB, 10 * MIB))

    def test_speed_and_time_left(self):
        self.rate.add(0)
        downloaded = self.feed(*[(1.0, 2 * MIB)] * 4)
        self.assertAlmostEqual(2 * MIB, self.rate.speed())
        self.assertAlmostEqual(46.0, self.rate.remaining(downloaded, 100 * MIB))

    def test_only_recent_seconds_count(self):
        self.rate.add(0)
        downloaded = self.feed(*[(1.0, 10 * MIB)] * 5)
        for _ in range(10):
            self.clock.now += 1.0
            downloaded += MIB
            self.rate.add(downloaded)
        self.assertAlmostEqual(MIB, self.rate.speed(), delta=MIB * 0.25)

    def test_resumed_download_counts_only_new_bytes(self):
        self.rate.add(500 * MIB)
        self.clock.now += 2.0
        self.rate.add(504 * MIB)
        self.assertAlmostEqual(2 * MIB, self.rate.speed())

    def test_restarted_download_starts_over(self):
        self.rate.add(0)
        self.clock.now += 2.0
        self.rate.add(50 * MIB)
        self.clock.now += 1.0
        self.rate.add(MIB)
        self.assertIsNone(self.rate.speed())

    def test_reset(self):
        self.rate.add(0)
        self.clock.now += 2.0
        self.rate.add(MIB)
        self.rate.reset()
        self.assertIsNone(self.rate.speed())


class FormattingTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual("0 s", format_duration(0.2))
        self.assertEqual("45 s", format_duration(45))
        self.assertEqual("1 min 20 s", format_duration(80))
        self.assertEqual("2 min", format_duration(120))
        self.assertEqual("25 min", format_duration(25 * 60 + 13))
        self.assertEqual("1 h 5 min", format_duration(65 * 60))
        self.assertEqual("2 h", format_duration(7200))

    def test_speed(self):
        self.assertEqual("12.4 MiB/s", format_speed(12.4 * MIB))
        self.assertEqual("512.0 KiB/s", format_speed(512 * 1024))

    def test_progress_text(self):
        clock = FakeClock()
        rate = TransferRate(clock=clock)
        self.assertEqual("37%", progress_text(0.37, rate, 37 * MIB, 100 * MIB))
        self.assertEqual("3.0 MiB", progress_text(None, rate, 3 * MIB, None))
        rate.add(0)
        clock.now += 2.0
        rate.add(4 * MIB)
        self.assertEqual("4% · 2.0 MiB/s · 48 s left", progress_text(0.04, rate, 4 * MIB, 100 * MIB))
        self.assertEqual("4.0 MiB · 2.0 MiB/s", progress_text(None, rate, 4 * MIB, None))


if __name__ == "__main__":
    unittest.main()
