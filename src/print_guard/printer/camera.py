"""Chamber camera client for P1/A1-series printers (TLS on port 6000).

After the TLS handshake the client sends an 80-byte login (two 32-bit words
0x40 and 0x3000, eight zero bytes, then "bblp" and the access code, each padded
to 32 bytes). The printer then streams JPEG frames, each preceded by a 16-byte
header whose first four bytes are the little-endian JPEG size. A P1S sends
roughly one frame every one to two seconds.

The printer allows only a couple of simultaneous camera viewers, so this keeps
one connection open and shares the newest frame with the rest of print-guard.
"""

from __future__ import annotations

import collections
import logging
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass

from print_guard.printer.tls import verify_identity

log = logging.getLogger(__name__)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MAX_FRAME_BYTES = 8 * 1024 * 1024


def auth_packet(access_code: str, username: str = "bblp") -> bytes:
    def pad(s: str) -> bytes:
        raw = s.encode("ascii")
        if len(raw) > 32:
            raise ValueError("camera credentials longer than 32 bytes")
        return raw.ljust(32, b"\0")

    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + pad(username) + pad(access_code)


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    at: float
    index: int


class CameraError(RuntimeError):
    pass


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("camera stream closed by the printer")
        buf += chunk
    return bytes(buf)


def read_frame(sock: ssl.SSLSocket) -> bytes:
    """Read one framed JPEG from an authenticated camera socket."""
    header = _recv_exact(sock, 16)
    size = int.from_bytes(header[0:4], "little")
    if not 0 < size <= MAX_FRAME_BYTES:
        raise CameraError(f"implausible frame size {size}")
    payload = _recv_exact(sock, size)
    if not payload.startswith(JPEG_SOI) or not payload.endswith(JPEG_EOI):
        raise CameraError("frame is not a complete JPEG")
    return payload


def open_stream(host: str, port: int, access_code: str, ctx: ssl.SSLContext, timeout: float,
                verify_serial: str | None = None) -> ssl.SSLSocket:
    """Connect and log in. verify_serial checks the certificate after the handshake
    (only needed when the context doesn't already check it during the handshake)."""
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        tls = ctx.wrap_socket(raw, server_hostname=host)
        if verify_serial:
            verify_identity(tls, verify_serial)
        tls.settimeout(timeout)
        tls.sendall(auth_packet(access_code))
        return tls
    except BaseException:
        raw.close()
        raise


def grab_one(host: str, port: int, access_code: str, ctx: ssl.SSLContext, timeout: float = 15.0,
             verify_serial: str | None = None) -> bytes:
    """Connect, read a single frame and disconnect (used by `check` and `snapshot`)."""
    with open_stream(host, port, access_code, ctx, timeout, verify_serial) as tls:
        return read_frame(tls)


class CameraStream:
    """Background reader that keeps the newest frame and a sparse history."""

    def __init__(self, host: str, port: int, access_code: str, ctx: ssl.SSLContext,
                 history_every: float = 60.0, history_len: int = 45, read_timeout: float = 20.0,
                 verify_serial: str | None = None) -> None:
        self.host, self.port = host, port
        self._code = access_code
        self._ctx = ctx
        self._verify_serial = verify_serial
        self._timeout = read_timeout
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._history: collections.deque[Frame] = collections.deque(maxlen=history_len)
        self._history_every = history_every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._index = 0
        self.connected = False
        self.last_error = ""
        self.frames = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def frame_near(self, seconds_ago: float) -> Frame | None:
        """The stored frame closest to `seconds_ago` in the past (for before/after comparisons)."""
        target = time.time() - seconds_ago
        with self._lock:
            if not self._history:
                return None
            return min(self._history, key=lambda f: abs(f.at - target))

    def _keep(self, jpeg: bytes) -> None:
        now = time.time()
        with self._lock:
            self._index += 1
            frame = Frame(jpeg=jpeg, at=now, index=self._index)
            self._latest = frame
            if not self._history or now - self._history[-1].at >= self._history_every:
                self._history.append(frame)
        self.frames += 1

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            got_frame = False
            try:
                with open_stream(self.host, self.port, self._code, self._ctx, self._timeout,
                                 self._verify_serial) as tls:
                    while not self._stop.is_set():
                        jpeg = read_frame(tls)
                        if not got_frame:
                            log.info("camera stream connected")
                            got_frame = True
                            self.connected = True
                            self.last_error = ""
                            backoff = 2.0
                        self._keep(jpeg)
            except (OSError, CameraError, ssl.SSLError) as e:
                message = f"{type(e).__name__}: {e}"
                if not got_frame and isinstance(e, ConnectionError):
                    message += (" (the printer closed the stream right after login: check the access code, "
                                "that LAN Only Liveview is enabled, and that no more than one other app is "
                                "watching the camera)")
                if message != self.last_error:
                    log.warning("camera: %s; retrying in %.0fs", message, backoff)
                self.last_error = message
            finally:
                self.connected = False
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)
