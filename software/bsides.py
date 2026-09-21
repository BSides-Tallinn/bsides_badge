"""BSides badge user interface and application entry point."""

import sys
import os
import gc
import uasyncio as asyncio
import time, micropython
from machine import Pin, I2C, RTC
import ssd1306
import bsides_logo
import rgb_leds
from badge_config import (
    format_device_id, hardware_for, load_badge_config, save_badge_config)
from battery import estimate_soc, read_battery_voltage

# Writer
from writer.writer import Writer
import writer.freesans20 as freesans20
import writer.font10 as font10
import writer.font6 as font6

# -----------------------
# Settings
# -----------------------
I2C_SCL = 1
I2C_SDA = 0
OLED_WIDTH = 128
OLED_HEIGHT = 64

badge_config = load_badge_config()
BADGE_VERSION = badge_config["badge_version"]
HARDWARE = hardware_for(BADGE_VERSION)
OLED_ADDRESS = HARDWARE["oled_address"]

# Buttons
BTN_NEXT_PIN = 5      # Next / Increase
BTN_PREV_PIN = 8      # Previous / Decrease
BTN_SELECT_PIN = HARDWARE["select_pin"]  # Enter
BTN_BACK_PIN = 9      # Back
DEBOUNCE_MS = 50

# Auto-repeat
REPEAT_DELAY = 500     # ms before auto-repeat starts
REPEAT_INTERVAL = 10  # ms between repeats

INACTIVITY_TIMEOUT = 5000  # ms
LOGO_PERIOD = 3000  # ms

URL_QR = "badge.bsides.ee"

# -----------------------
# Globals
# -----------------------
button_event = None
last_button = None
last_activity = 0

BTN_NEXT = 1
BTN_PREV = 2
BTN_SELECT = 3
BTN_BACK = 4

btn_state = {}       # {btn_id: pressed or not}
repeat_tasks = {}    # {btn_id: task}
_last_event_ms = {}  # debounce tracking

i2c_oled = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA))
oled = ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c_oled,
                           addr=OLED_ADDRESS)
wri6  = Writer(oled, font6, verbose=False)
wri10 = Writer(oled, font10, verbose=False)
wri20 = Writer(oled, freesans20, verbose=False)

username_wri = wri20
username_lines = None

# -----------------------
# Parameters
# -----------------------

class Parameter:
    def __init__(self, name, value, maxval):
        self.name = name
        self.value = value
        self.maxval = maxval

# -----------------------
# LED effects
# -----------------------

led_effects    = rgb_leds.LED_EFFECTS
led_effect     = Parameter("Light_effect", 0, 10)
led_brightness = Parameter("Brightness", 10, 100)
led_hue        = Parameter("Hue", 180, 360)
led_sat        = Parameter("Saturation", 100, 100)
led_speed      = Parameter("Speed", 30, 100)

# -----------------------
# Badge configuration storage
# -----------------------

params = {
    "Brightness": led_brightness,
    "Hue": led_hue,
    "Saturation": led_sat,
    "Speed": led_speed,
    "Light_effect" : led_effect
}

# --- Snake high score param (persistent in badge.json) ---
snake_high_score = Parameter("SnakeHighScore", 0, 9999)
params["SnakeHighScore"] = snake_high_score

# --- Pacman high score param (persistent in badge.json) ---
pacman_high_score = Parameter("PacmanHighScore", 0, 99999)
params["PacmanHighScore"] = pacman_high_score

# --- Tetris high score param (persistent in badge.json) ---
tetris_high_score = Parameter("TetrisHighScore", 0, 999999)
params["TetrisHighScore"] = tetris_high_score

# --- Flappy Bird high score param (persistent in badge.json) ---
flappy_high_score = Parameter("FlappyHighScore", 0, 9999)
params["FlappyHighScore"] = flappy_high_score

def save_params():
    badge_config["params"] = {
        name: param.value for name, param in params.items()
    }
    save_badge_config(badge_config)

def load_params():
    for name, val in badge_config.get("params", {}).items():
        if name in params:
            params[name].value = val

# -----------------------
# Username and ID
# -----------------------
USERNAME = badge_config.get("holder_name") or None
device_id = badge_config["device_id"]
print("Device ID: {}".format(device_id))
# -----------------------
# Button IRQ handling
# -----------------------
def _push_button(btn_id):
    global last_button, last_activity
    last_button = btn_id
    last_activity = time.ticks_ms()
    if button_event:
        button_event.set()

def _schedule_push(btn):
    btn_id, pin_state = btn
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_event_ms.get(btn_id, 0)) < DEBOUNCE_MS:
        return
    _last_event_ms[btn_id] = now

    if pin_state == 0:  # pressed
        btn_state[btn_id] = 1
        _push_button(btn_id)
        # SELECT repeats only on screens that explicitly opt in. Repeating it
        # globally would repeatedly activate menu entries and other actions.
        repeat_select = btn_id == BTN_SELECT and \
                        getattr(screen, "repeat_select", False)
        if btn_id in (BTN_NEXT, BTN_PREV) or repeat_select:
            repeat_tasks[btn_id] = asyncio.create_task(_repeat_task(btn_id))
    else:  # released
        btn_state[btn_id] = 0
        t = repeat_tasks.pop(btn_id, None)
        if t:
            t.cancel()

def make_irq(btn_id):
    def handler(pin):
        micropython.schedule(_schedule_push, (btn_id, pin.value()))
    return handler

def setup_buttons():
    cfg = [(BTN_NEXT_PIN, BTN_NEXT),
           (BTN_PREV_PIN, BTN_PREV),
           (BTN_SELECT_PIN, BTN_SELECT),
           (BTN_BACK_PIN, BTN_BACK)]
    for pin_num, btn_id in cfg:
        p = Pin(pin_num, Pin.IN)  # external pull-ups
        p.irq(trigger=Pin.IRQ_FALLING|Pin.IRQ_RISING, handler=make_irq(btn_id))

async def _repeat_task(btn_id):
    try:
        await asyncio.sleep_ms(REPEAT_DELAY)
        while btn_state[btn_id]:
            _push_button(btn_id)
            await asyncio.sleep_ms(REPEAT_INTERVAL)
    except asyncio.CancelledError:
        return

# -----------------------
# Screen base class
# -----------------------
class Screen:
    def __init__(self, oled):
        self.oled = oled

    def render(self):
        pass

    async def handle_button(self, btn):
        pass

# -----------------------
# Lights screens
# -----------------------
class ParamScreen(Screen):
    def __init__(self, oled, writer, param, returnscreen, barfill=False, wraparound=False):
        super().__init__(oled)
        self.writer = writer
        self.param = param
        self.returnscreen = returnscreen
        self.barfill = barfill
        self.wraparound = wraparound

    def render(self):
        self.oled.fill(0)

        val = self.param.value
        bar_x = 0
        bar_y = 30
        bar_w = self.oled.width
        bar_h = 10
        self.oled.rect(bar_x, bar_y, bar_w, bar_h, 1)

        # Knob/fill position
        pos = bar_x + (val * (bar_w - 1)) // self.param.maxval
        if not self.barfill:
            self.oled.vline(pos, bar_y, bar_h, 1)
        else:
            self.oled.fill_rect(bar_x, bar_y, pos, bar_h, 1)

        # Numeric display
        self.writer.set_textpos(self.oled, 50, 0)
        self.writer.printstring("{}: {:3d}".format(self.param.name, val))
        self.oled.show()

    async def handle_button(self, btn):
        if btn == BTN_NEXT and (self.wraparound or self.param.value < self.param.maxval):
            self.param.value = (self.param.value + 1) % (self.param.maxval + 1)
        elif btn == BTN_PREV and (self.wraparound or self.param.value > 0):
            self.param.value = (self.param.value - 1) % (self.param.maxval + 1)
        elif btn in (BTN_SELECT, BTN_BACK):
            return self.returnscreen(self.oled)
        return self

class BrightnessScreen(ParamScreen):
    def __init__(self, oled):
        super().__init__(oled, wri10, led_brightness, LightsScreen, barfill=True, wraparound=False)

class SpeedScreen(ParamScreen):
    def __init__(self, oled):
        super().__init__(oled, wri10, led_speed, LightsScreen, barfill=True, wraparound=False)

class SaturationScreen(ParamScreen):
    def __init__(self, oled):
        super().__init__(oled, wri10, led_sat, LightsScreen, barfill=False, wraparound=False)

class HueScreen(ParamScreen):
    def __init__(self, oled):
        super().__init__(oled, wri10, led_hue, LightsScreen, barfill=False, wraparound=True)

class ListScreen(Screen):
    def __init__(self, oled, title, items):
        super().__init__(oled)
        self.title = title
        self.items = items  # list of strings or tuples
        self.headerwriter = wri10
        self.listwriter = wri6
        self.index = 0
        self.offset = 0  # first visible item

        # metrics
        self.line_height = self.listwriter.font.height()
        self.rows = (self.oled.height - 20) // self.line_height  # room below header

    async def handle_button(self, btn):
        if btn == BTN_NEXT:
            self.index = (self.index + 1) % len(self.items)
        elif btn == BTN_PREV:
            self.index = (self.index - 1) % len(self.items)
        elif btn == BTN_BACK:
            return self.on_back()
        elif btn == BTN_SELECT:
            return self.on_select(self.index)

        # adjust scroll offset
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + self.rows:
            self.offset = self.index - self.rows + 1

        return self

    def render(self):
        self.oled.fill(0)
        self.headerwriter.set_textpos(self.oled, 0, 0)
        self.headerwriter.printstring(self.title)

        visible = range(self.offset, min(len(self.items), self.offset + self.rows))
        for row, i in enumerate(visible):
            y = 20 + row * self.line_height
            prefix = ">" if i == self.index else " "
            self.listwriter.set_textpos(self.oled, y, 0)
            self.listwriter.printstring("{}{}".format(prefix, self.items[i][0]))

        self.oled.show()

    # --- to be customized in child classes ---
    def on_select(self, index):
        pass

    def on_back(self):
        pass

class EffectScreen(ListScreen):
    def __init__(self, oled):
        super().__init__(oled, "LED effects", led_effects)

    def on_select(self, index):
        global led_effect
        led_effect.value = index
        return self

    def on_back(self):
        return LightsScreen(self.oled)

lights_screens = [("Effects", EffectScreen),
                  ("Brightness", BrightnessScreen),
                  ("Hue", HueScreen),
                  ("Saturation", SaturationScreen),
                  ("Speed", SpeedScreen)]

class LightsScreen(ListScreen):
    def __init__(self, oled):
        super().__init__(oled, "Lights", lights_screens)

    def on_select(self, index):
        cls = lights_screens[index][1]
        return cls(self.oled)

    def on_back(self):
        save_params()
        return MenuScreen(self.oled)


# -----------------------
# Utils screens
# -----------------------

class StopwatchScreen(Screen):
    """
    Simple stopwatch with live updating.
    Controls:
      SELECT: Start/Stop
      PREV:   Reset (when stopped)
      BACK:   Exit
    """
    def __init__(self, oled):
        super().__init__(oled)
        self.running = False
        self.start_ms = 0
        self.elapsed_ms = 0
        # start a small updater so time refreshes while running
        self._ticker = asyncio.create_task(self._tick())

    async def _tick(self):
        try:
            while True:
                if screen is self and self.running:
                    self.render()
                await asyncio.sleep_ms(100)
        except asyncio.CancelledError:
            return

    def _fmt(self, ms):
        s, cs = divmod(ms // 10, 100)      # centiseconds
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return "%02d:%02d:%02d.%02d" % (h, m, s, cs)

    def render(self):
        # update elapsed if running
        if self.running:
            now = time.ticks_ms()
            self.elapsed_ms = time.ticks_add(
                time.ticks_diff(now, self.start_ms), 0
            ) + self._paused_base

        self.oled.fill(0)
        # Title
        wri10.set_textpos(self.oled, 0, 0)
        wri10.printstring("Stopwatch")
        # Time (big)
        wri20.set_textpos(self.oled, 28, 0)
        wri20.printstring(self._fmt(self.elapsed_ms))
        # Hints
        wri6.set_textpos(self.oled, 50, 0)
        if self.running:
            wri6.printstring("SELECT=Stop  BACK=Exit")
        else:
            wri6.printstring("SELECT=Start PREV=Reset BACK=Exit")
        self.oled.show()

    async def handle_button(self, btn):
        if btn == BTN_SELECT:
            if not self.running:
                # starting: remember base elapsed (supports resume)
                self._paused_base = self.elapsed_ms
                self.start_ms = time.ticks_ms()
                self.running = True
            else:
                # stopping: lock in elapsed
                now = time.ticks_ms()
                self.elapsed_ms = self._paused_base + time.ticks_diff(now, self.start_ms)
                self.running = False
        elif btn == BTN_PREV and not self.running:
            self.elapsed_ms = 0
            self._paused_base = 0
        elif btn == BTN_BACK:
            # stop updater task when leaving
            self._ticker.cancel()
            return UtilsScreen(self.oled)
        self.render()
        return self

    # initialize paused base
    _paused_base = 0


class StatusScreen(Screen):
    def _fit(self, text):
        while text and wri6.stringlen(text) > self.oled.width:
            text = text[:-1]
        return text

    def render(self):
        self.oled.fill(0)
        # The small Writer font is 14 pixels high, despite its historical
        # filename. Use the framebuffer's 8-pixel font for the heading so all
        # four 2026 status rows fit without trying to start a row at y=64.
        self.oled.text("Status", 0, 0, 1)
        # FrameBuffer.text() is fixed at 8 pixels per character. "ID:" plus
        # every valid 12-character ID is 15 characters (120 pixels).
        self.oled.text(format_device_id(device_id), 0, 8, 1)

        lines = [
            "HW: {}".format(BADGE_VERSION),
            "Git: {}".format(badge_config.get("git_commit", "unknown")),
        ]
        battery_pin = HARDWARE.get("battery_pin")
        if battery_pin is not None:
            try:
                voltage = read_battery_voltage(battery_pin)
                lines.append("Bat: {:.2f}V ~{}%".format(
                    voltage, estimate_soc(voltage)))
            except Exception as exc:
                print("Battery read failed:", exc)
                lines.append("Bat: read error")

        y = 16
        for line in lines:
            wri6.set_textpos(self.oled, y, 0)
            wri6.printstring(self._fit(line))
            y += wri6.font.height()
        self.oled.show()

    async def handle_button(self, btn):
        if btn in (BTN_SELECT, BTN_BACK):
            return BadgeScreen(self.oled)
        return self


utils_screens = [("Stopwatch", StopwatchScreen)]

class UtilsScreen(ListScreen):
    def __init__(self, oled):
        super().__init__(oled, "Utils", utils_screens)

    def on_select(self, index):
        cls = utils_screens[index][1]
        return cls(self.oled)

    def on_back(self):
        return MenuScreen(self.oled)


# -----------------------
# Badge screens
# -----------------------
class FetchNameScreen(Screen):
    def __init__(self, oled):
        super().__init__(oled)
        self.oled = oled
        self.index = 0  # only one item
        self.message = ""  # status message to display
        self.fetch_task = None

    async def handle_button(self, btn):
        if btn == BTN_BACK:
            self._cancel_fetch()
            return BadgeScreen(oled)
        if btn == BTN_SELECT and self.fetch_task is None:
            self.fetch_task = asyncio.create_task(self._run_fetch())

        return self

    async def _run_fetch(self):
        self.message = "Starting secure fetch..."
        self.render()
        RTC().memory(b"wifi_fetch")
        await asyncio.sleep_ms(100)
        import machine
        machine.reset()

    def _cancel_fetch(self):
        if self.fetch_task:
            self.fetch_task.cancel()
            self.fetch_task = None

    def render(self):
        self.oled.fill(0)

        # 15 fixed-width characters fit in the 128-pixel display.
        self.oled.text(format_device_id(device_id), 0, 0, 1)

        if self.message:
            y = 10
            wri6.set_textpos(self.oled, y, 0)
            wri6.printstring(self.message)
        else:
            # menu item
            y = 10
            wri6.set_textpos(self.oled, y, 0)
            wri6.printstring(URL_QR)

            # menu item
            y += wri6.font.height() + 2
            wri6.set_textpos(self.oled, y, 0)
            wri6.printstring(">Fetch name")

        self.oled.show()

class CodeRepoScreen(Screen):
    async def handle_button(self, btn):
        if btn in (BTN_SELECT, BTN_BACK):
            return BadgeScreen(oled)
        return self

    def render(self):
        self.oled.fill(0)

        wri10.set_textpos(self.oled, 0, 0)
        wri10.printstring("Badge code git")

        y = wri10.font.height() + 4
        wri6.set_textpos(self.oled, y, 0)
        wri6.printstring("github.com/ BSides-Tallinn/ bsides_badge")

        self.oled.show()

badge_screens = [("Status", StatusScreen),
                 ("Fetch Name", FetchNameScreen),
                 ("Code git", CodeRepoScreen)]

class BadgeScreen(ListScreen):
    def __init__(self, oled):
        super().__init__(oled, "Badge setup", badge_screens)

    def on_select(self, index):
        cls = badge_screens[index][1]
        return cls(self.oled)

    def on_back(self):
        save_params()
        return MenuScreen(self.oled)

# -----------------------
# Sponsors screens
# -----------------------

LOGO_FOLDER = "logos"


def unload_sponsor_logos():
    """Evict generated logo modules and release their framebuffer bytearrays."""
    try:
        filenames = os.listdir(LOGO_FOLDER)
    except OSError:
        filenames = ()
    for filename in filenames:
        if filename.endswith(".py"):
            module_name = filename[:-3]
            if module_name in sys.modules:
                del sys.modules[module_name]
    gc.collect()

class SponsorsScreen(Screen):
    def __init__(self, oled):
        super().__init__(oled)

        # Keep only filenames. Each framebuffer is loaded for one render and
        # immediately evicted so ten 1 KiB images are never retained together.
        if LOGO_FOLDER not in sys.path:
            sys.path.append(LOGO_FOLDER)
        unload_sponsor_logos()
        self.logo_modules = sorted(
            filename[:-3] for filename in os.listdir(LOGO_FOLDER)
            if filename.endswith(".py"))
        self.current_logo = 0
        if not self.logo_modules:
            raise RuntimeError("No valid logos found!")

    def render(self):
        module_name = self.logo_modules[self.current_logo]
        module = None
        try:
            module = __import__(module_name)
            self.oled.fill(0)
            self.oled.blit(module.fb, 0, 0)
            self.oled.show()
        finally:
            if module is not None:
                del module
            unload_sponsor_logos()

    async def handle_button(self, btn):
        if btn == BTN_NEXT:
            self.current_logo = (self.current_logo + 1) % len(self.logo_modules)
        elif btn == BTN_PREV:
            self.current_logo = (self.current_logo - 1) % len(self.logo_modules)
        if btn == BTN_BACK:
            unload_sponsor_logos()
            return MenuScreen(self.oled)
        return self

# -----------------------
# Text screens
# -----------------------

class TextScreen(Screen):
    def __init__(self, oled, writer, text):
        super().__init__(oled)
        self.wri = writer

        # wrap long text
        self.text = self._wrap_text(text)

        # metrics
        self.line_height = self.wri.font.height()
        self.rows = oled.height // self.line_height
        self.offset = 0

    def _wrap_text(self, text):
        lines = []
        # split paragraphs by explicit newline
        for para in text.split("\n"):
            words = para.split()
            line = ""
            for word in words:
                test_line = (line + " " + word).strip()
                if self.wri.stringlen(test_line) <= self.oled.width:
                    line = test_line
                else:
                    lines.append(line)
                    line = word
            if line:
                lines.append(line)
            if para == "":  # preserve blank lines
                lines.append("")
        return lines

    def render(self):
        self.oled.fill(0)
        y = 0
        for i in range(self.offset, min(len(self.text), self.offset + self.rows)):
            self.wri.set_textpos(self.oled, y, 0)
            self.wri.printstring(self.text[i])
            y += self.line_height
        self.oled.show()

    async def handle_button(self, btn):
        if btn == BTN_NEXT and self.offset + self.rows < len(self.text):
            self.offset += 1
        elif btn == BTN_PREV and self.offset > 0:
            self.offset -= 1
        elif btn == BTN_BACK:
            return MenuScreen(self.oled)
        return self

class AboutScreen(TextScreen):
    def __init__(self, oled):
        text = (
            "BSides is a worldwide infosec event format, organized by the local infosec community in every city it is held. BSides Tallinn is organized by a non-profit core-team, volunteers and sponsors since 2021.\n\n"
            "One core difference of all BSides events is that the talks on the stage are proposed by anyone and selected by a program committee - professionals representing the organizers, private companies, academia, the state, freelancers.\n\n"
            "Talks, presentations, demos, proof-of-concepts across a very broad spectrum of infosec topics. All of the content is proposed by community members."
        )
        super().__init__(oled, wri6, text)


class OurteamScreen(TextScreen):
    def __init__(self, oled):
        text = (
            "Organizers: Hans, Silvia, Matis, Liisa, Johanna, Martti, Rainer, Kadi\n\n"
            "Badge: Konstantin\n\n"
            "Volunteers: Kristo, Merli, Hanna, Sten, Beekay, Liam, Hordii, Armin, Alex"
        )
        super().__init__(oled, wri6, text)

# -----------------------
# Menu screen
# -----------------------

GAMES_FOLDER = "games"


def discover_games():
    """Return (display name, screen class) pairs exported by games/*.py."""
    games = []
    try:
        filenames = sorted(os.listdir(GAMES_FOLDER))
    except OSError as exc:
        print("Cannot scan games directory:", exc)
        return games

    for filename in filenames:
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        module_name = filename[:-3]
        try:
            module = __import__("games." + module_name, None, None,
                                ("GAME_NAME", "GameScreen"))
            games.append((module.GAME_NAME, module.GameScreen))
        except Exception as exc:
            # One broken optional game should not prevent the badge from booting.
            print("Cannot load game {}: {}".format(filename, exc))
    return games


class GamesScreen(ListScreen):
    def __init__(self, oled):
        self.games = discover_games()
        super().__init__(oled, "Games", self.games)

    def on_select(self, index):
        return self.games[index][1](self.oled)

    def on_back(self):
        return MenuScreen(self.oled)

class MenuScreen(Screen):
    items = [("About", AboutScreen),
             ("Sponsors", SponsorsScreen),
             ("Our team", OurteamScreen),
             ("Utils", UtilsScreen),
             ("Lights", LightsScreen),
             ("Badge", BadgeScreen),
             ("Games", GamesScreen)]

    def __init__(self, oled):
        super().__init__(oled)
        self.index = 0
        self.render()

    def render(self):
        self.oled.fill(0)
        wri20.set_textpos(self.oled, 17, 20)
        wri20.printstring(MenuScreen.items[self.index][0])
        self.oled.show()

    async def handle_button(self, btn):
        if btn == BTN_NEXT:
            self.index = (self.index+1) % len(MenuScreen.items)
            self.render()
        elif btn == BTN_PREV:
            self.index = (self.index-1) % len(MenuScreen.items)
            self.render()
        elif btn == BTN_SELECT:
            return MenuScreen.items[self.index][1](self.oled)
        return self

# -----------------------
# UI manager
# -----------------------
screen = None

async def ui_task(oled):
    global screen

    while True:
        await button_event.wait()
        button_event.clear()
        btn = last_button
        if screen == None:
            screen = MenuScreen(oled)
        screen = await screen.handle_button(btn)

        # Games with their own animation loop render themselves.
        if not getattr(screen, "manages_own_render", False):
            screen.render()

def show_bsides_logo(oled):
    oled.fill(0)
    oled.blit(bsides_logo.fb, 0, 0)
    oled.show()

def wrap_text(text, writer, max_width, max_height):
    line_height = writer.font.height()
    max_rows = max_height // line_height

    words = text.split()
    lines, line = [], ""

    for word in words:
        # if a word itself is too long, split it at character level
        while writer.stringlen(word) > max_width:
            for i in range(1, len(word) + 1):
                if writer.stringlen(word[:i]) > max_width:
                    lines.append(word[:i-1])
                    word = word[i-1:]
                    break
        test_line = (line + " " + word).strip()
        if writer.stringlen(test_line) <= max_width:
            line = test_line
        else:
            lines.append(line)
            line = word
        if len(lines) >= max_rows:
            break
    if line and len(lines) < max_rows:
        lines.append(line)

    # truncate if too many lines
    if len(lines) > max_rows:
        lines = lines[:max_rows]
        # replace last line with ellipsis if there’s space
        if writer.stringlen(lines[-1] + "...") <= max_width:
            lines[-1] += "..."
        else:
            lines[-1] = lines[-1][:-3] + "..."

    return lines

def show_username(oled, name):
    global username_lines
    oled.fill(0)

    if not username_lines:
        username_lines = wrap_text(name, username_wri, oled.width, oled.height)
    total_height = len(username_lines) * username_wri.font.height()
    y = (oled.height - total_height) // 2

    for line in username_lines:
        x = (oled.width - username_wri.stringlen(line)) // 2
        username_wri.set_textpos(oled, y, x)
        username_wri.printstring(line)
        y += username_wri.font.height()

    oled.show()

async def inactivity_task(oled):
    global screen
    last_toggle = time.ticks_ms()
    showing_logo = True

    while True:
        await asyncio.sleep_ms(500)
        inactive = (screen == None or isinstance(screen, MenuScreen)) and time.ticks_diff(time.ticks_ms(), last_activity) > INACTIVITY_TIMEOUT
        if inactive:
            now = time.ticks_ms()
            if time.ticks_diff(now, last_toggle) >= LOGO_PERIOD:
                showing_logo = not showing_logo
                last_toggle = now

            if showing_logo or not USERNAME:
                show_bsides_logo(oled)
            else:
                show_username(oled, USERNAME)


# -----------------------
# Main
# -----------------------
async def main():
    global button_event, last_activity
    np = rgb_leds.init_neopixels()
    button_event = asyncio.Event()
    last_activity = time.ticks_ms()

    setup_buttons()
    load_params()
    show_bsides_logo(oled)
    print("Username: {}".format(USERNAME))

    await asyncio.gather(
        ui_task(oled), inactivity_task(oled),
        rgb_leds.neopixel_task(
            np, led_effect, led_brightness, led_hue, led_sat, led_speed))

try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
