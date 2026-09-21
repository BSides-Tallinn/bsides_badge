import time
import urandom
import framebuf
import uasyncio as asyncio

import bsides


GAME_NAME = "Pacman"

BTN_NEXT = bsides.BTN_NEXT
BTN_PREV = bsides.BTN_PREV
BTN_SELECT = bsides.BTN_SELECT
BTN_BACK = bsides.BTN_BACK
wri6 = bsides.wri6

# Maze legend:
#   '#' wall           '.' dot          'o' power pellet
#   ' ' empty corridor '-' ghost door   'H' ghost house
# 32x12 cells of 4x4 px fill the 128x48 playfield below the HUD. Row 7 is
# the wrap-around tunnel. The layout is left/right symmetric and has no
# dead ends, which keeps the greedy ghost movement from getting stuck.
MAZE = (
    "################################",
    "#o............................o#",
    "#.##.##.##.###.##.###.##.##.##.#",
    "#.......#..............#.......#",
    "#.##.##.#.####.##.####.#.##.##.#",
    "#....##.#.#..........#.#.##....#",
    "#.##.##.#.#.###--###.#.#.##.##.#",
    "  ......#....#HHHH#....#......  ",
    "#.##.##.#.##.######.##.#.##.##.#",
    "#.##....#.##.######.##.#....##.#",
    "#o............................o#",
    "################################",
)
GRID_W = 32
GRID_H = 12
TUNNEL_ROW = 7
PAC_START = (15, 10)
HOUSE_IN = (15, 7)       # cell an eaten ghost returns to
HOUSE_OUT = (15, 5)      # first corridor cell above the door
GHOST_STARTS = ((15, 7), (14, 7), (16, 7))
GHOST_CORNERS = ((30, 1), (1, 1), (1, 10))
GHOST_RELEASE = (0, 12, 24)   # ticks after (re)start before each ghost leaves

DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))   # R, D, L, U

# Timing (game ticks unless noted)
TICK_MS_BASE = 170
TICK_MS_MIN = 90
TICK_MS_STEP = 15
READY_TICKS = 8
DYING_TICKS = 8
CLEAR_TICKS = 8
FRIGHT_TICKS = 35
FRIGHT_BLINK = 10
SCATTER_TICKS = 35
CHASE_TICKS = 120
GHOST_SKIP_EVERY = 5      # normal ghosts pause one tick in five
TURN_GUARD_MS = 120       # ignore auto-repeated turn presses

# Ghost states
G_WAIT = 0     # inside the house, waiting for release
G_LEAVE = 1    # heading out through the door
G_NORMAL = 2   # scatter / chase (or frightened when .scared is set)
G_EYES = 3     # eaten, returning to the house

# Scoring
DOT_POINTS = 10
PELLET_POINTS = 50
GHOST_POINTS = 200
EXTRA_LIFE_AT = 10000
START_LIVES = 3
MAX_LIFE_ICONS = 5


# ---------- maze helpers (pure functions, unit tested) ----------
def cell_at(x, y):
    if 0 <= y < GRID_H:
        return MAZE[y][x]
    return "#"


def step(x, y, d):
    """Next cell in direction d, wrapping horizontally for the tunnel."""
    dx, dy = DIRS[d]
    return (x + dx) % GRID_W, y + dy


def pac_passable(cell):
    return cell == "." or cell == "o" or cell == " "


def ghost_passable(cell, doors):
    if cell == "-":
        return doors
    return cell != "#"


def count_dots():
    return sum(row.count(".") + row.count("o") for row in MAZE)


def house_distances():
    """Breadth-first distance from HOUSE_IN over ghost-passable cells.

    Eaten ghosts follow this map home, so they can never get lost."""
    dist = bytearray(b"\xff" * (GRID_W * GRID_H))
    hx, hy = HOUSE_IN
    dist[hy * GRID_W + hx] = 0
    queue = [HOUSE_IN]
    while queue:
        x, y = queue.pop(0)
        d = dist[y * GRID_W + x]
        for i in range(4):
            nx, ny = step(x, y, i)
            if not ghost_passable(cell_at(nx, ny), True):
                continue
            j = ny * GRID_W + nx
            if dist[j] == 0xFF:
                dist[j] = d + 1
                queue.append((nx, ny))
    return dist


class Ghost:
    def __init__(self, idx):
        self.idx = idx
        self.reset()

    def reset(self):
        self.x, self.y = GHOST_STARTS[self.idx]
        self.px, self.py = self.x, self.y
        self.d = 3
        self.state = G_WAIT
        self.scared = False


class GameScreen(bsides.Screen):
    """
    Pacman for 128x64 SSD1306.
    - Grid: 4x4 px cells, HUD row at top (same layout as Snake).
    - Controls:
        NEXT  -> turn clockwise (relative to heading)
        PREV  -> turn counter-clockwise
        SELECT-> pause/resume (or restart on game over)
        BACK  -> exit to menu
      Turns are buffered and applied at the first cell where they fit.
      Press the same button twice to reverse.

    The main UI sees manages_own_render and leaves rendering to the game loop.
    """
    manages_own_render = True
    CELL = 4

    def __init__(self, oled):
        super().__init__(oled)

        # ----- GEOMETRY -----
        self.HUD_H = wri6.font.height()
        self.GRID_Y0 = self.HUD_H
        self.PF_H = GRID_H * self.CELL

        # ----- GAME STATE -----
        self.running = True
        self.paused = False
        self.game_over = False
        self.score = 0
        self.level = 1
        self.lives = START_LIVES
        self.extra_life_given = False
        self._last_turn = 0

        param = getattr(bsides, "pacman_high_score", None)
        self.high_score = param.value if param else 0

        self.walls = self._build_walls()
        self.house_dist = house_distances()
        self.ghosts = [Ghost(i) for i in range(len(GHOST_STARTS))]
        self._start_level()

        # Start loop last
        self._task = asyncio.create_task(self._loop())
        self.render()

    # ---------- setup ----------
    def _start_level(self):
        self.dots = [bytearray(row.encode()) for row in MAZE]
        self.dots_left = count_dots()
        self.tick_ms = max(TICK_MS_MIN,
                           TICK_MS_BASE - (self.level - 1) * TICK_MS_STEP)
        self._reset_positions()

    def _reset_positions(self):
        self.pac_x, self.pac_y = PAC_START
        self.pac_prev = PAC_START
        self.pac_d = 2            # left
        self.pac_want = None
        self.mouth = 0
        for g in self.ghosts:
            g.reset()
        self.tick = 0
        self.mode_ticks = 0
        self.fright = 0
        self.ghost_chain = 0
        self.phase = "ready"
        self.freeze = READY_TICKS

    def _build_walls(self):
        """Pre-render the maze walls as outlines into a framebuffer."""
        w = self.oled.width
        fb = framebuf.FrameBuffer(bytearray(w * self.PF_H // 8), w, self.PF_H,
                                  framebuf.MONO_VLSB)
        c = self.CELL

        def wall(x, y):
            return 0 <= x < GRID_W and 0 <= y < GRID_H and MAZE[y][x] == "#"

        for y in range(GRID_H):
            for x in range(GRID_W):
                px, py = x * c, y * c
                if MAZE[y][x] == "-":
                    fb.hline(px, py + 1, c, 1)   # ghost house door
                    continue
                if not wall(x, y):
                    continue
                up, down = wall(x, y - 1), wall(x, y + 1)
                left, right = wall(x - 1, y), wall(x + 1, y)
                if not up:
                    fb.hline(px, py, c, 1)
                if not down:
                    fb.hline(px, py + c - 1, c, 1)
                if not left:
                    fb.vline(px, py, c, 1)
                if not right:
                    fb.vline(px + c - 1, py, c, 1)
                # Inner corners of L-shaped wall regions need one pixel.
                if up and left and not wall(x - 1, y - 1):
                    fb.pixel(px, py, 1)
                if up and right and not wall(x + 1, y - 1):
                    fb.pixel(px + c - 1, py, 1)
                if down and left and not wall(x - 1, y + 1):
                    fb.pixel(px, py + c - 1, 1)
                if down and right and not wall(x + 1, y + 1):
                    fb.pixel(px + c - 1, py + c - 1, 1)
        return fb

    # ---------- game logic ----------
    def _add_score(self, n):
        self.score += n
        if self.score >= EXTRA_LIFE_AT and not self.extra_life_given:
            self.extra_life_given = True
            self.lives += 1

    def _end_game(self):
        self.game_over = True
        if self.score > self.high_score:
            self.high_score = self.score
            try:
                bsides.pacman_high_score.value = self.high_score
                bsides.save_params()
            except Exception:
                pass
        self.render()

    def _tick(self):
        if self.phase == "ready":
            self.freeze -= 1
            if self.freeze <= 0:
                self.phase = "play"
            return
        if self.phase == "dying":
            self.freeze -= 1
            if self.freeze <= 0:
                self.lives -= 1
                if self.lives <= 0:
                    self._end_game()
                else:
                    self._reset_positions()
            return
        if self.phase == "clear":
            self.freeze -= 1
            if self.freeze <= 0:
                self.level += 1
                self._start_level()
            return

        self.tick += 1
        self._move_pacman()
        if self._check_collisions():
            return
        self._move_ghosts()
        if self._check_collisions():
            return
        if self.dots_left <= 0:
            self.phase = "clear"
            self.freeze = CLEAR_TICKS

    def _move_pacman(self):
        x, y = self.pac_x, self.pac_y
        if self.pac_want is not None:
            nx, ny = step(x, y, self.pac_want)
            if pac_passable(cell_at(nx, ny)):
                self.pac_d = self.pac_want
                self.pac_want = None
        self.pac_prev = (x, y)
        nx, ny = step(x, y, self.pac_d)
        if pac_passable(cell_at(nx, ny)):
            self.pac_x, self.pac_y = nx, ny
            self.mouth ^= 1
            self._eat(nx, ny)

    def _eat(self, x, y):
        row = self.dots[y]
        c = row[x]
        if c == 46:        # '.'
            row[x] = 32
            self.dots_left -= 1
            self._add_score(DOT_POINTS)
        elif c == 111:     # 'o'
            row[x] = 32
            self.dots_left -= 1
            self._add_score(PELLET_POINTS)
            self.fright = FRIGHT_TICKS
            self.ghost_chain = 0
            for g in self.ghosts:
                if g.state in (G_NORMAL, G_LEAVE):
                    g.scared = True
                    if g.state == G_NORMAL:
                        g.d = (g.d + 2) % 4

    def _move_ghosts(self):
        t = self.tick
        if self.fright > 0:
            self.fright -= 1
            if self.fright == 0:
                for g in self.ghosts:
                    g.scared = False
        else:
            self.mode_ticks += 1
        scatter = (self.mode_ticks % (SCATTER_TICKS + CHASE_TICKS)) < SCATTER_TICKS

        for g in self.ghosts:
            g.px, g.py = g.x, g.y
            if g.state == G_WAIT:
                if t >= GHOST_RELEASE[g.idx]:
                    g.state = G_LEAVE
                continue
            if g.state == G_EYES:
                self._step_eyes(g)
                self._step_eyes(g)
                continue
            if g.scared:
                if t & 1:
                    continue
            elif g.state == G_NORMAL and t % GHOST_SKIP_EVERY == 0:
                continue
            if g.state == G_LEAVE:
                self._step_leave(g)
            else:
                self._step_normal(g, scatter)

    def _step_leave(self, g):
        tx, ty = HOUSE_OUT
        best, best_d = None, 9999
        for d in range(4):
            nx, ny = step(g.x, g.y, d)
            if not ghost_passable(cell_at(nx, ny), True):
                continue
            m = abs(nx - tx) + abs(ny - ty)
            if m < best_d:
                best, best_d = d, m
        if best is None:
            return
        g.d = best
        g.x, g.y = step(g.x, g.y, best)
        if (g.x, g.y) == HOUSE_OUT:
            g.state = G_NORMAL
            g.d = 2 if urandom.getrandbits(1) else 0

    def _step_eyes(self, g):
        if (g.x, g.y) == HOUSE_IN:
            g.state = G_LEAVE
            g.scared = False
            return
        best, best_d = None, 0xFF
        for d in range(4):
            nx, ny = step(g.x, g.y, d)
            if not ghost_passable(cell_at(nx, ny), True):
                continue
            m = self.house_dist[ny * GRID_W + nx]
            if m < best_d:
                best, best_d = d, m
        if best is None:
            return
        g.d = best
        g.x, g.y = step(g.x, g.y, best)
        if (g.x, g.y) == HOUSE_IN:
            g.state = G_LEAVE
            g.scared = False

    def _chase_target(self, g):
        px, py = self.pac_x, self.pac_y
        if g.idx == 1:
            dx, dy = DIRS[self.pac_d]
            return px + 4 * dx, py + 4 * dy
        if g.idx == 2:
            if abs(g.x - px) + abs(g.y - py) <= 6:
                return GHOST_CORNERS[g.idx]
        return px, py

    def _step_normal(self, g, scatter):
        rev = (g.d + 2) % 4
        options = []
        for d in range(4):
            if d == rev:
                continue
            nx, ny = step(g.x, g.y, d)
            if ghost_passable(cell_at(nx, ny), False):
                options.append(d)
        if not options:
            options = [rev]

        if g.scared:
            best = options[urandom.getrandbits(4) % len(options)]
        else:
            tx, ty = GHOST_CORNERS[g.idx] if scatter else self._chase_target(g)
            best, best_d = options[0], 1 << 30
            for d in options:
                nx, ny = step(g.x, g.y, d)
                m = (nx - tx) * (nx - tx) + (ny - ty) * (ny - ty)
                if m < best_d:
                    best, best_d = d, m
        g.d = best
        g.x, g.y = step(g.x, g.y, best)

    def _check_collisions(self):
        pac = (self.pac_x, self.pac_y)
        for g in self.ghosts:
            if g.state in (G_WAIT, G_EYES):
                continue
            here = (g.x, g.y)
            swapped = here == self.pac_prev and (g.px, g.py) == pac
            if here != pac and not swapped:
                continue
            if g.scared:
                self.ghost_chain += 1
                self._add_score(GHOST_POINTS << (self.ghost_chain - 1))
                g.scared = False
                g.state = G_EYES
            else:
                self.phase = "dying"
                self.freeze = DYING_TICKS
                return True
        return False

    async def _loop(self):
        try:
            while self.running:
                if not self.paused and not self.game_over:
                    self._tick()
                    self.render()
                await asyncio.sleep_ms(self.tick_ms)
        except asyncio.CancelledError:
            return

    # ---------- drawing ----------
    def _draw_hud(self):
        self.oled.fill_rect(0, 0, self.oled.width, self.HUD_H, 0)

        wri6.set_textpos(self.oled, 0, 0)
        wri6.printstring("{:d}".format(self.score))

        # Lives as small blocks in the middle
        n = min(self.lives, MAX_LIFE_ICONS)
        x0 = (self.oled.width - (n * 5 - 2)) // 2
        y0 = (self.HUD_H - 3) // 2
        for i in range(n):
            self.oled.fill_rect(x0 + i * 5, y0, 3, 3, 1)

        hi_txt = "HI:{:d}".format(self.high_score)
        x_hi = self.oled.width - wri6.stringlen(hi_txt)
        wri6.set_textpos(self.oled, 0, x_hi)
        wri6.printstring(hi_txt)

        self.oled.hline(0, self.HUD_H - 1, self.oled.width, 1)

    def _draw_dots(self):
        c = self.CELL
        y0 = self.GRID_Y0
        for y in range(GRID_H):
            row = self.dots[y]
            py = y0 + y * c
            for x in range(GRID_W):
                v = row[x]
                if v == 46:
                    self.oled.pixel(x * c + 1, py + 1, 1)
                elif v == 111:
                    self.oled.fill_rect(x * c + 1, py + 1, 2, 2, 1)

    def _draw_pacman(self):
        c = self.CELL
        px = self.pac_x * c
        py = self.GRID_Y0 + self.pac_y * c
        self.oled.fill_rect(px, py, c, c, 1)
        if self.mouth:
            d = self.pac_d
            if d == 0:
                self.oled.fill_rect(px + 2, py + 1, 2, 2, 0)
            elif d == 2:
                self.oled.fill_rect(px, py + 1, 2, 2, 0)
            elif d == 1:
                self.oled.fill_rect(px + 1, py + 2, 2, 2, 0)
            else:
                self.oled.fill_rect(px + 1, py, 2, 2, 0)

    def _draw_ghost(self, g):
        c = self.CELL
        px = g.x * c
        py = self.GRID_Y0 + g.y * c
        o = self.oled
        if g.state == G_EYES:
            o.pixel(px + 1, py + 1, 1)
            o.pixel(px + 2, py + 1, 1)
            return
        if g.scared:
            blink = self.fright <= FRIGHT_BLINK and (self.fright & 1)
            if blink:
                o.fill_rect(px, py, c, c, 1)
            else:
                o.rect(px, py, c, c, 1)
            return
        o.fill_rect(px, py, c, 3, 1)
        o.pixel(px, py + 3, 1)
        o.pixel(px + 3, py + 3, 1)
        o.pixel(px + 1, py + 1, 0)
        o.pixel(px + 2, py + 1, 0)

    def render(self):
        self.oled.fill(0)
        self._draw_hud()

        if not (self.phase == "clear" and (self.freeze & 1)):
            self.oled.blit(self.walls, 0, self.GRID_Y0)
        self._draw_dots()

        for g in self.ghosts:
            self._draw_ghost(g)
        if not (self.phase == "dying" and (self.freeze & 1)):
            self._draw_pacman()

        # Overlays
        if self.paused:
            self._overlay_center("PAUSED")
        elif self.game_over:
            self._overlay_gameover()
        elif self.phase == "ready":
            self._overlay_center("READY!" if self.level == 1
                                 else "LEVEL {:d}".format(self.level))

        self.oled.show()

    def _overlay_center(self, text):
        """Draw a single-line centered overlay; safely clamps width."""
        pad = 2
        fh = wri6.font.height()
        max_text_w = self.oled.width - 2 * pad

        if wri6.stringlen(text) > max_text_w:
            base = text
            while base and wri6.stringlen(base + "...") > max_text_w:
                base = base[:-1]
            text = (base + "...") if base else "..."

        tw = wri6.stringlen(text)
        box_w = min(self.oled.width, tw + 2 * pad)
        box_h = fh + 2 * pad

        x = (self.oled.width - box_w) // 2
        if x < 0: x = 0
        y = self.GRID_Y0 + (self.PF_H - box_h) // 2
        if y < self.GRID_Y0: y = self.GRID_Y0

        self.oled.fill_rect(x, y, box_w, box_h, 0)
        self.oled.rect(x, y, box_w, box_h, 1)

        tw = wri6.stringlen(text)
        tx = x + (box_w - tw) // 2
        if tx < 0: tx = 0
        wri6.set_textpos(self.oled, y + pad, tx)
        wri6.printstring(text)

    def _overlay_gameover(self):
        """Two-line centered overlay that always fits."""
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
        y = self.GRID_Y0 + (self.PF_H - box_h) // 2
        if y < self.GRID_Y0: y = self.GRID_Y0

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
        if btn in (BTN_NEXT, BTN_PREV) and not self.paused \
                and not self.game_over and self.phase in ("ready", "play"):
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_turn) >= TURN_GUARD_MS:
                self._last_turn = now
                base = self.pac_d if self.pac_want is None else self.pac_want
                turn = 1 if btn == BTN_NEXT else -1
                self.pac_want = (base + turn) % 4

        if btn == BTN_SELECT:
            if self.game_over:
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
            else:
                self.paused = not self.paused
                self.render()
                return self

        if btn == BTN_BACK:
            self.running = False
            try:
                if self._task:
                    self._task.cancel()
            except Exception:
                pass
            return bsides.GamesScreen(self.oled)

        return self
