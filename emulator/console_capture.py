"""Forward console bytes and publish bounded heap samples atomically."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

RECORD = re.compile(rb"HEAP1 ([0-9]{1,10}) ([0-9]{1,10}) ([0-9]{1,10})")


class Capture:
    def __init__(self, path):
        self.path = Path(path)
        self.temp = self.path.with_suffix(".tmp")
        self.buffer = b""
        self.discard = False
        self.sequence = None
        self.epoch = 0
        self.minimum = self.peak = None

    def feed(self, data):
        while data:
            chunk, separator, data = data.partition(b"\n")
            if not self.discard:
                if len(self.buffer) + len(chunk) > 128:
                    self.discard = True
                    self.buffer = b""
                else:
                    self.buffer += chunk
            if separator:
                if not self.discard:
                    self.record(self.buffer.rstrip(b"\r"))
                self.discard = False
                self.buffer = b""

    def record(self, line):
        if line == b"HEAP1 BOOT":
            self.epoch += 1
            self.sequence = self.minimum = self.peak = None
            self.remove(self.path)
            return
        match = RECORD.fullmatch(line)
        if not match:
            return
        sequence, allocated, free = map(int, match.groups())
        if not 0 < allocated + free <= 512 * 1024:
            return
        if self.sequence is not None and sequence < self.sequence:
            self.epoch += 1
            self.minimum = self.peak = None
        if sequence == self.sequence:
            return
        self.sequence = sequence
        self.minimum = free if self.minimum is None else min(self.minimum, free)
        self.peak = allocated if self.peak is None else max(self.peak, allocated)
        sample = dict(epoch=self.epoch, sequence=sequence, allocated=allocated,
                      free=free, minimum=self.minimum, peak=self.peak,
                      received=time.monotonic())
        try:
            self.temp.write_text(json.dumps(sample), encoding="utf-8")
            os.replace(self.temp, self.path)
        except OSError as exc:
            print(f"Heap sample publication failed: {exc}", file=sys.stderr)
            self.remove(self.temp)

    @staticmethod
    def remove(path):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Heap sample cleanup failed: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    capture = Capture(args.path)
    capture.remove(args.path)
    try:
        while True:
            data = os.read(sys.stdin.fileno(), 4096)
            if not data:
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            capture.feed(data)
    finally:
        capture.remove(args.path)
        capture.remove(capture.temp)


if __name__ == "__main__":
    main()
