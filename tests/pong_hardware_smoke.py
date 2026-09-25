"""Run a 25-second Pong host/display soak on one connected badge.

    mpremote connect COM14 run tests/pong_hardware_smoke.py

The peer paddle heartbeat is injected in memory; no second badge is required.
This checks the real OLED, scheduler, and UART driver but not cable quality.
"""

import gc
import sys
import time
import uasyncio as asyncio
from machine import I2C, Pin

import ssd1306


class BadgeStub:
    device_id = "FFFFFFFFFFFF"


class CountingOLED(ssd1306.SSD1306_I2C):
    def __init__(self, *args, **kwargs):
        self.frames = 0
        super().__init__(*args, **kwargs)

    def show(self):
        self.frames += 1
        super().show()


sys.modules["bsides"] = BadgeStub()
from games import pong


async def main():
    oled = CountingOLED(128, 64, I2C(0, scl=Pin(1), sda=Pin(0)))
    gc.collect()
    start_free = gc.mem_free()
    game = pong.PongScreen(oled)
    game.linked = True
    game.is_host = True
    game.got_peer = True
    game.phase = "play"
    game.play_start = time.ticks_ms()
    game.last_rx = game.play_start

    try:
        for _ in range(125):
            game._handle_line(b"P25")
            await asyncio.sleep_ms(200)
        elapsed = time.ticks_diff(time.ticks_ms(), game.play_start)
        gc.collect()
        fps = oled.frames * 1000 / elapsed
        print("Pong soak: phase=%s elapsed_ms=%d fps=%.1f free_start=%d free_end=%d" %
              (game.phase, elapsed, fps, start_free, gc.mem_free()))
        if game.phase != "play" or elapsed < 25000:
            raise RuntimeError("Pong host did not stay in play for 25 seconds")
        if fps < 14 or fps > 18:
            raise RuntimeError("Pong frame rate outside expected range")
    finally:
        await game._stop()


asyncio.run(main())
