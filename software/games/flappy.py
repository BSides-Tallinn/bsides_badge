import time
import urandom
import uasyncio as asyncio

import bsides


GAME_NAME = "Flappy Bird"

BTN_NEXT = bsides.BTN_NEXT
BTN_PREV = bsides.BTN_PREV
BTN_SELECT = bsides.BTN_SELECT
BTN_BACK = bsides.BTN_BACK
wri6 = bsides.wri6

FRAME_MS = 50             # physics and render step (20 fps)

# Bird
BIRD_X = 24
BIRD_W = 5
BIRD_H = 4
GRAVITY = 0.35            # px per frame^2
FLAP_V = -2.4             # px per frame, upwards (one flap rises about 7 px)
MAX_FALL = 3.5

# Pipes
PIPE_W = 8
PIPE_SPACING = 60         # px between pipe left edges
SCROLL = 2.0              # px per frame
GAP_START = 26
GAP_MIN = 20
GAP_SHRINK_EVERY = 10     # pipes passed per 1 px of gap lost
GAP_MARGIN = 4            # min pipe stub above and below the gap
GAP_MAX_JUMP = 16         # max vertical change between consecutive gaps

REPEAT_FILTER_MS = 60     # drops the 10 ms auto-repeat of NEXT/PREV
RESTART_DELAY_MS = 500    # ignore SELECT right after a crash


# ---------- pure helpers (unit tested) ----------
def gap_for_score(score):
    return max(GAP_MIN, GAP_START - score // GAP_SHRINK_EVERY)


def overlaps(ax, ay, aw, ah, bx, by, bw, bh):
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def bird_hits_pipe(bird_y, pipe_x, gap_top, gap_h, top, bottom):
    """True when the bird box touches either half of a pipe."""
    by = int(bird_y)
    px = int(pipe_x)
    if overlaps(BIRD_X, by, BIRD_W, BIRD_H, px, top, PIPE_W, gap_top - top):
        return True
    low = gap_top + gap_h
    return overlaps(BIRD_X, by, BIRD_W, BIRD_H, px, low, PIPE_W, bottom - low)


class GameScreen(bsides.Screen):
    """
    Flappy Bird for 128x64 SSD1306.
    - HUD row at top (same layout as Snake), ground line at the bottom.
    - Controls:
        SELECT/NEXT -> flap (also starts the run and resumes from pause)
        PREV        -> pause/resume
        SELECT      -> restart on game over
        BACK        -> exit to menu

    The main UI sees manages_own_render and leaves rendering to the game loop.
    """
    manages_own_render = True

    def __init__(self, oled):
        super().__init__(oled)

        # ----- GEOMETRY -----
        self.HUD_H = wri6.font.height()
        self.top = self.HUD_H                 # first playfield row
        self.ground = oled.height - 2         # ground line; bird dies on it

        # ----- GAME STATE -----
        self.running = True
        self.paused = False
        self.phase = "ready"                  # ready/play/dying/over
        self.score = 0
        self.frame = 0
        self.wing = 0

        param = getattr(bsides, "flappy_high_score", None)
        self.high_score = param.value if param else 0

        self.mid_y = (self.top + self.ground - BIRD_H) // 2
        self.y = float(self.mid_y)
        self.vy = 0.0
        self.pipes = []                       # [x, gap_top, gap_h, scored]
        old = time.ticks_add(time.ticks_ms(), -10 * RESTART_DELAY_MS)
        self._last_press = {BTN_NEXT: old, BTN_PREV: old}
        self._over_t = old

        # Start loop last
        self._task = asyncio.create_task(self._loop())
        self.render()

    # ---------- game logic ----------
    def _spawn_pipe(self, x):
        gap_h = gap_for_score(self.score + len(self.pipes))
        lo = self.top + GAP_MARGIN
        hi = self.ground - GAP_MARGIN - gap_h
        if self.pipes:
            # keep the next gap within reach of the previous one
            prev = self.pipes[-1][1]
            lo = max(lo, prev - GAP_MAX_JUMP)
            hi = max(lo, min(hi, prev + GAP_MAX_JUMP))
        gap_top = lo + urandom.getrandbits(8) % (hi - lo + 1)
        self.pipes.append([float(x), gap_top, gap_h, False])

    def _flap(self):
        self.vy = FLAP_V
        self.wing ^= 1

    def _step(self):
        self.frame += 1
        if self.phase == "ready":
            # gentle bob while waiting for the first flap
            t = self.frame % 16
            self.y = self.mid_y + (t if t < 8 else 16 - t) // 2 - 2
            return
        if self.phase == "over":
            return

        self.vy = min(MAX_FALL, self.vy + GRAVITY)
        self.y += self.vy
        if self.y < self.top:
            self.y = float(self.top)
            self.vy = 0.0
        if self.y + BIRD_H >= self.ground:
            self.y = float(self.ground - BIRD_H)
            self._end_game()
            return
        if self.phase == "dying":
            return

        if not self.pipes or self.pipes[-1][0] <= self.oled.width - PIPE_SPACING:
            self._spawn_pipe(self.oled.width)
        for p in self.pipes:
            p[0] -= SCROLL
            if not p[3] and p[0] + PIPE_W < BIRD_X:
                p[3] = True
                self.score += 1
        if self.pipes[0][0] + PIPE_W < 0:
            self.pipes.pop(0)

        for p in self.pipes:
            if bird_hits_pipe(self.y, p[0], p[1], p[2], self.top, self.ground):
                self.phase = "dying"
                self.vy = 0.0
                return

    def _end_game(self):
        self.phase = "over"
        self._over_t = time.ticks_ms()
        if self.score > self.high_score:
            self.high_score = self.score
            try:
                bsides.flappy_high_score.value = self.high_score
                bsides.save_params()
            except Exception:
                pass

    async def _loop(self):
        try:
            while self.running:
                start = time.ticks_ms()
                if not self.paused and self.phase != "over":
                    self._step()
                    self.render()
                spent = time.ticks_diff(time.ticks_ms(), start)
                await asyncio.sleep_ms(max(5, FRAME_MS - spent))
        except asyncio.CancelledError:
            return

    # ---------- drawing ----------
    def _draw_hud(self):
        self.oled.fill_rect(0, 0, self.oled.width, self.HUD_H, 0)

        wri6.set_textpos(self.oled, 0, 0)
        wri6.printstring("SCORE:{:d}".format(self.score))

        hi_txt = "HI:{:d}".format(self.high_score)
        x_hi = self.oled.width - wri6.stringlen(hi_txt)
        wri6.set_textpos(self.oled, 0, x_hi)
        wri6.printstring(hi_txt)

        self.oled.hline(0, self.HUD_H - 1, self.oled.width, 1)

    def _draw_pipes(self):
        o = self.oled
        for p in self.pipes:
            x = int(p[0])
            gap_top, gap_h = p[1], p[2]
            low = gap_top + gap_h
            # upper pipe: body, blank row, 2 px lip at the gap
            o.fill_rect(x + 1, self.top, PIPE_W - 2, max(0, gap_top - 3 - self.top), 1)
            o.fill_rect(x, gap_top - 2, PIPE_W, 2, 1)
            # lower pipe
            o.fill_rect(x, low, PIPE_W, 2, 1)
            o.fill_rect(x + 1, low + 3, PIPE_W - 2, max(0, self.ground - low - 3), 1)

    def _draw_bird(self):
        o = self.oled
        x, y = BIRD_X, int(self.y)
        o.fill_rect(x, y, BIRD_W, BIRD_H, 1)
        o.pixel(x, y, 0)
        o.pixel(x, y + BIRD_H - 1, 0)
        o.pixel(x + 3, y + 1, 0)                        # eye
        o.pixel(x + BIRD_W, y + 2, 1)                   # beak
        o.pixel(x + 1, y + (2 if self.wing else 1), 0)  # wing

    def _draw_ground(self):
        o = self.oled
        o.hline(0, self.ground, o.width, 1)
        shift = 0 if self.phase in ("dying", "over") else (self.frame * 2) % 6
        for x in range(-shift, o.width, 6):
            o.hline(x, self.ground + 1, 3, 1)

    def render(self):
        self.oled.fill(0)
        self._draw_pipes()
        self._draw_bird()
        self._draw_ground()
        self._draw_hud()

        if self.phase == "over":
            self._overlay(["GAME OVER", "SELECT=Restart"])
        elif self.paused:
            self._overlay(["PAUSED"])
        elif self.phase == "ready":
            self._overlay(["SELECT=Flap"], low=True)

        self.oled.show()

    def _overlay(self, lines, low=False):
        """Centered text box that always fits; low=True sits near the ground."""
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
        box_h = len(lines) * fh + (len(lines) - 1) * gap + 2 * pad

        x = (self.oled.width - box_w) // 2
        if x < 0: x = 0
        if low:
            y = self.ground - box_h - 1
        else:
            y = self.top + (self.ground - self.top - box_h) // 2
        if y < self.top: y = self.top

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

        if btn == BTN_BACK:
            self.running = False
            try:
                if self._task:
                    self._task.cancel()
            except Exception:
                pass
            return bsides.GamesScreen(self.oled)

        # NEXT/PREV auto-repeat every 10 ms while held. Holding must not
        # flap or toggle pause repeatedly, so drop events that follow closely.
        if btn in self._last_press:
            repeat = time.ticks_diff(now, self._last_press[btn]) < REPEAT_FILTER_MS
            self._last_press[btn] = now
            if repeat:
                return self

        if self.phase == "over":
            if btn == BTN_SELECT and \
                    time.ticks_diff(now, self._over_t) >= RESTART_DELAY_MS:
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

        if btn == BTN_PREV:
            if self.phase == "play":
                self.paused = not self.paused
                self.render()
            return self

        if btn in (BTN_SELECT, BTN_NEXT):
            self.paused = False
            if self.phase == "ready":
                self.phase = "play"
            if self.phase == "play":
                self._flap()

        return self
