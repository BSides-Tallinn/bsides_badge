"""Synchronous ESP-EMU control protocol; one owner per connection."""
import socket
import select
import math
import time
from itertools import islice


class ProtocolError(ValueError):
    """A complete error response; the connection remains usable."""


class EspControl:
    def __init__(self, host="127.0.0.1", port=5556, timeout=3, nodelay=True):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Expected finite positive control timeout")
        self.timeout = timeout
        self.cancel_event = None
        self.deadline_limit = None
        self.buffer = bytearray()
        self.sock = socket.create_connection((host, port), timeout=timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, int(nodelay))
        except BaseException:
            self.sock.close()
            raise
        self.closed = False

    def command(self, command):
        result = self.commands((command,))[0]
        if isinstance(result, ProtocolError):
            raise result
        return result

    def commands(self, commands):
        """Send a bounded batch; drain every reply even on protocol rejection."""
        if isinstance(commands, (str, bytes)):
            raise ValueError("Expected a sequence of commands")
        commands = tuple(islice(commands, 17))
        if not 1 <= len(commands) <= 16:
            raise ValueError("Expected 1 to 16 commands")
        if any(not isinstance(command, str) or not command or "\n" in command or "\r" in command for command in commands):
            raise ValueError("Expected one command per entry")
        data = ("\n".join(commands) + "\n").encode("ascii")
        if self.closed:
            raise ConnectionError("Control connection closed")
        results = []
        deadline = time.monotonic() + self.timeout
        if self.deadline_limit is not None:
            deadline = min(deadline, self.deadline_limit)
        try:
            self._check_idle_socket()
            if self.closed:
                raise ConnectionError("Control peer closed connection")
            self.sock.settimeout(self._remaining(deadline))
            self.sock.sendall(data)
            for _ in commands:
                raw = self._readline(deadline)
                if not raw or not raw.endswith(b"\n"):
                    raise ConnectionError("Incomplete control response")
                line = raw.decode("ascii").strip()
                if line == "ok":
                    results.append("")
                elif line.startswith("ok "):
                    results.append(line[3:])
                elif line == "err" or line.startswith(("err ", "err:")):
                    results.append(ProtocolError(line))
                else:
                    raise ConnectionError("Unexpected control response")
            self._check_idle_socket()
            return results
        except BaseException as exc:
            try:
                self.close()
            except BaseException:
                # Preserve the original interruption/error; close attempts both resources.
                pass
            if isinstance(exc, (OSError, UnicodeError)):
                raise ConnectionError(str(exc)) from exc
            raise

    def _check_idle_socket(self):
        """Reject unsolicited data already available between request batches."""
        if self.buffer:
            raise ConnectionError("Unexpected trailing control data")
        if not select.select([self.sock], [], [], 0)[0]:
            return
        # Readability may indicate EOF rather than unsolicited data. Do not
        # block waiting for bytes, or discard a completed batch on clean EOF.
        self.sock.setblocking(False)
        try:
            pending = self.sock.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return
        finally:
            self.sock.settimeout(self.timeout)
        if pending:
            raise ConnectionError("Unexpected unsolicited control data")
        self.close()

    def _remaining(self, deadline):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ConnectionError("Control operation cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Control batch deadline exceeded")
        return remaining

    def _readline(self, deadline):
        """Read one bounded line without resetting the absolute batch deadline."""
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                if end >= 65536:
                    raise ConnectionError("Control response too long")
                raw = bytes(self.buffer[:end+1])
                del self.buffer[:end+1]
                return raw
            if len(self.buffer) >= 65536:
                raise ConnectionError("Control response too long")
            remaining = self._remaining(deadline)
            self.sock.settimeout(min(remaining, 0.2) if self.cancel_event is not None else remaining)
            try:
                chunk = self.sock.recv(min(4096, 65536-len(self.buffer)))
            except socket.timeout:
                self._remaining(deadline)
                continue
            if not chunk:
                raise ConnectionError("Incomplete control response")
            self.buffer.extend(chunk)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.buffer.clear()
            self.sock.close()
