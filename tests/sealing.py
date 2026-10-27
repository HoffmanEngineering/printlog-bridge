"""A sealer for tests: what the web app does in WebCrypto (3d-print-log-api
`docs/bridge-credential-sealing.check.py`'s `seal`), so tests can hand the bridge sealed values.
The bridge itself never seals."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from printlog_bridge.core.credentials import INFO, CredentialKey, associated_data

VECTORS = (
    Path(__file__).parent / "fixtures" / "credentials" / "bridge-credential-sealing.vectors.json"
)
RAW = serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint


def vectors() -> dict[str, Any]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def vector_key() -> CredentialKey:
    """The vectors' recipient key."""
    der = base64.b64decode(vectors()["recipient"]["privateKeyPkcs8Base64"])
    return CredentialKey(serialization.load_der_private_key(der, password=None))


def seal_bytes(
    public_key: Any, ad: str, plaintext: bytes, eph: Any = None, nonce: bytes | None = None
) -> str:
    eph = eph or ec.generate_private_key(ec.SECP256R1())
    eph_raw = eph.public_key().public_bytes(*RAW)
    shared = eph.exchange(ec.ECDH(), public_key)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=eph_raw + public_key.public_bytes(*RAW),
        info=INFO,
    ).derive(shared)
    nonce = nonce or os.urandom(12)
    blob = eph_raw + nonce + AESGCM(key).encrypt(nonce, plaintext, ad.encode("ascii"))
    return "v1." + base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")


def seal(key: CredentialKey, bridge_id: int, entry_id: int, field: str, plaintext: str) -> str:
    """`plaintext` sealed to `key` for that bridge, entry and field."""
    public = serialization.load_der_public_key(key.spki)
    return seal_bytes(public, associated_data(bridge_id, entry_id, field), plaintext.encode())
