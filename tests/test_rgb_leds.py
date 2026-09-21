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

    def test_ui_module_contains_no_led_effect_implementations(self):
        source = (ROOT / "software" / "bsides.py").read_text(encoding="utf-8")
        self.assertNotIn("def led_eff_", source)
        self.assertNotIn("import neopixel", source)


if __name__ == "__main__":
    unittest.main()
