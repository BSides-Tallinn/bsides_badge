"""Host-side tests for RAM-only SocialBatteryScreen."""

import ast
import asyncio
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "software" / "bsides.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
SCREEN = next(node for node in TREE.body
              if isinstance(node, ast.ClassDef) and node.name == "SocialBatteryScreen")


CONTRAST_SETTINGS = {"SOCIAL_DIM_TIMEOUT_MS", "SOCIAL_DIM_CONTRAST",
                     "OLED_FULL_CONTRAST"}
CONTRAST_NODES = [
    node for node in TREE.body if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id in CONTRAST_SETTINGS
            for target in node.targets)
]


class Screen:
    def __init__(self, oled):
        self.oled = oled


class FakeOled(list):
    def __init__(self):
        super().__init__()
        self.contrast_calls = []

    def contrast(self, value):
        self.contrast_calls.append(value)


class FakeBattery:
    MIN_LEVEL = 1
    MAX_LEVEL = 5
    def draw_oled(self, oled, level):
        oled.append(level)


class MenuScreen(Screen):
    pass


class SocialBatteryScreenTests(unittest.TestCase):
    def setUp(self):
        self.oled = FakeOled()
        self.context = {
            "Screen": Screen,
            "MenuScreen": MenuScreen,
            "social_battery_level": 3,
            "social_battery": FakeBattery(),
            "BTN_NEXT": 1,
            "BTN_PREV": 2,
            "BTN_SELECT": 3,
            "BTN_BACK": 4,
        }
        module = ast.Module(body=CONTRAST_NODES + [SCREEN], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "bsides.py", "exec"),
             self.context)
        self.screen = self.context["SocialBatteryScreen"](self.oled)

    def press(self, button):
        return asyncio.run(self.screen.handle_button(button))

    def test_level_changes_remain_in_ram(self):
        self.press(1)
        self.assertEqual(self.context["social_battery_level"], 4)
        result = self.press(4)
        self.assertIsInstance(result, MenuScreen)
        reopened = self.context["SocialBatteryScreen"](self.oled)
        self.assertEqual(reopened.level, 4)

    def test_select_does_not_write_or_exit(self):
        self.assertIs(self.press(3), self.screen)
        self.assertEqual(self.context["social_battery_level"], 3)

    def test_navigation_clamps_to_five_levels(self):
        for _ in range(10):
            self.press(1)
        self.assertEqual(self.screen.level, 5)
        for _ in range(10):
            self.press(2)
        self.assertEqual(self.screen.level, 1)

    def test_all_button_transitions(self):
        expected = ((1, 2, 1), (2, 3, 1), (3, 4, 2),
                    (4, 5, 3), (5, 5, 4))
        for level, next_level, prev_level in expected:
            for button, target in ((1, next_level), (2, prev_level),
                                   (3, level), (4, level)):
                with self.subTest(level=level, button=button):
                    self.context["social_battery_level"] = level
                    self.screen = self.context["SocialBatteryScreen"](self.oled)
                    result = self.press(button)
                    self.assertEqual(self.screen.level, target)
                    self.assertEqual(self.context["social_battery_level"], target)
                    if button == 4:
                        self.assertIsInstance(result, MenuScreen)
                    else:
                        self.assertIs(result, self.screen)

    def test_frame_cache(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        gauge = Mock(side_effect=lambda *args: list(args))
        self.context.update(screen=self.screen,
                            rgb_leds=SimpleNamespace(NEOPIXEL_COUNT=16),
                            led_brightness=SimpleNamespace(value=10),
                            _social_frame_level=None, _social_frame_brightness=None,
                       _social_frame=None)
        self.context["social_battery"].gauge_pixels = gauge
        node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef)
                    and n.name == "social_battery_frame")
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     "bsides.py", "exec"), self.context)
        frame = self.context["social_battery_frame"]
        first = frame()
        self.assertIs(first, frame())
        self.press(1)
        self.assertIsNot(first, frame())
        self.context["led_brightness"].value = 20
        self.assertEqual(frame(), [4, 16, 20])
        self.assertEqual(gauge.call_count, 3)
        self.context["screen"] = MenuScreen(self.oled)
        self.assertIsNone(frame())

    def test_idle_contrast_and_interaction(self):
        self.screen.update_contrast(59999)
        self.assertEqual(self.oled.contrast_calls, [])
        self.screen.update_contrast(60000)
        self.screen.update_contrast(120000)
        self.screen.update_contrast(-1)  # ticks_diff past its half-period
        self.screen.update_contrast(0)
        self.screen.update_contrast(59999)
        self.assertEqual(self.oled.contrast_calls, [32])
        self.assertIs(self.press(3), self.screen)
        self.assertEqual(self.screen.level, 3)
        self.assertEqual(self.context["social_battery_level"], 3)
        self.assertEqual(self.oled.contrast_calls, [32, 255])
        self.screen.update_contrast(60000)
        self.press(4)
        self.assertEqual(self.oled.contrast_calls, [32, 255, 32, 255])

    def test_render_only_when_level_changes(self):
        self.screen.render()
        self.screen.render()
        self.press(3)
        self.screen.render()
        self.assertEqual(self.oled, [3])
        for button in (1, 1, 1, 2, 2, 2, 2, 2):
            self.press(button)
            self.screen.render()
        self.assertEqual(self.oled, [3, 4, 5, 4, 3, 2, 1])
        self.press(4)
        reopened = self.context["SocialBatteryScreen"](self.oled)
        reopened.render()
        self.assertEqual(self.oled, [3, 4, 5, 4, 3, 2, 1, 1])

    def test_select_restores_contrast_without_rendering(self):
        self.screen.render()
        self.screen.update_contrast(60000)
        self.press(3)
        self.screen.render()
        self.assertEqual(self.oled, [3])
        self.assertEqual(self.oled.contrast_calls, [32, 255])

    def test_failed_render_can_retry(self):
        from unittest.mock import patch

        with patch.object(self.context["social_battery"], "draw_oled",
                          side_effect=OSError("display write failed")):
            with self.assertRaises(OSError):
                self.screen.render()
        self.screen.render()
        self.assertEqual(self.oled, [3])

    def test_render_shows_current_preview(self):
        self.press(2)
        self.screen.render()
        self.assertEqual(self.oled, [2])


class SocialBatteryIntegrationTests(unittest.TestCase):
    def test_menu_ui_and_led_lifecycle(self):
        import importlib.util
        from unittest.mock import Mock, patch
        from types import SimpleNamespace

        # Only the strip size and parameter values are needed here. Avoid
        # importing another test module and its hardware stubs.

        spec = importlib.util.spec_from_file_location(
            "social_battery_integration", ROOT / "software" / "social_battery.py")
        battery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(battery)
        oled = Mock()
        writer = Mock()
        event = Mock()
        context = dict(Screen=Screen, social_battery=battery,
                       rgb_leds=SimpleNamespace(NEOPIXEL_COUNT=16),
                       led_brightness=SimpleNamespace(value=100),
                       wri20=writer, BTN_NEXT=1, BTN_PREV=2,
                       BTN_SELECT=3, BTN_BACK=4, asyncio=asyncio,
                       screen=None, game_active=False,
                       game_lights_off=SimpleNamespace(value=1), button_event=event,
                       _social_frame_level=None, _social_frame_brightness=None,
                       _social_frame=None)
        for name in ("AboutScreen", "SponsorsScreen", "OurteamScreen",
                     "UtilsScreen", "LightsScreen", "BadgeScreen", "GamesScreen"):
            context[name] = type(name, (Screen,), {})
        names = {"SocialBatteryScreen", "MenuScreen", "social_battery_frame",
                 "mute_game_lights", "ui_task"}
        nodes = [node for node in TREE.body
                 if getattr(node, "name", None) in names
                 or (isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name)
                             and t.id == "social_battery_level" for t in node.targets))]
        exec(compile(ast.Module(body=CONTRAST_NODES + nodes, type_ignores=[]),
                     "bsides.py", "exec"), context)
        menu = context["MenuScreen"](oled)
        context["screen"] = menu
        social_index = next(i for i, (label, cls) in enumerate(menu.items)
                            if label == "Social" and cls is context["SocialBatteryScreen"])
        navigate = [1] * social_index
        buttons = navigate + [3, 3, 1, 3, 4] + navigate + [3]
        frames = []
        faces = []
        social_transfers = []
        index = 0

        async def wait():
            nonlocal index
            if index == len(buttons):
                raise asyncio.CancelledError()
            context["last_button"] = buttons[index]
            index += 1

        # Advance the real UI task one event at a time, then sample the
        # production callback. The separate driver tests cover its loop.
        async def one_event():
            await wait()
            event.wait = stop

        async def stop():
            raise asyncio.CancelledError()

        async def scenario():
            for _ in buttons:
                event.wait = one_event
                previous_writes = oled.show.call_count
                with self.assertRaises(asyncio.CancelledError):
                    await context["ui_task"](oled)
                frame = context["social_battery_frame"]()
                frames.append(None if frame is None else list(frame))
                if isinstance(context["screen"], context["SocialBatteryScreen"]):
                    faces.append(oled.blit.call_args.args[0])
                    social_transfers.append(oled.show.call_count - previous_writes)

        with patch.object(battery, "_face_frame",
                          side_effect=lambda i: battery.FACES[i]):
            asyncio.run(scenario())
        neutral = [(255, 200, 0)] * 10 + [(0, 0, 0)] * 6
        happy = [(120, 220, 0)] * 13 + [(0, 0, 0)] * 3
        self.assertEqual(frames, [None] * social_index
                         + [neutral, neutral, happy, happy, None]
                         + [None] * social_index + [happy])
        self.assertEqual(faces, [battery.FACES[2], battery.FACES[2],
                                 battery.FACES[3], battery.FACES[3],
                                 battery.FACES[3]])
        self.assertEqual(social_transfers, [1, 0, 1, 0, 1])
        self.assertEqual(context["social_battery_level"], 4)


if __name__ == "__main__":
    unittest.main()
