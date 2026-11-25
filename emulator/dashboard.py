"""Single-screen curses layout; independent of the control transport."""
try:
    import curses
except ModuleNotFoundError as exc:
    if exc.name not in ('curses', '_curses'):
        raise
    curses = None  # Pure rendering/layout functions remain importable.
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType

MIN_WIDTH = 124
MIN_HEIGHT = 31


@dataclass(frozen=True)
class Panel:
    y: int
    x: int
    height: int
    width: int


@lru_cache(maxsize=1)
def layout(height, width):
    if height < MIN_HEIGHT or width < MIN_WIDTH:
        return None
    left = max(68, width - 56)
    right = width - left
    return MappingProxyType({
        "help": Panel(1, 0, 4, left),
        "oled": Panel(5, 0, height - 6, left),
        "led": Panel(1, left, 17, right),
        "telemetry": Panel(18, left, height - 19, right),
    })


def put(screen, y, x, value, attr=0, limit=None):
    height, width = screen.getmaxyx()
    if not (0 <= y < height and 0 <= x < width):
        return
    count = min(width-x, limit if limit is not None else width-x)
    if y == height-1:
        count = min(count, width-x-1)
    if count <= 0:
        return
    # Strip control characters from protocol/status messages.
    value = "".join(c if c.isprintable() else " " for c in str(value))
    try:
        screen.addnstr(y, x, value, count, attr)
    except curses.error:
        pass


def panel(screen, p, title):
    put(screen, p.y, p.x, "+" + "-"*(p.width-2) + "+")
    put(screen, p.y, p.x+2, f" {title} ", curses.A_BOLD, p.width-4)
    for y in range(p.y+1, p.y+p.height-1):
        put(screen, y, p.x, "|")
        put(screen, y, p.x+p.width-1, "|")
    put(screen, p.y+p.height-1, p.x, "+" + "-"*(p.width-2) + "+")


def line(screen, p, row, value, attr=0):
    if 0 <= row < p.height-2:
        put(screen, p.y+1+row, p.x+2, value, attr, p.width-4)


def oled_attr():
    if curses.has_colors() and curses.COLOR_PAIRS > 1:
        try:
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
            return curses.color_pair(1)
        except curses.error:
            pass
    return curses.A_NORMAL


def draw(screen, args, frame_rows, pixels, frames, fps, metadata, telemetry,
         connected, status, draw_leds, memory_format, poll_hz, detailed=False, heap=None, incomplete=0, observations=0, *, render_hz=0, dropped=0, age_ms=None,
         rtt_ms=None, input_ms=None, queue_depth=0, generation=0, oled_style=0, oled_age_ms=None, rgb_age_ms=None):
    screen.erase()
    height, width = screen.getmaxyx()
    panels = layout(height, width)
    put(screen, 0, 0, f"BSides Badge | {args.host}:{args.port} | "
        f"{'Connected' if connected else 'Disconnected'} | target {args.refresh_hz:g} Hz", curses.A_BOLD)
    if panels is None:
        put(screen, 2, 0, f"Resize terminal: minimum {MIN_WIDTH} columns x {MIN_HEIGHT} rows.")
        put(screen, 3, 0, f"Current: {width} x {height} | R Reset | Q Quit")
        put(screen, height-1, 0, status)
        screen.noutrefresh()
        curses.doupdate()
        return
    help_panel, oled, led, stats = (panels[key] for key in ("help", "oled", "led", "telemetry"))
    panel(screen, help_panel, "Controls")
    line(screen, help_panel, 0, "Left/P Prev | Right/N Next | Enter/S Select | B/Backspace Back")
    line(screen, help_panel, 1, "R Reset | L LED colours | T Telemetry detail | Q Quit")
    panel(screen, oled, "OLED 128 x 64")
    top = oled.y + 1 + (oled.height-2-16)//2
    left = oled.x + (oled.width-64)//2
    attr = oled_style
    for i in range(16):
        put(screen, top+i, left, frame_rows[i] if frame_rows else " "*64, attr, 64)
    if frame_rows is None:
        put(screen, top+7, left+17, "Waiting for OLED framebuffer", attr, 64-17)
    panel(screen, led, "RGB LED ring")
    draw_leds(screen, pixels, args, led.y+1, led.x+(led.width-52)//2)
    line(screen, led, 14, f"{len(pixels)} pixels | {args.led_colors} | "
         + ("FPS n/a" if fps is None else f"{fps:.1f} FPS"))
    panel(screen, stats, f"Telemetry | partial RGB: {incomplete}/{observations}")
    line(screen, stats, 0, f"RGB {frames:,} | acquire {poll_hz:.1f} | draw {render_hz:.1f} Hz")
    if heap is None:
        line(screen, stats, 1, "Guest heap: n/a (disabled or stale)")
    else:
        total = heap.allocated + heap.free
        line(screen, stats, 1, f"Guest heap: {heap.allocated/1024:.1f}/{total/1024:.1f} KiB ({100*heap.allocated/total:.1f}%)")
    line(screen, stats, 2, f"ESP-EMU RSS: {memory_format(telemetry.emulator_rss)}")
    line(screen, stats, 3, f"Controller RSS: {memory_format(telemetry.controller_rss)}")
    if heap is not None:
        line(screen, stats, 4, f"Free: {heap.free/1024:.1f} KiB | min: {heap.minimum/1024:.1f} KiB")
        line(screen, stats, 5, f"Peak guest allocation: {heap.peak/1024:.1f} KiB")

    elif detailed:
        line(screen, stats, 4, f"ESP-EMU peak: {memory_format(telemetry.emulator_peak)}")
        line(screen, stats, 5, f"Controller peak: {memory_format(telemetry.controller_peak)}")

    else:
        line(screen, stats, 4, "OLED: " + ("frame received" if frame_rows else "waiting"))
        line(screen, stats, 5, "Host memory sampled at 1 Hz")
    if detailed:
        fmt = lambda value: "n/a" if value is None else f"{value:.1f}"
        line(screen, stats, 6, f"RTT: {fmt(rtt_ms)} ms | pub: {fmt(age_ms)} ms")
        line(screen, stats, 7, f"Skipped: {dropped} | Q:{queue_depth} | epoch:{generation}")
        status += f" | dispatch: {fmt(input_ms)} ms | OLED/RGB age: {fmt(oled_age_ms)}/{fmt(rgb_age_ms)} ms"
        line(screen, stats, 8, f"ESP-EMU PID: {telemetry.emulator_pid or 'n/a'}")
        line(screen, stats, 9, metadata or "OLED state: n/a")
    put(screen, height-1, 0, "Status: " + status)
    screen.noutrefresh()
    curses.doupdate()
