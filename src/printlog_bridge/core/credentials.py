"""LAN credentials sealed to the bridge in the app (#100, story 14b): the bridge's key, and opening
what the app sealed to it. The scheme is `printlog-credential` v1, pinned in 3d-print-log-api's
`docs/bridge-credential-sealing.md`; this follows its reference opener exactly.

- **The key.** One P-256 key in `<state_dir>/credential-key.pem` (PKCS#8 PEM, mode 600). It never
  leaves the bridge: only its public half (DER SubjectPublicKeyInfo, base64) and that half's
  fingerprint (SHA-256, hex) go to 3D Print Log. Made the first time something needs it (the
  check-in, `pair`); kept across re-pairing and upgrades; replaced only by `rotate-key`, or when
  the state directory is lost. Either way the owner confirms the new key in the app.
- **Opening.** `v1.` + base64url(ephemeral point ‖ nonce ‖ ciphertext ‖ tag): ECDH with the
  ephemeral point, HKDF-SHA256, AES-256-GCM with the associated data
  `printlog-credential:v1:{bridgeId}:{entryId}:{field}` built from the bridge's own ids. A value
  that isn't that shape, or fails its tag, is `Undecryptable`; one that opens to something that
  isn't a credential (invalid UTF-8, a control character, 0 or more than 256 bytes) is
  `InvalidPlaintext`. What opens is used exactly as it is: no trimming, no normalization.
- **The dependency.** `cryptography` is imported only here, and only when needed: a bridge without
  it (a platform PyPI has no wheel for) runs every other printer, sends no key, and reports sealed
  values as undecryptable. `doctor` says how to install it.

Opened values are kept in memory only (core/app_printers.py), never written down.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from printlog_bridge.core.device import write_private_file

log = logging.getLogger(__name__)

KEY_FILE = "credential-key.pem"
BAD_SUFFIX = ".bad"
VERSION = "v1"
INFO = b"printlog-credential:v1:aes-256-gcm"
FIELDS = ("access_code", "password", "username", "api_key", "moonraker_api_key")
"""Every field the app can seal, as the adapters' `app_secrets` name them."""
MIN_BLOB, MAX_BLOB = 94, 349
MAX_PLAINTEXT = 256
_SHAPE = re.compile(r"v1\.[A-Za-z0-9_-]+")
_CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f]")
"""C0 controls, DEL and C1 controls: refused in any credential."""

MISSING_PACKAGE = (
    "the cryptography package isn't installed, so this bridge can't use access codes and "
    "passwords entered in 3D Print Log (printers in the config file, and [app_printer <id>] "
    "sections, still work). Install it into the bridge's Python: pip install cryptography"
)


class Undecryptable(Exception):
    """A sealed value that doesn't open here: not the v1 shape (`Malformed`), sealed to another
    key, moved to another bridge, entry or field, or changed. Reported as `undecryptable`."""


class Malformed(Undecryptable):
    """Not a v1 sealed value at all: another version, bad encoding or length, or a point off the
    curve."""


class InvalidPlaintext(Exception):
    """It opened, but isn't a credential. Reported as `invalid_plaintext`."""


class KeyUnavailable(Exception):
    """No key to open with: the package is missing, or the key file can't be read."""


def available() -> bool:
    """Whether `cryptography` can be imported here."""
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
    except ImportError:
        return False
    return True


def associated_data(bridge_id: int, entry_id: int, field: str) -> str:
    return f"printlog-credential:{VERSION}:{int(bridge_id)}:{int(entry_id)}:{field}"


def fingerprint(spki: bytes) -> str:
    """Lower-case hex SHA-256 of the SubjectPublicKeyInfo bytes."""
    return hashlib.sha256(spki).hexdigest()


def short_fingerprint(full: str) -> str:
    """The first 16 hex digits in groups of four, as people compare them: `aed0 3e9a 8282 33b2`."""
    return " ".join(full[i : i + 4] for i in range(0, 16, 4))


def check_plaintext(data: bytes) -> str:
    """The plaintext rules: 1 to 256 bytes of strict UTF-8 with no control characters."""
    if not 1 <= len(data) <= MAX_PLAINTEXT:
        raise InvalidPlaintext("not 1 to 256 bytes")
    try:
        text = data.decode("utf-8")  # strict: refuses invalid bytes and encoded surrogates
    except UnicodeDecodeError:
        raise InvalidPlaintext("not valid UTF-8") from None
    if _CONTROL.search(text):
        raise InvalidPlaintext("holds a control character")
    return text


def _b64url_decode(text: str) -> bytes:
    """Canonical unpadded base64url, or Malformed."""
    try:
        blob = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise Malformed("not base64url") from None
    if base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii") != text:
        raise Malformed("not canonical base64url")
    return blob


class CredentialKey:
    """The bridge's P-256 key."""

    def __init__(self, private_key: Any) -> None:
        from cryptography.hazmat.primitives import serialization

        self._private = private_key
        self.spki = private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.public_key = base64.b64encode(self.spki).decode("ascii")
        """What the check-in sends: DER SPKI, standard base64 with padding (124 characters)."""
        self.fingerprint = fingerprint(self.spki)

    def __repr__(self) -> str:  # never the private key, wherever this ends up
        return f"CredentialKey({short_fingerprint(self.fingerprint)})"

    @classmethod
    def generate(cls) -> CredentialKey:
        from cryptography.hazmat.primitives.asymmetric import ec

        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: bytes) -> CredentialKey:
        """Raises ValueError for anything but an unencrypted P-256 private key."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        try:
            key = serialization.load_pem_private_key(pem, password=None)
        except (TypeError, ValueError) as e:
            raise ValueError(f"not a private key: {type(e).__name__}") from None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("not a P-256 key")
        return cls(key)

    def to_pem(self) -> str:
        from cryptography.hazmat.primitives import serialization

        return self._private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    def open(self, sealed: str, ad: str) -> str:
        """The plaintext of `sealed`, opened with the associated data `ad`. Raises Malformed,
        Undecryptable or InvalidPlaintext."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        if not isinstance(sealed, str) or not _SHAPE.fullmatch(sealed):
            raise Malformed("not a v1 sealed value")
        blob = _b64url_decode(sealed[3:])
        if not MIN_BLOB <= len(blob) <= MAX_BLOB or blob[0] != 0x04:
            raise Malformed("not a v1 sealed value")
        eph_raw, nonce, ciphertext = blob[:65], blob[65:77], blob[77:]
        try:
            eph = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_raw)
        except ValueError:
            raise Malformed("the ephemeral key isn't on the curve") from None
        shared = self._private.exchange(ec.ECDH(), eph)
        recipient_raw = self._private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=eph_raw + recipient_raw, info=INFO
        ).derive(shared)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, ad.encode("ascii"))
        except InvalidTag:
            raise Undecryptable("doesn't open with this bridge's key") from None
        # Used exactly as opened: no trimming, no normalization.
        return check_plaintext(plaintext)


def key_path(state_dir: Path) -> Path:
    return state_dir / KEY_FILE


def read_key(state_dir: Path) -> CredentialKey | None:
    """The stored key, without making one: None when there's no file. Raises KeyUnavailable when
    the package is missing or the file can't be read or parsed."""
    path = key_path(state_dir)
    try:
        pem = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise KeyUnavailable(f"can't read {path}: {e.strerror or e}") from None
    if not available():
        raise KeyUnavailable(MISSING_PACKAGE)
    try:
        return CredentialKey.from_pem(pem)
    except ValueError as e:
        raise KeyUnavailable(f"{path} isn't a usable key ({e})") from None


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def write_new_key(state_dir: Path) -> CredentialKey:
    """Makes a new key and stores it (mode 600), replacing any other."""
    key = CredentialKey.generate()
    write_private_file(key_path(state_dir), key.to_pem())
    return key


class KeyStore:
    """The key the running bridge uses: loaded (or made) once, and loaded again when its file
    changes (`rotate-key` while the bridge runs). `problem` says why there is none."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = key_path(state_dir)
        self.key: CredentialKey | None = None
        self.problem: str | None = None
        self._seen: Any = object()

    def load(self) -> bool:
        """Loads the key, making one when there is none. True when the key changed."""
        seen = _signature(self.path)
        if seen == self._seen and (self.key is not None or self.problem is not None):
            return False
        before = self.key.fingerprint if self.key is not None else None
        self.key, self.problem = self._load()
        self._seen = _signature(self.path)
        after = self.key.fingerprint if self.key is not None else None
        return after != before

    def _load(self) -> tuple[CredentialKey | None, str | None]:
        if not available():
            return None, MISSING_PACKAGE
        try:
            key = read_key(self.state_dir)
        except KeyUnavailable as e:
            if self.path.is_file() and _parses_badly(self.path):
                # Not a key at all (a truncated write, say): keep it aside and start again.
                # A file that can't be read is left alone: it may be fine for another user.
                aside = self.path.with_name(KEY_FILE + BAD_SUFFIX)
                with contextlib.suppress(OSError):
                    os.replace(self.path, aside)
                    log.warning(
                        "%s; moved it to %s and made a new key, which you confirm in 3D Print "
                        "Log (Bridges)", e, aside,
                    )  # fmt: skip
                    return self._make()
            log.error("the bridge's encryption key: %s", e)
            return None, str(e)
        if key is not None:
            return key, None
        return self._make()

    def _make(self) -> tuple[CredentialKey | None, str | None]:
        try:
            key = write_new_key(self.state_dir)
        except OSError as e:
            log.error("couldn't store a new encryption key in %s: %s", self.path, e)
            return None, f"can't write {self.path}: {e.strerror or e}"
        log.info(
            "made this bridge's encryption key (fingerprint %s) in %s: access codes and "
            "passwords entered in 3D Print Log are sealed to it",
            short_fingerprint(key.fingerprint), self.path,
        )  # fmt: skip
        return key, None


def _parses_badly(path: Path) -> bool:
    """Whether `path` can be read but isn't a P-256 private key."""
    try:
        pem = path.read_bytes()
    except OSError:
        return False
    try:
        CredentialKey.from_pem(pem)
    except ValueError:
        return True
    return False


class Opener:
    """Opens the desired list's sealed values with the bridge's key and its id in 3D Print Log
    (from the check-in)."""

    def __init__(self, keys: KeyStore | None, bridge_id: int | None = None) -> None:
        self.keys = keys
        self.bridge_id = bridge_id

    @property
    def key(self) -> CredentialKey | None:
        return self.keys.key if self.keys is not None else None

    def open(self, entry_id: int, field: str, sealed: str) -> str:
        """Raises KeyUnavailable (no key or no bridge id yet), Undecryptable or
        InvalidPlaintext."""
        key = self.key
        if key is None:
            problem = self.keys.problem if self.keys is not None else None
            raise KeyUnavailable(problem or "no encryption key")
        if self.bridge_id is None:
            raise KeyUnavailable("3D Print Log hasn't told the bridge its id yet")
        return key.open(sealed, associated_data(self.bridge_id, entry_id, field))


def stored_plaintexts(state_dir: Path, desired: Any, bridge_id: Any) -> list[str]:
    """What the stored desired list's sealed values open to, for scrubbing the support bundle.
    Never makes a key; empty when anything is missing."""
    if not isinstance(desired, dict) or not isinstance(bridge_id, int):
        return []
    try:
        key = read_key(state_dir)
    except KeyUnavailable:
        return []
    if key is None:
        return []
    found = []
    for printer in desired.get("printers") or []:
        if not isinstance(printer, dict) or not isinstance(printer.get("id"), int):
            continue
        for secret in printer.get("secrets") or []:
            if not isinstance(secret, dict):
                continue
            field, sealed = secret.get("field"), secret.get("sealed")
            if not isinstance(field, str) or not isinstance(sealed, str):
                continue
            with contextlib.suppress(Undecryptable, InvalidPlaintext):
                found.append(key.open(sealed, associated_data(bridge_id, printer["id"], field)))
    return found
