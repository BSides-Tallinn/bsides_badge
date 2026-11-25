#!/usr/bin/env python3
"""ESP-EMU badge controller; Python standard library only."""
import argparse
try:
    import curses
except ModuleNotFoundError as exc:
    if exc.name not in ('curses', '_curses'):
        raise
    curses = None  # Pure rendering/layout functions remain importable.
import locale
import time
import math
from functools import lru_cache

if __package__:
    from .peripherals import part_name
    from .dashboard import draw, oled_attr
    from .acquisition import Acquisition
    from .telemetry import mib
else:
    from peripherals import part_name
    from dashboard import draw, oled_attr
    from acquisition import Acquisition
    from telemetry import mib


@lru_cache(maxsize=16)
def braille_rows(frame):
    if len(frame) != 1024:
        raise ValueError('Expected 1024 framebuffer bytes')
    # ESP-EMU render exports row-major, MSB-first bytes, not the guest's page buffer.
    def pixel(x, y):
        return bool(frame[y * 16 + x // 8] & (128 >> (x % 8)))
    dots = ((0,0,1),(0,1,2),(0,2,4),(1,0,8),(1,1,16),(1,2,32),(0,3,64),(1,3,128))
    return [''.join(chr(0x2800 + sum(bit for dx,dy,bit in dots if pixel(x+dx,y+dy)))
                    for x in range(0,128,2)) for y in range(0,64,4)]


def display_rgb(rgb, mode="visible"):
    channels = tuple(int(rgb[i:i+2], 16) for i in (1, 3, 5))
    peak = max(channels)
    if mode == "visible" and 0 < peak < 175:
        return tuple(round(v * 175 / peak) for v in channels)
    return channels


LEVELS = (0, 95, 135, 175, 215, 255)
PALETTE_256 = tuple(
    (16 + 36*r + 6*g + b, (rv, gv, bv))
    for r, rv in enumerate(LEVELS)
    for g, gv in enumerate(LEVELS)
    for b, bv in enumerate(LEVELS)
) + tuple((232+i, (8+10*i,)*3) for i in range(24))
PALETTE_8 = tuple((i, tuple(255 if i & bit else 0 for bit in (1,2,4)))
                  for i in range(8))


@lru_cache(maxsize=4096)
def palette_index(rgb, colors, mode="visible"):
    target = display_rgb(rgb, mode)
    palette = PALETTE_256 if colors >= 256 else PALETTE_8[:max(0, min(colors, 8))]
    if not palette:
        return 0
    return min(palette, key=lambda item: sum((a-b)**2 for a,b in zip(target,item[1])))[0]


initialized_pairs = set()
color_attributes = {}


def color_attr(rgb, mode="visible", background=-1):
    if not curses.has_colors():
        return 0
    color = palette_index(rgb, curses.COLORS, mode)
    # The 256-colour palette uses indices 16..255 (240 entries).
    # Pair 0 is reserved and pair 1 belongs to the OLED.
    pair = color - 14 if curses.COLORS >= 256 else color + 2
    if pair >= curses.COLOR_PAIRS:
        return 0
    try:
        if initialized_pairs and pair in initialized_pairs:
            cached = color_attributes.get(pair)
            if cached is not None and cached[0:2] == (color, background):
                return cached[2]
        curses.init_pair(pair, color, background)
        attr = curses.color_pair(pair)
        initialized_pairs.add(pair)
        color_attributes[pair] = (color, background, attr)
        return attr
    except curses.error:
        initialized_pairs.discard(pair)
        color_attributes.pop(pair, None)
        return 0


BASE_RING = tuple((7 - round(6 * math.cos(i*math.tau/16)),
                   25 + round(23 * math.sin(i*math.tau/16))) for i in range(16))


@lru_cache(maxsize=32)
def _ring_positions(offset, counterclockwise):
    direction = -1 if counterclockwise else 1
    return tuple(BASE_RING[(offset + direction*i) % 16] for i in range(16))


def ring_positions(offset=0, counterclockwise=False):
    return _ring_positions(offset % 16, bool(counterclockwise))


def draw_leds(screen, pixels, args, top, left):
    height, width = screen.getmaxyx()
    positions = ring_positions(args.led_offset, args.led_counterclockwise)
    for i, (dy, dx) in enumerate(positions):
        y, x = top+dy, left+dx
        if y >= height or x+3 >= width:
            continue
        rgb = pixels[i] if i < len(pixels) else None
        try:
            screen.addstr(y, x, f'{i:02d}')
            if rgb is None:
                screen.addstr(y, x+2, '??')
            elif rgb == '#000000':
                screen.addstr(y, x+2, '··')
            else:
                screen.addstr(y, x+2, '██', color_attr(rgb, args.led_colors, args.led_background))
        except curses.error:
            pass


def run(screen, args):
    initialized_pairs.clear()
    color_attributes.clear()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    args.led_background = curses.COLOR_BLACK
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            args.led_background = -1
        except curses.error:
            pass
    screen.keypad(True)
    oled_style = oled_attr()
    buttons = {ord('b'): args.back, curses.KEY_BACKSPACE: args.back, 127: args.back,
               ord('p'): args.prev, curses.KEY_LEFT: args.prev,
               ord('n'): args.next, curses.KEY_RIGHT: args.next,
               ord('s'): args.select, 10: args.select, 13: args.select, curses.KEY_ENTER: args.select,
               ord('r'): "reset"}
    worker = Acquisition(args)
    interval = 1 / args.refresh_hz
    detailed = False
    next_render = time.monotonic()
    render_start = next_render
    render_count = 0
    render_hz = 0.0
    notice = ""
    notice_until = 0.0
    snapshot, dropped = worker.mailbox.read()
    worker.start()
    try:
        while True:
            now = time.monotonic()
            if now >= next_render:
                snapshot, dropped = worker.mailbox.read()
                if now-render_start >= 1:
                    render_hz = render_count/(now-render_start)
                    render_count = 0
                    render_start = now
                heap = snapshot.heap
                if heap and now-heap.received > 5:
                    heap = None
                status = notice if now < notice_until else snapshot.status
                if not worker.thread.is_alive():
                    status = "Worker stopped: " + snapshot.status
                age = max(0, (now-snapshot.published)*1000) if snapshot.published else None
                draw(screen, args, braille_rows(snapshot.frame) if snapshot.frame is not None else None,
                     snapshot.pixels, snapshot.frames, snapshot.fps, snapshot.metadata, snapshot.memory,
                     snapshot.connected and worker.thread.is_alive(), status, draw_leds, mib,
                     snapshot.poll_hz, detailed, heap, snapshot.incomplete, snapshot.observations,
                     render_hz=render_hz, dropped=dropped, age_ms=age, rtt_ms=snapshot.rtt_ms,
                     input_ms=snapshot.input_ms, queue_depth=snapshot.queue_depth,
                     generation=snapshot.generation, oled_style=oled_style,
                     oled_age_ms=None if snapshot.oled_time is None else max(0,(now-snapshot.oled_time)*1000),
                     rgb_age_ms=None if snapshot.rgb_time is None else max(0,(now-snapshot.rgb_time)*1000))
                render_count += 1
                next_render += (int((now-next_render)/interval)+1)*interval
            screen.timeout(max(1, int((next_render-time.monotonic())*1000)))
            key = screen.getch()
            if key in (ord('q'), 27):
                return
            if key == ord('t'):
                detailed = not detailed
            elif key == ord('l'):
                args.led_colors = 'raw' if args.led_colors == 'visible' else 'visible'
            elif key in buttons:
                # The event belongs to the generation actually displayed.
                if not snapshot.connected or not worker.thread.is_alive():
                    notice = "Input rejected: disconnected"
                elif not worker.submit(buttons[key], snapshot.generation):
                    notice = "Input rejected: queue full"
                else:
                    notice = "Input queued"
                notice_until = time.monotonic()+0.5
    finally:
        if not worker.stop():
            raise RuntimeError('Acquisition worker did not stop before shutdown deadline')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5556)
    parser.add_argument('--heap-file', help='Optional local guest heap sample file')
    parser.add_argument('--emulator-pid', type=int, help='Explicit local ESP-EMU PID for RSS sampling')
    for name in ('oled', 'back', 'next', 'prev', 'select', 'rgb'):
        parser.add_argument('--'+name, default=name)
    parser.add_argument('--press-ms', type=int, default=150)
    parser.add_argument('--refresh-hz', type=float, default=75, help='Target OLED/RGB polling frequency in Hz (1–120)')
    parser.add_argument('--led-colors', choices=('visible', 'raw'), default='visible',
                        help='Boost dim LED swatches or approximate raw intensity')
    parser.add_argument("--led-offset", type=int, default=0, help="Clockwise rotation in LED steps")
    parser.add_argument("--led-counterclockwise", action="store_true", help="Reverse LED index direction")
    args = parser.parse_args()
    if not 1 <= args.refresh_hz <= 120:
        parser.error("Refresh frequency must be between 1 and 120 Hz")
    if args.emulator_pid is not None and args.emulator_pid <= 0:
        parser.error('Emulator PID must be positive')
    if args.press_ms <= 0:
        parser.error("Use positive press duration")
    try:
        for name in ("oled", "back", "next", "prev", "select", "rgb"):
            part_name(getattr(args, name))
    except ValueError as exc:
        parser.error(str(exc))
    if curses is None:
        parser.error('Interactive controller requires curses; use Linux or WSL2')
    locale.setlocale(locale.LC_ALL, '')
    curses.wrapper(run, args)


if __name__ == '__main__':
    main()
