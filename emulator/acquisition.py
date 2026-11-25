"""Single-owner control worker and immutable latest-value snapshots."""
from dataclasses import dataclass, replace
from queue import Queue, Empty, Full
from threading import Thread, Event, Lock
import time

if __package__:
    from .esp_control import EspControl
    from .peripherals import VirtualBadge, parse_rgb, RGBState, CompleteRGB
    from .heap_client import HeapClient
    from .telemetry import FrameRate, Telemetry
else:
    from esp_control import EspControl
    from peripherals import VirtualBadge, parse_rgb, RGBState, CompleteRGB
    from heap_client import HeapClient
    from telemetry import FrameRate, Telemetry


@dataclass(frozen=True)
class MemorySnapshot:
    emulator_pid: int | None = None
    emulator_rss: int | None = None
    emulator_peak: int | None = None
    controller_rss: int | None = None
    controller_peak: int | None = None
    sampled: float | None = None


@dataclass(frozen=True)
class HeapSnapshot:
    allocated: int
    free: int
    minimum: int
    peak: int
    received: float


@dataclass(frozen=True)
class BadgeSnapshot:
    generation: int = 0
    sequence: int = 0
    published: float = 0.0
    connected: bool = False
    status: str = "Connecting"
    frame: bytes | None = None
    oled_time: float | None = None
    pixels: tuple[str, ...] = ()
    rgb_time: float | None = None
    frames: int = 0
    fps: float | None = None
    incomplete: int = 0
    observations: int = 0
    metadata: str = ""
    memory: MemorySnapshot = MemorySnapshot()
    heap: HeapSnapshot | None = None
    poll_hz: float = 0.0
    rtt_ms: float | None = None
    input_ms: float | None = None
    queue_depth: int = 0


class LatestMailbox:
    """One ready reference; consumer may retain its previous immutable snapshot."""
    def __init__(self):
        self._lock = Lock()
        self._latest = BadgeSnapshot()
        self._unread = False
        self._dropped = 0

    def publish(self, snapshot):
        with self._lock:
            if self._unread:
                self._dropped += 1
            self._latest = replace(snapshot, sequence=self._latest.sequence + 1)
            self._unread = True

    def read(self):
        with self._lock:
            self._unread = False
            return self._latest, self._dropped


@dataclass(frozen=True)
class Input:
    action: str
    generation: int
    queued: float


class Acquisition:
    """Own all sockets and mutable acquisition state in one worker."""
    def __init__(self, args, connect=EspControl, queue_size=32):
        self.args = args
        self.connect = connect
        self.mailbox = LatestMailbox()
        self.inputs = Queue(maxsize=queue_size)
        self.stop_event = Event()
        self.wake = Event()
        self.thread = Thread(target=self._run, name="badge-acquisition", daemon=True)
        self.snapshot = BadgeSnapshot()
        self.last_published = None
        self.badge = None
        self.pending = set()
        self.scheduled = set()
        self.rgb = CompleteRGB()
        self.rate = FrameRate()
        self.telemetry = Telemetry()
        self.heap = HeapClient(args.heap_file) if getattr(args, 'heap_file', None) else None
        self.next_metadata = 0.0
        self.next_recovery = 0.0
        self.next_connect = 0.0
        self.poll_count = 0
        self.poll_start = time.monotonic()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        # The worker owns closure; bounded socket timeouts allow orderly shutdown.
        self.thread.join(timeout=12)
        return not self.thread.is_alive()

    def submit(self, action, generation):
        try:
            self.inputs.put_nowait(Input(action, generation, time.monotonic()))
        except Full:
            return False
        self.wake.set()
        return True

    def _set(self, **values):
        if any(getattr(self.snapshot, key) != value for key, value in values.items()):
            self.snapshot = replace(self.snapshot, **values)

    def _publish(self):
        self._set(queue_depth=self.inputs.qsize())
        if self.snapshot is self.last_published:
            return
        self._set(published=time.monotonic())
        self.mailbox.publish(self.snapshot)
        self.last_published = self.snapshot

    def _boundary(self, status, connected):
        self.rgb.reset()
        self.rate.reset()
        if self.heap:
            self.heap.close()
        self.next_metadata = 0
        self.poll_count = 0
        self.poll_start = time.monotonic()
        if self.badge:
            self.badge.oled.frame = None
        self._set(generation=self.snapshot.generation+1, connected=connected,
                  status=status, frame=None, oled_time=None, pixels=(), rgb_time=None,
                  frames=0, fps=None, incomplete=0, observations=0, metadata="",
                  heap=None, memory=MemorySnapshot(), poll_hz=0, rtt_ms=None, input_ms=None)
        self.telemetry.next_sample = 0
        self.telemetry.emulator_pid = None
        if connected:
            self.telemetry.next_discovery = 0
        self._publish()

    def _disconnect(self, status):
        if self.badge:
            self.badge.control.close()
            self.badge = None
        self.next_connect = time.monotonic() + 1
        self._boundary(status, False)

    def _connect(self):
        args = self.args
        connection = self.connect(args.host, args.port)
        try:
            if isinstance(connection, EspControl):
                connection.cancel_event = self.stop_event
            badge = VirtualBadge(connection, **{name: getattr(args, name)
                                 for name in ("oled", "rgb", "back", "prev", "next", "select")})
            if args.oled not in badge.parts:
                raise ValueError("OLED part missing")
            self.badge = badge
            self.pending.update(self.scheduled)
            self.scheduled.clear()
            self._boundary("Connected; waiting for OLED update", True)
            self.next_recovery = 0
        except (OSError, ValueError):
            connection.close()
            self.badge = None
            raise

    def _recover(self):
        errors = []
        for name in tuple(self.pending):
            button = self.badge.buttons.get(name)
            try:
                if button is not None and button.state() != "pressed=false":
                    button.release()
                self.pending.discard(name)
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise ValueError("; ".join(errors))

    def _input(self, event):
        if not self.badge or event.generation != self.snapshot.generation:
            self._set(status="Input rejected: connection or generation changed")
            return
        self._set(input_ms=(time.monotonic()-event.queued)*1000)
        if event.action == "reset":
            self.badge.reset()
            self.pending.update(self.scheduled)
            self.scheduled.clear()
            self._boundary("Reset requested; waiting for OLED update", True)
            self._recover()
            return
        if event.action not in self.badge.buttons:
            self._set(status=f"Button {event.action} not attached")
            return
        if event.action in self.pending:
            self._set(status=f"{event.action}: recovery pending; input rejected")
            return
        if event.action in self.scheduled:
            if self.badge.buttons[event.action].state() != "pressed=false":
                self._set(status=f"{event.action}: previous hold active; input rejected")
                return
            self.scheduled.discard(event.action)
        self.pending.add(event.action)
        self.scheduled.add(event.action)
        self.badge.buttons[event.action].press(self.args.press_ms)
        self.pending.discard(event.action)
        self._set(status=f"{event.action}: press/release scheduled")

    def _poll(self):
        args, badge = self.args, self.badge
        before = time.monotonic()
        commands = [f"part {args.oled} render"]
        has_rgb = args.rgb in badge.parts
        if has_rgb:
            commands.append(f"part {args.rgb} state")
        state_due = before >= self.next_metadata
        if state_due:
            commands.append(f"part {args.oled} state")
        replies = badge.control.commands(commands)
        now = time.monotonic()
        self._set(rtt_ms=(now-before)*1000)
        # Decode RGB before OLED so a counter rollback invalidates older state.
        if has_rgb:
            try:
                if isinstance(replies[1], Exception):
                    raise replies[1]
                frames, colors = parse_rgb(replies[1])
                if frames < self.rgb.frames:
                    self.pending.update(self.scheduled)
                    self.scheduled.clear()
                    self._boundary("RGB counter reset; waiting for new state", True)
                    # Discard the boundary-crossing batch; observations are not atomic.
                    return
                self.rgb.observe(RGBState(frames, tuple(colors)))
                self.rate.observe(frames, now)
                self._set(frames=frames, pixels=self.rgb.colors, fps=self.rate.fps,
                          incomplete=self.rgb.incomplete, observations=self.rgb.observations,
                          rgb_time=now if len(colors)==self.rgb.expected else self.snapshot.rgb_time)
            except ValueError as exc:
                self._set(status=f"RGB error: {exc}")
        try:
            if isinstance(replies[0], Exception):
                raise replies[0]
            frame = badge.oled.decode(replies[0])
            if frame is not None:
                self._set(frame=frame, oled_time=now)
        except ValueError as exc:
            self._set(status=f"OLED error: {exc}")
        if state_due:
            self.next_metadata = now + 1
            for name in tuple(self.scheduled - self.pending):
                try:
                    if badge.buttons[name].state() == "pressed=false":
                        self.scheduled.discard(name)
                except ValueError as exc:
                    self._set(status=f"Button state error: {exc}")
            if isinstance(replies[-1], Exception):
                self._set(status=f"OLED state error: {replies[-1]}")
            else:
                self._set(metadata=replies[-1])
        self.poll_count += 1
        if now-self.poll_start >= 1:
            self._set(poll_hz=self.poll_count/(now-self.poll_start))
            self.poll_count = 0
            self.poll_start = now

    def _sample(self):
        now = time.monotonic()
        t = self.telemetry
        if now >= t.next_sample:
            t.sample(now, self.args.host, self.args.port, self.args.emulator_pid)
            self._set(memory=MemorySnapshot(t.emulator_pid, t.emulator_rss,
                      t.emulator_peak, t.controller_rss, t.controller_peak, now))
        if self.heap and self.badge:
            changed = self.heap.poll(now)
            if self.heap.fresh(now):
                if changed or self.snapshot.heap is None:
                    h = self.heap
                    self._set(heap=HeapSnapshot(h.allocated, h.free, h.minimum, h.peak, h.received))
            elif self.snapshot.heap is not None:
                self._set(heap=None)

    def _run(self):
        interval = 1/self.args.refresh_hz
        next_poll = 0.0
        try:
            while not self.stop_event.is_set():
                self.wake.clear()
                now = time.monotonic()
                try:
                    if self.badge is None and now >= self.next_connect:
                        self._connect()
                        next_poll = now
                    # Bound input work per iteration to avoid starvation of polling.
                    for _ in range(4):
                        if self.stop_event.is_set():
                            break
                        try:
                            event = self.inputs.get_nowait()
                        except Empty:
                            break
                        try:
                            self._input(event)
                        except ValueError as exc:
                            self._set(status=f"Input failed: {exc}")
                    if self.stop_event.is_set():
                        break
                    if self.badge and self.pending and now >= self.next_recovery:
                        self.next_recovery = now + 1
                        try:
                            self._recover()
                        except ValueError as exc:
                            self._set(status=f"Button recovery failed: {exc}")
                    if self.badge and time.monotonic() >= next_poll:
                        self._poll()
                        finished = time.monotonic()
                        next_poll += (int(max(0, finished-next_poll)/interval)+1)*interval
                    self._sample()
                except (OSError, ValueError) as exc:
                    self._disconnect(f"Control unavailable: {exc}")
                self._publish()
                delay = max(0, next_poll-time.monotonic()) if self.badge else 0.1
                if self.inputs.empty():
                    self.wake.wait(min(interval, delay) if self.badge else delay)
        except Exception as exc:
            self._boundary(f"Acquisition failed: {exc}", False)
        finally:
            if self.badge:
                control = self.badge.control
                if isinstance(control, EspControl):
                    control.cancel_event = None
                    control.deadline_limit = time.monotonic() + 2
                try:
                    self.pending.update(self.scheduled)
                    self._recover()
                except (OSError, ValueError):
                    pass
                finally:
                    control.close()
            if self.heap:
                self.heap.close()
