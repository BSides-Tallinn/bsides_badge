import sys
import os
import network
import socket
import ssl
import json
import uasyncio as asyncio
import time, micropython
from machine import Pin, I2C
import ssd1306, neopixel
import bsides_logo
import math
from badge_config import hardware_for, load_badge_config, save_badge_config
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

NEOPIXEL_PIN = 3
NEOPIXEL_COUNT = 16
NEOPIXEL_FPS = 50

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

SSID = "bsides-badge"
PASSWORD = "bsidestallinn"
URL = "https://badge.bsides.ee"
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

led_startup    = True
led_effects    = []
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
# Hardware init
# -----------------------

def init_neopixels():
    np = neopixel.NeoPixel(Pin(NEOPIXEL_PIN, Pin.OUT), NEOPIXEL_COUNT)
    np.fill((0,0,0))
    np.write()
    return np

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

        lines = [
            "ID: {}".format(device_id),
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

        y = 8
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
        self.wlan = None

    async def handle_button(self, btn):
        global username_lines, USERNAME
        if btn == BTN_SELECT:
            self.message = "Connecting WiFi..."
            self.render()
            try:
                await self._connect_wifi()
            except Exception as e:
                self.message = f"WiFi error: {e}"
                self.render()
                return self

            self.message = "Fetching name..."
            self.render()
            try:
                name = await self._fetch_name()
                self.message = f"Name: {name}"
                self.render()
                # Reset name lines and store in badge.json.
                USERNAME = name
                username_lines = None
                try:
                    badge_config["holder_name"] = name
                    save_badge_config(badge_config)
                except OSError as e:
                    self.message += f" (save error: {e})"
                    self.render()
            except Exception as e:
                self.message = f"Fetch error: {e}"
                self.render()
        elif btn == BTN_BACK:
            await self._disconnect_wifi()
            return BadgeScreen(oled)

        return self

    async def _connect_wifi(self):
        if not self.wlan:
            self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        if not self.wlan.isconnected():
            self.wlan.connect(SSID, PASSWORD)
            for _ in range(20):  # wait up to ~10 seconds
                await asyncio.sleep(0.5)
                if self.wlan.isconnected():
                    return
            raise RuntimeError("Could not connect to WiFi")

    async def _disconnect_wifi(self):
        if not self.wlan:
            return
        try:
            self.wlan.disconnect()
        except OSError:
            pass
        for _ in range(20):  # up to ~10 seconds
            if not self.wlan.isconnected():
                break
            await asyncio.sleep(0.5)
        self.wlan.active(False)

    async def _fetch_name(self):
        # parse URL
        proto, rest = URL.split("://", 1)
        if "/" in rest:
            host, base_path = rest.split("/", 1)
            base_path = "/" + base_path
        else:
            host, base_path = rest, ""

        port = 443 if proto == "https" else 80

        # resolve host
        addr_info = socket.getaddrinfo(host, port)
        addr = addr_info[0][-1]
        s = socket.socket()
        s.connect(addr)

        if proto == "https":
            s = ssl.wrap_socket(s, server_hostname=host)

        path = base_path + "/getname/" + device_id
        req = "GET {} HTTP/1.0\r\nHost: {}\r\n\r\n".format(path, host)
        s.send(req.encode())

        # read response
        resp = b""
        while True:
            data = s.recv(512)
            if not data:
                break
            resp += data
        s.close()

        # extract body
        body = resp.split(b"\r\n\r\n", 1)[-1]
        try:
            data = json.loads(body)
        except ValueError:
            raise RuntimeError("Invalid JSON")

        # Check for error
        if "error" in data:
            raise RuntimeError("{}".format(data.get("error","")))

        # compare IDs case-insensitively
        if data.get("id", "").upper() != device_id.upper() or "name" not in data:
            raise RuntimeError("Unexpected response")

        return data["name"].strip()

    def render(self):
        self.oled.fill(0)

        # header = device_id
        wri6.set_textpos(self.oled, 0, 0)
        wri6.printstring("ID: {}".format(device_id))

        if self.message:
            y = wri6.font.height() + 2
            wri6.set_textpos(self.oled, y, 0)
            wri6.printstring(self.message)
        else:
            # menu item
            y = wri6.font.height() + 2
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
        wri6.printstring("github.com/ks000/ bsides_badge")

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

class SponsorsScreen(Screen):
    def __init__(self, oled):
        super().__init__(oled)

        # Import logos dynamically
        LOGO_FOLDER = "logos"
        if LOGO_FOLDER not in sys.path:
            sys.path.append(LOGO_FOLDER)
        logo_files = sorted([f for f in os.listdir(LOGO_FOLDER) if f.endswith(".py")])

        self.logos = []
        self.current_logo = 0
        for f in logo_files:
            module_name = f[:-3]  # strip '.py'
            mod = __import__(module_name)
            if hasattr(mod, "fb"):
                self.logos.append(mod.fb)
            else:
                print(f"Warning: {module_name} has no attribute 'fb'")

        if not self.logos:
            raise RuntimeError("No valid logos found!")

    def render(self):
        self.oled.fill(0)
        self.oled.blit(self.logos[self.current_logo], 0, 0)
        self.oled.show()

    async def handle_button(self, btn):
        if btn == BTN_NEXT:
            self.current_logo = (self.current_logo + 1) % len(self.logos)
        elif btn == BTN_PREV:
            self.current_logo = (self.current_logo - 1) % len(self.logos)
        if btn == BTN_BACK:
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
            "Volunteers: Elis, Elle, Kristo, Merli, Hanna, Liam, Sten"
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
# NeoPixel effects
# -----------------------
def hsv_to_rgb(h, s, v):
    """Convert HSV to standard RGB tuple."""
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return (int((r + m) * 255), int((g + m) * 255), int((b + m) * 255))

def led_eff_off(np, oldstate):
    np.fill((0,0,0))
    return oldstate

def led_eff_rainbow(np, oldstate):
    """Rainbow running around the circle"""
    pos = oldstate or 0
    for i in range(len(np)):
        pixel_hue = ((i * 360 // len(np)) + pos) % 360
        np[i] = hsv_to_rgb(pixel_hue, led_sat.value/100, led_brightness.value/100)
    return (pos + led_speed.value/10) % 360

def led_eff_breathe(np, oldstate):
    """All LEDs smoothly brighten and dim"""
    br, d = oldstate or (0, 1)
    rgb = hsv_to_rgb(led_hue.value, led_sat.value/100, br*led_brightness.value/100)

    for i in range(len(np)):
        np[i] = rgb
    br += d * led_speed.value / 1000
    if br >= 1.0:
        br = 1.0
        d = -1
    elif br <= 0.0:
        br = 0.0
        d = 1
    return (br, d)

def led_eff_comet(np, oldstate, tail=5):
    """Single bright dot with fading tail"""
    state = oldstate or 0
    head_idx = int(state) % len(np)
    fade_coeff = 0.5 + ((led_speed.maxval - led_speed.value) / led_speed.maxval * 0.4)
    # fade all LEDs slightly
    for i in range(len(np)):
        np[i] = tuple(int(x * fade_coeff) for x in np[i])
    # light the comet head
    np[head_idx] = hsv_to_rgb(led_hue.value, led_sat.value/100, led_brightness.value/100)
    
    return state + led_speed.value / 100


def led_eff_startup(np, oldstate):
    head, phase = oldstate or (0, 0)

    rgb_on = hsv_to_rgb(led_hue.value, led_sat.value/100, led_brightness.value/100)
    rgb_off = (0,0,0)
    for i in range(len(np)):
        rgb = rgb_on if (i <= head) == (phase == 0) else rgb_off
        np[i] = rgb
    
    if head < len(np) - 1:
        return (head + 1, phase)
    elif phase == 0:
        return (0, 1)
    else:
        return None


def led_eff_autocycle(np, oldstate):
    """
    Automatically cycles through all effects every minute.
    Reuse the existing led_effect functions one by one.
    """
    state = oldstate or {"idx": 1, "timer": time.ticks_ms(), "inner": None}
    now = time.ticks_ms()

    # every 60 seconds go to next effect (skip index 0 = Off)
    if time.ticks_diff(now, state["timer"]) > 60_000:
        state["idx"] += 1
        if state["idx"] >= len(led_effects):
            state["idx"] = 1        # wrap around, stay above 0
        state["timer"] = now
        state["inner"] = None       # reset inner effect state

    # run the current inner effect
    effect_fn = led_effects[state["idx"]][1]
    state["inner"] = effect_fn(np, state["inner"])
    return state


def led_eff_rainbow_comet(np, oldstate):
    """
    A comet that runs around the ring while its color cycles through the rainbow.
    The trail fades naturally, preserving past hues for a multicolor tail.
    """
    # state keeps a sub-pixel position and a hue
    state = oldstate or {"pos": 0.0, "hue": 0}

    # Where's the head right now?
    head_idx = int(state["pos"]) % len(np)

    # Fade existing LEDs slightly to create a tail
    # Faster speed -> slightly less fade; slower speed -> more persistence
    fade_coeff = 0.5 + ((led_speed.maxval - led_speed.value) / led_speed.maxval * 0.4)
    for i in range(len(np)):
        r, g, b = np[i]
        np[i] = (int(r * fade_coeff), int(g * fade_coeff), int(b * fade_coeff))

    # Set the head with the current rainbow hue
    rgb = hsv_to_rgb(state["hue"], led_sat.value/100, led_brightness.value/100)
    np[head_idx] = rgb

    # Advance position and hue based on Speed
    state["pos"] += led_speed.value / 100     # movement per frame
    state["hue"] = (state["hue"] + max(1, int(led_speed.value / 10))) % 360

    return state


def led_eff_ping_pong(np, oldstate):
    """
    Two bouncing heads with fading tails (like a KITT/Cylon sweep on a ring).
    """
    n = len(np)
    state = oldstate or {"pos": 0.0, "dir": 1}

    # Fade existing pixels for trailing effect
    fade = 0.5 + ((led_speed.maxval - led_speed.value) / led_speed.maxval * 0.4)
    for i in range(n):
        r, g, b = np[i]
        np[i] = (int(r * fade), int(g * fade), int(b * fade))

    # Primary head position (linear, reflecting at ends)
    pos = state["pos"]
    dir_ = state["dir"]
    speed = max(0.05, led_speed.value / 100)  # movement per frame
    pos += dir_ * speed
    if pos <= 0:
        pos = 0
        dir_ = 1
    elif pos >= n - 1:
        pos = n - 1
        dir_ = -1

    head1 = int(pos)
    # Second head mirrors across the strip ends
    head2 = (n - 1) - head1

    rgb = hsv_to_rgb(led_hue.value, led_sat.value/100, led_brightness.value/100)
    np[head1] = rgb
    np[head2] = rgb

    state["pos"], state["dir"] = pos, dir_
    return state


def led_eff_dual_hue(np, oldstate):
    """
    Opposite halves blend Hue -> Hue+180, rotating slowly.
    """
    state = oldstate or {"phase": 0.0}
    n = len(np)

    hue_a = led_hue.value % 360
    hue_b = (hue_a + 180) % 360
    s = led_sat.value / 100
    v = led_brightness.value / 100

    for i in range(n):
        # angle around ring with a rotating offset
        a = (2 * math.pi * i / n) + state["phase"]
        # smooth, mirrored gradient: 1 on one side, 0 on the opposite side
        m = 0.5 * (1 + math.cos(a))  # 1..0..1 around the circle
        # interpolate hue between A and B by m
        # (distance <= 180 so simple lerp is fine)
        hue = (hue_a * m + hue_b * (1 - m)) % 360
        np[i] = hsv_to_rgb(hue, s, v)

    # rotate divider; Speed controls rotation rate
    state["phase"] += led_speed.value / 400.0
    return state


def led_eff_aurora(np, oldstate):
    """
    Northern-lights style waves in green and purple.
    """
    state = oldstate or {"p1": 0.0, "p2": 0.0}
    n = len(np)

    hue_g = 130   # green-ish
    hue_p = 280   # purple-ish
    s = (led_sat.value / 100) * 0.9
    v_max = led_brightness.value / 100

    for i in range(n):
        x = 2 * math.pi * i / n
        # two gentle, offset waves
        w1 = 0.5 * (1 + math.sin(x + state["p1"]))       # 0..1
        w2 = 0.5 * (1 + math.sin(2 * x - state["p2"]))   # 0..1

        # color mix and brightness breathing
        mix = 0.6 * w1 + 0.4 * (1 - w2)                  # 0..1
        hue = (hue_g * mix + hue_p * (1 - mix)) % 360
        v = (0.25 + 0.75 * (0.5 * (1 + math.sin(x*0.8 + state["p2"]/2)))) * v_max

        np[i] = hsv_to_rgb(hue, s, v)

    # slow evolving phases; Speed affects flow
    sp = max(0.05, led_speed.value / 200.0)
    state["p1"] += sp * 0.6
    state["p2"] += sp * 0.3
    return state


def led_eff_spiral_spin(np, oldstate):
    """
    Rotating brightness wave around the ring, giving a spiral illusion.
    """
    state = oldstate or {"phase": 0.0}
    n = len(np)
    waves = 2  # try 1, 2, or 3 for different looks
    gamma = 1.6  # contrast

    s = led_sat.value/100
    v_base = led_brightness.value/100
    hue = led_hue.value

    for i in range(n):
        # normalized position around the ring
        t = (i / n) * (2 * math.pi * waves) + state["phase"]
        b = 0.5 * (1 + math.sin(t))              # 0..1
        b = b ** gamma                           # contrast curve
        r, g, b_rgb = hsv_to_rgb(hue, s, v_base * b)
        np[i] = (r, g, b_rgb)

    # Rotate the wave; speed controls angular velocity
    state["phase"] += (led_speed.value / 200)    # tweak feel here
    return state


def led_eff_police(np, oldstate):
    """
    Simulates police lights by flashing red and blue strobes on opposite sides.
    """
    state = oldstate or {"phase": 0}
    n = len(np)
    half_size = n // 2

    # Phase determines which lights are on.
    # 0-24: Red group on
    # 25-49: All off
    # 50-74: Blue group on
    # 75-99: All off
    phase = state["phase"]

    s = led_sat.value / 100
    v = led_brightness.value / 100
    red = hsv_to_rgb(0, s, v)
    blue = hsv_to_rgb(240, s, v)
    off = (0, 0, 0)

    np.fill(off)

    if 0 <= phase < 25:  # Red half
        for i in range(1, half_size - 1):
            np[i] = red
    elif 50 <= phase < 75:  # Blue half
        for i in range(half_size + 1, n - 1):
            np[i] = blue

    # Speed controls the flash rate. A higher value means faster flashing.
    increment = max(1, led_speed.value / 10)
    state["phase"] = (phase + increment) % 100

    return state


async def neopixel_task(np):
    global led_effect
    global led_effects
    global led_startup
    t = None
    prev_effect = 0
    led_effects = [("Off", led_eff_off),
                   ("Rainbow", led_eff_rainbow),
                   ("Breathe", led_eff_breathe),
                   ("Comet", led_eff_comet),
                   ("Rainbow Comet", led_eff_rainbow_comet),
                   ("Ping-Pong", led_eff_ping_pong),
                   ("Dual Hue", led_eff_dual_hue),        
                   ("Aurora", led_eff_aurora),
                   ("Spiral Spin", led_eff_spiral_spin),
                   ("Police", led_eff_police),
                   ("Cycle_All", led_eff_autocycle)]

    while True:
        if led_startup == True:
            t = led_eff_startup(np, t)
            if t == None:
                led_startup = False
        else:
            if prev_effect != led_effect.value:
                t = None
                prev_effect = led_effect.value
            if led_effect.value in range(len(led_effects)):
                t = led_effects[led_effect.value][1](np, t)
        np.write()
        await asyncio.sleep_ms(int(1000/NEOPIXEL_FPS))

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
    np = init_neopixels()
    button_event = asyncio.Event()
    last_activity = time.ticks_ms()

    setup_buttons()
    load_params()
    show_bsides_logo(oled)
    print("Username: {}".format(USERNAME))

    await asyncio.gather(ui_task(oled), inactivity_task(oled), neopixel_task(np))

try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
