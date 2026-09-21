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

    def __init__(self, *_args):
        pass

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Task:
    def cancel(self):
        pass


class _Clock:
    now = 1000

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_add(a, b):
        return a + b


def _install_stubs():
    bsides = types.ModuleType("bsides")
    bsides.BTN_NEXT, bsides.BTN_PREV = 1, 2
    bsides.BTN_SELECT, bsides.BTN_BACK = 3, 4
    bsides.wri6 = _Writer()
    bsides.Screen = type("Screen", (), {"__init__": lambda self, oled: setattr(self, "oled", oled)})
    bsides.GamesScreen = lambda oled: "games"
    bsides.save_params = lambda: None
    bsides.btn_state = {}
    sys.modules["bsides"] = bsides

    uasyncio = types.ModuleType("uasyncio")
    uasyncio.create_task = lambda coro: (coro.close(), _Task())[1]
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
    "tetris_test", ROOT / "software" / "games" / "tetris.py")
tetris = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tetris)
sys.modules["time"] = __import__("time")  # restore for unittest


class PieceTableTests(unittest.TestCase):
    def test_every_rotation_has_four_cells_in_box(self):
        for kind, rotations in enumerate(tetris.PIECES):
            self.assertEqual(len(rotations), 4, kind)
            for cells in rotations:
                self.assertEqual(len(set(cells)), 4, kind)
                for x, y in cells:
                    self.assertTrue(0 <= x < 4 and 0 <= y < 4, (kind, cells))

    def test_rotations_are_connected(self):
        for kind, rotations in enumerate(tetris.PIECES):
            for cells in rotations:
                seen, queue = {cells[0]}, [cells[0]]
                while queue:
                    x, y = queue.pop()
                    for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if n in cells and n not in seen:
                            seen.add(n)
                            queue.append(n)
                self.assertEqual(len(seen), 4, (kind, cells))

    def test_spawn_fits_in_empty_well(self):
        well = [0] * tetris.ROWS
        for kind in range(len(tetris.PIECES)):
            self.assertTrue(tetris.fits(well, tetris.piece_cells(kind, 0, tetris.SPAWN_X, 0)))

    def test_bag_yields_each_piece_once_per_seven(self):
        bag = tetris.Bag()
        for _ in range(3):
            self.assertEqual(sorted(bag.next() for _ in range(7)), list(range(7)))


class WellTests(unittest.TestCase):
    def test_fits_rejects_walls_floor_and_blocks(self):
        well = [0] * tetris.ROWS
        self.assertFalse(tetris.fits(well, [(-1, 0)]))
        self.assertFalse(tetris.fits(well, [(tetris.COLS, 0)]))
        self.assertFalse(tetris.fits(well, [(0, tetris.ROWS)]))
        self.assertTrue(tetris.fits(well, [(0, -1)]))
        well[5] = 1 << 3
        self.assertFalse(tetris.fits(well, [(3, 5)]))
        self.assertTrue(tetris.fits(well, [(4, 5)]))

    def test_lock_and_clear(self):
        well = [0] * tetris.ROWS
        well[14] = tetris.FULL_ROW & ~1
        well[13] = tetris.FULL_ROW & ~1
        well[12] = 1 << 9
        self.assertTrue(tetris.lock(well, [(0, 13), (0, 14)]))
        rows = tetris.full_rows(well)
        self.assertEqual(rows, [13, 14])
        tetris.remove_rows(well, rows)
        self.assertEqual(well[14], 1 << 9)
        self.assertEqual(sum(well[:14]), 0)

    def test_lock_above_top_reports_failure(self):
        well = [0] * tetris.ROWS
        self.assertFalse(tetris.lock(well, [(0, -1), (0, 0)]))
        self.assertEqual(well[0], 1)

    def test_gravity_speeds_up_with_level(self):
        self.assertEqual(tetris.gravity_ms(1), tetris.GRAVITY_BASE_MS)
        self.assertGreater(tetris.gravity_ms(2), tetris.gravity_ms(5))
        self.assertEqual(tetris.gravity_ms(99), tetris.GRAVITY_MIN_MS)


class GameFlowTests(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        _Clock.now = 1000
        tetris.bsides.btn_state.clear()
        self.game = tetris.GameScreen(_FakeOled())

    def test_line_clear_scores_and_spawns_next_piece(self):
        g = self.game
        g.kind, g.rot, g.px, g.py = 0, 0, 3, 0     # horizontal I piece
        g.well[14] = tetris.FULL_ROW & ~0b1111000   # gap under the I piece
        while g._fall():
            pass
        self.assertEqual(g.clearing, [14])
        self.assertEqual(g.score, 0)
        _Clock.now += tetris.CLEAR_FLASH_MS + 1
        g._finish_clear()
        self.assertEqual(g.score, tetris.LINE_POINTS[1])
        self.assertEqual(g.lines, 1)
        self.assertEqual(g.well[14], 0)
        self.assertEqual(g.py, 0)
        self.assertFalse(g.game_over)

    def test_level_rises_every_ten_lines(self):
        g = self.game
        g.lines = 9
        g.clearing = [14]
        g.well[14] = tetris.FULL_ROW
        g._finish_clear()
        self.assertEqual(g.level, 2)

    def test_rotation_kicks_off_the_wall(self):
        g = self.game
        g.kind, g.rot, g.px, g.py = 0, 1, -2, 3     # vertical I hugging the left wall
        self.assertTrue(tetris.fits(g.well, g._cells()))
        self.assertTrue(g._rotate())
        self.assertTrue(tetris.fits(g.well, g._cells()))

    def test_stacking_to_the_top_ends_game_and_saves_high_score(self):
        g = self.game
        saved = []
        tetris.bsides.tetris_high_score = types.SimpleNamespace(value=0)
        tetris.bsides.save_params = lambda: saved.append(True)
        g.score = 500
        for y in range(1, tetris.ROWS):
            g.well[y] = tetris.FULL_ROW & ~1
        g.kind, g.rot, g.px, g.py = 1, 0, 3, 0   # O piece resting on the stack
        g._fall()
        self.assertTrue(g.game_over)
        self.assertEqual(g.high_score, 500)
        self.assertEqual(tetris.bsides.tetris_high_score.value, 500)
        self.assertTrue(saved)

    def test_random_play_never_crashes(self):
        g = self.game
        for _ in range(3000):
            action = random.random()
            if action < 0.3:
                g._try(g.rot, g.px + random.choice((-1, 1)), g.py)
            elif action < 0.4:
                g._rotate()
            g._fall()
            if g.clearing:
                g._finish_clear()
            g.render()
            if g.game_over:
                break
            self.assertTrue(tetris.fits(g.well, g._cells()))
        self.assertTrue(g.game_over)


if __name__ == "__main__":
    unittest.main()
