import tempfile
import unittest
from pathlib import Path
from time import monotonic
from emulator.heap_client import HeapClient
from emulator.console_capture import Capture


class HeapTests(unittest.TestCase):
    def test_fragmented_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"heap.json"
            heap = HeapClient(path)
            capture = Capture(path)
            capture.feed(b"HEAP1 0 100")
            self.assertFalse(path.exists())
            capture.feed(b" 200\r\n")
            heap.next_poll = 0
            heap.poll()
            self.assertEqual(heap.allocated, 100)
            capture.feed(b"HEAP1 1 150 150\n")
            heap.next_poll = 0
            heap.poll()
            self.assertEqual((heap.minimum, heap.peak), (150, 150))
            self.assertFalse(heap.fresh(monotonic()+6))
            capture.feed(b"HEAP1 0 50 250\n")
            heap.next_poll = 0
            heap.poll()
            self.assertEqual((heap.minimum, heap.peak), (250, 50))
            heap.reset()
            heap.next_poll = 0
            heap.poll()
            self.assertIsNone(heap.allocated)

    def test_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"heap.json"
            capture = Capture(path)
            capture.feed(b"noise\nHEAP1 1 0 0\nHEAP1 2 9999999 1\n")
            capture.feed(b"x"*4096 + b"HEAP1 0 100 200\n")
            self.assertFalse(path.exists())
            capture.feed(b"HEAP1 0 100 200\n")
            self.assertTrue(path.exists())

    def test_client_rejects_invalid_samples(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"heap.json"
            heap = HeapClient(path)
            sample = dict(epoch=0, sequence=0, allocated=100, free=200,
                          minimum=200, peak=100, received=monotonic())
            for data in (b"{", b"{}", json.dumps(dict(sample, allocated=-1)).encode(),
                         json.dumps(dict(sample, received="invalid")).encode()):
                path.write_bytes(data)
                heap.next_poll = 0
                heap.poll()
                self.assertIsNone(heap.allocated)
            path.write_text(json.dumps(sample))
            heap.next_poll = 0
            heap.poll()
            self.assertEqual(heap.allocated, 100)

    def test_boot_marker_restarts_zero_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"heap.json"
            capture = Capture(path)
            capture.feed(b"HEAP1 BOOT\nHEAP1 0 150 150\n")
            capture.feed(b"HEAP1 BOOT\nHEAP1 0 50 250\n")
            self.assertEqual((capture.epoch, capture.minimum, capture.peak), (2, 250, 50))

    def test_publication_failure_does_not_stop_capture(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            capture = Capture(Path(tmp)/"heap.json")
            with patch("emulator.console_capture.os.replace", side_effect=OSError("disk error")):
                capture.feed(b"HEAP1 0 100 200\n")
            capture.feed(b"HEAP1 1 101 199\n")
            self.assertTrue(capture.path.exists())

    def test_shutdown_attempts_both_removals(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        from emulator.console_capture import main

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"heap.json"
            temp = path.with_suffix(".tmp")
            with patch("sys.argv", ["console_capture.py", str(path)]), \
                 patch("emulator.console_capture.sys.stdin") as stdin, \
                 patch("emulator.console_capture.os.read", return_value=b""), \
                 patch.object(Path, "unlink", autospec=True,
                              side_effect=[None, OSError("cleanup denied"), None]) as unlink, \
                 redirect_stderr(StringIO()) as stderr:
                stdin.fileno.return_value = 0
                main()
                self.assertEqual([call.args[0] for call in unlink.call_args_list],
                                 [path, path, temp])
                self.assertIn("Heap sample cleanup failed: cleanup denied", stderr.getvalue())

    def test_poll_reports_only_new_valid_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"heap.json"
            heap = HeapClient(path)
            capture = Capture(path)
            self.assertFalse(heap.poll())
            capture.feed(b"HEAP1 0 100 200\n")
            heap.next_poll = 0
            self.assertTrue(heap.poll())
            heap.next_poll = 0
            self.assertFalse(heap.poll())
            capture.feed(b"HEAP1 1 110 190\n")
            heap.next_poll = 0
            self.assertTrue(heap.poll())
