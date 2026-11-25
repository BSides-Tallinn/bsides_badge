import unittest
import socket
from unittest.mock import patch, call
from emulator.esp_control import EspControl, ProtocolError


class ControlTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("socket.create_connection")
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)
        self.control = EspControl()
        self.addCleanup(self.control.close)
        self.sock = self.connect.return_value
        readiness = patch("emulator.esp_control.select.select", return_value=([], [], []))
        self.ready = readiness.start()
        self.addCleanup(readiness.stop)

    def test_success(self):
        self.sock.recv.side_effect = [b"ok\n", b"ok oled next\n"]
        self.assertEqual(self.control.command("ping"), "")
        self.assertEqual(self.control.command("part list"), "oled next")
        self.assertEqual(self.sock.sendall.call_args_list, [call(b"ping\n"), call(b"part list\n")])

    def test_rejection_keeps_connection(self):
        self.sock.recv.side_effect = [b"err unknown\n", b"ok\n"]
        with self.assertRaises(ProtocolError):
            self.control.command("bad")
        self.assertEqual(self.control.command("ping"), "")

    def test_truncated_response_closes_connection(self):
        self.sock.recv.side_effect = [b"partial", b""]
        with self.assertRaises(ConnectionError):
            self.control.command("ping")
        with self.assertRaises(ConnectionError):
            self.control.command("ping")
        self.sock.sendall.assert_called_once()

    def test_timeout_closes_connection(self):
        self.sock.recv.side_effect = OSError("timed out")
        with self.assertRaises(ConnectionError):
            self.control.command("ping")
        self.assertTrue(self.control.closed)
        self.sock.close.assert_called_once()

    def test_command_injection(self):
        with self.assertRaises(ValueError):
            self.control.command("ping\nreset")
        self.sock.sendall.assert_not_called()

    def test_batch_drains_rejection(self):
        self.sock.recv.side_effect = [b"err: unknown\n", b"ok second\n", b"ok\n"]
        result = self.control.commands(("bad", "part list"))
        self.assertIsInstance(result[0], ProtocolError)
        self.assertEqual(result[1], "second")
        self.sock.sendall.assert_called_with(b"bad\npart list\n")
        self.assertEqual(self.control.command("ping"), "")

    def test_batch_truncation(self):
        self.sock.recv.side_effect = [b"ok\n", b""]
        with self.assertRaises(ConnectionError):
            self.control.commands(("ping", "ping"))
        self.assertTrue(self.control.closed)

    def test_interrupted_batch_closes_resources(self):
        self.sock.recv.side_effect = [b"ok\n", KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.control.commands(("ping", "ping"))
        self.assertTrue(self.control.closed)
        self.sock.close.assert_called_once()
        with self.assertRaises(ConnectionError):
            self.control.command("ping")

    def test_bare_batch_rejected(self):
        for value in ("ping", b"ping", (b"ping",)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.control.commands(value)
        self.sock.sendall.assert_not_called()

    def test_batch_deadline_includes_fragmented_replies(self):
        now = [0.0]
        def recv(size):
            now[0] += 1.1
            return b"o" if now[0] < 2 else b"k"
        self.sock.recv.side_effect = recv
        with patch("emulator.esp_control.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(ConnectionError, "deadline"):
                self.control.commands(("ping", "ping"))
        self.assertEqual(self.sock.recv.call_count, 3)
        self.sock.close.assert_called_once()

    def test_cancel_closes_connection(self):
        from threading import Event
        self.control.cancel_event = Event()
        self.control.cancel_event.set()
        with self.assertRaisesRegex(ConnectionError, "cancelled"):
            self.control.command("ping")
        self.sock.sendall.assert_not_called()
        self.sock.close.assert_called_once()

    def test_fragmented_and_coalesced_replies(self):
        self.sock.recv.side_effect = [b"o", b"k\nok second\n"]
        self.assertEqual(self.control.commands(("ping", "part list")), ["", "second"])
        self.assertFalse(self.control.closed)

    def test_completed_batch_not_discarded_at_deadline(self):
        now = [0.0]
        def recv(size):
            now[0] = 3.0
            return b"ok\nok second\n"
        self.sock.recv.side_effect = recv
        with patch("emulator.esp_control.time.monotonic", side_effect=lambda: now[0]):
            self.assertEqual(self.control.commands(("ping", "part list")), ["", "second"])
        self.assertFalse(self.control.closed)

    def test_buffered_trailing_response_rejected(self):
        self.sock.recv.return_value = b"ok\nok unsolicited\n"
        with self.assertRaisesRegex(ConnectionError, "trailing"):
            self.control.command("ping")
        self.sock.close.assert_called_once()

    def test_pending_response_after_batch_rejected(self):
        self.ready.side_effect = [([], [], []), ([self.sock], [], [])]
        self.sock.recv.side_effect = [b"ok\n", b"o"]
        with self.assertRaisesRegex(ConnectionError, "unsolicited"):
            self.control.command("ping")
        self.sock.recv.assert_called_with(1, socket.MSG_PEEK)
        self.sock.close.assert_called_once()

    def test_pending_response_before_next_batch_rejected(self):
        self.ready.side_effect = [([], [], []), ([], [], []), ([self.sock], [], [])]
        self.sock.recv.side_effect = [b"ok\n", b"o"]
        self.assertEqual(self.control.command("ping"), "")
        with self.assertRaisesRegex(ConnectionError, "unsolicited"):
            self.control.command("part list")
        self.sock.sendall.assert_called_once_with(b"ping\n")
        self.sock.close.assert_called_once()

    def test_clean_eof_after_complete_batch_preserves_reply(self):
        self.ready.side_effect = [([], [], []), ([self.sock], [], [])]
        self.sock.recv.side_effect = [b"ok\n", b""]
        self.assertEqual(self.control.command("ping"), "")
        self.assertTrue(self.control.closed)
        self.sock.close.assert_called_once()
