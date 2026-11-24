import importlib.util
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "social_battery_test", ROOT / "software" / "social_battery.py")
battery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(battery)


class FakeOled:
    def __init__(self):
        self.calls = []
    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
        return record


class SocialBatteryTests(unittest.TestCase):
    def test_level_mapping(self):
        for level, count in enumerate((4, 7, 10, 13, 16), 1):
            with self.subTest(level=level):
                pixels = battery.gauge_pixels(level, brightness=100)
                self.assertEqual(sum(p != (0, 0, 0) for p in pixels), count)
                self.assertEqual(pixels[count:], [(0, 0, 0)] * (16 - count))
                self.assertEqual(len(set(pixels[:count])), 1)

    def test_colors_and_brightness(self):
        self.assertEqual(battery.gauge_pixels(1, brightness=100)[0], (255, 0, 0))
        self.assertEqual(battery.gauge_pixels(3, brightness=100)[0], (255, 200, 0))
        self.assertEqual(battery.gauge_pixels(5, brightness=100)[0], (0, 255, 0))
        self.assertEqual(battery.gauge_pixels(1, brightness=0), [(0, 0, 0)] * 16)

    def test_complete_gauge_contract(self):
        cases = ((4, (255, 0, 0)), (7, (255, 90, 0)),
                 (10, (255, 200, 0)), (13, (120, 220, 0)),
                 (16, (0, 255, 0)))
        for level, (count, color) in enumerate(cases, 1):
            for brightness, effective in ((-1, 0), (0, 0), (10, 10),
                                          (50, 50), (100, 100), (101, 100)):
                with self.subTest(level=level, brightness=brightness):
                    expected = tuple(c * effective // 100 for c in color)
                    self.assertEqual(battery.gauge_pixels(level, brightness=brightness),
                                     [expected] * count + [(0, 0, 0)] * (16 - count))
        for invalid in (None, 0, 6, "3", True):
            self.assertEqual(battery.gauge_pixels(invalid),
                             [(25, 20, 0)] * 10 + [(0, 0, 0)] * 6)

    def test_reject_invalid_levels(self):
        for invalid in (None, 0, 6, "3", 2.5, True):
            self.assertEqual(battery.normalize_level(invalid), 3)

    def test_small_pixel_count(self):
        frame = battery.gauge_pixels(2, count=8)
        self.assertEqual(len(frame), 8)
        self.assertEqual(sum(pixel != (0, 0, 0) for pixel in frame), 4)

    def test_face_bitmaps(self):
        self.assertEqual(len(battery.FACES), 5)
        self.assertEqual(len(set(map(bytes, battery.FACES))), 5)
        for face in battery.FACES:
            self.assertEqual(len(face), 300)

    def test_face_bitmap_regression(self):
        # FNV-1a fingerprints freeze the current artwork, not its visual quality.
        expected = (0x9b66cbb3, 0x580e7223, 0x104f74b5, 0x13e31839, 0x9ecd715b)
        actual = []
        for face in battery.FACES:
            value = 2166136261
            for byte in face:
                value = ((value ^ byte) * 16777619) & 0xffffffff
            actual.append(value)
        self.assertEqual(tuple(actual), expected)

    def test_face_symmetry(self):
        for level, face in enumerate(battery.FACES, 1):
            with self.subTest(level=level):
                for y in range(50):
                    for x in range(24):
                        mirror = 47 - x
                        left = bool(face[y * 6 + x // 8] & (128 >> (x % 8)))
                        right = bool(face[y * 6 + mirror // 8] & (128 >> (mirror % 8)))
                        self.assertEqual(left, right, (level, x, y))

    def test_face_selection(self):
        for level in range(1, 6):
            oled = FakeOled()
            with patch.object(battery, "_face_frame", side_effect=lambda index: battery.FACES[index]):
                battery.draw_oled(oled, level)
            self.assertEqual([args for name, args in oled.calls if name == "blit"],
                             [(battery.FACES[level - 1], 77, 7)])

    def test_framebuffer_cache(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        factory = Mock(side_effect=lambda *args: object())
        with patch.dict("sys.modules", {"framebuf": SimpleNamespace(
                FrameBuffer=factory, MONO_HLSB=3)}):
            with patch.object(battery, "_frames", {}):
                first = battery._face_frame(0)
                self.assertIs(first, battery._face_frame(0))
                self.assertIsNot(first, battery._face_frame(1))
                self.assertEqual(factory.call_count, 2)
                self.assertIs(factory.call_args_list[0].args[0], battery.FACES[0])
                self.assertEqual(factory.call_args_list[0].args,
                                 (battery.FACES[0], 48, 50, 3))

    def test_expression_features(self):
        def lit(level, x, y):
            return bool(battery.FACES[level - 1][y * 6 + x // 8]
                        & (128 >> (x % 8)))
        self.assertTrue(lit(1, 19, 21))
        self.assertTrue(lit(1, 28, 21))
        self.assertTrue(lit(1, 24, 31))
        self.assertFalse(lit(1, 24, 21))
        self.assertTrue(lit(2, 24, 35))
        self.assertFalse(lit(3, 24, 35))
        self.assertTrue(lit(5, 24, 31))
        self.assertFalse(lit(4, 24, 31))

    def test_oled_title_and_face(self):
        for level in range(1, 6):
            oled = FakeOled()
            with patch.object(battery, "_face_frame", return_value=object()):
                battery.draw_oled(oled, level)
            self.assertIn(("text", ("My Social", 0, 21, 1)), oled.calls)
            self.assertIn(("text", ("Battery", 8, 35, 1)), oled.calls)
            self.assertEqual(sum(call[0] == "text" for call in oled.calls), 2)
            self.assertEqual([name for name, _ in oled.calls],
                             ["fill", "text", "text", "blit", "show"])
            self.assertEqual(oled.calls[0], ("fill", (0,)))


if __name__ == "__main__":
    unittest.main()
