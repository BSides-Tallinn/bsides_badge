import asyncio
import importlib.util
import random
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Font:
    @staticmethod
    def height():
        return 14


class _Writer:
    font = _Font()

    @staticmethod
    def stringlen(text):
        return 6 * len(text)

    @staticmethod
    def set_textpos(*_args):
        pass

    @staticmethod
    def printstring(_text):
        pass


class _FakeOled:
    width = 128
    height = 64

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Task:
    def cancel(self):
        pass


class _Clock:
    now = 100000

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_add(a, b):
        return a + b


async def _sleep_ms(_ms):
    return None


def _install_stubs():
    bsides = types.ModuleType("bsides")
    bsides.BTN_NEXT, bsides.BTN_PREV = 1, 2
    bsides.BTN_SELECT, bsides.BTN_BACK = 3, 4
    bsides.wri6 = _Writer()
    bsides.Screen = type("Screen", (), {"__init__": lambda self, oled: setattr(self, "oled", oled)})
    bsides.GamesScreen = lambda oled: "games"
    bsides.save_params = lambda: None
    sys.modules["bsides"] = bsides

    uasyncio = types.ModuleType("uasyncio")
    uasyncio.create_task = lambda coro: (coro.close(), _Task())[1]
    uasyncio.sleep_ms = _sleep_ms
    uasyncio.CancelledError = Exception
    sys.modules["uasyncio"] = uasyncio

    sys.modules.setdefault(
        "urandom", types.SimpleNamespace(getrandbits=lambda bits: random.getrandbits(bits)))
    time_stub = types.ModuleType("time")
    time_stub.ticks_ms = _Clock.ticks_ms
    time_stub.ticks_diff = _Clock.ticks_diff
    time_stub.ticks_add = _Clock.ticks_add
    sys.modules["time"] = time_stub


_install_stubs()
SPEC = importlib.util.spec_from_file_location(
    "flappy_test", ROOT / "software" / "games" / "flappy.py")
flappy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(flappy)
sys.modules["time"] = __import__("time")  # restore for unittest

BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4


def press(game, btn):
    return asyncio.run(game.handle_button(btn))


def next_pipe(game):
    for p in game.pipes:
        if p[0] + flappy.PIPE_W >= flappy.BIRD_X:
            return p
    return None


BOT_MIN_FRAMES_BETWEEN_FLAPS = 4    # 200 ms, a comfortable human tap rate


def bot_should_flap(game):
    """Flap when the next frame would sink past the lower part of the gap."""
    p = next_pipe(game)
    aim = p[1] + p[2] - 3 if p else game.mid_y + flappy.BIRD_H
    fall = min(flappy.MAX_FALL, game.vy + flappy.GRAVITY)
    return game.y + flappy.BIRD_H + fall > aim


class HelperTests(unittest.TestCase):
    def test_gap_shrinks_but_never_below_minimum(self):
        self.assertEqual(flappy.gap_for_score(0), flappy.GAP_START)
        self.assertLess(flappy.gap_for_score(40), flappy.GAP_START)
        self.assertEqual(flappy.gap_for_score(100000), flappy.GAP_MIN)
        self.assertGreater(flappy.GAP_MIN, flappy.BIRD_H * 2)

    def test_pipe_collision(self):
        top, bottom = 14, 62
        x = flappy.BIRD_X
        self.assertFalse(flappy.bird_hits_pipe(30, x, 28, 20, top, bottom))
        self.assertTrue(flappy.bird_hits_pipe(20, x, 28, 20, top, bottom))
        self.assertTrue(flappy.bird_hits_pipe(46, x, 28, 20, top, bottom))
        # touching edges exactly is still clear
        self.assertFalse(flappy.bird_hits_pipe(28, x, 28, 20, top, bottom))
        self.assertFalse(flappy.bird_hits_pipe(48 - flappy.BIRD_H, x, 28, 20, top, bottom))
        # pipe not yet reached / already passed
        self.assertFalse(flappy.bird_hits_pipe(20, x + flappy.BIRD_W, 28, 20, top, bottom))
        self.assertFalse(flappy.bird_hits_pipe(20, x - flappy.PIPE_W, 28, 20, top, bottom))


class GameTests(unittest.TestCase):
    def setUp(self):
        random.seed(11)
        _Clock.now = 100000
        self.game = flappy.GameScreen(_FakeOled())

    def test_waits_for_first_flap(self):
        g = self.game
        for _ in range(40):
            g._step()
        self.assertEqual(g.phase, "ready")
        self.assertEqual(g.pipes, [])
        self.assertLess(abs(g.y - g.mid_y), 4)
        press(g, BTN_SELECT)
        self.assertEqual(g.phase, "play")
        self.assertEqual(g.vy, flappy.FLAP_V)

    def test_gravity_then_ground_ends_game_and_saves_high_score(self):
        g = self.game
        saved = []
        flappy.bsides.flappy_high_score = types.SimpleNamespace(value=0)
        flappy.bsides.save_params = lambda: saved.append(True)
        g.phase, g.score, g.y, g.vy = "play", 7, float(g.ground - 12), 0.0
        for _ in range(60):
            g._step()
            if g.phase == "over":
                break
        self.assertEqual(g.phase, "over")
        self.assertEqual(g.high_score, 7)
        self.assertEqual(flappy.bsides.flappy_high_score.value, 7)
        self.assertTrue(saved)

    def test_ceiling_is_safe(self):
        g = self.game
        g.phase = "play"
        for _ in range(8):
            g._flap()
            g._step()
        self.assertEqual(g.phase, "play")
        self.assertGreaterEqual(g.y, g.top)

    def test_pipe_hit_drops_bird_then_game_over(self):
        g = self.game
        g.phase = "play"
        g.pipes = [[float(flappy.BIRD_X + 1), 40, 20, False]]
        g.y, g.vy = float(g.top + 2), 0.0
        g._step()
        self.assertEqual(g.phase, "dying")
        x_before = g.pipes[0][0]
        for _ in range(60):
            g._step()
        self.assertEqual(g.phase, "over")
        self.assertEqual(g.pipes[0][0], x_before)

    def test_passing_a_pipe_scores_once(self):
        g = self.game
        g.phase = "play"
        g.pipes = [[float(flappy.BIRD_X - flappy.PIPE_W + 1), g.top + 10, 30, False]]
        g.y, g.vy = float(g.top + 20), -1.0
        g._step()
        g.vy = -1.0
        g._step()
        self.assertEqual(g.score, 1)
        self.assertEqual(g.phase, "play")

    def test_spawned_gaps_stay_inside_playfield_and_within_reach(self):
        g = self.game
        for score in (0, 30, 500):
            g.score = score
            g.pipes = []
            for _ in range(300):
                g._spawn_pipe(128)
                _, gap_top, gap_h, _ = g.pipes[-1]
                self.assertGreaterEqual(gap_top, g.top + flappy.GAP_MARGIN)
                self.assertLessEqual(gap_top + gap_h, g.ground - flappy.GAP_MARGIN)
                if len(g.pipes) > 1:
                    self.assertLessEqual(abs(gap_top - g.pipes[-2][1]),
                                         flappy.GAP_MAX_JUMP)
                g.pipes = g.pipes[-1:]

    def test_held_next_does_not_repeat_flaps(self):
        g = self.game
        press(g, BTN_NEXT)
        self.assertEqual(g.vy, flappy.FLAP_V)
        _Clock.now += 500
        for _ in range(50):            # auto-repeat stream, 10 ms apart
            g.vy = 1.0
            press(g, BTN_NEXT)
            self.assertEqual(g.vy, flappy.FLAP_V if _ == 0 else 1.0)
            _Clock.now += 10
        _Clock.now += 200              # released, then a fresh tap
        press(g, BTN_NEXT)
        self.assertEqual(g.vy, flappy.FLAP_V)

    def test_pause_and_resume(self):
        g = self.game
        press(g, BTN_SELECT)
        _Clock.now += 300
        press(g, BTN_PREV)
        self.assertTrue(g.paused)
        _Clock.now += 300
        press(g, BTN_SELECT)
        self.assertFalse(g.paused)

    def test_restart_needs_a_short_delay_after_crash(self):
        g = self.game
        g.phase, g.score = "play", 3
        g.y = float(g.ground)
        g._step()
        self.assertEqual(g.phase, "over")
        press(g, BTN_SELECT)
        self.assertEqual(g.phase, "over")
        _Clock.now += flappy.RESTART_DELAY_MS
        press(g, BTN_SELECT)
        self.assertEqual(g.phase, "ready")
        self.assertEqual(g.score, 0)

    def test_back_exits_to_games_menu(self):
        self.assertEqual(press(self.game, BTN_BACK), "games")
        self.assertFalse(self.game.running)

    def test_game_is_passable_by_a_simple_bot(self):
        for seed in range(5):
            random.seed(seed)
            g = flappy.GameScreen(_FakeOled())
            g.phase = "play"
            last_flap = -99
            for frame in range(20000):
                if frame - last_flap >= BOT_MIN_FRAMES_BETWEEN_FLAPS \
                        and bot_should_flap(g):
                    g._flap()
                    last_flap = frame
                g._step()
                g.render()
                if g.phase != "play" or g.score >= 300:
                    break
            self.assertGreaterEqual(g.score, 300, "seed {}".format(seed))


if __name__ == "__main__":
    unittest.main()
