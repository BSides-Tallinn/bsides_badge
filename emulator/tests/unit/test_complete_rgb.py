import unittest
from emulator.peripherals import CompleteRGB, RGBState


class CompleteRGBTests(unittest.TestCase):
    def test_preserve_complete(self):
        view = CompleteRGB()
        colors = ("#010203",) * 16
        view.observe(RGBState(10, colors))
        view.observe(RGBState(11, colors[:8]))
        self.assertEqual(view.colors, colors)
        self.assertEqual((view.frames, view.incomplete, view.observations), (11, 1, 2))
        view.observe(RGBState(0, colors[:8]))
        self.assertEqual(view.colors, ())
        self.assertEqual((view.incomplete, view.observations), (1, 1))
