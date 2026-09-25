"""Opposite-phase PWM effects for the 2026 plug-in board."""

import math
import time
import uasyncio as asyncio
from machine import Pin, PWM


LED_PINS = (6, 7)
MAX_DUTY = 65535
EFFECTS = ("Breathe", "Blink", "Police", "On", "Off")


def duties(effect, elapsed_ms):
    """Return the two PWM levels for a frame, with Breathe as fallback."""
    if effect == 3:
        return MAX_DUTY, MAX_DUTY
    if effect == 4:
        return 0, 0
    if effect == 1:
        first = MAX_DUTY if elapsed_ms % 1000 < 500 else 0
    elif effect == 2:
        # Three quick alternations, then hold; reverse on the next half-cycle.
        phase = elapsed_ms % 1200
        pulse = phase % 600
        on = pulse < 100 or 200 <= pulse < 300 or 400 <= pulse
        first = MAX_DUTY if on != (phase >= 600) else 0
    else:
        phase = 2 * math.pi * (elapsed_ms % 3000) / 3000
        first = int(MAX_DUTY * (1 - math.cos(phase)) / 2)
    return first, MAX_DUTY - first


async def led_task(effect, lights_off=None):
    leds = []
    try:
        for pin in LED_PINS:
            leds.append(PWM(Pin(pin, Pin.OUT), freq=1000, duty_u16=0))
        previous = effect.value
        started = time.ticks_ms()
        while True:
            now = time.ticks_ms()
            if effect.value != previous:
                previous = effect.value
                started = now
            if lights_off is not None and lights_off():
                levels = (0, 0)
            else:
                levels = duties(effect.value, time.ticks_diff(now, started))
            for led, level in zip(leds, levels):
                led.duty_u16(level)
            await asyncio.sleep_ms(20)
    finally:
        for led in leds:
            led.duty_u16(0)
            led.deinit()
