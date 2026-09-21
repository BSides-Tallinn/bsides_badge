import time
import urandom
import machine
import uasyncio as asyncio

from writer.writer import Writer
import writer.font6 as font6
import writer.freesans20 as freesans20

# Badge-to-badge link: hardware UART1 on the UART pads (chip pins 27/28 =
# GPIO20/GPIO21 on the ESP32-C3). Cross-wire TX->RX between the two badges.
UART_ID = 1
UART_RX = 20
UART_TX = 21
UART_BAUD = 115200

# Button ids (same values as bsides)
BTN_NEXT = 1
BTN_PREV = 2
BTN_SELECT = 3
BTN_BACK = 4

GAME_SECONDS = 60
TICK_MS = 20          # physics tick
RENDER_EVERY = 2      # render every N ticks (~25 fps)
COUNTDOWN_MS = 3000
LINK_TIMEOUT_MS = 10000
LOST_TIMEOUT_MS = 1500

PADDLE_W = 2
PADDLE_H = 12
PADDLE_STEP = 1
BALL = 3
BALL_SPEED0 = 1.0
BALL_ACCEL = 0.15
BALL_SPEED_MAX = 2.4

GAME_NAME = "Pong"


class PongScreen:
    """
    Two-player Pong over the UART link. The badge with the higher device ID
    becomes host: it simulates ball, scores and match timer, and streams state
    to the guest. The guest renders a mirrored view and streams its paddle
    position back. Each player sees their own paddle on the left.
    Controls:
      NEXT/SELECT -> move paddle up/down (hold NEXT to keep moving)
      SELECT    -> rematch (when finished) / retry (on link error)
      BACK      -> exit to menu

    The main UI sees manages_own_render and leaves rendering to the game loop.
    """
    manages_own_render = True
    repeat_select = True

    def __init__(self, oled):
        self.oled = oled
        self.wri6 = Writer(oled, font6, verbose=False)
        self.wri20 = Writer(oled, freesans20, verbose=False)

        self.hud_h = self.wri6.font.height()
        self.pf_top = self.hud_h
        self.pf_bot = oled.height - 2
        self.pl_x = 2
        self.pr_x = oled.width - 2 - PADDLE_W

        import bsides
        self.my_id = bsides.device_id

        self.uart = machine.UART(UART_ID, baudrate=UART_BAUD,
                                 tx=machine.Pin(UART_TX),
                                 rx=machine.Pin(UART_RX), timeout=0)

        self.running = True
        self.phase = "link"          # link/count/play/over/nolink/lost
        self.link_started = time.ticks_ms()
        self.last_hello = 0
        self.linked = False
        self.is_host = None
        self.got_peer = False

        self.score_l = 0             # host-frame scores (host = left)
        self.score_r = 0
        self.paddle_y = self._clamp_paddle((self.pf_bot - PADDLE_H) // 2)
        self.op_y = self.paddle_y
        self.bx = (oled.width - BALL) // 2
        self.by = (self.pf_top + self.pf_bot - BALL) // 2
        self.vx = 0.0
        self.vy = 0.0
        self.time_left = GAME_SECONDS

        self.count_start = 0
        self.play_start = 0
        self.last_rx = time.ticks_ms()
        self.last_sent_paddle = -1
        self._dirty = True

        self._tasks = [asyncio.create_task(self._loop()),
                       asyncio.create_task(self._rx())]
        self.render()

    # ---------- helpers ----------
    def _clamp_paddle(self, y):
        return max(self.pf_top, min(self.pf_bot - PADDLE_H + 1, y))

    def _send(self, msg):
        self.uart.write(msg + b"\n")

    def _scores(self):
        if self.is_host:
            return self.score_l, self.score_r
        return self.score_r, self.score_l

    def _serve(self, direction=None):
        self.bx = (self.oled.width - BALL) // 2
        self.by = (self.pf_top + self.pf_bot - BALL) // 2
        if direction is None:
            direction = 1 if urandom.getrandbits(1) else -1
        self.vx = BALL_SPEED0 * direction
        self.vy = (urandom.getrandbits(8) - 128) / 160.0

    def _reset_match(self):
        self.score_l = 0
        self.score_r = 0
        self.paddle_y = self._clamp_paddle((self.pf_bot - PADDLE_H) // 2)
        self.op_y = self.paddle_y
        self.vx = 0.0
        self.vy = 0.0
        self.time_left = GAME_SECONDS
        self.bx = (self.oled.width - BALL) // 2
        self.by = (self.pf_top + self.pf_bot - BALL) // 2

    def _restart(self):
        self._reset_match()
        self.got_peer = False
        self.phase = "count"
        self.count_start = time.ticks_ms()
        self._send(b"G")
        self._dirty = True

    async def _stop(self):
        self.running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.sleep_ms(0)
        try:
            self.uart.deinit()
        except Exception:
            pass

    # ---------- host physics ----------
    def _overlaps(self, py):
        return self.by + BALL - 1 >= py and self.by <= py + PADDLE_H - 1

    def _bounce(self, py, direction):
        speed = min(BALL_SPEED_MAX, abs(self.vx) + BALL_ACCEL)
        self.vx = speed * direction
        off = (self.by + BALL / 2 - (py + PADDLE_H / 2)) / (PADDLE_H / 2)
        self.vy = off * speed * 0.75

    def _physics(self, now):
        self.time_left = GAME_SECONDS - time.ticks_diff(now, self.play_start) // 1000
        if self.time_left <= 0:
            self.time_left = 0
            self.phase = "over"
            self._send(b"E%d,%d" % (self.score_l, self.score_r))
            self._dirty = True
            return
        if time.ticks_diff(now, self.last_rx) >= LOST_TIMEOUT_MS:
            self.phase = "lost"
            self._dirty = True
            return

        self.bx += self.vx
        self.by += self.vy

        if self.by <= self.pf_top:
            self.by = self.pf_top
            self.vy = abs(self.vy)
        elif self.by + BALL - 1 >= self.pf_bot:
            self.by = self.pf_bot - BALL + 1
            self.vy = -abs(self.vy)

        if self.vx < 0 and self.bx <= self.pl_x + PADDLE_W and self.bx + BALL >= self.pl_x:
            if self._overlaps(self.paddle_y):
                self.bx = self.pl_x + PADDLE_W
                self._bounce(self.paddle_y, 1)
        if self.vx > 0 and self.bx + BALL >= self.pr_x and self.bx <= self.pr_x + PADDLE_W:
            if self._overlaps(self.op_y):
                self.bx = self.pr_x - BALL
                self._bounce(self.op_y, -1)

        if self.bx + BALL < 0:
            self.score_r += 1
            self._serve(-1)
        elif self.bx > self.oled.width:
            self.score_l += 1
            self._serve(1)

    def _send_state(self):
        self._send(b"S%d,%d,%d,%d,%d,%d" % (int(self.bx), int(self.by),
                   self.paddle_y, self.score_l, self.score_r, self.time_left))

    # ---------- main loop ----------
    async def _loop(self):
        tick = 0
        try:
            while self.running:
                now = time.ticks_ms()
                if self.phase == "link":
                    if time.ticks_diff(now, self.last_hello) >= 300:
                        self._send(b"H" + self.my_id.encode())
                        self.last_hello = now
                    if time.ticks_diff(now, self.link_started) >= LINK_TIMEOUT_MS:
                        self.phase = "nolink"
                        self._dirty = True
                elif self.phase == "count":
                    if self.is_host and not self.got_peer and \
                            time.ticks_diff(now, self.last_hello) >= 500:
                        self._send(b"G")
                        self.last_hello = now
                    if time.ticks_diff(now, self.count_start) >= COUNTDOWN_MS:
                        self.phase = "play"
                        self.play_start = now
                        self.last_rx = now
                        if self.is_host:
                            self._serve()
                        self._dirty = True
                elif self.phase == "play":
                    if self.is_host:
                        self._physics(now)
                        if tick % RENDER_EVERY == 0:
                            self._send_state()
                    else:
                        if time.ticks_diff(now, self.last_rx) >= LOST_TIMEOUT_MS:
                            self.phase = "lost"
                            self._dirty = True
                if self.phase in ("count", "play") and not self.is_host:
                    if self.paddle_y != self.last_sent_paddle or tick % 25 == 0:
                        self._send(b"P%d" % self.paddle_y)
                        self.last_sent_paddle = self.paddle_y
                if self.phase in ("count", "play") and tick % RENDER_EVERY == 0:
                    self._dirty = True
                if self._dirty:
                    self.render()
                    self._dirty = False
                tick += 1
                await asyncio.sleep_ms(TICK_MS)
        except asyncio.CancelledError:
            return

    # ---------- UART receive ----------
    async def _rx(self):
        buf = b""
        try:
            while self.running:
                n = self.uart.any()
                if n:
                    buf += self.uart.read(n)
                    i = buf.find(b"\n")
                    while i >= 0:
                        self._handle_line(buf[:i])
                        buf = buf[i + 1:]
                        i = buf.find(b"\n")
                    if len(buf) > 200:
                        buf = buf[-64:]
                await asyncio.sleep_ms(5)
        except asyncio.CancelledError:
            return

    def _handle_line(self, line):
        self.last_rx = time.ticks_ms()
        if not line:
            return
        tag, rest = line[0:1], line[1:]
        if tag == b"H":
            if self.phase in ("link", "nolink", "lost"):
                # Reply before the host starts its countdown. Without this,
                # the late-entering badge may only receive G, which it cannot
                # accept until it has learned the host's ID from an H packet.
                self._send(b"H" + self.my_id.encode())
                self.linked = True
                self.is_host = self.my_id > rest.decode()
                self.phase = "link"
                self.link_started = time.ticks_ms()
                if self.is_host:
                    self.phase = "count"
                    self.count_start = time.ticks_ms()
                    self._send(b"G")
                self._dirty = True
        elif tag == b"G":
            if not self.is_host and self.linked and self.phase in ("link", "count", "over"):
                self._reset_match()
                self.phase = "count"
                self.count_start = time.ticks_ms()
                self._dirty = True
        elif tag == b"P":
            if self.is_host:
                self.got_peer = True
                try:
                    self.op_y = self._clamp_paddle(int(rest))
                except ValueError:
                    pass
        elif tag == b"S":
            if not self.is_host and self.phase in ("play", "over"):
                self._apply_state(rest)
        elif tag == b"E":
            if not self.is_host:
                try:
                    l, r = rest.split(b",")
                    self.score_l, self.score_r = int(l), int(r)
                except ValueError:
                    pass
                self.time_left = 0
                self.phase = "over"
                self._dirty = True
        elif tag == b"R":
            if self.is_host and self.phase == "over":
                self._restart()

    def _apply_state(self, rest):
        try:
            parts = rest.split(b",")
            bx, by = int(parts[0]), int(parts[1])
            hy, sl, sr, t = int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
        except (ValueError, IndexError):
            return
        self.bx = self.oled.width - bx - BALL
        self.by = by
        self.op_y = self._clamp_paddle(hy)
        self.score_l, self.score_r = sl, sr
        self.time_left = t
        self._dirty = True

    # ---------- drawing ----------
    def _center6(self, text, y):
        w = self.wri6.stringlen(text)
        self.wri6.set_textpos(self.oled, y, max(0, (self.oled.width - w) // 2))
        self.wri6.printstring(text)

    def render(self):
        oled = self.oled
        oled.fill(0)
        if self.phase == "link":
            self._center6("PONG", 10)
            self._center6("Linking..." if not self.linked else "Linked, wait...", 28)
            self._center6("BACK=Exit", 50)
        elif self.phase == "nolink":
            self._center6("No peer found", 16)
            self._center6("SELECT=Retry", 34)
            self._center6("BACK=Exit", 48)
        elif self.phase == "lost":
            self._center6("Link lost", 16)
            self._center6("SELECT=Retry", 34)
            self._center6("BACK=Exit", 48)
        elif self.phase == "count":
            self._center6("PONG", 0)
            n = 3 - time.ticks_diff(time.ticks_ms(), self.count_start) // 1000
            self.wri20.set_textpos(oled, 24, 56)
            self.wri20.printstring(str(max(1, n)))
        elif self.phase in ("play", "over"):
            self._draw_field()
            if self.phase == "over":
                self._overlay_over()
        oled.show()

    def _draw_field(self):
        oled = self.oled
        me, op = self._scores()
        self.wri6.set_textpos(oled, 0, 0)
        self.wri6.printstring("ME {:d} {:02d}s OP {:d}".format(me, self.time_left, op))
        oled.hline(0, self.hud_h - 1, oled.width, 1)
        oled.hline(0, self.pf_bot, oled.width, 1)
        for y in range(self.pf_top, self.pf_bot - 1, 4):
            oled.vline(oled.width // 2, y, 2, 1)
        oled.fill_rect(self.pl_x, self.paddle_y, PADDLE_W, PADDLE_H, 1)
        oled.fill_rect(self.pr_x, self.op_y, PADDLE_W, PADDLE_H, 1)
        oled.fill_rect(int(self.bx), int(self.by), BALL, BALL, 1)

    def _overlay_over(self):
        oled = self.oled
        me, op = self._scores()
        if me > op:
            title = "YOU WIN"
        elif me < op:
            title = "YOU LOSE"
        else:
            title = "DRAW"
        lines = [title, "{:d}:{:d}".format(me, op), "SELECT=Rematch", "BACK=Exit"]
        pad, gap = 2, 1
        fh = self.wri6.font.height()
        maxw = max(self.wri6.stringlen(s) for s in lines)
        box_w = min(oled.width, maxw + 2 * pad)
        box_h = 4 * fh + 3 * gap + 2 * pad
        x = (oled.width - box_w) // 2
        y = max(0, (oled.height - box_h) // 2)
        oled.fill_rect(x, y, box_w, box_h, 0)
        oled.rect(x, y, box_w, box_h, 1)
        ty = y + pad
        for s in lines:
            tw = self.wri6.stringlen(s)
            self.wri6.set_textpos(oled, ty, max(0, x + (box_w - tw) // 2))
            self.wri6.printstring(s)
            ty += fh + gap

    # ---------- input ----------
    async def handle_button(self, btn):
        if btn == BTN_BACK:
            await self._stop()
            import bsides
            return bsides.GamesScreen(self.oled)
        if self.phase in ("count", "play"):
            if btn == BTN_NEXT:
                self.paddle_y = self._clamp_paddle(self.paddle_y - PADDLE_STEP)
                self._dirty = True
            elif btn == BTN_SELECT:
                self.paddle_y = self._clamp_paddle(self.paddle_y + PADDLE_STEP)
                self._dirty = True
        elif self.phase == "over" and btn == BTN_SELECT:
            self._send(b"R")
            if self.is_host:
                self._restart()
        elif self.phase in ("nolink", "lost") and btn == BTN_SELECT:
            self.phase = "link"
            self.linked = False
            self.is_host = None
            self.link_started = time.ticks_ms()
            self.last_hello = 0
            self._dirty = True
        return self


# Contract used by bsides.discover_games().
GameScreen = PongScreen
