"""Wire formats: camera login and framing, SSDP announcements, and secret redaction."""

from __future__ import annotations

import logging
import struct

import pytest

from print_guard.credentials import RedactingFilter, mask_serial, redact, register_secret
from print_guard.printer.camera import CameraError, auth_packet, read_frame
from print_guard.printer.discovery import parse_notify


class FakeSock:
    def __init__(self, data: bytes, chunk: int = 7) -> None:
        self.data, self.chunk = data, chunk

    def recv(self, n: int) -> bytes:
        out, self.data = self.data[:min(n, self.chunk)], self.data[min(n, self.chunk):]
        return out


def test_camera_auth_packet_layout():
    pkt = auth_packet("abcd1234")
    assert len(pkt) == 80
    assert struct.unpack("<IIII", pkt[:16]) == (0x40, 0x3000, 0, 0)
    assert pkt[16:20] == b"bblp" and pkt[20:48] == b"\0" * 28
    assert pkt[48:56] == b"abcd1234" and pkt[56:] == b"\0" * 24


def test_read_frame_reassembles_chunks():
    jpeg = b"\xff\xd8" + b"x" * 1000 + b"\xff\xd9"
    sock = FakeSock(struct.pack("<IIII", len(jpeg), 0, 1, 0) + jpeg)
    assert read_frame(sock) == jpeg  # type: ignore[arg-type]


def test_read_frame_rejects_garbage():
    with pytest.raises(CameraError):
        read_frame(FakeSock(struct.pack("<IIII", 0, 0, 0, 0)))  # type: ignore[arg-type]
    bad = b"not a jpeg"
    with pytest.raises(CameraError):
        read_frame(FakeSock(struct.pack("<IIII", len(bad), 0, 1, 0) + bad))  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        read_frame(FakeSock(b""))  # type: ignore[arg-type]


def test_ssdp_announcement():
    msg = (b"NOTIFY * HTTP/1.1\r\nHost: 239.255.255.250:1990\r\nServer: UPnP/1.0\r\nLocation: 192.168.1.77\r\n"
           b"NT: urn:bambulab-com:device:3dprinter:1\r\nNTS: ssdp:alive\r\nUSN: 01P09A0000000001\r\n"
           b"DevModel.bambu.com: C12\r\nDevName.bambu.com: Workshop\r\nDevConnect.bambu.com: lan\r\n"
           b"DevVersion.bambu.com: 01.10.00.00\r\n\r\n")
    ann = parse_notify(msg, "192.168.1.77")
    assert ann is not None
    assert (ann.ip, ann.serial, ann.model, ann.connect_mode) == ("192.168.1.77", "01P09A0000000001", "P1S", "lan")
    assert parse_notify(b"M-SEARCH * HTTP/1.1\r\n", "1.1.1.1") is None
    assert parse_notify(b"NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\nUSN: x\r\n", "1.1.1.1") is None


def test_secrets_never_reach_logs(caplog):
    register_secret("Zq9secret8")
    logger = logging.getLogger("redaction-test")
    handler = caplog.handler
    handler.addFilter(RedactingFilter())
    try:
        logger.error("connecting with %s", "Zq9secret8")
        try:
            raise RuntimeError("bad password Zq9secret8")
        except RuntimeError:
            logger.exception("failed")
    finally:
        handler.removeFilter(handler.filters[-1])
    text = caplog.text
    assert "Zq9secret8" not in text
    assert "***" in text
    assert redact("x Zq9secret8 y") == "x *** y"


def test_mask_serial():
    assert mask_serial("01P09ABCDEF1234") == "01P09...234"
    assert mask_serial("short") == "***"
