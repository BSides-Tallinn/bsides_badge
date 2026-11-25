import unittest
from emulator.controller import braille_rows, ring_positions, display_rgb, palette_index

class RenderingTests(unittest.TestCase):
    def test_braille(self):
        frame = bytes([128]) + bytes(1023)
        rows = braille_rows(frame)
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(len(row) == 64 for row in rows))
        self.assertEqual(rows[0][0], chr(0x2801))

    def test_ring(self):
        baseline = [(1, 25), (1, 34), (3, 41), (5, 46), (7, 48), (9, 46), (11, 41), (13, 34), (13, 25), (13, 16), (11, 9), (9, 4), (7, 2), (5, 4), (3, 9), (1, 16)]
        self.assertEqual(ring_positions(), tuple(baseline))
        self.assertEqual(ring_positions(16*10**400), tuple(baseline))
        self.assertIs(ring_positions(), ring_positions(16))
        for offset in range(16):
            for reverse in (False, True):
                actual = ring_positions(offset, reverse)
                self.assertEqual(actual, tuple(baseline[(offset + (-1 if reverse else 1)*i)%16] for i in range(16)))
                cells = [(y, x+j) for y,x in actual for j in range(4)]
                self.assertEqual(len(set(cells)), 64)

    def test_color(self):
        self.assertEqual(display_rgb('#000707'), (0,175,175))
        self.assertEqual(display_rgb('#000707', 'raw'), (0,7,7))
        self.assertEqual(display_rgb('#000000'), (0,0,0))
        self.assertNotEqual(palette_index('#000707',256), palette_index('#000000',256))

    def test_exported_frame_orientation(self):
        frame = bytearray(1024)
        frame[16] = 0x40  # x=1, y=1, right middle Braille dot
        frame[15] = 1  # x=127, y=0
        rows = braille_rows(bytes(frame))
        self.assertEqual(rows[0][0], chr(0x2810))
        self.assertEqual(rows[0][-1], chr(0x2808))

    def test_high_palette_pairs(self):
        from unittest.mock import Mock, patch
        from emulator.controller import color_attr, initialized_pairs, color_attributes
        backend = Mock(COLORS=256, COLOR_PAIRS=256, error=RuntimeError)
        backend.has_colors.return_value = True
        backend.color_pair.side_effect = lambda n: n
        initialized_pairs.clear()
        try:
            with patch("emulator.controller.curses", backend):
                init = backend.init_pair
                self.assertEqual(color_attr("#eeeeee", "raw"), 241)
                self.assertEqual(color_attr("#eeeeee", "raw"), 241)
                init.assert_called_once_with(241, 255, -1)
                self.assertEqual(color_attr("#eeeeee", "raw", 0), 241)
                self.assertEqual(init.call_count, 2)
                init.assert_called_with(241, 255, 0)
        finally:
            initialized_pairs.clear()
            color_attributes.clear()

    def test_palette_preserves_original_order_and_distance(self):
        import random
        levels = (0, 95, 135, 175, 215, 255)
        cube = [(16+36*r+6*g+b, (rv, gv, bv))
                for r, rv in enumerate(levels)
                for g, gv in enumerate(levels)
                for b, bv in enumerate(levels)]
        cube += [(232+i, (8+10*i,)*3) for i in range(24)]
        rng = random.Random(123)
        for colors in (0, 4, 8, 16, 256):
            palette = cube if colors >= 256 else [
                (i, tuple(255 if i & bit else 0 for bit in (1,2,4)))
                for i in range(min(colors, 8))]
            for _ in range(30):
                rgb = f"#{rng.randrange(1<<24):06x}"
                for mode in ("raw", "visible"):
                    target = display_rgb(rgb, mode)
                    expected = min(palette, key=lambda entry: sum(
                        (a-b)**2 for a,b in zip(target, entry[1])))[0] if palette else 0
                    self.assertEqual(palette_index(rgb, colors, mode), expected)
