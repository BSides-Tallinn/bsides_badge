"""Boot routing and import-side-effect regression tests."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "software"


def module(name, **attrs):
    return types.SimpleNamespace(**attrs)


class StartupTests(unittest.TestCase):
    def test_import_is_inert_and_initialize_uses_selected_profile(self):
        config = {"badge_version": "2026", "device_id": "123456789ABC",
                  "holder_name": "Test", "params": {}}
        profile = {"oled_address": 0x3C, "select_pin": 10}
        pin = Mock()
        i2c = Mock()
        display = Mock()
        writer = Mock()
        load = Mock(return_value=config)
        hardware = Mock(return_value=profile)
        run = Mock()
        loop = Mock()
        fonts = {name: types.ModuleType(name) for name in
                 ("writer", "writer.writer", "writer.font6", "writer.font10",
                  "writer.freesans20")}
        fonts["writer"].__path__ = []
        fonts["writer.writer"].Writer = writer
        for name in ("writer", "font6", "font10", "freesans20"):
            setattr(fonts["writer"], name, fonts["writer." + name])
        stubs = {
            **fonts,
            "machine": module("machine", Pin=pin, I2C=i2c, RTC=Mock()),
            "uasyncio": module("uasyncio", run=run, new_event_loop=loop),
            "micropython": module("micropython"),
            "ssd1306": module("ssd1306", SSD1306_I2C=display),
            "bsides_logo": module("bsides_logo"),
            "rgb_leds": module("rgb_leds", LED_EFFECTS=[]),
            "badge_config": module("badge_config", format_device_id=Mock(),
                                   hardware_for=hardware, load_badge_config=load,
                                   save_badge_config=Mock()),
            "battery": module("battery", estimate_soc=Mock(), read_battery_voltage=Mock()),
        }
        spec = importlib.util.spec_from_file_location("startup_test_bsides", ROOT / "bsides.py")
        badge = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(badge)
            for mock in (pin, i2c, display, writer, load, hardware, run, loop):
                mock.assert_not_called()
            self.assertIsNone(badge.oled)
            self.assertIsNone(badge.wri6)
            badge.initialize(config)
            load.assert_not_called()
            hardware.assert_called_once_with("2026")
            display.assert_called_once_with(128, 64, i2c.return_value, addr=0x3C)
            self.assertEqual(writer.call_count, 3)
            self.assertEqual(badge.BTN_SELECT_PIN, 10)
            self.assertEqual(badge.device_id, config["device_id"])
            self.assertEqual(badge.USERNAME, "Test")
            self.assertIs(badge.badge_config, config)
            # Check explicit startup without creating a real coroutine.
            with patch.object(badge, "initialize") as init, patch.object(badge, "main", new_callable=Mock) as main:
                badge.run(config)
                init.assert_called_once_with(config)
                run.assert_called_once_with(main.return_value)
                loop.assert_called_once()
                run.side_effect = RuntimeError("startup failure")
                with self.assertRaisesRegex(RuntimeError, "startup failure"):
                    badge.run(config)
                self.assertEqual(loop.call_count, 2)

    def test_boot_modes(self):
        source = compile((ROOT / "main.py").read_text(), str(ROOT / "main.py"), "exec")
        profiles = {"2025_prototype": 4, "2025": 4, "2026": 10}
        for version, select in profiles.items():
            for rtc, held in ((b"wifi_fetch", True), (b"", True), (b"", False)):
                with self.subTest(version=version, rtc=rtc, held=held):
                    config = {"badge_version": version}
                    load = Mock(return_value=config)
                    hardware = Mock(side_effect=lambda requested: {"select_pin": profiles[requested]})
                    run = Mock()
                    pin = Mock()
                    pin.IN = 1
                    pin.return_value.value.return_value = 0 if held else 1
                    # Record imports to prove alternate modes never import bsides.
                    import builtins
                    original_import = builtins.__import__
                    imported = []
                    def traced(name, *args, **kwargs):
                        imported.append(name)
                        return original_import(name, *args, **kwargs)
                    stubs = {
                        "machine": module("machine", Pin=pin, RTC=lambda: module("rtc", memory=lambda: rtc)),
                        "time": module("time", sleep=Mock()),
                        "badge_config": module("badge_config", load_badge_config=load,
                                               hardware_for=hardware),
                        "bsides": module("bsides", run=run),
                        "wifi_fetch": module("wifi_fetch"),
                    }
                    with patch.dict(sys.modules, stubs), patch("builtins.__import__", traced):
                        exec(source, {})
                    load.assert_called_once()
                    hardware.assert_called_once_with(version)
                    if rtc:
                        self.assertIn("wifi_fetch", imported)
                        self.assertNotIn("bsides", imported)
                        pin.assert_not_called()
                        run.assert_not_called()
                    else:
                        pin.assert_called_once_with(select, pin.IN)
                        self.assertNotIn("wifi_fetch", imported)
                        if held:
                            self.assertNotIn("bsides", imported)
                            run.assert_not_called()
                        else:
                            self.assertIn("bsides", imported)
                            run.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
