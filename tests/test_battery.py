import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "software"))
from battery import estimate_soc


class BatteryTests(unittest.TestCase):
    def test_soc_is_clamped(self):
        self.assertEqual(estimate_soc(2.8), 0)
        self.assertEqual(estimate_soc(4.3), 100)

    def test_soc_interpolates(self):
        self.assertEqual(estimate_soc(3.75), 50)
        self.assertEqual(estimate_soc(4.05), 90)


if __name__ == "__main__":
    unittest.main()
