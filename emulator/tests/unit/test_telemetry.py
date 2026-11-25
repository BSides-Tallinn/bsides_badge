import tempfile
import unittest
from pathlib import Path
from emulator.telemetry import FrameRate, find_emulator, process_memory, mib


class TelemetryTests(unittest.TestCase):
    def test_rate_and_reset(self):
        rate = FrameRate()
        rate.observe(100, 0)
        rate.observe(134, 1)
        self.assertEqual(rate.fps, 34)
        rate.observe(1, 2)
        self.assertIsNone(rate.fps)
        rate.observe(35, 3)
        self.assertEqual(rate.fps, 34)
        rate.reset()
        self.assertIsNone(rate.fps)
        rate.observe(100, 4)
        self.assertIsNone(rate.fps)

    def test_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            p = proc / "123"
            p.mkdir()
            (p / "status").write_text("VmRSS:\t1024 kB\nVmHWM:\t2048 kB\n")
            (p / "cmdline").write_bytes(b"/tmp/esp-emu\0--control-tcp\x00127.0.0.1:5556\0")
            self.assertEqual(process_memory(123, proc), (1024, 2048))
            self.assertEqual(find_emulator("127.0.0.1", 5556, proc), 123)
            self.assertEqual(find_emulator("localhost", 5556, proc), 123)
            (p/"cmdline").write_bytes(b"/tmp/esp-emu\0--control-tcp\0localhost:5556\0")
            self.assertEqual(find_emulator("127.0.0.1", 5556, proc), 123)
            self.assertIsNone(find_emulator("127.0.0.1", 5557, proc))
            self.assertEqual(process_memory(456, proc), (None, None))
            self.assertEqual(mib(1024), "1.0 MiB")

    def test_failed_discovery_backoff_and_revalidation(self):
        from unittest.mock import patch
        from emulator.telemetry import Telemetry
        telemetry = Telemetry()
        with patch("emulator.telemetry.find_emulator", side_effect=[None, 123, 456]) as discover, patch("emulator.telemetry.matches_emulator", side_effect=[True, False]), patch("emulator.telemetry.process_memory", return_value=(100, 200)):
            for now in range(5):
                telemetry.sample(now, "localhost", 5556)
            self.assertEqual(discover.call_count, 1)
            telemetry.sample(5, "localhost", 5556)
            self.assertEqual(telemetry.emulator_pid, 123)
            telemetry.sample(6, "localhost", 5556)
            self.assertEqual(discover.call_count, 2)
            telemetry.sample(7, "localhost", 5556)
            self.assertEqual(telemetry.emulator_pid, 456)
            self.assertEqual(discover.call_count, 3)
