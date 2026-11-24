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

    def test_override_priority_caching_and_effect_restoration(self):
        from unittest.mock import patch

        pixels = FakeNeoPixel(3, 16)
        frame = [(255, 0, 0)] * 4 + [(0, 0, 0)] * 12
        current = frame
        snapshots = []

        async def sleep_ms(_delay):
            nonlocal current
            snapshots.append((list(pixels), pixels.writes))
            if len(snapshots) == 3:
                current = None
            if len(snapshots) == 5:
                raise asyncio.CancelledError()

        with patch.object(rgb_leds.asyncio, "sleep_ms", sleep_ms, create=True):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(rgb_leds.neopixel_task(
                    pixels, Parameter(3, 10), Parameter(100),
                    Parameter(180), Parameter(100), Parameter(30),
                    override_frame=lambda: current))

        self.assertEqual(snapshots[0][0], frame)
        self.assertEqual(snapshots[0][1], snapshots[1][1])
        self.assertEqual(snapshots[1][1], snapshots[2][1])
        self.assertEqual(snapshots[3][0], [(0, 255, 255)] + [(0, 0, 0)] * 15)
        self.assertGreater(snapshots[3][1], snapshots[2][1])

    def test_override_respects_mute_and_resumes(self):
        from unittest.mock import patch

        pixels = FakeNeoPixel(3, 16)
        frame = [(0, 255, 0)] * 16
        muted = False
        snapshots = []

        async def sleep_ms(_delay):
            nonlocal muted
            snapshots.append(list(pixels))
            if len(snapshots) == 1:
                muted = True
            elif len(snapshots) == 3:
                muted = False
            elif len(snapshots) == 4:
                raise asyncio.CancelledError()

        with patch.object(rgb_leds.asyncio, "sleep_ms", sleep_ms, create=True):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(rgb_leds.neopixel_task(
                    pixels, Parameter(1, 10), Parameter(100),
                    Parameter(180), Parameter(100), Parameter(30),
                    lights_off=lambda: muted, override_frame=lambda: frame))

        self.assertEqual(snapshots[0], frame)
        self.assertEqual(snapshots[1], [(0, 0, 0)] * 16)
        self.assertEqual(snapshots[2], [(0, 0, 0)] * 16)
        self.assertEqual(snapshots[3], frame)

    def test_invalid_override_falls_back(self):
        from unittest.mock import patch
        pixels = FakeNeoPixel(3, 16)
        ticks = 0

        async def sleep_ms(_delay):
            nonlocal ticks
            ticks += 1
            if ticks == 3:
                raise asyncio.CancelledError()

        with patch("builtins.print") as warning:
            with patch.object(rgb_leds.asyncio, "sleep_ms", sleep_ms, create=True):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(rgb_leds.neopixel_task(
                        pixels, Parameter(1), Parameter(100), Parameter(180),
                        Parameter(100), Parameter(30),
                        override_frame=lambda: [(255, 0, 0)]))
            warning.assert_called_once()
        self.assertEqual(pixels.writes, 3)
        self.assertTrue(any(color != (0, 0, 0) for color in pixels))

    def test_invalid_override_channels_fall_back_and_recover(self):
        from unittest.mock import patch

        pixels = FakeNeoPixel(3, 16)
        gauge = [(255, 0, 0)] * 16
        normal = [(0, 0, 0)] * 16
        invalid_colors = [(255.0, 0, 0), (255, False, 0), (256, 0, 0),
                          (-1, 0, 0), (255, 0), [255, 0, 0], None]
        invalid_frames = [gauge[:-1] + [color] for color in invalid_colors]
        inputs = [gauge] + invalid_frames + [gauge, None]
        snapshots = []

        async def sleep_ms(_delay):
            snapshots.append(list(pixels))
            if len(snapshots) == len(inputs):
                raise asyncio.CancelledError()

        with patch("builtins.print") as warning:
            with patch.object(rgb_leds.asyncio, "sleep_ms", sleep_ms, create=True):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(rgb_leds.neopixel_task(
                        pixels, Parameter(0), Parameter(100), Parameter(180),
                        Parameter(100), Parameter(30),
                        override_frame=lambda: inputs[len(snapshots)]))
            warning.assert_called_once()
        self.assertEqual(snapshots, [gauge] + [normal] * len(invalid_frames)
                         + [gauge, normal])

    def test_override_changes_and_exact_recovery(self):
        from unittest.mock import patch
        pixels = FakeNeoPixel(3, 16)
        a = [(255, 0, 0)] * 16
        b = [(0, 255, 0)] * 4 + [(0, 0, 0)] * 12
        invalid = [(1, 2, 3)]
        inputs = [a, b, list(b), invalid, invalid, a, invalid, None]
        snapshots = []
        calls = []

        def effect(strip, state):
            calls.append((list(strip), state))
            strip[0] = (0, 0, 99)
            return 1

        async def sleep_ms(_delay):
            snapshots.append((list(pixels), pixels.writes))
            if len(snapshots) == len(inputs):
                raise asyncio.CancelledError()

        with patch.object(rgb_leds, "LED_EFFECTS", [("test", effect)]):
            with patch.object(rgb_leds.asyncio, "sleep_ms", sleep_ms, create=True):
                with patch("builtins.print") as warning:
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(rgb_leds.neopixel_task(
                            pixels, Parameter(0), Parameter(100), Parameter(180),
                            Parameter(100), Parameter(30),
                            override_frame=lambda: inputs[len(snapshots)]))
                    self.assertEqual(warning.call_count, 2)
        normal = [(0, 0, 99)] + [(0, 0, 0)] * 15
        self.assertEqual([frame for frame, _ in snapshots],
                         [a, b, b, normal, normal, a, normal, normal])
        self.assertEqual([writes for _, writes in snapshots],
                         [1, 2, 2, 3, 4, 5, 6, 7])
        self.assertEqual(calls[0], ([(0, 0, 0)] * 16, None))
        self.assertEqual(calls[2], ([(0, 0, 0)] * 16, None))

    def test_override_cancels_interrupted_startup(self):
        from unittest.mock import patch
        pixels = FakeNeoPixel(3, 16)
        gauge = [(255, 0, 0)] * 16
        inputs = [None, gauge, None, None]
        snapshots = []

        async def sleep_ms(_delay):
            snapshots.append(list(pixels))
            if len(snapshots) == len(inputs):
                raise asyncio.CancelledError()

        with patch.object(rgb_leds.asyncio, "sleep_ms", sleep_ms, create=True):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(rgb_leds.neopixel_task(
                    pixels, Parameter(0), Parameter(100), Parameter(180),
                    Parameter(100), Parameter(30),
                    override_frame=lambda: inputs[len(snapshots)]))
        self.assertEqual(snapshots[0], [(0, 255, 255)] + [(0, 0, 0)] * 15)
        self.assertEqual(snapshots[1], gauge)
        self.assertEqual(snapshots[2:], [[(0, 0, 0)] * 16] * 2)

    def test_ui_module_contains_no_led_effect_implementations(self):
        source = (ROOT / "software" / "bsides.py").read_text(encoding="utf-8")
        self.assertNotIn("def led_eff_", source)
        self.assertNotIn("import neopixel", source)


if __name__ == "__main__":
    unittest.main()
