"""AES-128-CBC in pure Python, for the one thing the Anycubic handshake needs: opening the MQTT
credentials the printer's `/ctrl` answers with (`handshake.py`).

Pure Python because the bridge runs without `cryptography` (it has no wheel for 32-bit Arm, and
`core/credentials.py` is the only place that may import it). The message is a few hundred bytes
once per connection, so speed doesn't matter. Written from FIPS-197 (the cipher) and SP 800-38A
(CBC); `tests/test_anycubic_handshake.py` checks it against FIPS-197's example vector and against
`cryptography` on random data. `encrypt_cbc` exists for the tests' fake printer.
"""

from __future__ import annotations


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _sbox() -> tuple[list[int], list[int]]:
    """The S-box from its definition: the multiplicative inverse in GF(2^8), then the affine
    transform."""
    inverse = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if _mul(a, b) == 1:
                inverse[a] = b
                break
    sbox = [0] * 256
    for a in range(256):
        x = inverse[a]
        y = x
        for shift in range(1, 5):
            y ^= ((x << shift) | (x >> (8 - shift))) & 0xFF
        sbox[a] = y ^ 0x63
    inv = [0] * 256
    for a, s in enumerate(sbox):
        inv[s] = a
    return sbox, inv


_SBOX: list[int] = []
_INV_SBOX: list[int] = []


def _tables() -> None:
    if not _SBOX:
        sbox, inv = _sbox()
        _SBOX.extend(sbox)
        _INV_SBOX.extend(inv)


def _expand(key: bytes) -> list[list[int]]:
    """The 11 round keys of AES-128, each 16 bytes."""
    if len(key) != 16:
        raise ValueError("AES-128 takes a 16-byte key")
    words = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        word = list(words[i - 1])
        if i % 4 == 0:
            word = word[1:] + word[:1]
            word = [_SBOX[b] for b in word]
            word[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([a ^ b for a, b in zip(words[i - 4], word)])
    return [sum(words[r * 4 : r * 4 + 4], []) for r in range(11)]


def _shift_rows(s: list[int]) -> list[int]:
    return [s[(i + 4 * (i % 4)) % 16] for i in range(16)]


def _inv_shift_rows(s: list[int]) -> list[int]:
    return [s[(i - 4 * (i % 4)) % 16] for i in range(16)]


def _mix(s: list[int], m: tuple[int, int, int, int]) -> list[int]:
    out = []
    for c in range(4):
        col = s[c * 4 : c * 4 + 4]
        for r in range(4):
            out.append(
                _mul(col[0], m[(0 - r) % 4])
                ^ _mul(col[1], m[(1 - r) % 4])
                ^ _mul(col[2], m[(2 - r) % 4])
                ^ _mul(col[3], m[(3 - r) % 4])
            )
    return out


_MIX = (2, 3, 1, 1)
_INV_MIX = (14, 11, 13, 9)


def _encrypt_block(block: bytes, keys: list[list[int]]) -> bytes:
    s = [a ^ b for a, b in zip(block, keys[0])]
    for r in range(1, 11):
        s = _shift_rows([_SBOX[b] for b in s])
        if r < 10:
            s = _mix(s, _MIX)
        s = [a ^ b for a, b in zip(s, keys[r])]
    return bytes(s)


def _decrypt_block(block: bytes, keys: list[list[int]]) -> bytes:
    s = [a ^ b for a, b in zip(block, keys[10])]
    for r in range(9, -1, -1):
        s = [_INV_SBOX[b] for b in _inv_shift_rows(s)]
        s = [a ^ b for a, b in zip(s, keys[r])]
        if r > 0:
            s = _mix(s, _INV_MIX)
    return bytes(s)


def encrypt_block(block: bytes, key: bytes) -> bytes:
    """One block (16 bytes) with AES-128, as FIPS-197's example."""
    _tables()
    return _encrypt_block(block, _expand(key))


def decrypt_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    """`data` (a multiple of 16 bytes) decrypted, padding left on: the caller says what it is."""
    _tables()
    if len(iv) != 16 or len(data) % 16:
        raise ValueError("CBC takes a 16-byte IV and whole blocks")
    keys = _expand(key)
    out = bytearray()
    previous = iv
    for i in range(0, len(data), 16):
        block = data[i : i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, keys), previous))
        previous = block
    return bytes(out)


def encrypt_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    """`data` (whole blocks: pad it first) encrypted. For the tests' fake printer."""
    _tables()
    if len(iv) != 16 or len(data) % 16:
        raise ValueError("CBC takes a 16-byte IV and whole blocks")
    keys = _expand(key)
    out = bytearray()
    previous = iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i : i + 16], previous))
        previous = _encrypt_block(block, keys)
        out += previous
    return bytes(out)
