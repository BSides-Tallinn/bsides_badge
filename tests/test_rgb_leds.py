import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FakeNeoPixel(list):
    def __init__(self, _pin, count):
        super().__init__([(0, 0, 0)] * count)
        self.writes = 0

    def fill(self, value):
        for index in range(len(self)):
            self[index] = value

    def write(self):
        self.writes += 1


class FakePin:
    OUT = 1

    def __init__(self, number, mode=None):
        self.number = number
        self.mode = mode


sys.modules.setdefault("uasyncio", asyncio)
sys.modules.setdefault("neopixel", types.SimpleNamespace(NeoPixel=FakeNeoPixel))
sys.modules.setdefault("machine", types.SimpleNamespace(Pin=FakePin))
SPEC = importlib.util.spec_from_file_location(
    "rgb_leds_test", ROOT / "software" / "rgb_leds.py")
rgb_leds = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(rgb_leds)


class Parameter:
    def __init__(self, value, maxval=100):
        self.value = value
        self.maxval = maxval


class RgbLedTests(unittest.TestCase):
    def setUp(self):
        rgb_leds.time.ticks_ms = lambda: 0
        rgb_leds.time.ticks_diff = lambda end, start: end - start
        rgb_leds.led_effect = Parameter(0, 10)
        rgb_leds.led_brightness = Parameter(10)
        rgb_leds.led_hue = Parameter(180, 360)
        rgb_leds.led_sat = Parameter(100)
        rgb_leds.led_speed = Parameter(30)

    def test_all_effects_render_one_frame(self):
        pixels = FakeNeoPixel(3, 16)
        for name, effect in rgb_leds.LED_EFFECTS:
            with self.subTest(effect=name):
                effect(pixels, None)
                self.assertEqual(len(pixels), 16)

    def test_neopixel_initialization_clears_strip(self):
        pixels = rgb_leds.init_neopixels()
        self.assertEqual(pixels, [(0, 0, 0)] * rgb_leds.NEOPIXEL_COUNT)
        self.assertEqual(pixels.writes, 1)

    def test_game_mute_clears_strip_once_and_restores_effect(self):
        from unittest.mock import patch

        pixels = FakeNeoPixel(3, 16)
        muted = False
        snapshots = []

        async def sleep_ms(_delay):
            nonlocal muted
            snapshots.append((list(pixels), pixels.writes))
            if len(snapshots) == 1:
                muted = True
            elif len(snapshots) == 3:
                muted = False
            elif len(snapshots) == 4:
                raise asyncio.CancelledError()

        with patch.object(rgb_leds.asyncio, "sleep_ms", sleep_ms, create=True):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(rgb_leds.neopixel_task(
                    pixels, Parameter(1, 10), Parameter(10), Parameter(180),
                    Parameter(100), Parameter(30), lambda: muted))

        self.assertTrue(any(color != (0, 0, 0) for color in snapshots[0][0]))
        self.assertEqual(snapshots[1][0], [(0, 0, 0)] * 16)
        self.assertEqual(snapshots[1][1], snapshots[2][1])
        self.assertTrue(any(color != (0, 0, 0) for color in snapshots[3][0]))

    def test_ui_module_contains_no_led_effect_implementations(self):
        source = (ROOT / "software" / "bsides.py").read_text(encoding="utf-8")
        self.assertNotIn("def led_eff_", source)
        self.assertNotIn("import neopixel", source)

    def test_police_lights_full_half_with_no_dark_leds(self):
        pixels = FakeNeoPixel(3, 16)
        off = (0, 0, 0)

        state = rgb_leds.led_eff_police(pixels, {"phase": 0})
        red_half = list(pixels[0:8])
        blue_half = list(pixels[8:16])
        self.assertTrue(all(color != off for color in red_half),
                        "all 8 red-half LEDs (0-7) should be lit, "
                        "including the top-of-column LED at index 7")
        self.assertTrue(all(color == off for color in blue_half))

        pixels = FakeNeoPixel(3, 16)
        rgb_leds.led_eff_police(pixels, {"phase": 50})
        red_half = list(pixels[0:8])
        blue_half = list(pixels[8:16])
        self.assertTrue(all(color == off for color in red_half))
        self.assertTrue(all(color != off for color in blue_half),
                        "all 8 blue-half LEDs (8-15) should be lit, "
                        "including the top-of-column LED at index 8")


if __name__ == "__main__":
    unittest.main()
