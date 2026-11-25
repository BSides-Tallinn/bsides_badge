import unittest
from emulator.dashboard import layout, MIN_WIDTH, MIN_HEIGHT
from emulator.controller import ring_positions


class DashboardTests(unittest.TestCase):
    def test_minimum(self):
        self.assertIsNone(layout(MIN_HEIGHT-1, MIN_WIDTH))
        self.assertIsNone(layout(MIN_HEIGHT, MIN_WIDTH-1))

    def test_geometry(self):
        for height, width in ((MIN_HEIGHT, MIN_WIDTH), (32, 130), (40, 180)):
            panels = layout(height, width)
            cells = set()
            for p in panels.values():
                self.assertGreaterEqual(p.y, 1)
                self.assertLessEqual(p.y+p.height, height-1)
                self.assertLessEqual(p.x+p.width, width)
                occupied = {(y, x) for y in range(p.y, p.y+p.height)
                            for x in range(p.x, p.x+p.width)}
                self.assertFalse(cells & occupied)
                cells.update(occupied)
            self.assertGreaterEqual(panels["oled"].width-2, 64)
            self.assertGreaterEqual(panels["oled"].height-2, 16)
            self.assertGreaterEqual(panels["telemetry"].height-2, 10)
            led = panels["led"]
            offset = (led.width-52)//2
            for y, x in ring_positions():
                self.assertGreaterEqual(y, 0)
                self.assertLess(y, led.height-2)
                self.assertNotEqual(y, 14, 'Ring must not overlap LED status row')
                self.assertGreaterEqual(offset+x, 1)
                self.assertLess(offset+x+3, led.width-1)

    def test_layout_cache_is_read_only_and_bounded(self):
        layout.cache_clear()
        first = layout(MIN_HEIGHT, MIN_WIDTH)
        self.assertIs(first, layout(MIN_HEIGHT, MIN_WIDTH))
        with self.assertRaises(TypeError):
            first["oled"] = None
        second = layout(MIN_HEIGHT+1, MIN_WIDTH)
        self.assertIsNot(first, second)
        self.assertEqual(layout.cache_info().currsize, 1)
