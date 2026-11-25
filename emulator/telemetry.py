"""Host process memory and sampled RGB throughput (no guest instrumentation)."""
from dataclasses import dataclass
from pathlib import Path
import os


def process_memory(pid, proc=Path("/proc")):
    """Return Linux RSS and high-water mark in KiB, or unavailable values."""
    try:
        fields = dict(line.split(":", 1) for line in
                      (proc / str(pid) / "status").read_text().splitlines() if ":" in line)
        def kib(key):
            value, unit = fields[key].split()
            return int(value) if unit == "kB" else None
        return kib("VmRSS"), kib("VmHWM")
    except (OSError, ValueError, KeyError):
        return None, None


def matches_emulator(pid, host, port, proc=Path("/proc")):
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    endpoints = {f"localhost:{port}", f"127.0.0.1:{port}"} if host in ("localhost", "127.0.0.1") else {f"{host}:{port}"}
    try:
        args = (proc / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
        return Path(args[0]).name == "esp-emu" and any(
            arg == "--control-tcp" and i+1 < len(args) and args[i+1] in endpoints
            or arg in {"--control-tcp=" + endpoint for endpoint in endpoints}
            for i, arg in enumerate(args))
    except OSError:
        return False


def find_emulator(host, port, proc=Path("/proc")):
    """Find a unique local esp-emu process with the requested control endpoint."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    matches = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.name.isdecimal() and matches_emulator(int(entry.name), host, port, proc):
            matches.append(int(entry.name))
    return matches[0] if len(matches) == 1 else None


@dataclass
class FrameRate:
    previous_frames: int | None = None
    previous_time: float = 0.0
    fps: float | None = None

    def reset(self):
        self.previous_frames = None
        self.fps = None

    def observe(self, frames, now):
        if self.previous_frames is None or frames < self.previous_frames or now < self.previous_time:
            self.previous_frames = frames
            self.previous_time = now
            self.fps = None
        elif now - self.previous_time >= 1:
            self.fps = (frames - self.previous_frames) / (now - self.previous_time)
            self.previous_frames = frames
            self.previous_time = now


@dataclass
class Telemetry:
    emulator_pid: int | None = None
    emulator_rss: int | None = None
    emulator_peak: int | None = None
    controller_rss: int | None = None
    controller_peak: int | None = None
    next_sample: float = 0.0
    next_discovery: float = 0.0

    def sample(self, now, host, port, pid=None):
        if now < self.next_sample:
            return
        self.next_sample = now + 1
        if pid is not None:
            self.emulator_pid = pid
        else:
            if self.emulator_pid is not None and not matches_emulator(self.emulator_pid, host, port):
                self.emulator_pid = None
                self.next_discovery = 0
            if self.emulator_pid is None and now >= self.next_discovery:
                self.emulator_pid = find_emulator(host, port)
                self.next_discovery = now + 5
        self.emulator_rss, self.emulator_peak = process_memory(self.emulator_pid) if self.emulator_pid else (None, None)
        self.controller_rss, self.controller_peak = process_memory(os.getpid())


def mib(kib):
    return "n/a" if kib is None else f"{kib / 1024:.1f} MiB"
