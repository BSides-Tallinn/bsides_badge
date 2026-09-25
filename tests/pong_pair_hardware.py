"""Run concurrently on two 2025 badges wired TX/RX/GND together.

    mpremote connect COM14 run tests/pong_pair_hardware.py
    mpremote connect COM31 run tests/pong_pair_hardware.py

Each side reports transitions and fails if its link drops during one match.
"""

import sys
import time
import uasyncio as asyncio
from machine import I2C, Pin

import ssd1306
from badge_config import load_badge_config


class BadgeStub:
    device_id = load_badge_config()["device_id"]


sys.modules["bsides"] = BadgeStub()
from games import pong


async def main():
    oled = ssd1306.SSD1306_I2C(128, 64, I2C(0, scl=Pin(1), sda=Pin(0)))
    game = pong.PongScreen(oled)
    started = time.ticks_ms()
    previous = None
    play_started = None
    next_report = started
    try:
        while time.ticks_diff(time.ticks_ms(), started) < 125000:
            now = time.ticks_ms()
            if game.phase != previous:
                print("Pong %s: %s at %dms host=%s rx_age=%dms" %
                      (game.my_id, game.phase, time.ticks_diff(now, started),
                       game.is_host, time.ticks_diff(now, game.last_rx)))
                previous = game.phase
                if game.phase == "play":
                    play_started = now
                if game.phase == "lost":
                    raise RuntimeError("link failed during paired match")
                if game.phase == "over":
                    if play_started is None:
                        raise RuntimeError("match ended before play began")
                    elapsed = time.ticks_diff(now, play_started)
                    print("Pong %s: match complete in %dms" % (game.my_id, elapsed))
                    if elapsed < 59000:
                        raise RuntimeError("match ended early")
                    return
            if game.phase == "play" and time.ticks_diff(now, next_report) >= 0:
                print("Pong %s: %ds left, rx age %dms" %
                      (game.my_id, game.time_left,
                       time.ticks_diff(now, game.last_rx)))
                next_report = time.ticks_add(now, 10000)
            await asyncio.sleep_ms(100)
        raise RuntimeError("match did not complete in 125 seconds")
    finally:
        await game._stop()


asyncio.run(main())
