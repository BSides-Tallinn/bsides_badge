import time
import urandom
import uasyncio as asyncio

import bsides


GAME_NAME = "Tetris"

BTN_NEXT = bsides.BTN_NEXT
BTN_PREV = bsides.BTN_PREV
BTN_SELECT = bsides.BTN_SELECT
BTN_BACK = bsides.BTN_BACK
wri6 = bsides.wri6

# Well geometry: a 10x15 portrait well of 4x4 px cells in the middle of the
# display, with the next piece and level on the left and scores on the right.
COLS = 10
ROWS = 15
CELL = 4
WELL_X = 44
WELL_Y = 2
FULL_ROW = (1 << COLS) - 1

# Each piece: four rotations, each a tuple of (x, y) offsets in a 4x4 box.
PIECES = (
    # I
    (((0, 1), (1, 1), (2, 1), (3, 1)), ((2, 0), (2, 1), (2, 2), (2, 3)),
     ((0, 2), (1, 2), (2, 2), (3, 2)), ((1, 0), (1, 1), (1, 2), (1, 3))),
    # O
    (((1, 0), (2, 0), (1, 1), (2, 1)),) * 4,
    # T
    (((1, 0), (0, 1), (1, 1), (2, 1)), ((1, 0), (1, 1), (2, 1), (1, 2)),
     ((0, 1), (1, 1), (2, 1), (1, 2)), ((1, 0), (0, 1), (1, 1), (1, 2))),
    # S
    (((1, 0), (2, 0), (0, 1), (1, 1)), ((1, 0), (1, 1), (2, 1), (2, 2)),
     ((1, 1), (2, 1), (0, 2), (1, 2)), ((0, 0), (0, 1), (1, 1), (1, 2))),
    # Z
    (((0, 0), (1, 0), (1, 1), (2, 1)), ((2, 0), (1, 1), (2, 1), (1, 2)),
     ((0, 1), (1, 1), (1, 2), (2, 2)), ((1, 0), (0, 1), (1, 1), (0, 2))),
    # J
    (((0, 0), (0, 1), (1, 1), (2, 1)), ((1, 0), (2, 0), (1, 1), (1, 2)),
     ((0, 1), (1, 1), (2, 1), (2, 2)), ((1, 0), (1, 1), (0, 2), (1, 2))),
    # L
    (((2, 0), (0, 1), (1, 1), (2, 1)), ((1, 0), (1, 1), (1, 2), (2, 2)),
     ((0, 1), (1, 1), (2, 1), (0, 2)), ((0, 0), (1, 0), (1, 1), (1, 2))),
)
KICKS = (0, -1, 1, -2, 2)        # horizontal nudges tried when rotating
SPAWN_X = 3

LINE_POINTS = (0, 40, 100, 300, 1200)
LINES_PER_LEVEL = 10
GRAVITY_BASE_MS = 800
GRAVITY_MIN_MS = 100
GRAVITY_STEP_MS = 70
SOFT_DROP_MS = 50
HOLD_DROP_MS = 300      # hold SELECT this long to start soft dropping
MOVE_GUARD_MS = 90      # minimum gap between auto-repeated moves
CLEAR_FLASH_MS = 180
LOOP_MS = 20


# ---------- pure helpers (unit tested) ----------
def piece_cells(kind, rot, x, y):
    return [(x + dx, y + dy) for dx, dy in PIECES[kind][rot % 4]]


def fits(well, cells):
    """True when no cell is outside the well or on a locked block.

    Cells above the top row are allowed so pieces can spawn partly hidden."""
    for cx, cy in cells:
        if cx < 0 or cx >= COLS or cy >= ROWS:
            return False
        if cy >= 0 and well[cy] & (1 << cx):
            return False
    return True


def lock(well, cells):
    """Write cells into the well; False when a cell is above the top."""
    ok = True
    for cx, cy in cells:
        if cy < 0:
            ok = False
        else:
            well[cy] |= 1 << cx
    return ok


def full_rows(well):
    return [y for y in range(ROWS) if well[y] == FULL_ROW]


def remove_rows(well, rows):
    for y in sorted(rows):
        del well[y]
        well.insert(0, 0)


def gravity_ms(level):
    return max(GRAVITY_MIN_MS, GRAVITY_BASE_MS - (level - 1) * GRAVITY_STEP_MS)


class Bag:
    """Seven-piece bag randomizer: every piece once before any repeats."""

    def __init__(self):
        self.items = []

    def next(self):
        if not self.items:
            self.items = list(range(len(PIECES)))
            for i in range(len(self.items) - 1, 0, -1):
                j = urandom.getrandbits(3) % (i + 1)
                self.items[i], self.items[j] = self.items[j], self.items[i]
        return self.items.pop()


class GameScreen(bsides.Screen):
    """
    Tetris for 128x64 SSD1306.
    - Well: 10x15 cells of 4x4 px, next piece and level on the left,
      score and high score on the right.
    - Controls:
        NEXT  -> move right (hold to slide)
        PREV  -> move left (hold to slide)
        SELECT-> rotate; hold to soft drop (or restart on game over)
        BACK  -> exit to menu

    The main UI sees manages_own_render and leaves rendering to the game loop.
    """
    manages_own_render = True

    def __init__(self, oled):
        super().__init__(oled)

        # ----- GAME STATE -----
        self.running = True
        self.game_over = False
        self.score = 0
        self.lines = 0
        self.level = 1

        param = getattr(bsides, "tetris_high_score", None)
        self.high_score = param.value if param else 0

        self.well = [0] * ROWS
        self.bag = Bag()
        self.next_kind = self.bag.next()
        self.kind = 0
        self.rot = 0
        self.px = SPAWN_X
        self.py = 0
        self.clearing = []
        self.clear_until = 0
        self._last_move = 0
        self._select_t = None     # ticks_ms when SELECT went down
        self._last_soft = 0
        self.last_fall = time.ticks_ms()
        self._dirty = False
        self._spawn()

        # Start loop last
        self._task = asyncio.create_task(self._loop())
        self.render()

    # ---------- piece handling ----------
    def _cells(self):
        return piece_cells(self.kind, self.rot, self.px, self.py)

    def _try(self, rot, x, y):
        if fits(self.well, piece_cells(self.kind, rot, x, y)):
            self.rot, self.px, self.py = rot % 4, x, y
            return True
        return False

    def _spawn(self):
        self.kind = self.next_kind
        self.next_kind = self.bag.next()
        self.rot = 0
        self.px = SPAWN_X
        self.py = 0
        if not fits(self.well, self._cells()):
            self._end_game()

    def _rotate(self):
        for k in KICKS:
            if self._try(self.rot + 1, self.px + k, self.py):
                return True
        return False

    def _fall(self):
        """Move the piece down one row; lock it when it cannot move."""
        if self._try(self.rot, self.px, self.py + 1):
            return True
        self._lock_piece()
        return False

    def _lock_piece(self):
        if not lock(self.well, self._cells()):
            self._end_game()
            return
        rows = full_rows(self.well)
        if rows:
            self.clearing = rows
            self.clear_until = time.ticks_add(time.ticks_ms(), CLEAR_FLASH_MS)
        else:
            self._spawn()

    def _finish_clear(self):
        n = len(self.clearing)
        remove_rows(self.well, self.clearing)
        self.clearing = []
        self.score += LINE_POINTS[n] * self.level
        self.lines += n
        self.level = 1 + self.lines // LINES_PER_LEVEL
        self._spawn()

    def _ghost_y(self):
        y = self.py
        while fits(self.well, piece_cells(self.kind, self.rot, self.px, y + 1)):
            y += 1
        return y

    def _end_game(self):
        self.game_over = True
        if self.score > self.high_score:
            self.high_score = self.score
            try:
                bsides.tetris_high_score.value = self.high_score
                bsides.save_params()
            except Exception:
                pass

    def _select_held(self):
        if self._select_t is None:
            return False
        state = getattr(bsides, "btn_state", {})
        if not state.get(BTN_SELECT):
            self._select_t = None
            return False
        return True

    async def _loop(self):
        try:
            while self.running:
                if not self.game_over:
                    now = time.ticks_ms()
                    if self.clearing:
                        if time.ticks_diff(now, self.clear_until) >= 0:
                            self._finish_clear()
                            self.last_fall = now
                            self._dirty = True
                    else:
                        held = self._select_held() and \
                            time.ticks_diff(now, self._select_t) >= HOLD_DROP_MS
                        if held:
                            if time.ticks_diff(now, self._last_soft) >= SOFT_DROP_MS:
                                self._last_soft = now
                                if self._fall():
                                    self.score += 1
                                self.last_fall = now
                                self._dirty = True
                        elif time.ticks_diff(now, self.last_fall) >= gravity_ms(self.level):
                            self._fall()
                            self.last_fall = now
                            self._dirty = True
                    if self._dirty:
                        self.render()
                        self._dirty = False
                await asyncio.sleep_ms(LOOP_MS)
        except asyncio.CancelledError:
            return

    # ---------- drawing ----------
    def _block(self, x, y):
        self.oled.fill_rect(WELL_X + x * CELL, WELL_Y + y * CELL, CELL - 1, CELL - 1, 1)

    def _draw_well(self):
        o = self.oled
        h = WELL_Y + ROWS * CELL
        o.vline(WELL_X - 1, 0, h + 1, 1)
        o.vline(WELL_X + COLS * CELL, 0, h + 1, 1)
        o.hline(WELL_X - 1, h, COLS * CELL + 2, 1)

        for y in range(ROWS):
            row = self.well[y]
            if not row:
                continue
            if y in self.clearing:
                o.fill_rect(WELL_X, WELL_Y + y * CELL, COLS * CELL, CELL, 1)
                continue
            for x in range(COLS):
                if row & (1 << x):
                    self._block(x, y)

        if self.game_over or self.clearing:
            return
        gy = self._ghost_y()
        if gy != self.py:
            for cx, cy in piece_cells(self.kind, self.rot, self.px, gy):
                if cy >= 0:
                    o.pixel(WELL_X + cx * CELL + 1, WELL_Y + cy * CELL + 1, 1)
        for cx, cy in self._cells():
            if cy >= 0:
                self._block(cx, cy)

    def _draw_panels(self):
        o = self.oled
        fh = wri6.font.height()

        # Left: next piece, level, lines
        wri6.set_textpos(o, 0, 2)
        wri6.printstring("NEXT")
        nx, ny = 10, fh + 2
        for dx, dy in PIECES[self.next_kind][0]:
            o.fill_rect(nx + dx * CELL, ny + dy * CELL, CELL - 1, CELL - 1, 1)
        wri6.set_textpos(o, fh + 2 + 4 * CELL + 2, 2)
        wri6.printstring("LV{:d}".format(self.level))
        wri6.set_textpos(o, o.height - fh, 2)
        wri6.printstring("LN{:d}".format(self.lines))

        # Right: score and high score
        rx = WELL_X + COLS * CELL + 3
        wri6.set_textpos(o, 0, rx)
        wri6.printstring("SC")
        wri6.set_textpos(o, fh, rx)
        wri6.printstring("{:d}".format(self.score))
        wri6.set_textpos(o, o.height - 2 * fh, rx)
        wri6.printstring("HI")
        wri6.set_textpos(o, o.height - fh, rx)
        wri6.printstring("{:d}".format(self.high_score))

    def render(self):
        self.oled.fill(0)
        self._draw_well()
        self._draw_panels()
        if self.game_over:
            self._overlay_gameover()
        self.oled.show()

    def _overlay_gameover(self):
        """Two-line overlay centered on the whole display."""
        lines = ["GAME OVER", "SELECT=Restart"]
        pad = 2
        gap = 1
        fh = wri6.font.height()

        trimmed = []
        for s in lines:
            if wri6.stringlen(s) <= self.oled.width - 2 * pad:
                trimmed.append(s)
            else:
                base = s
                while base and wri6.stringlen(base + "...") > self.oled.width - 2 * pad:
                    base = base[:-1]
                trimmed.append((base + "...") if base else "...")
        lines = trimmed

        max_line_w = max(wri6.stringlen(s) for s in lines)
        box_w = min(self.oled.width, max_line_w + 2 * pad)
        box_h = 2 * fh + gap + 2 * pad

        x = (self.oled.width - box_w) // 2
        if x < 0: x = 0
        y = (self.oled.height - box_h) // 2
        if y < 0: y = 0

        self.oled.fill_rect(x, y, box_w, box_h, 0)
        self.oled.rect(x, y, box_w, box_h, 1)

        ty = y + pad
        for s in lines:
            tw = wri6.stringlen(s)
            tx = x + (box_w - tw) // 2
            if tx < 0: tx = 0
            wri6.set_textpos(self.oled, ty, tx)
            wri6.printstring(s)
            ty += fh + gap

    # ---------- input ----------
    async def handle_button(self, btn):
        now = time.ticks_ms()
        if self.game_over:
            if btn == BTN_SELECT:
                # cancel old loop before restart
                try:
                    if self._task:
                        self._task.cancel()
                        await asyncio.sleep_ms(0)
                except Exception:
                    pass
                # re-init fresh
                self.__init__(self.oled)
                return self
        elif not self.clearing:
            if btn in (BTN_NEXT, BTN_PREV):
                if time.ticks_diff(now, self._last_move) >= MOVE_GUARD_MS:
                    self._last_move = now
                    dx = 1 if btn == BTN_NEXT else -1
                    if self._try(self.rot, self.px + dx, self.py):
                        self.render()
            elif btn == BTN_SELECT:
                if self._select_t is None:
                    self._select_t = now
                    self._rotate()
                    self.render()

        if btn == BTN_BACK:
            self.running = False
            try:
                if self._task:
                    self._task.cancel()
            except Exception:
                pass
            return bsides.GamesScreen(self.oled)

        return self
