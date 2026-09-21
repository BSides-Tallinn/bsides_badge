import time
import urandom
import machine
import uasyncio as asyncio

import bsides


GAME_NAME = "Tic-tac-toe"

# Badge-to-badge link: same wiring as Pong. Hardware UART1 on the UART pads
# (GPIO20/GPIO21 on the ESP32-C3), TX cross-wired to RX, plus GND.
UART_ID = 1
UART_RX = 20
UART_TX = 21
UART_BAUD = 115200

BTN_NEXT = bsides.BTN_NEXT
BTN_PREV = bsides.BTN_PREV
BTN_SELECT = bsides.BTN_SELECT
BTN_BACK = bsides.BTN_BACK
wri6 = bsides.wri6

TICK_MS = 50
HELLO_MS = 300            # hello interval while looking for a peer
BEAT_MS = 250             # state / client heartbeat while playing
LINK_TIMEOUT_MS = 10000
LOST_TIMEOUT_MS = 2000
MOVE_GUARD_MS = 150       # cursor step rate while NEXT/PREV is held
BLINK_TICKS = 6

CELL = 20                 # board is 3x3 cells of 20 px at (BOARD_X, BOARD_Y)
BOARD_X = 2
BOARD_Y = 2
PANEL_X = 66

EMPTY = "-"
WIN_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7),
             (2, 5, 8), (0, 4, 8), (2, 4, 6))


# ---------- pure helpers (unit tested) ----------
def winner(board):
    """Return (result, line): result is 'X', 'O', 'D' for draw or '-'."""
    for line in WIN_LINES:
        a, b, c = line
        if board[a] != EMPTY and board[a] == board[b] == board[c]:
            return board[a], line
    if EMPTY not in board:
        return "D", None
    return EMPTY, None


def checksum(payload):
    c = 0
    for b in payload:
        c ^= b
    return c


def encode(payload):
    """Frame a payload as T<payload>*<xor checksum in hex>.

    The T prefix and checksum make Pong traffic, line noise from plugging the
    cable, and truncated lines all fail to decode instead of becoming moves."""
    return b"T" + payload + ("*%02X" % checksum(payload)).encode()


def decode(line):
    if len(line) < 5 or line[0:1] != b"T" or line[-3:-2] != b"*":
        return None
    payload = line[1:-3]
    try:
        want = int(line[-2:].decode(), 16)
    except (ValueError, UnicodeError):
        return None
    if checksum(payload) != want:
        return None
    return payload


class TicTacToeScreen:
    """
    Two-player tic-tac-toe over the UART link (same cable as Pong).
    The badge with the higher device ID becomes host: it owns the board and
    plays X. The host rebroadcasts the full game state as a heartbeat, and the
    guest repeats its pending move until the board shows it, so dropped or
    corrupted lines never desynchronise the game. The starter alternates.
    Controls:
      NEXT/PREV -> move cursor to the next/previous empty cell
      SELECT    -> place mark on your turn / new game when finished /
                   retry when no peer was found
      BACK      -> exit to menu

    The main UI sees manages_own_render and leaves rendering to the game loop.
    """
    manages_own_render = True

    def __init__(self, oled):
        self.oled = oled
        self.my_id = bsides.device_id

        self.uart = machine.UART(UART_ID, baudrate=UART_BAUD,
                                 tx=machine.Pin(UART_TX),
                                 rx=machine.Pin(UART_RX), timeout=0)

        self.running = True
        self.peer_id = None
        self.is_host = None
        self._buf = b""
        self._last_move = time.ticks_add(time.ticks_ms(), -MOVE_GUARD_MS)
        self.tick = 0
        self.blink = True
        self._dirty = True

        self._clear_game()
        self._start_link()

        self._tasks = [asyncio.create_task(self._loop()),
                       asyncio.create_task(self._rx())]
        self.render()

    # ---------- state helpers ----------
    def _clear_game(self):
        self.game_no = 0              # 0 = no game yet
        self.board = [EMPTY] * 9
        self.turn = "X"
        self.cursor = 4
        self.peer_cursor = -1
        self.pending = None           # guest: move waiting for the host
        self.want_rematch = False     # guest: rematch waiting for the host
        self.counted_game = 0
        self.wins = 0
        self.losses = 0
        self.draws = 0

    def _start_link(self):
        now = time.ticks_ms()
        self.phase = "link"           # link/nolink/lost/clash/play
        self.link_started = now
        self.last_hello = time.ticks_add(now, -HELLO_MS)
        self.last_beat = now
        self.last_rx = now
        self._dirty = True

    def _seeking(self):
        return self.phase in ("link", "nolink", "lost")

    def my_mark(self):
        return "X" if self.is_host else "O"

    def _fix_cursor(self):
        """Keep the cursor on an empty cell while any remain."""
        if self.board[self.cursor] != EMPTY and EMPTY in self.board:
            self._step_cursor(1)

    def _step_cursor(self, step):
        for _ in range(9):
            self.cursor = (self.cursor + step) % 9
            if self.board[self.cursor] == EMPTY:
                return

    def _tally(self):
        result = winner(self.board)[0]
        if result == EMPTY or self.counted_game == self.game_no:
            return
        self.counted_game = self.game_no
        if result == "D":
            self.draws += 1
        elif result == self.my_mark():
            self.wins += 1
        else:
            self.losses += 1

    def _board_changed(self):
        self._tally()
        self._fix_cursor()
        self._dirty = True

    # ---------- link ----------
    def _send(self, payload):
        self.uart.write(encode(payload) + b"\n")

    def _send_hello(self, need):
        self._send(("H%s,%d,%d" % (self.my_id, 1 if need else 0,
                                   self.game_no)).encode())

    def _send_state(self):
        self._send(("S%d,%s,%s,%d" % (self.game_no, "".join(self.board),
                                      self.turn, self.cursor)).encode())
        self.last_beat = time.ticks_ms()

    def _send_client(self):
        if self.want_rematch:
            move = "R"
        elif self.pending is not None:
            move = str(self.pending)
        else:
            move = EMPTY
        self._send(("C%d,%d,%s" % (self.game_no, self.cursor, move)).encode())
        self.last_beat = time.ticks_ms()

    def _enter_play(self):
        self.phase = "play"
        self.last_rx = time.ticks_ms()
        self._dirty = True

    # ---------- host rules ----------
    def _new_game(self):
        if self.game_no == 0:
            # random base so a restarted host never reuses a number the
            # guest still holds a pending move for
            self.game_no = 2 * (1 + urandom.getrandbits(12))
        self.game_no += 1
        self.board = [EMPTY] * 9
        self.turn = "X" if self.game_no % 2 else "O"
        self.cursor = 4
        self._board_changed()
        self._send_state()

    def _place(self, cell, mark):
        if winner(self.board)[0] != EMPTY or self.turn != mark:
            return False
        if not 0 <= cell < 9 or self.board[cell] != EMPTY:
            return False
        self.board[cell] = mark
        self.turn = "O" if mark == "X" else "X"
        self._board_changed()
        self._send_state()
        return True

    # ---------- receive ----------
    def _handle_line(self, line):
        payload = decode(line)
        if not payload:
            return
        tag = payload[0:1]
        try:
            parts = payload[1:].decode().split(",")
            if tag == b"H":
                self._on_hello(parts[0], parts[1] == "1", int(parts[2]))
            elif tag == b"S":
                self._on_state(int(parts[0]), parts[1], parts[2], int(parts[3]))
            elif tag == b"C":
                self._on_client(int(parts[0]), int(parts[1]), parts[2])
            else:
                return
        except (ValueError, IndexError, UnicodeError):
            return

    def _on_hello(self, peer_id, peer_seeking, peer_game):
        self.last_rx = time.ticks_ms()
        if peer_id == self.my_id:
            self.phase = "clash"
            self._dirty = True
            return
        if peer_id != self.peer_id:
            self.peer_id = peer_id
            self._clear_game()
        self.is_host = self.my_id > peer_id
        if not self.is_host and peer_game != self.game_no:
            # The host is on another game (it restarted, or we did). A queued
            # move or rematch belongs to the old game and must not leak into
            # the new one. After a plain reconnect the numbers match and the
            # queue survives.
            self.pending = None
            self.want_rematch = False
        if peer_seeking:
            self._send_hello(False)
        if self._seeking() or self.phase == "clash":
            self._enter_play()
        if self.is_host:
            if self.game_no == 0:
                self._new_game()
            else:
                self._send_state()
        self._dirty = True

    def _on_state(self, game_no, board, turn, cursor):
        if self.is_host is not False:
            return
        if len(board) != 9 or turn not in ("X", "O"):
            return
        for ch in board:
            if ch not in "-XO":
                return
        self.last_rx = time.ticks_ms()
        if self._seeking():
            self._enter_play()
        if self.phase != "play":
            return
        if game_no != self.game_no:
            self.game_no = game_no
            self.pending = None
            self.want_rematch = False
            self.cursor = 4
        new_board = list(board)
        if self.pending is not None and new_board[self.pending] != EMPTY:
            self.pending = None
        changed = new_board != self.board or turn != self.turn \
            or cursor != self.peer_cursor
        self.board = new_board
        self.turn = turn
        self.peer_cursor = cursor
        if changed:
            self._board_changed()

    def _on_client(self, game_no, cursor, move):
        if self.is_host is not True:
            return
        self.last_rx = time.ticks_ms()
        if self._seeking():
            self._enter_play()
        if self.phase != "play":
            return
        if cursor != self.peer_cursor:
            self.peer_cursor = cursor
            self._dirty = True
        if game_no != self.game_no:
            return
        if move == "R":
            if winner(self.board)[0] != EMPTY:
                self._new_game()
        elif move != EMPTY:
            self._place(int(move), "O")

    def _poll_uart(self):
        n = self.uart.any()
        if not n:
            return
        self._buf += self.uart.read(n)
        i = self._buf.find(b"\n")
        while i >= 0:
            self._handle_line(self._buf[:i])
            self._buf = self._buf[i + 1:]
            i = self._buf.find(b"\n")
        if len(self._buf) > 200:
            self._buf = self._buf[-64:]

    # ---------- loops ----------
    def _tick(self, now):
        self.tick += 1
        if self.phase in ("link", "lost", "clash"):
            # keep announcing during a clash so the other badge sees it too
            if time.ticks_diff(now, self.last_hello) >= HELLO_MS:
                self._send_hello(True)
                self.last_hello = now
            if self.phase == "link" and \
                    time.ticks_diff(now, self.link_started) >= LINK_TIMEOUT_MS:
                self.phase = "nolink"
                self._dirty = True
        elif self.phase == "play":
            if time.ticks_diff(now, self.last_rx) >= LOST_TIMEOUT_MS:
                self.phase = "lost"
                self.last_hello = time.ticks_add(now, -HELLO_MS)
                self._dirty = True
            elif time.ticks_diff(now, self.last_beat) >= BEAT_MS:
                if self.is_host:
                    self._send_state()
                else:
                    self._send_client()
            if self.tick % BLINK_TICKS == 0:
                self.blink = not self.blink
                self._dirty = True
        if self._dirty:
            self.render()
            self._dirty = False

    async def _loop(self):
        try:
            while self.running:
                self._tick(time.ticks_ms())
                await asyncio.sleep_ms(TICK_MS)
        except asyncio.CancelledError:
            return

    async def _rx(self):
        try:
            while self.running:
                self._poll_uart()
                await asyncio.sleep_ms(10)
        except asyncio.CancelledError:
            return

    async def _stop(self):
        self.running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.sleep_ms(0)
        try:
            self.uart.deinit()
        except Exception:
            pass

    # ---------- drawing ----------
    def _center6(self, text, y):
        w = wri6.stringlen(text)
        wri6.set_textpos(self.oled, y, max(0, (self.oled.width - w) // 2))
        wri6.printstring(text)

    def _panel(self, text, y):
        wri6.set_textpos(self.oled, y, PANEL_X)
        wri6.printstring(text)

    def _draw_mark(self, cell, mark):
        o = self.oled
        x = BOARD_X + (cell % 3) * CELL
        y = BOARD_Y + (cell // 3) * CELL
        if mark == "X":
            o.line(x + 5, y + 5, x + 14, y + 14, 1)
            o.line(x + 6, y + 5, x + 15, y + 14, 1)
            o.line(x + 14, y + 5, x + 5, y + 14, 1)
            o.line(x + 15, y + 5, x + 6, y + 14, 1)
        elif hasattr(o, "ellipse"):
            o.ellipse(x + 10, y + 10, 6, 6, 1)
            o.ellipse(x + 10, y + 10, 5, 5, 1)
        else:
            o.rect(x + 4, y + 4, 13, 13, 1)
            o.rect(x + 5, y + 5, 11, 11, 1)

    def _draw_board(self):
        o = self.oled
        size = 3 * CELL
        for i in (1, 2):
            o.vline(BOARD_X + i * CELL, BOARD_Y, size, 1)
            o.hline(BOARD_X, BOARD_Y + i * CELL, size, 1)
        for cell in range(9):
            if self.board[cell] != EMPTY:
                self._draw_mark(cell, self.board[cell])

        result, line = winner(self.board)
        if line:
            a, c = line[0], line[2]
            x1 = BOARD_X + (a % 3) * CELL + CELL // 2
            y1 = BOARD_Y + (a // 3) * CELL + CELL // 2
            x2 = BOARD_X + (c % 3) * CELL + CELL // 2
            y2 = BOARD_Y + (c // 3) * CELL + CELL // 2
            o.line(x1, y1, x2, y2, 1)
            if y1 == y2:
                o.line(x1, y1 + 1, x2, y2 + 1, 1)
            else:
                o.line(x1 + 1, y1, x2 + 1, y2, 1)
        if result != EMPTY:
            return

        if 0 <= self.peer_cursor < 9 and self.peer_cursor != self.cursor:
            px = BOARD_X + (self.peer_cursor % 3) * CELL
            py = BOARD_Y + (self.peer_cursor // 3) * CELL
            o.fill_rect(px + 3, py + 3, 2, 2, 1)
        if self.blink:
            cx = BOARD_X + (self.cursor % 3) * CELL
            cy = BOARD_Y + (self.cursor // 3) * CELL
            o.rect(cx + 2, cy + 2, CELL - 3, CELL - 3, 1)

    def _draw_panel(self):
        fh = wri6.font.height()
        mark = self.my_mark()
        self._panel("You: " + mark, 0)
        result = winner(self.board)[0]
        if result == EMPTY:
            self._panel("Your go" if self.turn == mark else "Wait...", fh)
        else:
            if result == "D":
                text = "DRAW"
            else:
                text = "WIN!" if result == mark else "LOST"
            self._panel(text, fh)
            self._panel("SEL=New", 2 * fh)
        self._panel("{:d}:{:d}".format(self.wins, self.losses),
                    self.oled.height - fh)

    def render(self):
        oled = self.oled
        oled.fill(0)
        if self.phase == "link":
            self._center6("TIC-TAC-TOE", 10)
            self._center6("Linking...", 28)
            self._center6("BACK=Exit", 50)
        elif self.phase == "nolink":
            self._center6("No peer found", 16)
            self._center6("SELECT=Retry", 34)
            self._center6("BACK=Exit", 48)
        elif self.phase == "lost":
            self._center6("Link lost", 10)
            self._center6("Reconnecting...", 28)
            self._center6("BACK=Exit", 50)
        elif self.phase == "clash":
            self._center6("Same badge ID", 16)
            self._center6("on both badges", 30)
            self._center6("BACK=Exit", 48)
        elif self.game_no == 0:
            self._center6("TIC-TAC-TOE", 10)
            self._center6("Linked, wait...", 28)
            self._center6("BACK=Exit", 50)
        else:
            self._draw_board()
            self._draw_panel()
        oled.show()

    # ---------- input ----------
    async def handle_button(self, btn):
        if btn == BTN_BACK:
            await self._stop()
            return bsides.GamesScreen(self.oled)

        if self.phase == "nolink":
            if btn == BTN_SELECT:
                self._start_link()
            return self
        if self.phase != "play" or self.game_no == 0:
            return self

        if btn in (BTN_NEXT, BTN_PREV):
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_move) >= MOVE_GUARD_MS:
                self._last_move = now
                self._step_cursor(1 if btn == BTN_NEXT else -1)
                self.blink = True
                self._dirty = True
        elif btn == BTN_SELECT:
            finished = winner(self.board)[0] != EMPTY
            if self.is_host:
                if finished:
                    self._new_game()
                else:
                    self._place(self.cursor, "X")
            elif finished:
                self.want_rematch = True
                self._send_client()
            elif self.turn == "O" and self.board[self.cursor] == EMPTY:
                self.pending = self.cursor
                self._send_client()
        return self


# Contract used by bsides.discover_games().
GameScreen = TicTacToeScreen
