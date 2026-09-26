"""NeoPixel initialization, effects, and animation task."""

import math
import time
import uasyncio as asyncio

import neopixel
from machine import Pin


NEOPIXEL_PIN = 3
NEOPIXEL_COUNT = 16
NEOPIXEL_FPS = 50

led_effect = None
led_brightness = None
led_hue = None
led_sat = None
led_speed = None


def init_neopixels():
    pixels = neopixel.NeoPixel(Pin(NEOPIXEL_PIN, Pin.OUT), NEOPIXEL_COUNT)
    pixels.fill((0, 0, 0))
    pixels.write()
    return pixels


def hsv_to_rgb(h, s, v):
    """Convert HSV to an RGB tuple."""
    h %= 360
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


def led_eff_off(pixels, oldstate):
    pixels.fill((0, 0, 0))
    return oldstate


def led_eff_rainbow(pixels, oldstate):
    pos = oldstate or 0
    for index in range(len(pixels)):
        hue = ((index * 360 // len(pixels)) + pos) % 360
        pixels[index] = hsv_to_rgb(
            hue, led_sat.value / 100, led_brightness.value / 100)
    return (pos + led_speed.value / 10) % 360


def led_eff_breathe(pixels, oldstate):
    brightness, direction = oldstate or (0, 1)
    rgb = hsv_to_rgb(
        led_hue.value, led_sat.value / 100,
        brightness * led_brightness.value / 100)
    for index in range(len(pixels)):
        pixels[index] = rgb
    brightness += direction * led_speed.value / 1000
    if brightness >= 1:
        brightness, direction = 1, -1
    elif brightness <= 0:
        brightness, direction = 0, 1
    return brightness, direction


def led_eff_comet(pixels, oldstate):
    state = oldstate or 0
    head = int(state) % len(pixels)
    fade = 0.5 + ((led_speed.maxval - led_speed.value) / led_speed.maxval * 0.4)
    for index in range(len(pixels)):
        pixels[index] = tuple(int(value * fade) for value in pixels[index])
    pixels[head] = hsv_to_rgb(
        led_hue.value, led_sat.value / 100, led_brightness.value / 100)
    return state + led_speed.value / 100


def led_eff_startup(pixels, oldstate):
    head, phase = oldstate or (0, 0)
    on = hsv_to_rgb(
        led_hue.value, led_sat.value / 100, led_brightness.value / 100)
    for index in range(len(pixels)):
        pixels[index] = on if (index <= head) == (phase == 0) else (0, 0, 0)
    if head < len(pixels) - 1:
        return head + 1, phase
    if phase == 0:
        return 0, 1
    return None


def led_eff_autocycle(pixels, oldstate):
    state = oldstate or {"idx": 1, "timer": time.ticks_ms(), "inner": None}
    now = time.ticks_ms()
    if time.ticks_diff(now, state["timer"]) > 60000:
        state["idx"] += 1
        # The last entry is Cycle_All itself and must not be selected recursively.
        if state["idx"] >= len(LED_EFFECTS) - 1:
            state["idx"] = 1
        state["timer"] = now
        state["inner"] = None
    effect = LED_EFFECTS[state["idx"]][1]
    state["inner"] = effect(pixels, state["inner"])
    return state


def led_eff_rainbow_comet(pixels, oldstate):
    state = oldstate or {"pos": 0.0, "hue": 0}
    head = int(state["pos"]) % len(pixels)
    fade = 0.5 + ((led_speed.maxval - led_speed.value) / led_speed.maxval * 0.4)
    for index in range(len(pixels)):
        red, green, blue = pixels[index]
        pixels[index] = (int(red * fade), int(green * fade), int(blue * fade))
    pixels[head] = hsv_to_rgb(
        state["hue"], led_sat.value / 100, led_brightness.value / 100)
    state["pos"] += led_speed.value / 100
    state["hue"] = (state["hue"] + max(1, int(led_speed.value / 10))) % 360
    return state


def led_eff_ping_pong(pixels, oldstate):
    count = len(pixels)
    state = oldstate or {"pos": 0.0, "dir": 1}
    fade = 0.5 + ((led_speed.maxval - led_speed.value) / led_speed.maxval * 0.4)
    for index in range(count):
        red, green, blue = pixels[index]
        pixels[index] = (int(red * fade), int(green * fade), int(blue * fade))
    position = state["pos"] + state["dir"] * max(0.05, led_speed.value / 100)
    direction = state["dir"]
    if position <= 0:
        position, direction = 0, 1
    elif position >= count - 1:
        position, direction = count - 1, -1
    head = int(position)
    rgb = hsv_to_rgb(
        led_hue.value, led_sat.value / 100, led_brightness.value / 100)
    pixels[head] = rgb
    pixels[count - 1 - head] = rgb
    state["pos"], state["dir"] = position, direction
    return state


def led_eff_dual_hue(pixels, oldstate):
    state = oldstate or {"phase": 0.0}
    count = len(pixels)
    hue_a = led_hue.value % 360
    hue_b = (hue_a + 180) % 360
    saturation = led_sat.value / 100
    value = led_brightness.value / 100
    for index in range(count):
        angle = (2 * math.pi * index / count) + state["phase"]
        mix = 0.5 * (1 + math.cos(angle))
        hue = (hue_a * mix + hue_b * (1 - mix)) % 360
        pixels[index] = hsv_to_rgb(hue, saturation, value)
    state["phase"] += led_speed.value / 400
    return state


def led_eff_aurora(pixels, oldstate):
    state = oldstate or {"p1": 0.0, "p2": 0.0}
    count = len(pixels)
    saturation = led_sat.value / 100 * 0.9
    max_value = led_brightness.value / 100
    for index in range(count):
        position = 2 * math.pi * index / count
        wave1 = 0.5 * (1 + math.sin(position + state["p1"]))
        wave2 = 0.5 * (1 + math.sin(2 * position - state["p2"]))
        mix = 0.6 * wave1 + 0.4 * (1 - wave2)
        hue = (130 * mix + 280 * (1 - mix)) % 360
        value = (0.25 + 0.75 * (0.5 * (
            1 + math.sin(position * 0.8 + state["p2"] / 2)))) * max_value
        pixels[index] = hsv_to_rgb(hue, saturation, value)
    speed = max(0.05, led_speed.value / 200)
    state["p1"] += speed * 0.6
    state["p2"] += speed * 0.3
    return state


def led_eff_spiral_spin(pixels, oldstate):
    state = oldstate or {"phase": 0.0}
    count = len(pixels)
    saturation = led_sat.value / 100
    base_value = led_brightness.value / 100
    for index in range(count):
        position = (index / count) * (2 * math.pi * 2) + state["phase"]
        value = (0.5 * (1 + math.sin(position))) ** 1.6
        pixels[index] = hsv_to_rgb(
            led_hue.value, saturation, base_value * value)
    state["phase"] += led_speed.value / 200
    return state


def led_eff_police(pixels, oldstate):
    state = oldstate or {"phase": 0}
    count = len(pixels)
    half = count // 2
    phase = state["phase"]
    saturation = led_sat.value / 100
    value = led_brightness.value / 100
    pixels.fill((0, 0, 0))
    if 0 <= phase < 25:
        for index in range(0, half - 1):
            pixels[index] = hsv_to_rgb(0, saturation, value)
    elif 50 <= phase < 75:
        for index in range(half + 1, count):
            pixels[index] = hsv_to_rgb(240, saturation, value)
    state["phase"] = (phase + max(1, led_speed.value / 10)) % 100
    return state


LED_EFFECTS = [
    ("Off", led_eff_off),
    ("Rainbow", led_eff_rainbow),
    ("Breathe", led_eff_breathe),
    ("Comet", led_eff_comet),
    ("Rainbow Comet", led_eff_rainbow_comet),
    ("Ping-Pong", led_eff_ping_pong),
    ("Dual Hue", led_eff_dual_hue),
    ("Aurora", led_eff_aurora),
    ("Spiral Spin", led_eff_spiral_spin),
    ("Police", led_eff_police),
    ("Cycle_All", led_eff_autocycle),
]


async def neopixel_task(pixels, effect, brightness, hue, saturation, speed,
                        lights_off=None):
    global led_effect, led_brightness, led_hue, led_sat, led_speed
    led_effect = effect
    led_brightness = brightness
    led_hue = hue
    led_sat = saturation
    led_speed = speed

    state = None
    previous_effect = 0
    startup = True
    muted = False
    while True:
        if lights_off is not None and lights_off():
            if not muted:
                pixels.fill((0, 0, 0))
                pixels.write()
                muted = True
            await asyncio.sleep_ms(1000 // NEOPIXEL_FPS)
            continue
        muted = False
        if startup:
            state = led_eff_startup(pixels, state)
            if state is None:
                startup = False
        else:
            if previous_effect != led_effect.value:
                state = None
                previous_effect = led_effect.value
            if led_effect.value in range(len(LED_EFFECTS)):
                state = LED_EFFECTS[led_effect.value][1](pixels, state)
        pixels.write()
        await asyncio.sleep_ms(1000 // NEOPIXEL_FPS)
