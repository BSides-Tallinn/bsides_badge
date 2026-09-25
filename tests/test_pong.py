"""Pong timing and reconnection checks without badge hardware."""

import asyncio
import importlib.util
import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PONG_PATH = Path(__file__).resolve().parents[1] / "software" / "games" / "pong.py"


class Clock:
    now = 100000

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_add(a, b):
        return a + b


class UART:
    def __init__(self, *_args, **kwargs):
        self.rxbuf = kwargs["rxbuf"]
        self.sent = []

    def write(self, data):
        self.sent.append((Clock.now, data))

    def any(self):
        return 0

    def deinit(self):
        pass


class OLED:
    width = 128
    height = 64

    def __init__(self):
        self.frames = []

    def show(self):
        self.frames.append(Clock.now)
        Clock.now += 25  # measured full display transfer on the 2025 badge

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class Writer:
    font = types.SimpleNamespace(height=lambda: 14)

    def __init__(self, *_args, **_kwargs):
        pass

    def set_textpos(self, *_args):
        pass

    def printstring(self, *_args):
        pass

    def stringlen(self, value):
        return len(value) * 6


class PongTests(unittest.TestCase):
    def setUp(self):
        Clock.now = 100000
        self.badge = types.ModuleType("bsides")
        self.badge.device_id = "AAAAAAAAAAAA"
        self.badge.GamesScreen = lambda oled: oled

        machine = types.ModuleType("machine")
        machine.Pin = lambda *_args, **_kwargs: None
        machine.UART = UART

        uasyncio = types.ModuleType("uasyncio")
        uasyncio.create_task = lambda coro: (coro.close(), None)[1]
        uasyncio.sleep_ms = self.sleep_ms
        uasyncio.CancelledError = asyncio.CancelledError

        writer_package = types.ModuleType("writer")
        writer_package.__path__ = []
        writer = types.ModuleType("writer.writer")
        writer.Writer = Writer
        font6 = types.ModuleType("writer.font6")
        freesans20 = types.ModuleType("writer.freesans20")
        urandom = types.ModuleType("urandom")
        urandom.getrandbits = random.getrandbits
        fake_time = types.ModuleType("time")
        fake_time.ticks_ms = Clock.ticks_ms
        fake_time.ticks_diff = Clock.ticks_diff
        fake_time.ticks_add = Clock.ticks_add

        modules = {
            "bsides": self.badge, "machine": machine, "uasyncio": uasyncio,
            "writer": writer_package, "writer.writer": writer,
            "writer.font6": font6,
            "writer.freesans20": freesans20, "urandom": urandom,
            "time": fake_time,
        }
        self.patcher = patch.dict(sys.modules, modules)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        spec = importlib.util.spec_from_file_location("pong_under_test", PONG_PATH)
        self.pong = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pong)

    async def sleep_ms(self, milliseconds):
        Clock.now += milliseconds
        await asyncio.sleep(0)

    def test_guest_heartbeats_and_rendering_survive_slow_display(self):
        screen = self.pong.PongScreen(OLED())
        self.assertEqual(screen.uart.rxbuf, 1024)
        screen.phase = "play"
        screen.is_host = False
        screen.play_start = Clock.now
        screen.last_rx = Clock.now
        stop_at = Clock.now + 2000

        async def run():
            task = asyncio.create_task(screen._loop())
            while Clock.now < stop_at:
                await asyncio.sleep(0)
            screen.running = False
            await task

        asyncio.run(run())
        beats = [when for when, data in screen.uart.sent
                 if self.pong.unframe(data[:-1]).startswith(b"P")]
        self.assertGreaterEqual(len(beats), 7)
        self.assertLessEqual(max(b - a for a, b in zip(beats, beats[1:])), 300)
        self.assertGreaterEqual(len(screen.oled.frames), 25)
        self.assertLessEqual(len(screen.oled.frames), 38)
        self.assertEqual(screen.phase, "play")

    def test_lost_peer_reconnects_without_button(self):
        screen = self.pong.PongScreen(OLED())
        screen.phase = "lost"
        screen.peer_id = "BBBBBBBBBBBB"
        screen._handle_line(b"HBBBBBBBBBBBB")
        self.assertEqual(screen.phase, "link")
        self.assertFalse(screen.is_host)
        self.assertTrue(any(self.pong.unframe(data[:-1]) == b"HAAAAAAAAAAAA"
                            for _, data in screen.uart.sent))
        screen._handle_line(b"G")
        self.assertEqual(screen.phase, "count")

    def test_playing_host_accepts_seeking_peer(self):
        self.badge.device_id = "BBBBBBBBBBBB"
        screen = self.pong.PongScreen(OLED())
        screen.phase = "play"
        screen.peer_id = "AAAAAAAAAAAA"
        screen.is_host = True
        screen.got_peer = True
        screen.play_start = Clock.now - 2000
        screen._handle_line(b"HAAAAAAAAAAAA")
        self.assertEqual(screen.phase, "count")
        self.assertFalse(screen.got_peer)
        self.assertTrue(any(self.pong.unframe(data[:-1]) == b"G"
                            for _, data in screen.uart.sent))

    def test_corrupt_and_unexpected_end_packets_are_ignored(self):
        valid = self.pong.frame(b"E1,2")
        self.assertEqual(self.pong.unframe(valid[:-1]), b"E1,2")
        self.assertIsNone(self.pong.unframe(b"E1,2"))
        self.assertIsNone(self.pong.unframe(valid[:-2] + b"0"))
        screen = self.pong.PongScreen(OLED())
        screen.is_host = False
        screen.phase = "link"
        screen._handle_line(b"E1,2")
        self.assertEqual(screen.phase, "link")


if __name__ == "__main__":
    unittest.main()
