"""TLS to a Bambu Lab printer, and pinning its certificate.

The printer's MQTT broker (8883) and FTPS server (990) present a certificate issued by one of
Bambu Lab's own certificate authorities, with the printer's serial as its name (OpenBambuAPI's
tls.md). No public CA vouches for it, and Bambu has added a new CA with several model generations
(ha-bambulab bundles one per generation), so a list of them kept in the bridge would refuse the
next new model. So the chain isn't verified. Instead the bridge pins the certificate: the first
one a service presents is remembered (its SHA-256, in the printer's state), and a different one
later is refused before the access code is sent, with a message saying how to accept it
(`tls_fingerprint` in the config) if the printer really changed. What that leaves open: on the
first connect, something on the LAN posing as the printer would be trusted, and would get the
access code. `tls_fingerprint` closes that too, for whoever wants to check it on the printer
first.

TLS 1.2 at most, as ha-bambulab connects: some printer firmwares have failed TLS 1.3 handshakes.
"""

from __future__ import annotations

import hashlib
import logging
import re
import ssl

from printlog_bridge.core.state import PrinterState

log = logging.getLogger(__name__)

KEY = "bambu_certificates"
"""Where in `state.discovery` each service's pinned fingerprint is kept."""
_HEX = re.compile(r"[0-9A-Fa-f]{64}")


class CertificateChanged(Exception):
    """The printer presented a certificate other than the pinned one. Safe to show as is."""


def client_context() -> ssl.SSLContext:
    """TLS without verifying the chain or the name: the certificate is pinned instead."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


def fingerprint(der: bytes) -> str:
    """The certificate's SHA-256 as `AB:CD:...`, as `openssl x509 -fingerprint -sha256`
    writes it."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def parse_fingerprint(text: str) -> str | None:
    """A SHA-256 fingerprint written with or without colons, in either case, as `fingerprint`
    writes it; None when it isn't one."""
    bare = re.sub(r"[\s:]", "", text)
    if not _HEX.fullmatch(bare):
        return None
    bare = bare.upper()
    return ":".join(bare[i : i + 2] for i in range(0, len(bare), 2))


class CertificatePin:
    """Checks each certificate a printer's service (`mqtt`, `ftps`) presents against the one it
    presented before, or against `configured` (a `tls_fingerprint`), which wins."""

    def __init__(self, printer: str, configured: str | None, state: PrinterState | None) -> None:
        self.printer = printer
        self.configured = parse_fingerprint(configured) if configured else None
        self.state = state

    def check(self, der: bytes, service: str) -> None:
        found = fingerprint(der)
        pins = self.state.discovery.get(KEY) if self.state is not None else None
        pins = pins if isinstance(pins, dict) else {}
        expected = self.configured if service == "mqtt" and self.configured else pins.get(service)
        if expected is not None and expected != found:
            raise CertificateChanged(
                f"the printer's {service.upper()} certificate isn't the one it had before "
                f"(now {found}). If you didn't replace the printer or reset it, something else "
                "on your network may be answering for it, so the access code wasn't sent. If "
                f"you did, set tls_fingerprint = {found} in [printer {self.printer}]"
                + (" and restart printlog-bridge." if service == "mqtt" else ".")
            )
        if pins.get(service) == found or self.state is None or self.state.read_only:
            return
        if expected is None:
            log.info(
                "[printer %s] pinned the printer's %s certificate (SHA-256 %s)",
                self.printer, service.upper(), found,
            )  # fmt: skip
        self.state.discovery[KEY] = {**pins, service: found}
        self.state.save()
