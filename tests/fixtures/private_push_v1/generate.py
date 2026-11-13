"""Regenerate vectors.json: `uv run python tests/fixtures/private_push_v1/generate.py`.

Every key is derived from a public label, so it is a test key by construction.
HPKE and the padding are randomized, so a run changes the ciphertexts; the
keys, headers and plaintexts stay the same. tests/test_push_envelope.py opens
the result, so a file this script writes that no longer opens fails there.
"""

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from istota.push import envelope

OUT = Path(__file__).with_name("vectors.json")
NOW = 1_800_000_000
BINDING = "6b1f0c2e-4d5a-4f3b-8e9c-0a1b2c3d4e5f"
GENERATION = "R2VuZXJhdGlvbi12MS10ZQ"
LABELS = {
    "sender": ("ed25519", "istota private push v1 test sender key"),
    "other_sender": ("ed25519", "istota private push v1 test other sender key"),
    "device": ("x25519", "istota private push v1 test device key"),
    "other_device": ("x25519", "istota private push v1 test other device key"),
}
CHAT = {"kind": "chat", "room_token": "w8x2k4m9", "room_id": 7,
        "connection_binding": BINDING, "connection_generation": 2}


def _keys():
    keys, entries = {}, {}
    for name, (kind, label) in LABELS.items():
        raw = hashlib.sha256(label.encode("utf-8")).digest()
        key = (Ed25519PrivateKey if kind == "ed25519" else X25519PrivateKey).from_private_bytes(raw)
        public = key.public_key().public_bytes_raw()
        keys[name] = key
        entries[name] = {"type": kind, "label": label, "private_raw": envelope.b64url(raw),
                         "public_raw": envelope.b64url(public), "key_id": envelope.key_id(public)}
    return keys, entries


def _device_public(keys, name="device"):
    return keys[name].public_key().public_bytes_raw()


def _mid(n: int) -> str:
    return envelope.b64url(hashlib.sha256(b"istota private push v1 test event %d" % n).digest()[:16])


def _positive(keys, n, name, description, *, title, body, badge=3, group="room:w8x2k4m9",
              target=CHAT, alert_title="Istota"):
    payload, collapse = envelope.seal(
        signing_key=keys["sender"], device_public_key=_device_public(keys), binding=BINDING,
        generation=GENERATION, now=NOW, alert_title=alert_title, title=title, body=body,
        badge=badge, logical_group=group, target=target, mid=_mid(n))
    opened = envelope.open_for_tests(payload, device_private_key=keys["device"],
                                     sender_public_key=keys["sender"].public_key(), now=NOW)
    private = json.loads(payload)["istota_private"]
    sealed = envelope.b64url_decode(private["c"])
    length = int.from_bytes(opened.padded[:4], "big")
    return {
        "name": name, "description": description, "now": NOW,
        "device_key": "device", "sender_key": "sender",
        "binding": BINDING, "generation": GENERATION, "logical_group": group,
        "collapse_id": collapse,
        "header": opened.header_bytes.decode("utf-8"),
        "header_b64": private["h"],
        "info_b64": envelope.b64url(envelope.INFO_PREFIX + opened.header_bytes),
        "enc_b64": envelope.b64url(sealed[:32]),
        "enc_ciphertext_b64": private["c"],
        "signature_b64": private["s"],
        "padded_length": len(opened.padded),
        "plaintext": opened.padded[4:4 + length].decode("utf-8"),
        "apns_payload": payload.decode("utf-8"),
    }


def _pack(header_bytes: bytes, sealed: bytes, signature: bytes, badge=3) -> str:
    aps = {"alert": {"title": "Istota", "body": envelope.GENERIC_BODY}, "mutable-content": 1, "sound": "default"}
    if badge is not None:
        aps["badge"] = badge
    outer = {"aps": aps, "istota_private": {"h": envelope.b64url(header_bytes), "c": envelope.b64url(sealed),
                                            "s": envelope.b64url(signature)}}
    return json.dumps(outer, separators=(",", ":"), ensure_ascii=False)


def _negative(name, description, apns_payload, expected_error, *, device_key="device", sender_key="sender",
              now=NOW):
    return {"name": name, "description": description, "now": now, "device_key": device_key,
            "sender_key": sender_key, "expected_error": expected_error, "apns_payload": apns_payload}


def _base_header(n: int, **changes) -> dict:
    header = {"v": 1, "binding": BINDING, "generation": GENERATION, "kid": None, "sid": None,
              "mid": _mid(n), "iat": NOW, "exp": NOW + 300, "kind": "alert", "collapse": None, "badge": 3}
    header.update(changes)
    return header


def _inner_for(header: dict, **changes) -> dict:
    inner = {name: header[name] for name in ("v", "binding", "generation", "mid", "exp", "badge")}
    inner.update(title="Sam Example", body="Lunch at noon?", group=None, target=CHAT)
    inner.update(changes)
    return inner


def _crafted(keys, header_bytes: bytes, padded: bytes, *, device="device", badge=3) -> str:
    sealed, signature = envelope.sign_and_encrypt(header_bytes, padded, _device_public(keys, device), keys["sender"])
    return _pack(header_bytes, sealed, signature, badge=badge)


def _negatives(keys, base: dict) -> list:
    payload = base["apns_payload"]
    outer = json.loads(payload)
    header = envelope.b64url_decode(outer["istota_private"]["h"])
    sealed = envelope.b64url_decode(outer["istota_private"]["c"])
    signature = envelope.b64url_decode(outer["istota_private"]["s"])
    kid = envelope.key_id(_device_public(keys))
    sid = envelope.key_id(keys["sender"].public_key().public_bytes_raw())
    vectors = []

    altered = header.replace(b'"badge":3', b'"badge":4')
    assert altered != header
    vectors.append(_negative("tampered_header", "The header's badge changed from 3 to 4 after signing.",
                             _pack(altered, sealed, signature, badge=4), "signature"))
    vectors.append(_negative("tampered_ciphertext", "The last byte of enc || ciphertext has one bit flipped.",
                             _pack(header, sealed[:-1] + bytes([sealed[-1] ^ 1]), signature), "signature"))
    vectors.append(_negative("tampered_signature", "The last byte of the signature has one bit flipped.",
                             _pack(header, sealed, signature[:-1] + bytes([signature[-1] ^ 1])), "signature"))
    vectors.append(_negative("wrong_device_key", "Opened with a device key the header's kid does not name.",
                             payload, "device_key", device_key="other_device"))
    vectors.append(_negative("wrong_sender_key", "Verified against a sender key the header's sid does not name.",
                             payload, "sender_key", sender_key="other_sender"))
    vectors.append(_negative("expired", "Opened one second after exp.", payload, "expired", now=NOW + 301))

    fields = _base_header(20, kid=kid, sid=sid, exp=NOW + 301)
    long_header = envelope.encode_json(fields)
    vectors.append(_negative("lifetime_over_300", "Signed correctly, with exp - iat = 301.",
                             _crafted(keys, long_header, envelope.pad(envelope.encode_json(_inner_for(fields)))),
                             "lifetime"))

    fields = _base_header(21, kid=kid, sid=sid, v=2)
    v2 = envelope.encode_json(fields)
    vectors.append(_negative("unknown_version", "Signed correctly, with v = 2.",
                             _crafted(keys, v2, envelope.pad(envelope.encode_json(_inner_for(fields)))), "version"))

    fields = _base_header(22, kid=kid, sid=sid)
    duplicated = envelope.encode_json(fields)[:-1] + b',"badge":4}'
    vectors.append(_negative("duplicate_header_key", "Signed correctly; the header names badge twice (3, then 4).",
                             _crafted(keys, duplicated, envelope.pad(envelope.encode_json(_inner_for(fields)))),
                             "duplicate_key"))

    fields = _base_header(23, kid=kid, sid=sid)
    inner = envelope.encode_json(_inner_for(fields))
    padded = envelope.pad(inner)
    overlong = (len(padded) - 3).to_bytes(4, "big") + padded[4:]
    vectors.append(_negative("padded_length_inconsistent",
                             "Signed and encrypted correctly; the length prefix is one byte past the bucket.",
                             _crafted(keys, envelope.encode_json(fields), overlong), "padding"))

    fields = _base_header(24, kid=kid, sid=sid)
    inner = envelope.encode_json(_inner_for(fields))
    oversized = len(inner).to_bytes(4, "big") + inner + b"\x00" * (1024 - 4 - len(inner))
    vectors.append(_negative("padding_not_smallest_bucket",
                             "Signed and encrypted correctly; a 512-byte plaintext padded to 1024.",
                             _crafted(keys, envelope.encode_json(fields), oversized), "padding"))

    fields = _base_header(25, kid=kid, sid=sid)
    inner = envelope.encode_json(_inner_for(fields, binding="00000000-0000-4000-8000-000000000000"))
    vectors.append(_negative("inner_mismatch", "Signed and encrypted correctly; the inner binding differs.",
                             _crafted(keys, envelope.encode_json(fields), envelope.pad(inner)), "inner_mismatch"))

    fields = _base_header(26, kid=kid, sid=sid)
    vectors.append(_negative("decrypt_failure",
                             "Signed correctly and naming the device's kid, but encrypted to another key.",
                             _crafted(keys, envelope.encode_json(fields),
                                      envelope.pad(envelope.encode_json(_inner_for(fields))), device="other_device"),
                             "decrypt"))
    return vectors


def main() -> None:
    keys, entries = _keys()
    long_link = "/istota/notifications?view=" + "a" * 600
    positive = [
        _positive(keys, 1, "ascii_chat", "A chat alert in the smallest bucket.",
                  title="Sam Example", body="Lunch at noon?"),
        _positive(keys, 2, "multibyte_at_caps", "An 80-character title and 180-character body, all multibyte.",
                  title="\U0001d11e" * 80, body="é\U0001f600" * 90),
        _positive(keys, 3, "bucket_1024", "A notification whose link fills the 1024-byte bucket.",
                  title="Build finished", body="All checks passed.", group="notif:jobs",
                  target={"kind": "notification", "notification_id": 42, "link": long_link}),
        _positive(keys, 4, "bucket_2048", "A notification whose link fills the 2048-byte bucket.",
                  title="Build finished", body="All checks passed.", group="notif:jobs",
                  target={"kind": "notification", "notification_id": 43, "link": "/istota/" + "b" * 1600}),
        _positive(keys, 5, "inbox_fallback", "A target too large for any bucket, replaced by the inbox target.",
                  title="Build finished", body="All checks passed.", group="notif:jobs",
                  target={"kind": "notification", "notification_id": 44, "link": "/istota/" + "c" * 3000}),
        _positive(keys, 6, "null_collapse", "No logical group, so no collapse ID and a null group.",
                  title="Sam Example", body="Are you around?", group=None),
        _positive(keys, 7, "null_badge", "No badge: the header and inner badge are null and aps has none.",
                  title="Sam Example", body="See you soon.", badge=None),
    ]
    buckets = [vector["padded_length"] for vector in positive]
    assert set(buckets) == set(envelope.BUCKETS), buckets
    assert json.loads(positive[4]["plaintext"])["target"] == envelope.INBOX_TARGET
    document = {
        "description": "Istota private push v1 test vectors. Every key here is a test key derived from its label "
                       "as SHA-256(label); none is used anywhere else. See README.md and "
                       "docs/reference/private-push-v1.md.",
        "version": 1,
        "suite": {"kem": "DHKEM(X25519, HKDF-SHA256)", "kdf": "HKDF-SHA256", "aead": "AES-128-GCM",
                  "mode": "base", "signature": "Ed25519"},
        "keys": entries,
        "positive": positive,
        "negative": _negatives(keys, positive[0]),
    }
    OUT.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", "utf-8")


if __name__ == "__main__":
    main()
