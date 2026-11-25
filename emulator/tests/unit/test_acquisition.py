import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock
from emulator.acquisition import Acquisition, BadgeSnapshot, LatestMailbox


class AcquisitionTests(unittest.TestCase):
    def test_latest_value_and_immutable_reader(self):
        mailbox = LatestMailbox()
        mailbox.publish(BadgeSnapshot(frame=b"a"))
        first, dropped = mailbox.read()
        self.assertEqual(dropped, 0)
        mailbox.publish(BadgeSnapshot(frame=b"b"))
        mailbox.publish(BadgeSnapshot(frame=b"c"))
        latest, dropped = mailbox.read()
        self.assertEqual((latest.frame, latest.sequence, dropped), (b"c", 3, 1))
        self.assertEqual(first.frame, b"a")
        with self.assertRaises(FrozenInstanceError):
            latest.frame = b"d"

    def test_input_queue_bound_and_fifo(self):
        worker = Acquisition(SimpleNamespace(host="localhost", heap_port=None), queue_size=2)
        self.assertTrue(worker.submit("next", 1))
        self.assertTrue(worker.submit("reset", 1))
        self.assertFalse(worker.submit("back", 1))
        self.assertEqual(worker.inputs.get_nowait().action, "next")
        self.assertEqual(worker.inputs.get_nowait().action, "reset")

    def test_boundary_invalidates_state_and_rejects_old_input(self):
        worker = Acquisition(SimpleNamespace(host="localhost", heap_port=None))
        worker._set(frame=b"old", pixels=("#ffffff",)*16, connected=True)
        worker.submit("next", 0)
        worker._boundary("reset", True)
        worker.badge = object()
        worker._input(worker.inputs.get_nowait())
        snapshot = worker.snapshot
        self.assertIsNone(snapshot.frame)
        self.assertEqual(snapshot.pixels, ())
        self.assertEqual(snapshot.generation, 1)
        self.assertIn("rejected", snapshot.status)

    def test_worker_lifecycle_and_nonblocking_mailbox(self):
        from threading import Event
        from time import monotonic, sleep

        started = Event()
        release = Event()

        class Control:
            closed = False
            def command(self, command):
                if command == "part list":
                    return "oled rgb next"
                return ""
            def commands(self, commands):
                started.set()
                release.wait(2)
                return ["00"*1024 if "render" in command else
                        "frames=1 pixels=0" if "rgb" in command else ""
                        for command in commands]
            def close(self):
                self.closed = True

        args = SimpleNamespace(host="localhost", port=5556, heap_port=None,
                               oled="oled", rgb="rgb", next="next", prev="prev",
                               back="back", select="select", refresh_hz=75,
                               press_ms=150, emulator_pid=None)
        control = Control()
        worker = Acquisition(args, connect=lambda *args: control)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            snapshot, _ = worker.mailbox.read()
            self.assertTrue(snapshot.connected)
            self.assertIsNone(snapshot.frame)
            self.assertTrue(worker.submit("next", snapshot.generation))
            release.set()
            deadline = monotonic()+2
            while monotonic()<deadline:
                latest, _ = worker.mailbox.read()
                if latest.frame is not None and latest.input_ms is not None:
                    break
                sleep(0.01)
            self.assertEqual(latest.frame, bytes(1024))
            self.assertIsNotNone(latest.input_ms)
            self.assertEqual(latest.incomplete, latest.observations)
        finally:
            release.set()
            self.assertTrue(worker.stop())
        self.assertTrue(control.closed)

    def test_reset_boundary_precedes_failed_release(self):
        from emulator.esp_control import ProtocolError
        from emulator.acquisition import Input
        from time import monotonic

        class Button:
            def state(self):
                return "pressed=true"
            def release(self):
                raise ProtocolError("release failed")

        worker = Acquisition(SimpleNamespace(host="localhost", heap_port=None))
        worker.badge = SimpleNamespace(reset=Mock(),
                                       oled=SimpleNamespace(frame=b"old"),
                                       buttons={"next": Button()})
        worker.scheduled.add("next")
        worker._set(frame=b"old", connected=True)
        with self.assertRaises(ValueError):
            worker._input(Input("reset", 0, monotonic()))
        worker.badge.reset.assert_called_once_with()
        snapshot, _ = worker.mailbox.read()
        self.assertEqual(snapshot.generation, 1)
        self.assertIsNone(snapshot.frame)
        self.assertIsNone(worker.badge.oled.frame)
        self.assertIn("next", worker.pending)

    def test_reconnect_does_not_depend_on_release(self):
        from emulator.esp_control import ProtocolError
        class Control:
            closed = False
            def command(self, command):
                if command == "part list":
                    return "oled next rgb"
                if command.endswith(" state"):
                    return "pressed=true"
                raise ProtocolError("release rejected")
            def close(self):
                self.closed = True
        args = SimpleNamespace(host="localhost", port=5556, heap_file=None,
                               oled="oled", rgb="rgb", next="next", prev="prev",
                               back="back", select="select")
        control = Control()
        worker = Acquisition(args, connect=lambda *args: control)
        worker.scheduled.add("next")
        worker._connect()
        with self.assertRaises(ValueError):
            worker._recover()
        self.assertTrue(worker.snapshot.connected)
        self.assertFalse(control.closed)
        self.assertIn("next", worker.pending)

    def test_unchanged_state_not_republished(self):
        worker = Acquisition(SimpleNamespace(host="localhost", heap_file=None))
        worker._publish()
        first, _ = worker.mailbox.read()
        worker._set(status=first.status)
        worker._publish()
        second, dropped = worker.mailbox.read()
        self.assertIs(first, second)
        self.assertEqual(dropped, 0)
        worker.submit("next", 0)
        worker._publish()
        latest, _ = worker.mailbox.read()
        self.assertEqual(latest.sequence, first.sequence+1)
        self.assertEqual(latest.queue_depth, 1)
        worker._boundary("reset", False)
        boundary, _ = worker.mailbox.read()
        self.assertEqual(boundary.generation, 1)

    def test_heap_snapshot_reused_until_new_record_or_expiry(self):
        worker = Acquisition(SimpleNamespace(host="localhost", heap_file=None))
        worker.badge = object()
        worker.telemetry.next_sample = float("inf")
        worker.heap = Mock(allocated=100, free=200, minimum=200, peak=100, received=1)
        worker.heap.poll.side_effect = [True, False, False, True]
        worker.heap.fresh.side_effect = [True, True, False, True]
        worker._sample()
        first = worker.snapshot.heap
        worker._sample()
        self.assertIs(worker.snapshot.heap, first)
        worker._sample()
        self.assertIsNone(worker.snapshot.heap)
        worker.heap.received = 10
        worker._sample()
        self.assertEqual(worker.snapshot.heap.received, 10)
