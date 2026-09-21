import importlib.util
import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def __init__(self, *_args):
        pass

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Task:
    def cancel(self):
        pass


def _install_stubs():
    bsides = types.ModuleType("bsides")
    bsides.BTN_NEXT, bsides.BTN_PREV = 1, 2
    bsides.BTN_SELECT, bsides.BTN_BACK = 3, 4
    bsides.wri6 = _Writer()
    bsides.Screen = type("Screen", (), {"__init__": lambda self, oled: setattr(self, "oled", oled)})
    bsides.GamesScreen = lambda oled: "games"
    bsides.save_params = lambda: None
    sys.modules["bsides"] = bsides

    framebuf = types.ModuleType("framebuf")
    framebuf.MONO_VLSB = 0
    framebuf.FrameBuffer = _FakeOled
    sys.modules["framebuf"] = framebuf

    uasyncio = types.ModuleType("uasyncio")
    uasyncio.create_task = lambda coro: (coro.close(), _Task())[1]
    uasyncio.CancelledError = Exception
    sys.modules["uasyncio"] = uasyncio

    sys.modules.setdefault(
        "urandom", types.SimpleNamespace(getrandbits=lambda bits: random.getrandbits(bits)))
    time_stub = types.ModuleType("time")
    time_stub.ticks_ms = lambda: 0
    time_stub.ticks_diff = lambda a, b: a - b
    sys.modules["time"] = time_stub


_install_stubs()
SPEC = importlib.util.spec_from_file_location(
    "pacman_test", ROOT / "software" / "games" / "pacman.py")
pacman = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pacman)
sys.modules["time"] = __import__("time")  # restore for unittest


def _pac_neighbours(x, y):
    out = []
    for d in range(4):
        nx, ny = pacman.step(x, y, d)
        if pacman.pac_passable(pacman.cell_at(nx, ny)):
            out.append((nx, ny))
    return out


class MazeLayoutTests(unittest.TestCase):
    def test_dimensions(self):
        self.assertEqual(len(pacman.MAZE), pacman.GRID_H)
        for row in pacman.MAZE:
            self.assertEqual(len(row), pacman.GRID_W)
        self.assertEqual(pacman.GRID_W * 4, 128)
        self.assertLessEqual(pacman.GRID_H * 4 + _Font.height(), 64)

    def test_symmetric(self):
        for row in pacman.MAZE:
            self.assertEqual(row, row[::-1])

    def test_tunnel_open_both_sides(self):
        row = pacman.MAZE[pacman.TUNNEL_ROW]
        self.assertEqual(row[0], " ")
        self.assertEqual(row[-1], " ")
        self.assertEqual(pacman.step(0, pacman.TUNNEL_ROW, 2),
                         (pacman.GRID_W - 1, pacman.TUNNEL_ROW))

    def test_every_dot_reachable_from_start(self):
        start = pacman.PAC_START
        self.assertTrue(pacman.pac_passable(pacman.cell_at(*start)))
        seen, queue = {start}, [start]
        while queue:
            for n in _pac_neighbours(*queue.pop()):
                if n not in seen:
                    seen.add(n)
                    queue.append(n)
        dots = {(x, y) for y in range(pacman.GRID_H) for x in range(pacman.GRID_W)
                if pacman.MAZE[y][x] in ".o"}
        self.assertEqual(len(dots), pacman.count_dots())
        self.assertTrue(dots <= seen)

    def test_no_dead_ends(self):
        for y in range(pacman.GRID_H):
            for x in range(pacman.GRID_W):
                if pacman.pac_passable(pacman.MAZE[y][x]):
                    self.assertGreaterEqual(len(_pac_neighbours(x, y)), 2, (x, y))

    def test_ghost_house_shape(self):
        for x, y in pacman.GHOST_STARTS:
            self.assertEqual(pacman.MAZE[y][x], "H")
        hx, hy = pacman.HOUSE_IN
        ox, oy = pacman.HOUSE_OUT
        self.assertEqual(pacman.MAZE[hy][hx], "H")
        self.assertEqual(pacman.MAZE[hy - 1][hx], "-")
        self.assertTrue(pacman.pac_passable(pacman.MAZE[oy][ox]))
        self.assertFalse(pacman.pac_passable("-"))
        self.assertFalse(pacman.pac_passable("H"))
        self.assertFalse(pacman.ghost_passable("-", False))
        self.assertTrue(pacman.ghost_passable("-", True))

    def test_eyes_can_get_home_from_anywhere(self):
        dist = pacman.house_distances()
        for y in range(pacman.GRID_H):
            for x in range(pacman.GRID_W):
                if pacman.pac_passable(pacman.MAZE[y][x]):
                    self.assertNotEqual(dist[y * pacman.GRID_W + x], 0xFF, (x, y))


class GameLogicTests(unittest.TestCase):
    def setUp(self):
        random.seed(1)
        self.game = pacman.GameScreen(_FakeOled())

    def test_ghosts_leave_house(self):
        g = self.game
        g.phase = "play"
        for _ in range(60):
            g._tick()
            if g.phase == "dying":
                g.phase = "play"
        for ghost in g.ghosts:
            self.assertNotEqual(ghost.state, pacman.G_WAIT)
            self.assertNotEqual(pacman.cell_at(ghost.x, ghost.y), "#")

    def test_eaten_ghost_returns_and_leaves_again(self):
        g = self.game
        ghost = g.ghosts[0]
        ghost.x, ghost.y = 1, 1
        ghost.state = pacman.G_EYES
        for _ in range(80):
            g._step_eyes(ghost)
            if ghost.state != pacman.G_EYES:
                break
        self.assertEqual((ghost.x, ghost.y), pacman.HOUSE_IN)
        self.assertEqual(ghost.state, pacman.G_LEAVE)
        for _ in range(10):
            g._step_leave(ghost)
            if ghost.state == pacman.G_NORMAL:
                break
        self.assertEqual(ghost.state, pacman.G_NORMAL)
        self.assertEqual((ghost.x, ghost.y), pacman.HOUSE_OUT)

    def test_pellet_frightens_and_ghost_is_eaten(self):
        g = self.game
        g.phase = "play"
        ghost = g.ghosts[0]
        ghost.state = pacman.G_NORMAL
        ghost.x, ghost.y = 2, 1
        g.pac_x, g.pac_y = 1, 1
        g.dots[1][1] = ord("o")
        g.pac_d = 3  # up, blocked, so pacman stays put
        g._eat(1, 1)
        self.assertTrue(ghost.scared)
        self.assertEqual(g.score, pacman.PELLET_POINTS)
        ghost.x, ghost.y = 1, 1
        self.assertFalse(g._check_collisions())
        self.assertEqual(ghost.state, pacman.G_EYES)
        self.assertEqual(g.score, pacman.PELLET_POINTS + pacman.GHOST_POINTS)

    def test_collision_costs_a_life_and_game_over(self):
        g = self.game
        g.phase = "play"
        ghost = g.ghosts[0]
        ghost.state = pacman.G_NORMAL
        lives = g.lives
        for _ in range(lives):
            ghost.state = pacman.G_NORMAL
            ghost.x, ghost.y = g.pac_x, g.pac_y
            self.assertTrue(g._check_collisions())
            self.assertEqual(g.phase, "dying")
            for _ in range(pacman.DYING_TICKS):
                g._tick()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)

    def test_clearing_dots_advances_level(self):
        g = self.game
        g.phase = "play"
        g.dots_left = 1
        g.dots[10][14] = ord(".")
        g.pac_x, g.pac_y, g.pac_d = 15, 10, 2
        for ghost in g.ghosts:
            ghost.state = pacman.G_WAIT
        with patch.object(pacman, "GHOST_RELEASE", (999, 999, 999)):
            g._tick()
            self.assertEqual(g.phase, "clear")
            for _ in range(pacman.CLEAR_TICKS):
                g._tick()
        self.assertEqual(g.level, 2)
        self.assertEqual(g.dots_left, pacman.count_dots())
        self.assertLess(g.tick_ms, pacman.TICK_MS_BASE)

    def test_random_play_never_crashes(self):
        g = self.game
        for _ in range(600):
            if random.random() < 0.2:
                g.pac_want = (g.pac_d + random.choice((1, 3))) % 4
            g._tick()
            g.render()
            if g.game_over:
                break
            for ghost in g.ghosts:
                self.assertNotEqual(pacman.cell_at(ghost.x, ghost.y), "#")
            self.assertTrue(pacman.pac_passable(pacman.cell_at(g.pac_x, g.pac_y)))


if __name__ == "__main__":
    unittest.main()
