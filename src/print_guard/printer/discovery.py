"""Find Bambu printers on the LAN by listening for their SSDP announcements.

Printers broadcast an HTTP-style NOTIFY to UDP port 2021 about every 5 seconds
with their IP (Location), serial (USN), model code, name and whether LAN Only
mode is on (DevConnect). Listening is passive: nothing is sent to the printer.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

SSDP_PORT = 2021
MODEL_CODES = {"C11": "P1P", "C12": "P1S", "BL-P001": "X1C", "BL-P002": "X1", "C13": "X1E",
               "N1": "A1 mini", "N2S": "A1", "O1D": "H2D", "N7": "P2S"}


@dataclass(frozen=True)
class Announcement:
    ip: str
    serial: str
    model_code: str
    name: str
    connect_mode: str  # "lan" when LAN Only mode is on, "cloud" otherwise
    firmware: str

    @property
    def model(self) -> str:
        return MODEL_CODES.get(self.model_code, self.model_code or "unknown")


def parse_notify(data: bytes, sender_ip: str) -> Announcement | None:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()
    if not lines or not lines[0].upper().startswith("NOTIFY"):
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    if "bambulab" not in headers.get("nt", "").lower() and "devmodel.bambu.com" not in headers:
        return None
    serial = headers.get("usn", "")
    if not serial:
        return None
    return Announcement(
        ip=headers.get("location", sender_ip) or sender_ip,
        serial=serial,
        model_code=headers.get("devmodel.bambu.com", ""),
        name=headers.get("devname.bambu.com", ""),
        connect_mode=headers.get("devconnect.bambu.com", ""),
        firmware=headers.get("devversion.bambu.com", ""),
    )


def discover(seconds: float = 8.0, port: int = SSDP_PORT) -> list[Announcement]:
    """Listen for announcements and return each printer once."""
    found: dict[str, Announcement] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        # Bambu Studio may already hold the port; broadcasts reach every bound socket.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, (ip, _port) = sock.recvfrom(4096)
            except TimeoutError:
                break
            ann = parse_notify(data, ip)
            if ann:
                found[ann.serial] = ann
    finally:
        sock.close()
    return list(found.values())
