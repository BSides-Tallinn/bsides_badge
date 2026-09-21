import binascii
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("ubinascii", binascii)
sys.modules.setdefault("urandom", types.SimpleNamespace(getrandbits=lambda bits: 0xAB))
SPEC = importlib.util.spec_from_file_location(
    "badge_config_test", ROOT / "software" / "badge_config.py")
badge_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(badge_config)


class BadgeConfigTests(unittest.TestCase):
    def test_longest_device_id_fits_builtin_oled_text_row(self):
        text = badge_config.format_device_id("FFFFFFFFFFFF")
        self.assertEqual(text, "ID:FFFFFFFFFFFF")
        self.assertLessEqual(len(text) * 8, 128)

    def test_hardware_variants(self):
        self.assertEqual(badge_config.hardware_for("2025_prototype")["oled_address"], 0x3D)
        self.assertEqual(badge_config.hardware_for("2025")["select_pin"], 4)
        self.assertEqual(badge_config.hardware_for("2026")["select_pin"], 10)
        self.assertEqual(badge_config.hardware_for("2026")["battery_pin"], 4)

    def test_legacy_files_are_migrated(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                Path("params.json").write_text('{"Brightness": 55}', encoding="utf-8")
                Path("id.txt").write_text("a1b2c3d4e5f6", encoding="utf-8")
                Path("yourname.txt").write_text("Ada", encoding="utf-8")
                result = badge_config.load_badge_config()
            finally:
                os.chdir(original)

            self.assertEqual(result["device_id"], "A1B2C3D4E5F6")
            self.assertEqual(result["holder_name"], "Ada")
            self.assertEqual(result["params"]["Brightness"], 55)
            stored = json.loads((Path(temp_dir) / "badge.json").read_text(encoding="utf-8"))
            self.assertEqual(stored, result)
            for filename in ("params.json", "id.txt", "yourname.txt"):
                self.assertFalse((Path(temp_dir) / filename).exists())


if __name__ == "__main__":
    unittest.main()
