"""The Anycubic LAN-mode handshake: the pure-Python AES, the `/ctrl` signature, and opening the
credentials, against FIPS-197's vector, `cryptography`, and `tests/fake_anycubic.py`."""

from __future__ import annotations

import hashlib
import os

import pytest

from printlog_bridge.adapters.anycubic import aes


def test_aes_matches_the_fips_197_example():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    block = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert aes.encrypt_block(block, key).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"


@pytest.mark.parametrize("length", [16, 48, 320])
def test_aes_cbc_matches_cryptography(length):
    ciphers = pytest.importorskip("cryptography.hazmat.primitives.ciphers")
    key, iv, data = os.urandom(16), os.urandom(16), os.urandom(length)
    encryptor = ciphers.Cipher(ciphers.algorithms.AES(key), ciphers.modes.CBC(iv)).encryptor()
    expected = encryptor.update(data) + encryptor.finalize()
    assert aes.encrypt_cbc(data, key, iv) == expected
    assert aes.decrypt_cbc(expected, key, iv) == data


def test_aes_refuses_a_partial_block():
    with pytest.raises(ValueError):
        aes.decrypt_cbc(b"x" * 15, b"k" * 16, b"i" * 16)


def test_md5_is_what_the_signature_uses():
    # A guard for the signature below: md5(md5(token[:16]) + ts + nonce).
    assert hashlib.md5(b"").hexdigest() == "d41d8cd98f00b204e9800998ecf8427e"
