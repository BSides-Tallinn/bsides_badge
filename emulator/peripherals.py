"""Peripheral adapters independent of terminal rendering."""
import re
from dataclasses import dataclass


def parse_rgb(payload):
    fields = {}
    for item in payload.split():
        key, value = item.split('=', 1)
        if key in fields:
            raise ValueError(f"Duplicate RGB field: {key}")
        fields[key] = value
    if not {"pixels", "frames"} <= fields.keys():
        raise ValueError("Missing RGB counters")
    count = int(fields["pixels"])
    frames = int(fields["frames"])
    if frames < 0:
        raise ValueError("Negative frame counter")
    if not 0 <= count <= 1024:
        raise ValueError('Invalid pixel count')
    pixels = []
    for i in range(count):
        value = fields.get(str(i), '')
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            raise ValueError(f'Invalid RGB value: {value}')
        pixels.append(value.lower())
    return frames, pixels


def part_name(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid part name")
    return name


class OLED:
    def __init__(self, control, name="oled"):
        self.control = control
        self.name = part_name(name)
        self.frame = None

    def poll(self):
        """Return a new frame, or None; retain the previous frame in frame."""
        payload = self.control.command(f"part {self.name} render")
        return self.decode(payload)

    def decode(self, payload):
        if not payload:
            return None
        candidate = bytes.fromhex(payload)
        if len(candidate) != 1024:
            raise ValueError(f"Unexpected frame length: {len(candidate)}")
        self.frame = candidate
        return candidate

    def state(self):
        return self.control.command(f"part {self.name} state")


@dataclass(frozen=True)
class RGBState:
    frames: int
    colors: tuple[str, ...]


class RGB:
    def __init__(self, control, name="rgb"):
        self.control = control
        self.name = part_name(name)

    def state(self):
        frames, colors = parse_rgb(self.control.command(f"part {self.name} state"))
        return RGBState(frames, tuple(colors))


class Button:
    def __init__(self, control, name):
        self.control = control
        self.name = part_name(name)

    def state(self):
        return self.control.command(f"part {self.name} state")

    def release(self):
        self.control.command(f"part {self.name} input release")

    def press(self, duration_ms=150):
        if type(duration_ms) is not int or duration_ms <= 0:
            raise ValueError("Expected positive integer duration")
        prefix = f"part {self.name} input"
        self.control.command(f"{prefix} press")
        try:
            self.control.command(f"{prefix} release after {duration_ms}")
        except ValueError:
            try:
                self.control.command(f"{prefix} release")
            except ValueError:
                pass
            raise


class VirtualBadge:
    def __init__(self, control, oled="oled", rgb="rgb", back="back", prev="prev", next="next", select="select"):
        self.control = control
        self.parts = set(control.command("part list").split())
        self.oled = OLED(control, oled)
        self.rgb = RGB(control, rgb)
        self.buttons = {name: Button(control, name) for name in (back, prev, next, select) if name in self.parts}

    def reset(self):
        self.control.command("reset")
        self.oled.frame = None


@dataclass
class CompleteRGB:
    """Track raw observations separately from the last complete display frame."""
    expected: int = 16
    colors: tuple = ()
    frames: int = 0
    observations: int = 0
    incomplete: int = 0

    def observe(self, state):
        if state.frames < self.frames:
            self.reset()
        self.frames = state.frames
        self.observations += 1
        if len(state.colors) == self.expected:
            self.colors = state.colors
        else:
            self.incomplete += 1

    def reset(self):
        self.colors = ()
        self.frames = self.observations = self.incomplete = 0
