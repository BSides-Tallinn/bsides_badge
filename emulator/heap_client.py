"""Read the latest atomic console telemetry record; no guest UART ownership."""
import json
from pathlib import Path
import time


class HeapClient:
    def __init__(self, path):
        self.path = Path(path)
        self.reset()

    def reset(self):
        self.allocated = self.free = self.minimum = self.peak = None
        self.received = None
        self.epoch = self.sequence = None
        self.cutoff = time.monotonic()
        self.next_poll = 0.0

    def fresh(self, now):
        return self.received is not None and 0 <= now - self.received <= 5

    def close(self):
        self.reset()

    def poll(self, now=None):
        """Return True only when a newer valid record is accepted."""
        now = time.monotonic() if now is None else now
        if now < self.next_poll:
            return False
        self.next_poll = now + 0.25
        try:
            with self.path.open("rb") as stream:
                data = stream.read(1025)
            if len(data) > 1024:
                raise ValueError("Oversized sample")
            sample = json.loads(data)
            epoch, sequence, allocated, free, minimum, peak = (
                sample[key] for key in ("epoch", "sequence", "allocated", "free", "minimum", "peak"))
            received = sample["received"]
            if any(type(v) is not int or v < 0 for v in (epoch, sequence, allocated, free, minimum, peak)):
                raise ValueError("Invalid sample")
            if not 0 < allocated + free <= 512*1024 or minimum > free or peak < allocated:
                raise ValueError("Invalid heap values")
            if type(received) not in (int, float):
                raise ValueError("Invalid timestamp")
            if not self.cutoff <= received <= now or now-received > 5:
                return False
        except (OSError, ValueError, TypeError, KeyError):
            return False
        if self.received is not None and received <= self.received:
            return False
        if epoch != self.epoch or (self.sequence is not None and sequence < self.sequence):
            self.minimum = self.peak = None
        self.epoch, self.sequence = epoch, sequence
        self.allocated, self.free = allocated, free
        # Extrema are scoped to this reader's observed generation, not historical file values.
        self.minimum = free if self.minimum is None else min(self.minimum, free)
        self.peak = allocated if self.peak is None else max(self.peak, allocated)
        self.received = received
        return True
