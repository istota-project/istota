"""The private push envelope, version 1: seal for a device, open for the tests.

`docs/reference/private-push-v1.md` is the normative description; this module
is the reference implementation the iOS app and the relay are held to through
`tests/fixtures/private_push_v1/vectors.json`. Change neither without the other.

HPKE base mode (RFC 9180, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, AES-128-GCM)
protects the content; it does not say who sent it, so an Ed25519 signature over
the header and `enc || ciphertext` does. Every message is one single-shot HPKE
context: APNs drops, coalesces and reorders, so no context is ever kept.

No function here logs. Titles, bodies, headers, envelopes and keys stay out of
every log line, which is the caller's rule too.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from istota.lib.atomic_write import write_bytes_atomic

VERSION = 1
SUITE = Suite(KEM.X25519, KDF.HKDF_SHA256, AEAD.AES_128_GCM)
INFO_PREFIX = b"istota-private-push-v1\x00"
SIGN_PREFIX = b"istota-private-push-sign-v1\x00"
COLLAPSE_INFO = b"istota-push-collapse-v1"
GENERIC_BODY = "You have new activity."
INBOX_TARGET = {"kind": "inbox"}
MAX_LIFETIME = 300
FUTURE_SKEW = 60
BUCKETS = (512, 1024, 1536, 2048)
MAX_APNS_BYTES = 4096
TITLE_MAX = 80
BODY_MAX = 180
_ENC_BYTES = 32
_TAG_BYTES = 16
_SIGNATURE_BYTES = 64
_MAX_HEADER_BYTES = 1024
_HEADER_KEYS = {"v", "binding", "generation", "kid", "sid", "mid", "iat", "exp", "kind", "collapse", "badge"}
_INNER_KEYS = {"v", "binding", "generation", "mid", "exp", "badge", "title", "body", "group", "target"}
_MATCHED_KEYS = ("v", "binding", "generation", "mid", "exp", "badge")
_BINDING = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_B64URL = re.compile(r"[A-Za-z0-9_-]*")
_ID16 = re.compile(r"[A-Za-z0-9_-]{22}")
_ID32 = re.compile(r"[A-Za-z0-9_-]{43}")
_PUBLIC_KEY_LINE = re.compile(r"[A-Za-z0-9_-]{43}\n")


class EnvelopeError(ValueError):
    """A refusal with a stable reason, the string the vectors' `expected_error` names."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class PayloadTooLarge(EnvelopeError):
    def __init__(self, size: int):
        super().__init__("payload_too_large", f"{size} bytes")


class KeyFileError(ValueError):
    pass


@dataclass(frozen=True)
class Opened:
    header: dict
    header_bytes: bytes
    plaintext: dict
    padded: bytes


# --- Encodings ---------------------------------------------------------------


MAX_BADGE = 9999

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Unpadded base64url, canonical only: one encoding per byte string."""
    if not isinstance(text, str) or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not unpadded base64url")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise ValueError("not unpadded base64url") from error
    if b64url(data) != text:
        raise ValueError("non-canonical base64url")
    return data


def encode_json(obj: dict) -> bytes:
    """The header and inner serialization: compact, sorted, UTF-8."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _refuse_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise EnvelopeError("duplicate_key", key)
        seen[key] = value
    return seen


def _refuse_constant(name):
    raise ValueError(f"non-finite number {name}")


def _strict_json(data: bytes, reason: str):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EnvelopeError(reason, "invalid UTF-8") from error
    try:
        return json.loads(text, object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant)
    except EnvelopeError:
        raise
    except ValueError as error:
        raise EnvelopeError(reason, "invalid JSON") from error


# --- Identifiers ---------------------------------------------------------------


def usable_device_key(raw_public_key: bytes) -> bool:
    """Whether HPKE can seal to this X25519 key; a low-order point gives an all-zero secret and fails."""
    try:
        X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(raw_public_key))
    except ValueError:
        return False
    return True


def key_id(raw_public_key: bytes) -> str:
    """`kid` / `sid`: base64url SHA-256 of a raw 32-byte public key."""
    if len(raw_public_key) != 32:
        raise ValueError("a raw public key is 32 bytes")
    return b64url(hashlib.sha256(raw_public_key).digest())


def new_event_id() -> str:
    return b64url(secrets.token_bytes(16))


def _collapse_key(signing_key: Ed25519PrivateKey) -> bytes:
    hkdf = HKDF(algorithm=SHA256(), length=32, salt=None, info=COLLAPSE_INFO)
    return hkdf.derive(signing_key.private_bytes_raw())


def collapse_id(signing_key: Ed25519PrivateKey, binding: str, logical_group: str) -> str:
    """The opaque per-binding group: `apns-collapse-id`, header `collapse`, inner `group`."""
    message = (binding + "\x00" + logical_group).encode("utf-8")
    return b64url(hmac.new(_collapse_key(signing_key), message, "sha256").digest()[:16])


# --- Keys -----------------------------------------------------------------------


def load_signing_key(path) -> Ed25519PrivateKey:
    """The installation's Ed25519 key, unencrypted PKCS8 PEM."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise KeyFileError(f"cannot read the push signing key: {error.__class__.__name__}") from error
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as error:
        raise KeyFileError("the push signing key is not an unencrypted PKCS8 PEM key") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError("the push signing key is not an Ed25519 key")
    return key


def public_key_line(signing_key: Ed25519PrivateKey) -> str:
    """What the `.pub` sibling holds: the raw public key, base64url, one newline."""
    return b64url(signing_key.public_key().public_bytes_raw()) + "\n"


def load_public_key(path) -> bytes:
    """The raw 32-byte public key from a `.pub` file, which holds exactly one line."""
    try:
        text = Path(path).read_text("ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise KeyFileError(f"cannot read the push public key: {error.__class__.__name__}") from error
    if not _PUBLIC_KEY_LINE.fullmatch(text):
        raise KeyFileError("the push public key file is not one base64url line")
    try:
        raw = b64url_decode(text[:-1])
    except ValueError as error:
        raise KeyFileError("the push public key file is not canonical base64url") from error
    if len(raw) != 32:
        raise KeyFileError("the push public key is not 32 bytes")
    return raw


def public_key_path(signing_key_path) -> Path:
    return Path(f"{signing_key_path}.pub")


def write_signing_key(path, *, force: bool = False) -> Ed25519PrivateKey:
    """Write a fresh key to `path` (0600) and its `.pub` sibling (0644).

    Refuses with `FileExistsError` when either file exists, unless `force`.
    Each file is replaced atomically, the private key first, so a crash
    between the two leaves a pair doctor reports as mismatched rather than a
    public key with no private half.
    """
    path = Path(path)
    pub = public_key_path(path)
    if not force and (path.exists() or pub.exists()):
        raise FileExistsError(str(path))
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, mode=0o700)
        os.chmod(path.parent, 0o700)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    write_bytes_atomic(path, pem, mode=0o600, fsync=True)
    write_bytes_atomic(pub, public_key_line(key).encode("ascii"), mode=0o644, fsync=True)
    return key


# --- Validation shared by seal and open -----------------------------------------------


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_header(header: dict) -> None:
    if set(header) != _HEADER_KEYS:
        raise EnvelopeError("header_invalid", "wrong fields")
    if header["kind"] != "alert":
        raise EnvelopeError("header_invalid", "kind")
    if not isinstance(header["binding"], str) or not _BINDING.fullmatch(header["binding"]):
        raise EnvelopeError("header_invalid", "binding")
    for name, pattern in (("generation", _ID16), ("mid", _ID16), ("kid", _ID32), ("sid", _ID32)):
        if not isinstance(header[name], str) or not pattern.fullmatch(header[name]):
            raise EnvelopeError("header_invalid", name)
    collapse = header["collapse"]
    if collapse is not None and (not isinstance(collapse, str) or not _ID16.fullmatch(collapse)):
        raise EnvelopeError("header_invalid", "collapse")
    badge = header["badge"]
    if badge is not None and (not _is_int(badge) or not 0 <= badge <= MAX_BADGE):
        raise EnvelopeError("header_invalid", "badge")
    if not _is_int(header["iat"]) or not _is_int(header["exp"]) or header["iat"] < 0:
        raise EnvelopeError("header_invalid", "timestamps")
    if not 0 < header["exp"] - header["iat"] <= MAX_LIFETIME:
        raise EnvelopeError("lifetime")


def _check_inner(inner: dict, header: dict) -> None:
    if not isinstance(inner, dict) or set(inner) != _INNER_KEYS:
        raise EnvelopeError("inner_invalid", "wrong fields")
    if not isinstance(inner["title"], str) or len(inner["title"]) > TITLE_MAX:
        raise EnvelopeError("inner_invalid", "title")
    if not isinstance(inner["body"], str) or len(inner["body"]) > BODY_MAX:
        raise EnvelopeError("inner_invalid", "body")
    if inner["group"] is not None and not isinstance(inner["group"], str):
        raise EnvelopeError("inner_invalid", "group")
    if not isinstance(inner["target"], dict) or not isinstance(inner["target"].get("kind"), str):
        raise EnvelopeError("inner_invalid", "target")
    for name in _MATCHED_KEYS:
        if inner[name] != header[name] or type(inner[name]) is not type(header[name]):
            raise EnvelopeError("inner_mismatch", name)


# --- Seal ---------------------------------------------------------------------------


def pad(inner_json: bytes) -> bytes:
    """Length prefix, the JSON, random bytes to the smallest bucket that holds them."""
    needed = 4 + len(inner_json)
    for bucket in BUCKETS:
        if needed <= bucket:
            return len(inner_json).to_bytes(4, "big") + inner_json + secrets.token_bytes(bucket - needed)
    raise EnvelopeError("inner_too_large", f"{len(inner_json)} bytes")


def sign_and_encrypt(header_bytes: bytes, padded: bytes, device_public_key: bytes,
                     signing_key: Ed25519PrivateKey) -> tuple[bytes, bytes]:
    """`(enc || ciphertext, signature)` for exact header bytes and padded plaintext."""
    try:
        recipient = X25519PublicKey.from_public_bytes(device_public_key)
        sealed = SUITE.encrypt(padded, recipient, info=INFO_PREFIX + header_bytes)
    except Exception as error:
        # A stored key that is the wrong length or a low-order point lands here.
        raise EnvelopeError("seal", error.__class__.__name__) from error
    message = SIGN_PREFIX + len(header_bytes).to_bytes(4, "big") + header_bytes + sealed
    return sealed, signing_key.sign(message)


def _fit_inner(fields: dict, title: str, body: str, target: dict) -> bytes:
    """Shorten the body, then the title, to fit the largest bucket."""
    limit = BUCKETS[-1] - 4
    inner = dict(fields, title=title, body=body, target=target)

    def encoded():
        return encode_json(inner)

    while len(encoded()) > limit and inner["body"]:
        inner["body"] = inner["body"][:-1]
    while len(encoded()) > limit and inner["title"]:
        inner["title"] = inner["title"][:-1]
    return encoded()


def seal(*, signing_key: Ed25519PrivateKey, device_public_key: bytes, binding: str,
         generation: str, now: float, alert_title: str, title: str, body: str,
         badge: int | None, logical_group: str | None, target: dict,
         mid: str | None = None) -> tuple[bytes, str | None]:
    """The exact APNs body for one device and its collapse ID.

    `title`, `body` and `alert_title` arrive sanitized and capped by the caller;
    text over the caps is refused rather than cut here. A target too large to
    fit even with no text is replaced by the inbox target, keeping the text.
    """
    if len(title) > TITLE_MAX or len(body) > BODY_MAX:
        raise ValueError("title or body over its cap")
    if len(device_public_key) != 32:
        raise EnvelopeError("seal", "device public key is not 32 bytes")
    iat = int(now)
    collapse = None if logical_group is None else collapse_id(signing_key, binding, logical_group)
    header = {
        "v": VERSION, "binding": binding, "generation": generation,
        "kid": key_id(device_public_key), "sid": key_id(signing_key.public_key().public_bytes_raw()),
        "mid": new_event_id() if mid is None else mid, "iat": iat, "exp": iat + MAX_LIFETIME,
        "kind": "alert", "collapse": collapse, "badge": badge,
    }
    _check_header(header)
    header_bytes = encode_json(header)

    fields = {name: header[name] for name in _MATCHED_KEYS}
    fields["group"] = collapse
    limit = BUCKETS[-1] - 4
    inner_json = _fit_inner(fields, title, body, target)
    if len(inner_json) > limit:
        inner_json = _fit_inner(fields, title, body, dict(INBOX_TARGET))
    padded = pad(inner_json)

    sealed, signature = sign_and_encrypt(header_bytes, padded, device_public_key, signing_key)
    aps = {"alert": {"title": alert_title, "body": GENERIC_BODY}, "mutable-content": 1, "sound": "default"}
    if badge is not None:
        aps["badge"] = badge
    outer = {"aps": aps, "istota_private": {"h": b64url(header_bytes), "c": b64url(sealed), "s": b64url(signature)}}
    payload = json.dumps(outer, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_APNS_BYTES:
        raise PayloadTooLarge(len(payload))
    return payload, collapse


# --- Open (the reference receiver) ----------------------------------------------------------


def _decode_field(private: dict, name: str) -> bytes:
    try:
        return b64url_decode(private[name])
    except ValueError as error:
        raise EnvelopeError("malformed", name) from error


def open_for_tests(payload: bytes, *, device_private_key: X25519PrivateKey,
                   sender_public_key: Ed25519PublicKey, now: float,
                   binding: str | None = None, generation: str | None = None) -> Opened:
    """Verify and decrypt as a receiver must, in the protocol document's order.

    Used by the tests and as the reference the extension and the relay match.
    `binding` and `generation`, when given, are the receiver's active enrollment.
    """
    if len(payload) > MAX_APNS_BYTES:
        raise EnvelopeError("malformed", "payload over 4096 bytes")
    outer = _strict_json(payload, "malformed")
    if not isinstance(outer, dict) or set(outer) != {"aps", "istota_private"}:
        raise EnvelopeError("malformed", "outer fields")
    aps, private = outer["aps"], outer["istota_private"]
    if not isinstance(aps, dict) or not isinstance(private, dict) or set(private) != {"h", "c", "s"}:
        raise EnvelopeError("malformed", "istota_private fields")
    if not all(isinstance(private[name], str) for name in ("h", "c", "s")):
        raise EnvelopeError("malformed", "istota_private values")

    header_bytes = _decode_field(private, "h")
    sealed = _decode_field(private, "c")
    signature = _decode_field(private, "s")
    if not 0 < len(header_bytes) <= _MAX_HEADER_BYTES:
        raise EnvelopeError("malformed", "header size")
    if len(signature) != _SIGNATURE_BYTES:
        raise EnvelopeError("malformed", "signature size")
    if len(sealed) - _ENC_BYTES - _TAG_BYTES not in BUCKETS:
        raise EnvelopeError("malformed", "ciphertext size")

    header = _strict_json(header_bytes, "header_invalid")
    if not isinstance(header, dict):
        raise EnvelopeError("header_invalid", "not an object")
    if not _is_int(header.get("v")) or header["v"] != VERSION:
        raise EnvelopeError("version")
    _check_header(header)
    if header["sid"] != key_id(sender_public_key.public_bytes_raw()):
        raise EnvelopeError("sender_key")
    if header["kid"] != key_id(device_private_key.public_key().public_bytes_raw()):
        raise EnvelopeError("device_key")

    message = SIGN_PREFIX + len(header_bytes).to_bytes(4, "big") + header_bytes + sealed
    try:
        sender_public_key.verify(signature, message)
    except InvalidSignature as error:
        raise EnvelopeError("signature") from error

    if now > header["exp"]:
        raise EnvelopeError("expired")
    if now < header["iat"] - FUTURE_SKEW:
        raise EnvelopeError("not_yet_valid")
    if binding is not None and header["binding"] != binding:
        raise EnvelopeError("binding")
    if generation is not None and header["generation"] != generation:
        raise EnvelopeError("generation")
    if "badge" in aps and (aps["badge"] != header["badge"] or not _is_int(aps["badge"])):
        raise EnvelopeError("badge")

    try:
        padded = SUITE.decrypt(sealed, device_private_key, info=INFO_PREFIX + header_bytes)
    except Exception as error:
        raise EnvelopeError("decrypt", error.__class__.__name__) from error
    length = int.from_bytes(padded[:4], "big")
    if len(padded) not in BUCKETS or length > len(padded) - 4:
        raise EnvelopeError("padding")
    smaller = [bucket for bucket in BUCKETS if bucket < len(padded)]
    if smaller and 4 + length <= smaller[-1]:
        raise EnvelopeError("padding", "not the smallest bucket")
    inner = _strict_json(padded[4:4 + length], "inner_invalid")
    _check_inner(inner, header)
    return Opened(header=header, header_bytes=header_bytes, plaintext=inner, padded=padded)
