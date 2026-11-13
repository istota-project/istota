"""The private push envelope, version 1 (docs/reference/private-push-v1.md).

The fixture vectors in tests/fixtures/private_push_v1/ are the cross-repo
contract: the iOS app and the relay test against the same file, so every one
of them is re-opened here and a regenerated file that no longer opens fails.
"""

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from istota.push import envelope

VECTORS = Path(__file__).parent / "fixtures" / "private_push_v1" / "vectors.json"
BINDING = "3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"
GENERATION = "AAECAwQFBgcICQoLDA0ODw"
MID = "EBESExQVFhcYGRobHB0eHw"
NOW = 1_800_000_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.fixture(scope="module")
def keys():
    sender = Ed25519PrivateKey.generate()
    device = X25519PrivateKey.generate()
    return {
        "sender": sender,
        "sender_public": sender.public_key(),
        "device": device,
        "device_public_raw": device.public_key().public_bytes_raw(),
    }


def _seal(keys, **overrides):
    args = dict(
        signing_key=keys["sender"],
        device_public_key=keys["device_public_raw"],
        binding=BINDING,
        generation=GENERATION,
        mid=MID,
        now=NOW,
        alert_title="Istota",
        title="Sam Example",
        body="Lunch at noon?",
        badge=3,
        logical_group="room:w8x2k4m9",
        target={"kind": "chat", "room_token": "w8x2k4m9", "room_id": 7,
                "connection_binding": BINDING, "connection_generation": 2},
    )
    args.update(overrides)
    return envelope.seal(**args)


def _open(keys, payload, **overrides):
    args = dict(device_private_key=keys["device"], sender_public_key=keys["sender_public"], now=NOW)
    args.update(overrides)
    return envelope.open_for_tests(payload, **args)


def _parts(payload: bytes):
    outer = json.loads(payload)
    private = outer["istota_private"]
    return outer, _unb64(private["h"]), _unb64(private["c"]), _unb64(private["s"])


def _repack(outer: dict, header: bytes, sealed: bytes, signature: bytes) -> bytes:
    outer = json.loads(json.dumps(outer))
    outer["istota_private"] = {"h": _b64(header), "c": _b64(sealed), "s": _b64(signature)}
    return json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _reason(keys, payload, **overrides) -> str:
    with pytest.raises(envelope.EnvelopeError) as caught:
        _open(keys, payload, **overrides)
    return caught.value.reason


# --- Identifiers ---------------------------------------------------------------


def test_key_id_is_b64url_sha256_of_the_raw_key():
    raw = bytes(range(32))
    assert envelope.key_id(raw) == _b64(hashlib.sha256(raw).digest())
    assert len(envelope.key_id(raw)) == 43
    with pytest.raises(ValueError):
        envelope.key_id(b"\x00" * 31)


def test_collapse_id_matches_the_written_derivation(keys):
    raw = keys["sender"].private_bytes_raw()
    key = HKDF(algorithm=SHA256(), length=32, salt=None, info=b"istota-push-collapse-v1").derive(raw)
    expected = _b64(hmac.new(key, (BINDING + "\x00" + "room:w8x2k4m9").encode(), "sha256").digest()[:16])
    assert envelope.collapse_id(keys["sender"], BINDING, "room:w8x2k4m9") == expected
    assert len(expected) == 22


def test_collapse_id_is_stable_per_binding_and_group_and_differs_across_them(keys):
    first = envelope.collapse_id(keys["sender"], BINDING, "room:w8x2k4m9")
    assert envelope.collapse_id(keys["sender"], BINDING, "room:w8x2k4m9") == first
    other_binding = "00000000-0000-4000-8000-000000000000"
    assert envelope.collapse_id(keys["sender"], other_binding, "room:w8x2k4m9") != first
    assert envelope.collapse_id(keys["sender"], BINDING, "room:other") != first
    assert envelope.collapse_id(Ed25519PrivateKey.generate(), BINDING, "room:w8x2k4m9") != first


# --- Seal and open -----------------------------------------------------------------


def test_round_trip_returns_the_inner_plaintext(keys):
    payload, collapse = _seal(keys)
    opened = _open(keys, payload, binding=BINDING, generation=GENERATION)
    assert opened.plaintext == {
        "v": 1, "binding": BINDING, "generation": GENERATION, "mid": MID,
        "exp": NOW + 300, "badge": 3, "title": "Sam Example", "body": "Lunch at noon?",
        "group": collapse,
        "target": {"kind": "chat", "room_token": "w8x2k4m9", "room_id": 7,
                   "connection_binding": BINDING, "connection_generation": 2},
    }
    assert opened.header["collapse"] == collapse


def test_outer_payload_has_exactly_the_fixed_keys_and_no_plaintext(keys):
    payload, _ = _seal(keys)
    outer = json.loads(payload)
    assert list(outer) == ["aps", "istota_private"]
    assert outer["aps"] == {"alert": {"title": "Istota", "body": "You have new activity."},
                            "mutable-content": 1, "sound": "default", "badge": 3}
    assert set(outer["istota_private"]) == {"h", "c", "s"}
    text = payload.decode("utf-8")
    for secret in ("Sam Example", "Lunch at noon?", "w8x2k4m9", "room:"):
        assert secret not in text
    assert payload == json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_null_badge_omits_the_outer_badge(keys):
    payload, _ = _seal(keys, badge=None)
    assert "badge" not in json.loads(payload)["aps"]
    assert _open(keys, payload).header["badge"] is None


def test_null_group_gives_a_null_collapse(keys):
    payload, collapse = _seal(keys, logical_group=None)
    assert collapse is None
    opened = _open(keys, payload)
    assert opened.header["collapse"] is None and opened.plaintext["group"] is None


def test_header_bytes_are_the_sorted_compact_serialization(keys):
    payload, collapse = _seal(keys)
    _, header, _, _ = _parts(payload)
    expected = {
        "v": 1, "binding": BINDING, "generation": GENERATION,
        "kid": envelope.key_id(keys["device_public_raw"]),
        "sid": envelope.key_id(keys["sender_public"].public_bytes_raw()),
        "mid": MID, "iat": NOW, "exp": NOW + 300, "kind": "alert",
        "collapse": collapse, "badge": 3,
    }
    assert header == json.dumps(expected, separators=(",", ":"), sort_keys=True,
                                ensure_ascii=False).encode("utf-8")


def test_info_and_signature_inputs_are_as_written(keys):
    from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite

    payload, _ = _seal(keys)
    _, header, sealed, signature = _parts(payload)
    message = b"istota-private-push-sign-v1\x00" + len(header).to_bytes(4, "big") + header + sealed
    keys["sender_public"].verify(signature, message)
    padded = Suite(KEM.X25519, KDF.HKDF_SHA256, AEAD.AES_128_GCM).decrypt(
        sealed, keys["device"], info=b"istota-private-push-v1\x00" + header)
    length = int.from_bytes(padded[:4], "big")
    assert json.loads(padded[4:4 + length])["title"] == "Sam Example"


@pytest.mark.parametrize("filler,bucket", [(0, 512), (600, 1024), (1100, 1536), (1600, 2048)])
def test_padding_picks_the_smallest_bucket_with_a_length_prefix(keys, filler, bucket):
    from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite

    target = {"kind": "notification", "notification_id": 42, "link": "/istota/x" + "a" * filler}
    payload, _ = _seal(keys, target=target)
    _, header, sealed, _ = _parts(payload)
    assert len(sealed) == 32 + bucket + 16
    padded = Suite(KEM.X25519, KDF.HKDF_SHA256, AEAD.AES_128_GCM).decrypt(
        sealed, keys["device"], info=b"istota-private-push-v1\x00" + header)
    assert len(padded) == bucket
    length = int.from_bytes(padded[:4], "big")
    assert len(padded) - 4 - 512 < length <= len(padded) - 4 or bucket == 512
    assert json.loads(padded[4:4 + length])["target"] == target
    assert _open(keys, payload).plaintext["target"] == target


def test_multibyte_text_at_the_caps_fits_and_round_trips(keys):
    title = "\U0001d11e" * 80
    body = "é\U0001f600" * 90
    payload, _ = _seal(keys, title=title, body=body, alert_title="É" * 80)
    assert len(payload) <= 4096
    opened = _open(keys, payload)
    assert opened.plaintext["title"] == title and opened.plaintext["body"] == body
    assert json.loads(payload)["aps"]["alert"]["title"] == "É" * 80


def test_text_over_the_caps_is_refused(keys):
    with pytest.raises(ValueError):
        _seal(keys, title="x" * 81)
    with pytest.raises(ValueError):
        _seal(keys, body="x" * 181)


def test_a_target_that_cannot_fit_falls_back_to_the_inbox_keeping_the_text(keys):
    target = {"kind": "notification", "notification_id": 42, "link": "/istota/" + "a" * 3000}
    payload, _ = _seal(keys, target=target)
    opened = _open(keys, payload)
    assert opened.plaintext["target"] == {"kind": "inbox"}
    assert opened.plaintext["title"] == "Sam Example"
    assert opened.plaintext["body"] == "Lunch at noon?"


def test_body_then_title_shorten_before_the_target_is_dropped(keys):
    # Fits only once the body is shortened; the target survives.
    target = {"kind": "notification", "notification_id": 42, "link": "/istota/" + "a" * 1700}
    body = "\U0001f600" * 180
    payload, _ = _seal(keys, target=target, body=body)
    opened = _open(keys, payload)
    assert opened.plaintext["target"] == target
    assert opened.plaintext["title"] == "Sam Example"
    assert 0 < len(opened.plaintext["body"]) < 180
    assert body.startswith(opened.plaintext["body"])


def test_an_apns_body_over_4096_bytes_is_a_payload_error(keys):
    with pytest.raises(envelope.PayloadTooLarge):
        _seal(keys, alert_title="\U0001f600" * 400,
              target={"kind": "notification", "notification_id": 1, "link": "/" + "a" * 1700})


def test_a_bad_device_key_is_a_seal_error(keys):
    with pytest.raises(envelope.EnvelopeError) as caught:
        _seal(keys, device_public_key=b"\x00" * 32)
    assert caught.value.reason == "seal"
    with pytest.raises(envelope.EnvelopeError):
        _seal(keys, device_public_key=b"\x01" * 31)


@pytest.mark.parametrize("field,value", [
    ("binding", "3F2A9C1E-7B4D-4E8A-9C2F-1A2B3C4D5E6F"),
    ("generation", "short"),
    ("mid", "not/base64url-and-short"),
    ("badge", 10000),
    ("badge", True),
])
def test_seal_validates_its_header_inputs(keys, field, value):
    with pytest.raises(ValueError):
        _seal(keys, **{field: value})


def test_event_ids_are_fresh_16_byte_values(keys):
    first, second = envelope.new_event_id(), envelope.new_event_id()
    assert first != second and len(_unb64(first)) == 16 and len(first) == 22
    payload, _ = _seal(keys, mid=None)
    assert len(_open(keys, payload).header["mid"]) == 22


# --- Rejections -------------------------------------------------------------------


def test_altered_header_fails_the_signature(keys):
    payload, _ = _seal(keys)
    outer, header, sealed, signature = _parts(payload)
    altered = header.replace(b'"badge":3', b'"badge":4')
    assert altered != header
    assert _reason(keys, _repack(outer, altered, sealed, signature)) == "signature"


def test_altered_ciphertext_fails_the_signature(keys):
    payload, _ = _seal(keys)
    outer, header, sealed, signature = _parts(payload)
    altered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    assert _reason(keys, _repack(outer, header, altered, signature)) == "signature"


def test_altered_signature_fails(keys):
    payload, _ = _seal(keys)
    outer, header, sealed, signature = _parts(payload)
    altered = signature[:-1] + bytes([signature[-1] ^ 1])
    assert _reason(keys, _repack(outer, header, sealed, altered)) == "signature"


def test_wrong_device_key_is_refused(keys):
    payload, _ = _seal(keys)
    assert _reason(keys, payload, device_private_key=X25519PrivateKey.generate()) == "device_key"


def test_wrong_sender_key_is_refused(keys):
    payload, _ = _seal(keys)
    other = Ed25519PrivateKey.generate().public_key()
    assert _reason(keys, payload, sender_public_key=other) == "sender_key"


def test_lifetime_over_300_seconds_is_refused_even_when_signed(keys):
    payload, _ = _seal(keys)
    outer, header, _, _ = _parts(payload)
    fields = json.loads(header)
    fields["exp"] = fields["iat"] + 301
    long_header = envelope.encode_json(fields)
    sealed, signature = envelope.sign_and_encrypt(
        long_header, _open(keys, payload).padded, keys["device_public_raw"], keys["sender"])
    assert _reason(keys, _repack(outer, long_header, sealed, signature)) == "lifetime"


def test_expiry_and_future_skew(keys):
    payload, _ = _seal(keys)
    assert _reason(keys, payload, now=NOW + 301) == "expired"
    assert _reason(keys, payload, now=NOW - 61) == "not_yet_valid"
    _open(keys, payload, now=NOW + 300)
    _open(keys, payload, now=NOW - 60)


def test_binding_and_generation_must_match_when_given(keys):
    payload, _ = _seal(keys)
    assert _reason(keys, payload, binding="00000000-0000-4000-8000-000000000000") == "binding"
    assert _reason(keys, payload, generation="A" * 22) == "generation"


@pytest.mark.parametrize("raw", [
    b"not json",
    b'{"aps":{},"istota_private":{"h":"","c":"","s":""},"istota":{}}',
    b'{"aps":{},"istota_private":{"h":"a=","c":"","s":""}}',
    b"\xff\xfe",
])
def test_malformed_outer_payloads_are_refused(keys, raw):
    assert _reason(keys, raw) == "malformed"


def test_a_non_finite_number_in_the_header_is_a_header_error(keys):
    payload, _ = _seal(keys)
    outer, header, sealed, signature = _parts(payload)
    altered = header.replace(b'"badge":3', b'"badge":NaN')
    assert _reason(keys, _repack(outer, altered, sealed, signature)) == "header_invalid"


def test_outer_badge_must_equal_the_header_badge(keys):
    payload, _ = _seal(keys)
    outer = json.loads(payload)
    outer["aps"]["badge"] = 9
    rewritten = json.dumps(outer, separators=(",", ":")).encode()
    assert _reason(keys, rewritten) == "badge"


# --- Strict base64url and key files -----------------------------------------------


@pytest.mark.parametrize("text", ["AA==", "A", "AB", "A+B/", "AAA "])
def test_b64url_decode_is_strict(text):
    with pytest.raises(ValueError):
        envelope.b64url_decode(text)


def test_b64url_round_trip():
    for size in range(0, 40):
        data = bytes(range(size))
        assert envelope.b64url_decode(envelope.b64url(data)) == data


def test_load_signing_key_reads_pkcs8_pem(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "signing.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    loaded = envelope.load_signing_key(path)
    assert loaded.public_key().public_bytes_raw() == key.public_key().public_bytes_raw()


def test_load_signing_key_refuses_another_key_type_and_garbage(tmp_path):
    other = X25519PrivateKey.generate()
    path = tmp_path / "x25519.pem"
    path.write_bytes(other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    with pytest.raises(envelope.KeyFileError):
        envelope.load_signing_key(path)
    (tmp_path / "garbage.pem").write_text("nope")
    with pytest.raises(envelope.KeyFileError):
        envelope.load_signing_key(tmp_path / "garbage.pem")
    with pytest.raises(envelope.KeyFileError):
        envelope.load_signing_key(tmp_path / "missing.pem")


def test_public_key_file_round_trip_and_strictness(tmp_path):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    line = envelope.public_key_line(key)
    assert line == _b64(raw) + "\n"
    path = tmp_path / "signing.pem.pub"
    path.write_text(line)
    assert envelope.load_public_key(path) == raw
    for bad in (_b64(raw), _b64(raw) + "\n\n", " " + line, _b64(raw[:31]) + "\n"):
        path.write_text(bad)
        with pytest.raises(envelope.KeyFileError):
            envelope.load_public_key(path)


# --- The fixture vectors ------------------------------------------------------------


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text("utf-8"))


def _vector_keys(vectors, device_name: str, sender_name: str):
    device = X25519PrivateKey.from_private_bytes(_unb64(vectors["keys"][device_name]["private_raw"]))
    sender_raw = _unb64(vectors["keys"][sender_name]["public_raw"])
    return device, Ed25519PublicKey.from_public_bytes(sender_raw)


def test_vector_keys_are_derived_from_their_labels(vectors):
    for name, entry in vectors["keys"].items():
        private_raw = _unb64(entry["private_raw"])
        assert private_raw == hashlib.sha256(entry["label"].encode("utf-8")).digest(), name
        assert "test" in entry["label"]
        if entry["type"] == "ed25519":
            public = Ed25519PrivateKey.from_private_bytes(private_raw).public_key().public_bytes_raw()
        else:
            public = X25519PrivateKey.from_private_bytes(private_raw).public_key().public_bytes_raw()
        assert _unb64(entry["public_raw"]) == public, name
        assert entry["key_id"] == envelope.key_id(public), name


def test_vectors_cover_what_the_spec_requires(vectors):
    positive, negative = vectors["positive"], vectors["negative"]
    assert len(positive) >= 6 and len(negative) >= 8
    assert {v["padded_length"] for v in positive} == {512, 1024, 1536, 2048}
    assert any(json.loads(v["header"])["collapse"] is None for v in positive)
    assert any(json.loads(v["header"])["badge"] is None for v in positive)
    assert any(json.loads(v["plaintext"])["target"] == {"kind": "inbox"} for v in positive)
    assert any(any(ord(c) > 127 for c in json.loads(v["plaintext"])["body"]) for v in positive)
    reasons = {v["expected_error"] for v in negative}
    assert {"signature", "device_key", "sender_key", "lifetime", "version",
            "duplicate_key", "padding"} <= reasons


@pytest.mark.parametrize("index", range(7))
def test_every_positive_vector_opens(vectors, index):
    vector = vectors["positive"][index]
    device, sender = _vector_keys(vectors, vector["device_key"], vector["sender_key"])
    payload = vector["apns_payload"].encode("utf-8")
    assert len(payload) <= 4096
    opened = envelope.open_for_tests(payload, device_private_key=device, sender_public_key=sender,
                                     now=vector["now"], binding=vector["binding"],
                                     generation=vector["generation"])
    assert opened.header_bytes == vector["header"].encode("utf-8")
    assert opened.header_bytes == _unb64(vector["header_b64"])
    assert opened.plaintext == json.loads(vector["plaintext"])
    assert opened.padded[4:4 + int.from_bytes(opened.padded[:4], "big")] == vector["plaintext"].encode("utf-8")
    assert len(opened.padded) == vector["padded_length"]

    private = json.loads(payload)["istota_private"]
    assert private["h"] == vector["header_b64"]
    assert private["c"] == vector["enc_ciphertext_b64"]
    assert private["s"] == vector["signature_b64"]
    assert _unb64(vector["enc_b64"]) == _unb64(private["c"])[:32]
    assert _unb64(vector["info_b64"]) == b"istota-private-push-v1\x00" + opened.header_bytes

    sender_private = Ed25519PrivateKey.from_private_bytes(
        _unb64(vectors["keys"][vector["sender_key"]]["private_raw"]))
    group = vector["logical_group"]
    expected_collapse = None if group is None else envelope.collapse_id(sender_private, vector["binding"], group)
    assert opened.header["collapse"] == expected_collapse
    assert opened.plaintext["group"] == expected_collapse


def test_every_vector_is_parametrized(vectors):
    assert len(vectors["positive"]) == 7
    assert len(vectors["negative"]) == 13


@pytest.mark.parametrize("index", range(13))
def test_every_negative_vector_fails_for_its_stated_reason(vectors, index):
    vector = vectors["negative"][index]
    device, sender = _vector_keys(vectors, vector["device_key"], vector["sender_key"])
    with pytest.raises(envelope.EnvelopeError) as caught:
        envelope.open_for_tests(vector["apns_payload"].encode("utf-8"), device_private_key=device,
                                sender_public_key=sender, now=vector["now"])
    assert caught.value.reason == vector["expected_error"], vector["name"]
