"""TLS for the printer's LAN services (MQTT on 8883, camera on 6000).

Each Bambu printer presents a certificate signed by Bambu's CA whose common name
is its serial number. print-guard verifies both, so the access code is never
sent to a machine that merely claims to be the printer. Bambu's CA bundle comes
from ha-bambulab (MIT), bundled as data/bambu_ca.pem.

Two verification modes, chosen by a credential-free probe before connecting:
  * strict: the serial is checked during the handshake (works when the printer
    certificate has no subjectAltName, which is what Bambu printers present);
  * chain: the CA chain is checked during the handshake and the serial right
    after it, used if a future firmware adds subjectAltNames.
"""

from __future__ import annotations

import logging
import socket
import ssl
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)


class PrinterIdentityError(ssl.SSLError):
    """The device answered with a valid certificate for a different serial."""


class _SerialPinnedContext(ssl.SSLContext):
    """SSLContext that checks the peer certificate's name against the serial.

    paho-mqtt passes the IP address as server_hostname; overriding wrap_socket is
    the supported hook to check the serial instead.
    """

    expected_name: str = ""

    def wrap_socket(self, sock, *args, server_hostname=None, **kwargs):  # type: ignore[override]
        return super().wrap_socket(sock, *args, server_hostname=self.expected_name, **kwargs)


def _context(cls: type[ssl.SSLContext], verify: bool, extra_ca: str | Path | None) -> ssl.SSLContext:
    ctx = cls(ssl.PROTOCOL_TLS_CLIENT)
    # Some Bambu firmware never answers a TLS 1.3 ClientHello; 1.2 is answered immediately.
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx.verify_mode = ssl.CERT_REQUIRED
    pem = resources.files("print_guard.data").joinpath("bambu_ca.pem").read_text(encoding="ascii")
    ctx.load_verify_locations(cadata=pem)
    if extra_ca:
        ctx.load_verify_locations(cafile=str(extra_ca))
    # Bambu's CA certificates lack the key-usage extension Python 3.13+ demands by default.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.check_hostname = False
    return ctx


def make_context(serial: str, verify: bool = True, extra_ca: str | Path | None = None,
                 strict: bool = True) -> ssl.SSLContext:
    """Build the context used for MQTT and the camera."""
    if not verify or not strict:
        return _context(ssl.SSLContext, verify, extra_ca)
    ctx = _context(_SerialPinnedContext, True, extra_ca)
    assert isinstance(ctx, _SerialPinnedContext)
    ctx.check_hostname = True
    # The printer's serial lives in the certificate's common name, not a SAN.
    ctx.hostname_checks_common_name = True
    ctx.expected_name = serial
    return ctx


def peer_common_name(cert: dict) -> str:
    for rdn in cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return ""


@dataclass
class ProbeResult:
    reachable: bool
    common_name: str = ""
    issuer: str = ""
    has_san: bool = False
    verified: bool = False
    error: str = ""

    @property
    def strict_ok(self) -> bool:
        return self.verified and not self.has_san


def probe(host: str, port: int, extra_ca: str | Path | None = None, timeout: float = 6.0) -> ProbeResult:
    """Handshake with the printer without sending any credentials and report its identity."""
    ctx = _context(ssl.SSLContext, True, extra_ca)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, \
                ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}
            issuer = ""
            for rdn in cert.get("issuer", ()):
                for key, value in rdn:
                    if key == "commonName":
                        issuer = value
            return ProbeResult(
                reachable=True,
                common_name=peer_common_name(cert),
                issuer=issuer,
                has_san=bool(cert.get("subjectAltName")),
                verified=True,
            )
    except ssl.SSLCertVerificationError as e:
        return ProbeResult(reachable=True, verified=False,
                           error=f"certificate not signed by Bambu Lab: {e.verify_message}")
    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        return ProbeResult(reachable=False, error=f"{type(e).__name__}: {e}")


def verify_identity(sock: ssl.SSLSocket, serial: str) -> None:
    """Post-handshake serial check for the `chain` mode."""
    cert = sock.getpeercert() or {}
    cn = peer_common_name(cert)
    if cn != serial:
        raise PrinterIdentityError(f"printer certificate is for serial {cn!r}, expected the configured serial")
