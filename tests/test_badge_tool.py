import importlib.util
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("badge_tool", ROOT / "scripts" / "badge.py")
badge_tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = badge_tool
SPEC.loader.exec_module(badge_tool)


class BadgeToolTests(unittest.TestCase):
    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_2026_battery_voltage_line(self, _prefix, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "BADGE_BATTERY_VOLTAGE=4.087\r\n"
        line = badge_tool.battery_voltage_line(
            "COM10", {"badge_version": "2026"})
        self.assertEqual(line, "Battery voltage: 4.087 V")

    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    def test_2026_battery_voltage_warning_range(self, _prefix, run):
        cases = (
            ("3.799", True),
            ("3.800", False),
            ("4.200", False),
            ("4.201", True),
        )
        for voltage, should_warn in cases:
            with self.subTest(voltage=voltage):
                run.return_value.returncode = 0
                run.return_value.stdout = (
                    "BADGE_BATTERY_VOLTAGE={}\r\n".format(voltage)
                )
                line = badge_tool.battery_voltage_line(
                    "COM10", {"badge_version": "2026"})
                self.assertEqual("WARNING!!" in line, should_warn)

    @patch.object(badge_tool, "run")
    def test_non_2026_badge_skips_battery_measurement(self, run):
        line = badge_tool.battery_voltage_line(
            "COM10", {"badge_version": "2025"})
        self.assertIsNone(line)
        run.assert_not_called()

    @patch("builtins.print")
    @patch.object(badge_tool, "build_parser")
    def test_battery_voltage_is_last_output_line(self, build_parser, print_mock):
        args = Namespace(handler=lambda _args: "Battery voltage: 4.100 V")
        build_parser.return_value.parse_args.return_value = args
        self.assertEqual(badge_tool.main([]), 0)
        self.assertEqual(print_mock.call_args_list[-1],
                         call("Battery voltage: 4.100 V"))

    @patch.object(badge_tool.shutil, "which")
    @patch.object(badge_tool.importlib.util, "find_spec")
    def test_tool_command_prefers_importable_module(self, find_spec, which):
        find_spec.return_value = object()
        self.assertEqual(badge_tool.tool_command("mpremote"),
                         [sys.executable, "-m", "mpremote"])
        which.assert_not_called()

    @patch.object(badge_tool.shutil, "which", return_value="/usr/bin/mpremote")
    @patch.object(badge_tool.importlib.util, "find_spec", return_value=None)
    def test_tool_command_falls_back_to_path(self, _find_spec, _which):
        self.assertEqual(badge_tool.tool_command("mpremote"),
                         ["/usr/bin/mpremote"])

    def test_parse_latest_stable_firmware(self):
        page = """
        <a href="/resources/firmware/ESP32_GENERIC_C3-20250911-v1.26.1.bin">old</a>
        <a href="/resources/firmware/ESP32_GENERIC_C3-20260824-v1.29.0.bin">latest</a>
        <a href="/resources/firmware/ESP32_GENERIC_C3-20260919-v1.30.0-preview.1.bin">preview</a>
        """
        firmware = badge_tool.parse_latest_firmware(page)
        self.assertEqual(firmware.version, "1.29.0")
        self.assertEqual(firmware.date, "20260824")
        self.assertTrue(firmware.url.endswith("ESP32_GENERIC_C3-20260824-v1.29.0.bin"))

    def test_upload_filter(self):
        files = {path.relative_to(badge_tool.SOFTWARE_DIR).as_posix()
                 for path in badge_tool.upload_files()}
        self.assertIn("main.py", files)
        self.assertIn("lib/ssd1306.py", files)
        self.assertNotIn("badge.json", files)
        self.assertFalse(any("__pycache__" in path or path.endswith(".pyc") for path in files))

    def test_clean_bytecode_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            software_dir = Path(temp_dir)
            cache_dir = software_dir / "lib" / "__pycache__"
            cache_dir.mkdir(parents=True)
            (cache_dir / "module.pyc").write_bytes(b"cache")
            (software_dir / "orphan.pyo").write_bytes(b"cache")
            (software_dir / "main.py").write_text("pass\n", encoding="utf-8")
            with patch.object(badge_tool, "SOFTWARE_DIR", software_dir):
                self.assertEqual(badge_tool.clean_bytecode_cache(), 3)
            self.assertFalse(cache_dir.exists())
            self.assertFalse((software_dir / "orphan.pyo").exists())
            self.assertTrue((software_dir / "main.py").exists())

    @patch.object(badge_tool, "write_remote_config")
    @patch.object(badge_tool, "run")
    @patch.object(badge_tool, "mpremote_prefix", return_value=["mpremote"])
    @patch.object(badge_tool, "clean_bytecode_cache", return_value=0)
    def test_upload_uses_one_recursive_copy(
            self, _clean, _prefix, run, _write_config):
        files = [badge_tool.SOFTWARE_DIR / "main.py",
                 badge_tool.SOFTWARE_DIR / "games" / "snake.py"]
        with patch.object(badge_tool, "upload_files", return_value=files):
            badge_tool.upload_tree("/dev/ttyACM0", {})
        copy_commands = [call.args[0] for call in run.call_args_list
                         if "cp" in call.args[0]]
        self.assertEqual(copy_commands, [[
            "mpremote", "fs", "cp", "-r",
            str(badge_tool.SOFTWARE_DIR / "games"),
            str(badge_tool.SOFTWARE_DIR / "main.py"), ":",
        ]])

    def test_merge_preserves_identity_and_parameters(self):
        remote = {
            "device_id": "A1B2C3D4E5F6",
            "holder_name": "Ada",
            "badge_version": "2025",
            "git_commit": "old main",
            "params": {"Brightness": 42},
        }
        result = badge_tool.merge_config(remote, "2026", None, write_git=False)
        self.assertEqual(result["device_id"], remote["device_id"])
        self.assertEqual(result["holder_name"], "Ada")
        self.assertEqual(result["badge_version"], "2026")
        self.assertEqual(result["params"]["Brightness"], 42)


if __name__ == "__main__":
    unittest.main()
