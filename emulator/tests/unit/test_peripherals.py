import unittest
from unittest.mock import Mock, call
from emulator.peripherals import VirtualBadge, OLED, RGB, Button, parse_rgb

class PeripheralTests(unittest.TestCase):
    def test_cache(self):
        control = Mock()
        frame = bytes([128])*1024
        control.command.side_effect = [frame.hex(), '', '00']
        oled = OLED(control)
        self.assertEqual(oled.poll(), frame)
        self.assertIsNone(oled.poll())
        self.assertEqual(oled.frame, frame)
        with self.assertRaises(ValueError): oled.poll()
        self.assertEqual(oled.frame, frame)

    def test_rgb_order(self):
        fields = sorted(f'{i}=#{i:06x}' for i in range(16))
        control = Mock()
        control.command.return_value = ' '.join(fields + ['frames=510', 'pixels=16'])
        state = RGB(control).state()
        self.assertEqual(state.frames, 510)
        self.assertEqual(state.colors, tuple(f'#{i:06x}' for i in range(16)))

    def test_invalid_rgb(self):
        with self.assertRaises(ValueError): parse_rgb('pixels=1 0=#xyz')

    def test_missing_and_duplicate_counters(self):
        for payload in ("pixels=0", "frames=0", "frames=0 pixels=0 pixels=0", "frames=-1 pixels=0", "frames=0 pixels=1"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_rgb(payload)

    def test_button(self):
        control = Mock()
        Button(control, 'next').press(150)
        self.assertEqual(control.command.call_args_list, [call('part next input press'), call('part next input release after 150')])

    def test_release_fallback(self):
        control = Mock()
        control.command.side_effect = ['', ValueError('rejected'), '']
        with self.assertRaises(ValueError): Button(control, 'next').press()
        control.command.assert_called_with('part next input release')

    def test_discovery_and_reset(self):
        control = Mock()
        control.command.return_value = 'oled next rgb'
        badge = VirtualBadge(control)
        self.assertEqual(set(badge.buttons), {'next'})
        badge.oled.frame = bytes(1024)
        badge.reset()
        self.assertIsNone(badge.oled.frame)
        control.command.assert_called_with('reset')
