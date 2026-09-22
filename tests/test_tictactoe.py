import asyncio
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


class FakeUart:
    """One end of a cable. Lines written here arrive at .peer, unless the
    cable is unplugged or the lossy filter drops or corrupts them."""

    def __init__(self, *_args, **_kwargs):
        self.rx = b""
        self.peer = None
        self.plugged = True
        self.loss = 0.0
        self.sent = []
        self.closed = False

    def write(self, data):
        self.sent.append(data)
        if not self.peer or not self.plugged or not self.peer.plugged:
            return
        roll = random.random()
        if roll < self.loss:
            return
        if roll < self.loss * 1.5:
            data = bytes([data[0], data[1] ^ 0x01]) + data[2:]   # bit flip
        self.peer.rx += data

    def any(self):
        return len(self.rx)

    def read(self, n):
        data, self.rx = self.rx[:n], self.rx[n:]
        return data

    def deinit(self):
        self.closed = True


async def _sleep_ms(_ms):
    return None


def _install_stubs():
    bsides = types.ModuleType("bsides")
    bsides.BTN_NEXT, bsides.BTN_PREV = 1, 2
    bsides.BTN_SELECT, bsides.BTN_BACK = 3, 4
    bsides.wri6 = _Writer()
    bsides.device_id = "AAAAAAAAAAAA"
    bsides.GamesScreen = lambda oled: "games"
    sys.modules["bsides"] = bsides

    machine = types.ModuleType("machine")
    machine.UART = FakeUart
    machine.Pin = lambda *args, **kwargs: None
    sys.modules["machine"] = machine

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
    "tictactoe_test", ROOT / "software" / "games" / "tictactoe.py")
ttt = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ttt)
sys.modules["time"] = __import__("time")  # restore for unittest

BTN_NEXT, BTN_PREV, BTN_SELECT, BTN_BACK = 1, 2, 3, 4
HIGH_ID, LOW_ID = "FFFF00000001", "0000AAAA0002"


def make(device_id):
    ttt.bsides.device_id = device_id
    return ttt.TicTacToeScreen(_FakeOled())


def wire(a, b):
    a.uart.peer, b.uart.peer = b.uart, a.uart


def pump(games, ms=600):
    for _ in range(ms // ttt.TICK_MS):
        _Clock.now += ttt.TICK_MS
        for g in games:
            g._poll_uart()
            g._tick(_Clock.now)


def press(game, btn):
    _Clock.now += ttt.MOVE_GUARD_MS
    return asyncio.run(game.handle_button(btn))


def play_cell(game, cell, others):
    """Move this badge's cursor onto an empty cell and press SELECT."""
    for _ in range(9):
        if game.cursor == cell:
            break
        press(game, BTN_NEXT)
    assert game.cursor == cell, (game.cursor, cell, game.board)
    press(game, BTN_SELECT)
    pump([game] + others)


class HelperTests(unittest.TestCase):
    def test_winner(self):
        self.assertEqual(ttt.winner(list("---------")), ("-", None))
        self.assertEqual(ttt.winner(list("XXX-OO---")), ("X", (0, 1, 2)))
        self.assertEqual(ttt.winner(list("O-XO-XO--")), ("O", (0, 3, 6)))
        self.assertEqual(ttt.winner(list("X-O-XO--X")), ("X", (0, 4, 8)))
        self.assertEqual(ttt.winner(list("X-O-O-O-X")), ("O", (2, 4, 6)))
        self.assertEqual(ttt.winner(list("XOXXOOOXX")), ("D", None))
        # a full board with a winning line is a win, not a draw
        self.assertEqual(ttt.winner(list("XXXOOXXOO"))[0], "X")

    def test_frames_roundtrip_and_reject_garbage(self):
        line = ttt.encode(b"C12,4,7")
        self.assertEqual(ttt.decode(line), b"C12,4,7")
        self.assertIsNone(ttt.decode(line[:-1]))
        self.assertIsNone(ttt.decode(line.replace(b",7", b",8")))
        self.assertIsNone(ttt.decode(b""))
        self.assertIsNone(ttt.decode(b"T*00"))
        self.assertIsNone(ttt.decode(b"TC1,1,1*ZZ"))
        # Pong traffic on the same cable must never parse
        for pong in (b"HFFFF00000001", b"G", b"P23", b"S60,30,20,1,2,45", b"R"):
            self.assertIsNone(ttt.decode(pong))


class LinkedGameTests(unittest.TestCase):
    def setUp(self):
        random.seed(3)
        _Clock.now = 100000
        self.host = make(HIGH_ID)
        self.guest = make(LOW_ID)
        wire(self.host, self.guest)
        self.both = [self.host, self.guest]
        pump(self.both)

    def assert_in_sync(self):
        self.assertEqual(self.host.board, self.guest.board)
        self.assertEqual(self.host.turn, self.guest.turn)
        self.assertEqual(self.host.game_no, self.guest.game_no)

    def test_higher_id_hosts_and_plays_x(self):
        self.assertTrue(self.host.is_host)
        self.assertFalse(self.guest.is_host)
        self.assertEqual((self.host.my_mark(), self.guest.my_mark()), ("X", "O"))
        self.assertEqual((self.host.phase, self.guest.phase), ("play", "play"))
        self.assertNotEqual(self.guest.game_no, 0)
        self.assertEqual(self.host.turn, "X")
        self.assert_in_sync()

    def test_handshake_does_not_echo_forever(self):
        for g in self.both:
            g.uart.sent.clear()
        pump(self.both, 3000)
        for g in self.both:
            hellos = [m for m in g.uart.sent if m.startswith(b"TH")]
            self.assertEqual(hellos, [])

    def test_full_game_host_wins(self):
        h, g = self.host, self.guest
        play_cell(h, 0, [g])
        play_cell(g, 4, [h])
        play_cell(h, 1, [g])
        play_cell(g, 8, [h])
        play_cell(h, 2, [g])
        self.assert_in_sync()
        self.assertEqual(ttt.winner(g.board), ("X", (0, 1, 2)))
        self.assertEqual((h.wins, h.losses, g.wins, g.losses), (1, 0, 0, 1))

    def test_moves_out_of_turn_or_on_taken_cells_are_ignored(self):
        h, g = self.host, self.guest
        press(g, BTN_SELECT)                 # guest tries to move first
        pump(self.both)
        self.assertEqual(h.board, ["-"] * 9)
        play_cell(h, 4, [g])
        press(h, BTN_SELECT)                 # host tries to move twice
        pump(self.both)
        self.assertEqual(h.board.count("X"), 1)
        # a forged client move onto the taken centre cell
        h._on_client(h.game_no, 4, "4")
        self.assertEqual(h.board[4], "X")
        # a move for some other game number
        h._on_client(h.game_no + 2, 0, "0")
        self.assertEqual(h.board[0], "-")
        self.assert_in_sync()

    def test_cursor_skips_taken_cells_and_hold_is_rate_limited(self):
        h, g = self.host, self.guest
        play_cell(h, 4, [g])
        self.assertNotEqual(g.cursor, 4)
        g.cursor = 3
        press(g, BTN_NEXT)
        self.assertEqual(g.cursor, 5)
        press(g, BTN_PREV)
        self.assertEqual(g.cursor, 3)
        for _ in range(30):                  # 10 ms auto-repeat burst
            _Clock.now += 10
            asyncio.run(g.handle_button(BTN_NEXT))
        self.assertLessEqual(g.cursor, 7)
        self.assertGreaterEqual(g.cursor, 5)

    def test_rematch_from_guest_alternates_starter_and_keeps_score(self):
        h, g = self.host, self.guest
        for who, cell in ((h, 0), (g, 3), (h, 1), (g, 4), (h, 2)):
            play_cell(who, cell, [h if who is g else g])
        first = h.game_no
        press(g, BTN_SELECT)                 # guest asks for a new game
        pump(self.both)
        self.assertEqual(h.game_no, first + 1)
        self.assertEqual(h.board, ["-"] * 9)
        self.assertEqual(h.turn, "O")        # guest starts game two
        self.assertFalse(g.want_rematch)
        self.assert_in_sync()
        for who, cell in ((g, 0), (h, 3), (g, 1), (h, 4), (g, 2)):
            play_cell(who, cell, [h if who is g else g])
        self.assertEqual((h.wins, h.losses), (1, 1))
        self.assertEqual((g.wins, g.losses), (1, 1))

    def test_game_survives_a_lossy_corrupting_link(self):
        h, g = self.host, self.guest
        h.uart.loss = g.uart.loss = 0.5
        moves = ((h, 4), (g, 0), (h, 8), (g, 2), (h, 1), (g, 7), (h, 3), (g, 5), (h, 6))
        for who, cell in moves:
            mark = who.my_mark()
            press_target = cell
            for _ in range(9):
                if who.cursor == press_target:
                    break
                press(who, BTN_NEXT)
            press(who, BTN_SELECT)
            for _ in range(40):
                pump(self.both, 250)
                if h.board[cell] == mark and g.board[cell] == mark:
                    break
            self.assertEqual(h.board[cell], mark)
            self.assertEqual(g.board[cell], mark)
        self.assert_in_sync()
        self.assertEqual(ttt.winner(h.board)[0], "D")
        self.assertEqual((h.draws, g.draws), (1, 1))
        self.assertEqual((h.phase, g.phase), ("play", "play"))

    def test_unplugged_cable_is_detected_and_game_resumes(self):
        h, g = self.host, self.guest
        play_cell(h, 4, [g])
        h.uart.plugged = False
        pump(self.both, ttt.LOST_TIMEOUT_MS + 500)
        self.assertEqual((h.phase, g.phase), ("lost", "lost"))
        h.uart.plugged = True
        pump(self.both, 1500)
        self.assertEqual((h.phase, g.phase), ("play", "play"))
        self.assertEqual(h.board[4], "X")
        self.assert_in_sync()
        play_cell(g, 0, [h])
        self.assertEqual(h.board[0], "O")

    def test_guest_restart_gets_the_board_back(self):
        h = self.host
        play_cell(h, 4, [self.guest])
        play_cell(self.guest, 0, [h])
        asyncio.run(self.guest.handle_button(BTN_BACK))
        guest2 = make(LOW_ID)
        wire(h, guest2)
        pump([h, guest2], 1500)
        self.assertEqual(guest2.phase, "play")
        self.assertEqual(guest2.board, h.board)
        self.assertEqual(guest2.board[4], "X")

    def test_host_restart_starts_fresh_and_drops_stale_guest_move(self):
        h, g = self.host, self.guest
        play_cell(h, 4, [g])
        g.pending = 0                        # move queued as the host vanishes
        asyncio.run(h.handle_button(BTN_BACK))
        host2 = make(HIGH_ID)
        wire(host2, g)
        # Worst case: the random base gives the new host the very game
        # number the guest still holds, so only the hello handshake can
        # expose the restart.
        stale_game = g.game_no
        same_base = (stale_game - 1) // 2 - 1
        with patch.object(ttt.urandom, "getrandbits", lambda bits: same_base):
            pump([host2, g], 1500)
        self.assertEqual(host2.game_no, stale_game)
        self.assertEqual((host2.phase, g.phase), ("play", "play"))
        self.assertEqual(g.game_no, host2.game_no)
        self.assertIsNone(g.pending)
        self.assertEqual(host2.board, ["-"] * 9)
        self.assertEqual(g.board, ["-"] * 9)

    def test_back_releases_the_uart(self):
        self.assertEqual(press(self.host, BTN_BACK), "games")
        self.assertFalse(self.host.running)
        self.assertTrue(self.host.uart.closed)


class LonelyBadgeTests(unittest.TestCase):
    def setUp(self):
        _Clock.now = 100000

    def test_no_peer_times_out_and_select_retries(self):
        g = make(HIGH_ID)
        pump([g], ttt.LINK_TIMEOUT_MS + 500)
        self.assertEqual(g.phase, "nolink")
        press(g, BTN_SELECT)
        self.assertEqual(g.phase, "link")

    def test_late_peer_links_after_timeout(self):
        a = make(HIGH_ID)
        pump([a], ttt.LINK_TIMEOUT_MS + 500)
        b = make(LOW_ID)
        wire(a, b)
        pump([a, b], 1500)
        self.assertEqual((a.phase, b.phase), ("play", "play"))
        self.assertEqual(a.board, b.board)

    def test_identical_ids_are_reported(self):
        a, b = make(HIGH_ID), make(HIGH_ID)
        wire(a, b)
        pump([a, b], 1500)
        self.assertEqual((a.phase, b.phase), ("clash", "clash"))

    def test_pong_on_the_other_end_is_ignored(self):
        g = make(HIGH_ID)
        other = FakeUart()
        g.uart.peer, other.peer = other, g.uart
        for _ in range(20):
            other.write(b"H0000AAAA0002\n")
            other.write(b"G\n")
            pump([g], 300)
        self.assertIn(g.phase, ("link", "nolink"))
        self.assertIsNone(g.is_host)


if __name__ == "__main__":
    unittest.main()
