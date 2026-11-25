"""Measure decoded RGB frame throughput using observation timestamps."""
import argparse
import math
import time
from collections import Counter
from array import array

if __package__:
    from .esp_control import EspControl
    from .peripherals import RGB
    from .heap_client import HeapClient
    from .telemetry import Telemetry
else:
    from esp_control import EspControl
    from peripherals import RGB
    from heap_client import HeapClient
    from telemetry import Telemetry


class LatencyHistogram:
    """Fixed 1 ms buckets, with a final >= 10000 ms overflow bucket."""
    LIMIT_MS = 10000

    def __init__(self):
        self.bins = array("Q", [0]) * (self.LIMIT_MS + 1)
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0

    def add(self, ms):
        self.bins[min(int(ms), self.LIMIT_MS)] += 1
        self.count += 1
        self.total += ms
        self.maximum = max(self.maximum, ms)

    def median(self):
        ranks = ((self.count - 1)//2, self.count//2)
        cumulative = 0
        values = []
        for index, count in enumerate(self.bins):
            cumulative += count
            while len(values) < 2 and cumulative > ranks[len(values)]:
                values.append(index)
            if len(values) == 2:
                return sum(values)/2, self.LIMIT_MS in values
        raise ValueError("No latency observations")


def measure(rgb, duration=30, interval=0.01, expected_pixels=16, memory=None, heap=None):
    if not all(math.isfinite(x) and x > 0 for x in (duration, interval)) or expected_pixels < 1:
        raise ValueError("Expected positive duration, interval and pixel count")
    counts = Counter()
    latencies = LatencyHistogram()
    initial = rgb.state()
    start = previous_time = last_time = time.monotonic()
    previous_frames = last_frames = initial.frames
    deadline = start + duration
    while not latencies.count or time.monotonic() < deadline:
        before = time.monotonic()
        state = rgb.state()
        last_time = time.monotonic()
        if heap:
            heap.poll(last_time)
        if memory:
            memory(last_time)
        latencies.add((last_time - before) * 1000)
        if state.frames < last_frames:
            raise ValueError("Frame counter reset; repeat measurement")
        last_frames = state.frames
        counts[len(state.colors)] += 1
        if last_time - previous_time >= 1:
            fps = (last_frames - previous_frames) / (last_time - previous_time)
            print(f"FPS: {fps:7.2f} | Pixels: {len(state.colors):2d} | Frames: {last_frames}")
            previous_frames, previous_time = last_frames, last_time
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    elapsed = last_time - start
    total = sum(counts.values())
    median_ms, median_overflow = latencies.median()
    return {
        "elapsed": elapsed,
        "fps": (last_frames - initial.frames) / elapsed,
        "polling_hz": total / elapsed,
        "mean_ms": latencies.total/latencies.count,
        "median_ms": median_ms,
        "median_resolution_ms": 1,
        "median_overflow": median_overflow,
        "max_ms": latencies.maximum,
        "pixel_counts": dict(counts),
        "incomplete_percent": 100 * (total - counts[expected_pixels]) / total,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5556)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--interval", type=float, default=0.01)
    parser.add_argument("--heap-file")
    parser.add_argument("--memory", action="store_true", help="Sample local host process RSS")
    parser.add_argument("--expected-pixels", type=int, default=16)
    args = parser.parse_args()
    if not all(math.isfinite(x) and x > 0 for x in (args.duration, args.interval)) or args.expected_pixels < 1:
        parser.error("Expected finite positive duration, interval and pixel count")
    heap = HeapClient(args.heap_file) if args.heap_file else None
    telemetry = Telemetry()
    def sample(now):
        telemetry.sample(now, args.host, args.port)
    try:
        with EspControl(args.host, args.port) as control:
            result = measure(RGB(control), args.duration, args.interval, args.expected_pixels, sample if args.memory else None, heap)
        if args.memory:
            result.update(emulator_rss_kib=telemetry.emulator_rss, emulator_peak_kib=telemetry.emulator_peak)
        if heap:
            result.update(guest_min_free_bytes=heap.minimum, guest_peak_allocated_bytes=heap.peak, guest_sample_fresh=heap.fresh(time.monotonic()))
        for key, value in result.items():
            print(f"{key}: {value}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Benchmark failed: {exc}\n")
    finally:
        if heap:
            heap.close()


if __name__ == "__main__":
    main()
