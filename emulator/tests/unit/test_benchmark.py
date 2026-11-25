import unittest
from unittest.mock import patch
from types import SimpleNamespace
from emulator.measure_rgb_fps import measure


class BenchmarkTests(unittest.TestCase):
    def test_slow_telemetry_does_not_start_extra_poll(self):
        now = [0.0]
        calls = []
        def state():
            calls.append(True)
            now[0] += .1
            return SimpleNamespace(frames=len(calls), colors=("#000000",)*16)
        def memory(timestamp):
            now[0] += 2
        with patch("emulator.measure_rgb_fps.time.monotonic", side_effect=lambda: now[0]):
            result = measure(SimpleNamespace(state=state), duration=1, memory=memory)
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(result["elapsed"], .1)
        self.assertAlmostEqual(result["max_ms"], 100)

    def test_nonfinite_duration_and_interval(self):
        for duration, interval in ((float("inf"), .01), (1, float("inf")), (float("nan"), .01)):
            with self.subTest(duration=duration, interval=interval), self.assertRaises(ValueError):
                measure(None, duration=duration, interval=interval)

    def test_fixed_histogram(self):
        from emulator.measure_rgb_fps import LatencyHistogram
        histogram = LatencyHistogram()
        for value in (1.2, 2.8, 3.1, 4.9):
            histogram.add(value)
        self.assertEqual(histogram.median(), (2.5, False))
        self.assertAlmostEqual(histogram.total, 12.0)
        self.assertEqual(histogram.maximum, 4.9)
        size = len(histogram.bins)
        for _ in range(20000):
            histogram.add(20000)
        self.assertEqual(len(histogram.bins), size)
        self.assertEqual(histogram.median(), (10000, True))
