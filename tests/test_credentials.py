"""core/credentials.py: the bridge's key, and opening values sealed to it, against the scheme's
vectors (tests/fixtures/credentials, copied from 3d-print-log-api at 0db10aa: see SOURCE.txt)."""

from __future__ import annotations

import builtins
import hashlib
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sealing import seal, seal_bytes, vector_key, vectors

from printlog_bridge.core import credentials
from printlog_bridge.core.credentials import (
    KEY_FILE,
    InvalidPlaintext,
    KeyStore,
    KeyUnavailable,
    Malformed,
    Opener,
    Undecryptable,
    check_plaintext,
    read_key,
    short_fingerprint,
    stored_plaintexts,
)

DATA = vectors()
KEY = vector_key()


def test_the_vector_key_has_the_vectors_public_key_and_fingerprint():
    recipient = DATA["recipient"]

    assert KEY.public_key == recipient["publicKey"]
    assert len(KEY.public_key) == 124
    assert KEY.public_key.startswith("MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE")
    assert KEY.fingerprint == recipient["fingerprint"]
    assert KEY.fingerprint == hashlib.sha256(KEY.spki).hexdigest()
    assert short_fingerprint(KEY.fingerprint) == "aed0 3e9a 8282 33b2"


@pytest.mark.parametrize("vector", DATA["vectors"], ids=lambda v: f"{v['entryId']}-{v['field']}")
def test_every_vector_opens_to_its_plaintext_exactly(vector):
    ad = credentials.associated_data(vector["bridgeId"], vector["entryId"], vector["field"])
    assert ad == vector["associatedData"]

    opened = KEY.open(vector["sealed"], ad)

    assert opened == vector["plaintext"]
    assert opened.encode("utf-8").hex() == vector["plaintextUtf8Hex"]


@pytest.mark.parametrize("vector", DATA["vectors"], ids=lambda v: f"{v['entryId']}-{v['field']}")
def test_the_test_sealer_reseals_every_vector_byte_for_byte(vector):
    """So the values tests seal are what the web app would send."""
    eph = ec.derive_private_key(int(vector["ephemeralPrivateKeyHex"], 16), ec.SECP256R1())
    public = serialization.load_der_public_key(KEY.spki)

    sealed = seal_bytes(
        public,
        vector["associatedData"],
        vector["plaintext"].encode("utf-8"),
        eph,
        bytes.fromhex(vector["nonceHex"]),
    )

    assert sealed == vector["sealed"]


def test_typed_vectors_are_opened_untrimmed_and_unnormalized():
    """The sender trims and never normalizes; the bridge uses what opens as it is."""
    typed = [v for v in DATA["vectors"] if "typed" in v]
    assert typed
    for vector in typed:
        assert KEY.open(vector["sealed"], vector["associatedData"]) == vector["plaintext"]
    nfd = next(v for v in DATA["vectors"] if v["plaintextUtf8Hex"].endswith("cc81"))
    assert KEY.open(nfd["sealed"], nfd["associatedData"]).encode().hex().endswith("65cc81")


@pytest.mark.parametrize("entry", DATA["mustNotOpen"], ids=lambda n: n["why"])
def test_every_must_not_open_fails_the_way_its_reason_says(entry):
    if entry["reason"] == "plaintext":
        with pytest.raises(InvalidPlaintext):
            KEY.open(entry["sealed"], entry["associatedData"])
    else:
        assert entry["reason"] == "authentication"
        with pytest.raises(Undecryptable) as caught:
            KEY.open(entry["sealed"], entry["associatedData"])
        assert not isinstance(caught.value, Malformed)


@pytest.mark.parametrize("entry", DATA["invalidSealedValues"], ids=lambda b: b["why"])
def test_every_invalid_sealed_value_is_refused_by_its_shape(entry):
    with pytest.raises(Malformed):
        KEY.open(entry["value"], "printlog-credential:v1:42:7:access_code")


def test_an_unknown_version_is_undecryptable():
    with pytest.raises(Undecryptable):
        KEY.open("v2." + DATA["vectors"][0]["sealed"][3:], DATA["vectors"][0]["associatedData"])


@pytest.mark.parametrize(
    "data",
    [b"", b"x" * 257, b"\xff", b"a\tb", b"a\x7fb", "a\u0085b".encode(), "a\u009fb".encode()],
)
def test_the_plaintext_rules_refuse(data):
    with pytest.raises(InvalidPlaintext):
        check_plaintext(data)


@pytest.mark.parametrize("text", [" leading and trailing ", " nbsp ", "x" * 256, "é"])
def test_the_plaintext_rules_keep_spaces_and_unicode(text):
    assert check_plaintext(text.encode("utf-8")) == text


def test_a_fresh_seal_opens_only_for_its_bridge_entry_and_field():
    sealed = seal(KEY, 42, 7, "password", " pass word ")

    assert Opener(_store(KEY), 42).open(7, "password", sealed) == " pass word "
    for bridge_id, entry_id, field in [
        (43, 7, "password"),
        (42, 8, "password"),
        (42, 7, "api_key"),
    ]:
        with pytest.raises(Undecryptable):
            Opener(_store(KEY), bridge_id).open(entry_id, field, sealed)


def test_the_opener_needs_a_key_and_a_bridge_id():
    sealed = seal(KEY, 42, 7, "password", "secret")

    with pytest.raises(KeyUnavailable):
        Opener(_store(KEY), None).open(7, "password", sealed)
    with pytest.raises(KeyUnavailable):
        Opener(None, 42).open(7, "password", sealed)


def _store(key):
    store = KeyStore.__new__(KeyStore)
    store.key, store.problem = key, None
    return store


# -- the key file -------------------------------------------------------------------------------


def test_the_key_is_made_once_and_kept(tmp_path):
    store = KeyStore(tmp_path)

    assert store.load() is True
    first = store.key
    assert first is not None
    assert (tmp_path / KEY_FILE).is_file()
    assert store.load() is False

    again = KeyStore(tmp_path)
    again.load()
    assert again.key.fingerprint == first.fingerprint
    assert read_key(tmp_path).fingerprint == first.fingerprint


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_the_key_file_is_mode_600(tmp_path):
    KeyStore(tmp_path / "state").load()

    assert (tmp_path / "state" / KEY_FILE).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700


def test_a_key_file_written_by_another_process_is_picked_up(tmp_path):
    store = KeyStore(tmp_path)
    store.load()
    old = store.key.fingerprint

    credentials.write_new_key(tmp_path)  # rotate-key while the bridge runs

    assert store.load() is True
    assert store.key.fingerprint != old


def test_a_key_file_that_isnt_a_key_is_moved_aside_and_replaced(tmp_path, caplog):
    (tmp_path / KEY_FILE).write_text("not a key\n", encoding="utf-8")
    store = KeyStore(tmp_path)

    store.load()

    assert store.key is not None
    assert (tmp_path / (KEY_FILE + ".bad")).read_text(encoding="utf-8") == "not a key\n"
    assert "made a new key" in caplog.text


def test_another_curve_is_not_a_key(tmp_path):
    other = ec.generate_private_key(ec.SECP384R1())
    pem = other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    (tmp_path / KEY_FILE).write_bytes(pem)

    with pytest.raises(KeyUnavailable):
        read_key(tmp_path)


def test_an_unreadable_key_file_is_left_alone(tmp_path, monkeypatch):
    (tmp_path / KEY_FILE).write_text("whatever", encoding="utf-8")
    real = type(tmp_path).read_bytes

    def refuse(self):
        if self.name == KEY_FILE:
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(type(tmp_path), "read_bytes", refuse)
    store = KeyStore(tmp_path)

    store.load()

    assert store.key is None
    assert "Permission denied" in store.problem
    assert (tmp_path / KEY_FILE).read_text(encoding="utf-8") == "whatever"


def test_without_the_package_there_is_no_key_and_it_says_why(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def no_cryptography(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_cryptography)
    store = KeyStore(tmp_path)

    assert credentials.available() is False
    store.load()

    assert store.key is None
    assert store.problem == credentials.MISSING_PACKAGE
    assert not (tmp_path / KEY_FILE).exists()
    with pytest.raises(KeyUnavailable):
        Opener(store, 42).open(7, "access_code", DATA["vectors"][0]["sealed"])


def test_the_key_never_shows_its_private_half():
    assert repr(KEY) == "CredentialKey(aed0 3e9a 8282 33b2)"
    assert DATA["recipient"]["privateKeyHex"] not in repr(vars(KEY))


def test_stored_plaintexts_opens_the_stored_desired_list(tmp_path):
    (tmp_path / KEY_FILE).write_text(KEY.to_pem(), encoding="utf-8")
    desired = {
        "printers": [
            {
                "id": 7,
                "secrets": [
                    {"field": "access_code", "sealed": DATA["vectors"][0]["sealed"]},
                    {"field": "password", "sealed": "v1.nonsense"},
                ],
            }
        ]
    }

    assert stored_plaintexts(tmp_path, desired, 42) == ["12345678"]
    assert stored_plaintexts(tmp_path, desired, None) == []
    assert stored_plaintexts(tmp_path / "none", desired, 42) == []
