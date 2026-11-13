# Private push, version 1

This page is the wire format for private push notifications: how an Istota installation seals a notification for one iOS device, and how a receiver (the app's Notification Service Extension, the app on a tap, or a relay checking a signature) must verify and open it. It is normative. "Must" means a receiver that does otherwise is wrong; there are no optional behaviours.

The reference implementation is `src/istota/push/envelope.py`. The test vectors in `tests/fixtures/private_push_v1/vectors.json` are the contract between this repository, the iOS app and the relay: each of them tests against that one file. A change to this page, the module or the vectors is a change to all three.

## What it protects

The installation encrypts the title, body, routing target and display group of an alert to a key held only by the device, and signs the result with a key held only by the installation. Apple and any relay see a fixed alert, ciphertext, the delivery time, the padded size, the visible badge count, and opaque identifiers. They do not see room names, room tokens, notification IDs, routes, account IDs or the installation's address. Padding hides the exact length, not the timing or the volume of notifications.

## Suite

Version 1 fixes one suite. A receiver must never choose an algorithm from a field in the message.

- Encryption: RFC 9180 HPKE, base mode, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, AES-128-GCM.
- Signature: Ed25519 (RFC 8032).
- Every notification is its own single-shot HPKE context at sequence number zero. A context is never reused: APNs drops, coalesces and reorders notifications.

## Encodings

- base64url is RFC 4648 section 5 without padding. A receiver must refuse `=`, characters outside `A-Z a-z 0-9 - _`, a length that is 1 modulo 4, and a non-canonical encoding (one whose unused trailing bits are not zero).
- JSON is UTF-8. A receiver must refuse invalid UTF-8, duplicate object keys at any depth, and the non-standard numbers `NaN`, `Infinity` and `-Infinity`.
- The header and the inner plaintext are serialized compactly with sorted keys and no ASCII escaping (Python: `json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)`). This makes the vectors deterministic. A receiver must verify the bytes it received and must never reserialize them.
- Integers are JSON integers. `true` and `false` are not integers.

## Keys and identifiers

| Name | What it is |
|---|---|
| Sender key | The installation's Ed25519 signing key. Its public half is published to enrolled devices, which pin it. |
| Device key | An X25519 key pair the device generates per enrollment. Only the public half leaves the device. |
| Key ID | base64url of SHA-256 over a raw 32-byte public key: 43 characters. `kid` names the device key, `sid` the sender key. |
| `binding` | The connection binding the app generated for the connection: a lowercase UUID, `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` in hex digits `0-9 a-f`. |
| `generation` | 16 random bytes the app generates per device-key enrollment, base64url: 22 characters. |
| `mid` | Event ID: 16 random bytes per alert per device, base64url, 22 characters. A transport retry of the same bytes keeps it; nothing else reuses it. |

### Collapse IDs

The collapse ID groups alerts on the phone without naming the room. For a logical group `g` (for example `room:<token>` or `notif:<source>`) and binding `b`:

1. `collapse_key = HKDF-SHA256(salt = none, ikm = the sender key's raw 32-byte private key, info = "istota-push-collapse-v1", length = 32)`.
2. `collapse_id = base64url(HMAC-SHA256(collapse_key, UTF-8(b + "\x00" + g))[0:16])`: 22 characters.

The same value is the `apns-collapse-id` request header, the header's `collapse` and the inner `group`. A new sender key changes every collapse ID; that is expected, because a new sender key requires every device to enroll again. An alert with no logical group has a null `collapse` and a null `group`.

## The APNs payload

An encrypted alert's APNs body has exactly two top-level keys:

```json
{
  "aps": {
    "alert": {"title": "Istota", "body": "You have new activity."},
    "mutable-content": 1,
    "sound": "default",
    "badge": 3
  },
  "istota_private": {
    "h": "<base64url header bytes>",
    "c": "<base64url enc || ciphertext>",
    "s": "<base64url signature>"
  }
}
```

- `aps.alert.title` is the installation's bot name, with control characters removed, whitespace collapsed and cut to 80 characters. `aps.alert.body` is the fixed string `You have new activity.`. They are what iOS shows if the extension does not run or refuses the message.
- `aps.badge` is present only when the header's `badge` is not null, and must then equal it.
- There is no `thread-id` and no `istota` object. A receiver must refuse a body with any other top-level key: an unauthenticated routing object beside the envelope is never used.
- `istota_private` has exactly `h`, `c` and `s`, each a base64url string.
- The serialized body is at most 4096 bytes.

## Header

The header is a JSON object with exactly these fields:

| Field | Type | Rule |
|---|---|---|
| `v` | integer | `1` |
| `binding` | string | lowercase UUID, as above |
| `generation` | string | 22 base64url characters |
| `kid` | string | key ID of the device public key the message is encrypted to |
| `sid` | string | key ID of the sender public key that signed it |
| `mid` | string | 22 base64url characters |
| `iat` | integer | issue time, Unix seconds, not negative |
| `exp` | integer | expiry, Unix seconds; `0 < exp - iat <= 300` |
| `kind` | string | `"alert"` |
| `collapse` | string or null | 22 base64url characters, or null |
| `badge` | integer or null | 0 to 9999, or null |

The sender sets `iat` to the current time in whole seconds and `exp` to `iat + 300`. The decoded header is at most 1024 bytes.

## Inner plaintext

The plaintext is a JSON object with exactly these fields:

| Field | Type | Rule |
|---|---|---|
| `v`, `binding`, `generation`, `mid`, `exp`, `badge` | as in the header | must equal the header's value and type |
| `title` | string | at most 80 characters |
| `body` | string | at most 180 characters; empty when previews are off |
| `group` | string or null | the collapse ID, used as the local thread identifier |
| `target` | object | the routing target, with a string `kind` |

`target` is the routing object a plaintext alert would carry under `istota` (for a chat, `kind`, `room_token`, `room_id`, `connection_binding`, `connection_generation`; for a notification, `kind`, `notification_id` and an optional `link`), or `{"kind": "inbox"}` when that object does not fit. A receiver must still check a target against its own routing rules before it acts on it; the signature says who sent it, not that it is safe.

## Padding

The padded plaintext is:

```
uint32_be(len(json)) || json || random bytes
```

padded to the smallest of 512, 1024, 1536 and 2048 bytes that holds it. The JSON is therefore at most 2044 bytes.

A receiver must refuse a padded plaintext whose length is not one of the four buckets, whose length prefix exceeds the padded length minus 4, or which would fit a smaller bucket. It parses only the declared number of bytes, and only after the signature has verified and decryption has succeeded.

## Encryption and signature

```
info      = "istota-private-push-v1" || 0x00 || header_bytes
sealed    = HPKE.SealBase(device_public_key, info, aad = "", padded)   -- enc (32 bytes) || ciphertext
signed    = "istota-private-push-sign-v1" || 0x00 || uint32_be(len(header_bytes)) || header_bytes || sealed
signature = Ed25519.Sign(sender_private_key, signed)                   -- 64 bytes
```

The AAD is empty; the header is bound through `info` and the signature. `sealed` is 32 bytes of `enc`, then the ciphertext, which is the padded plaintext plus a 16-byte AES-GCM tag. Its length is therefore 560, 1072, 1584 or 2096 bytes.

In Python this is `Suite(KEM.X25519, KDF.HKDF_SHA256, AEAD.AES_128_GCM).encrypt(padded, device_public_key, info=info)` from `cryptography.hazmat.primitives.hpke` (cryptography 47 or later), which returns `enc || ciphertext`. On iOS, CryptoKit's `HPKE.Recipient(privateKey:ciphersuite:info:encapsulatedKey:)` with the first 32 bytes of `sealed` as the encapsulated key and the rest as the ciphertext is expected to open it; that has not yet been run against the vectors, and the iOS app's first stage records the result here.

## Sealing

1. Compute the collapse ID, if there is a logical group, and the header. Serialize the header.
2. Build the inner plaintext. If its JSON exceeds 2044 bytes, remove characters from the end of the body, then from the end of the title, until it fits. If it still does not fit with an empty body and title, replace the target with `{"kind": "inbox"}`, restore the original title and body, and shorten them again in the same order. Identifiers, the target's fields and ciphertext are never cut.
3. Pad, encrypt, sign, and build the APNs body.
4. Measure the serialized APNs body. Over 4096 bytes, the build fails and nothing is sent. A sender never falls back to a plaintext alert.

## Opening

A receiver must perform these checks in this order and refuse the message at the first that fails. The names in brackets are the `reason` values `envelope.open_for_tests` raises, and the `expected_error` values in the vectors.

1. The APNs body is at most 4096 bytes and is strict JSON with exactly `aps` and `istota_private`; `istota_private` has exactly `h`, `c`, `s`, each strict base64url. The decoded header is 1 to 1024 bytes, the signature 64 bytes, and `sealed` one of the four lengths above. (`malformed`; a repeated JSON key anywhere is `duplicate_key`)
2. The header is strict JSON (`header_invalid`, or `duplicate_key`) whose `v` is the integer 1 (`version`).
3. The header has exactly the fields above with their types and ranges (`header_invalid`), and `0 < exp - iat <= 300` (`lifetime`).
4. `sid` is the key ID of the pinned sender key (`sender_key`). `kid` is the key ID of the receiver's device key (`device_key`).
5. The Ed25519 signature verifies over `signed` with the pinned sender key (`signature`). Nothing below runs on an unverified message.
6. `now <= exp` (`expired`) and `now >= iat - 60` (`not_yet_valid`).
7. `binding` and `generation` are the receiver's active enrollment (`binding`, `generation`). The vectors leave these to the receiver.
8. If `aps.badge` is present, it is an integer equal to the header's `badge` (`badge`).
9. HPKE opens `sealed` with the device private key and `info` (`decrypt`).
10. The padding is as described above (`padding`).
11. The inner plaintext is strict JSON with exactly the fields above and their types (`inner_invalid`), and its `v`, `binding`, `generation`, `mid`, `exp` and `badge` equal the header's (`inner_mismatch`).

A receiver that refuses a message shows the fixed outer alert and attaches no target. Replay protection (claiming `mid` once per active binding until `exp` plus 60 seconds) is the receiver's own state and is not part of the wire format.

## Other payloads under a private deployment

- **Generic alert**, for a device without an encrypted enrollment: `{"aps": {"alert": {"title": <bot name>, "body": "You have new activity."}, "sound": "default", "badge": n}}`, with no `mutable-content` and no routing object.
- **Badge-only update**, unchanged from a plaintext deployment: priority 5, collapse ID `badge`, and only `aps.badge`.

## Test vectors

`tests/fixtures/private_push_v1/vectors.json` holds:

- `keys`: the test keys, each `SHA-256(label)` of a public label, with the raw public key and key ID. No vector key is used anywhere else.
- `positive`: messages that must open, each with the exact header bytes, `info`, `enc`, `enc || ciphertext`, the signature, the padded length, the exact inner JSON, the logical group and collapse ID, and the complete APNs body. They cover every padding bucket, multibyte text at the caps, the inbox fallback, a null collapse and a null badge.
- `negative`: messages that must be refused, each with the `expected_error` above and the `now` to open it at. Each is otherwise valid, so it fails at the check it names: altered header, ciphertext or signature, the wrong device or sender key, expiry, a lifetime over 300 seconds, version 2, a duplicated header key, an inconsistent length prefix, a padding bucket larger than needed, an inner field that disagrees with the header, and a message signed for the device but encrypted to another key.

`tests/fixtures/private_push_v1/README.md` says how the file is generated. `tests/test_push_envelope.py` opens every vector, so a regenerated file that no longer opens fails there.
