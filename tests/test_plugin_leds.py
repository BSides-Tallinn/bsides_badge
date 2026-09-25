import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "plugin_leds_test", ROOT / "software" / "plugin_leds.py")
plugin = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {
        "uasyncio": asyncio,
        "machine": types.SimpleNamespace(Pin=None, PWM=None)}):
    spec.loader.exec_module(plugin)


class PluginLedTests(unittest.TestCase):
    def test_animated_effects_are_complementary(self):
        for effect in range(3):
            for elapsed in range(0, 6000, 20):
                first, second = plugin.duties(effect, elapsed)
                self.assertEqual(first + second, 65535)
                self.assertTrue(0 <= first <= 65535)
                self.assertTrue(0 <= second <= 65535)

    def test_breathe_reaches_both_extremes_and_midpoint(self):
        self.assertEqual(plugin.duties(0, 0), (0, 65535))
        self.assertEqual(plugin.duties(0, 1500), (65535, 0))
        self.assertAlmostEqual(plugin.duties(0, 750)[0], 65535 / 2, delta=1)
        self.assertEqual(plugin.duties(0, 3000), (0, 65535))

    def test_blink_and_police_reverse_phase(self):
        for effect, half_period in ((1, 500), (2, 600)):
            for elapsed in range(0, half_period, 20):
                self.assertEqual(plugin.duties(effect, elapsed),
                                 plugin.duties(effect, elapsed + half_period)[::-1])
        self.assertNotEqual(plugin.duties(2, 0), plugin.duties(2, 100))

    def test_static_modes(self):
        for elapsed in (0, 500, 1500, 6000):
            self.assertEqual(plugin.duties(3, elapsed), (65535, 65535))
            self.assertEqual(plugin.duties(4, elapsed), (0, 0))

    def test_task_drives_pins_switches_mode_and_cleans_up(self):
        leds = []
        class FakePWM:
            def __init__(self, pin, **kwargs):
                self.pin = pin
                self.levels = []
                self.closed = False
                leds.append(self)

            def duty_u16(self, value):
                self.levels.append(value)

            def deinit(self):
                self.closed = True

        effect = types.SimpleNamespace(value=3)
        async def sleep_ms(_):
            if effect.value == 3:
                effect.value = 4
            else:
                raise asyncio.CancelledError()

        pin = types.SimpleNamespace(OUT=1)
        with patch.object(plugin, "PWM", FakePWM), \
                patch.object(plugin, "Pin", side_effect=lambda number, mode: number) as pin_mock, \
                patch.object(plugin.time, "ticks_ms", return_value=0, create=True), \
                patch.object(plugin.time, "ticks_diff", side_effect=lambda a, b: a-b, create=True), \
                patch.object(plugin.asyncio, "sleep_ms", sleep_ms, create=True):
            pin_mock.OUT = pin.OUT
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(plugin.led_task(effect))
        self.assertEqual([led.pin for led in leds], [6, 7])
        for led in leds:
            self.assertEqual(led.levels, [65535, 0, 0])
            self.assertTrue(led.closed)

    def test_game_mute_turns_off_both_channels_and_restores_them(self):
        leds = []
        class FakePWM:
            def __init__(self, pin, **_kwargs):
                self.levels = []
                leds.append(self)

            def duty_u16(self, value):
                self.levels.append(value)

            def deinit(self):
                pass

        muted = False
        cycles = 0

        async def sleep_ms(_delay):
            nonlocal muted, cycles
            cycles += 1
            if cycles == 1:
                muted = True
            elif cycles == 3:
                muted = False
            elif cycles == 4:
                raise asyncio.CancelledError()

        with patch.object(plugin, "PWM", FakePWM), \
                patch.object(plugin, "Pin", side_effect=lambda number, mode: number) as pin_mock, \
                patch.object(plugin.time, "ticks_ms", return_value=0, create=True), \
                patch.object(plugin.time, "ticks_diff", side_effect=lambda a, b: a-b, create=True), \
                patch.object(plugin.asyncio, "sleep_ms", sleep_ms, create=True):
            pin_mock.OUT = 1
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(plugin.led_task(types.SimpleNamespace(value=3),
                                            lambda: muted))

        for led in leds:
            self.assertEqual(led.levels, [65535, 0, 0, 65535, 0])


if __name__ == "__main__":
    unittest.main()
