#!/usr/bin/env python3
"""Drive a signed Telnyx webhook at a receiver, without Telnyx.

Telnyx can receive an inbound message, record the correct `webhook_url` and
`errors: []` against it, and never attempt delivery — leaving the whole istota
side of the path (signature check, profile and number gates, user lookup, task
creation, reply) unexercised and with no way to tell a working implementation
from a broken one. This builds the payload Telnyx would have sent, signs it
with an Ed25519 key you hold, and posts it.

The signature is over ``{timestamp}|{body}``, base64'd into
``telnyx-signature-ed25519`` with the stamp in ``telnyx-timestamp``. That is
Telnyx's scheme, so a receiver configured with the matching public key cannot
tell this from the real thing — which is the point, and also the limit:

    This cannot spoof a production deployment. Verification is against the
    configured `public_key`, which in production is Telnyx's, and the matching
    private key is theirs. Point a receiver at a key from `keygen` and you have
    a staging receiver; leave production's key alone and this is inert against
    it.

Usage:

    scripts/sms_mock_webhook.py keygen
    scripts/sms_mock_webhook.py inbound --url URL --key-file PATH \\
        --from +15551234567 --to +15551230000 --profile-id UUID --text "hello"
    scripts/sms_mock_webhook.py delivery --url URL --key-file PATH \\
        --from +15551230000 --to +15551234567 --profile-id UUID \\
        --status delivered

`--from` on an inbound event must be a number bound to a user, `--to` must be
in `service_numbers`, and `--profile-id` must match the configured profile, or
the receiver refuses the event for that reason rather than for its signature.
Those refusals are the interesting ones: each names a different gate.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DELIVERY_STATUSES = ("queued", "sending", "sent", "delivered", "delivery_failed")


def generate_keypair() -> tuple[str, str]:
    """Return (private_b64, public_b64) for a fresh Ed25519 key."""
    private = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode()
    return private_b64, public_b64


def load_private_key(value: str) -> Ed25519PrivateKey:
    raw = base64.b64decode(value.strip())
    if len(raw) != 32:
        raise ValueError(f"private key must decode to 32 bytes, got {len(raw)}")
    return Ed25519PrivateKey.from_private_bytes(raw)


def inbound_payload(
    *,
    from_number: str,
    to_number: str,
    profile_id: str,
    text: str,
    message_id: str | None = None,
    event_id: str | None = None,
    media: list[dict] | None = None,
) -> dict:
    """One `message.received` event, shaped as Telnyx sends it."""
    return {
        "data": {
            "id": event_id or str(uuid.uuid4()),
            "event_type": "message.received",
            "record_type": "event",
            "payload": {
                "id": message_id or str(uuid.uuid4()),
                "record_type": "message",
                "direction": "inbound",
                "type": "SMS",
                "messaging_profile_id": profile_id,
                "from": {"phone_number": from_number},
                "to": [{"phone_number": to_number}],
                "text": text,
                "media": media or [],
            },
        },
    }


def delivery_payload(
    *,
    from_number: str,
    to_number: str,
    profile_id: str,
    status: str,
    message_id: str,
    event_id: str | None = None,
    parts: int = 1,
    errors: list[dict] | None = None,
) -> dict:
    """One `message.finalized` delivery callback.

    `message_id` is required and is not defaulted to a fresh uuid: a callback
    names a message the ledger already claimed, so an invented id exercises the
    unknown-message path rather than the delivery-state path it looks like.
    """
    return {
        "data": {
            "id": event_id or str(uuid.uuid4()),
            "event_type": "message.finalized",
            "record_type": "event",
            "payload": {
                "id": message_id,
                "record_type": "message",
                "direction": "outbound",
                "type": "SMS",
                "messaging_profile_id": profile_id,
                "from": {"phone_number": from_number},
                "to": [{"phone_number": to_number, "status": status}],
                "parts": parts,
                "errors": errors or [],
            },
        },
    }


def sign_payload(
    payload: dict,
    private_key: Ed25519PrivateKey,
    *,
    timestamp: int | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Return the exact bytes to POST and the headers that authenticate them.

    The body is serialized once and signed as serialized — re-encoding it
    anywhere between here and the socket invalidates the signature, which is
    why this returns bytes rather than the dict it was handed.
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = private_key.sign(stamp.encode() + b"|" + body)
    headers = {
        "content-type": "application/json",
        "telnyx-signature-ed25519": base64.b64encode(signature).decode(),
        "telnyx-timestamp": stamp,
    }
    return body, headers


def post(url: str, body: bytes, headers: dict[str, str], *, timeout: float = 15.0):
    """POST and return (status, response_body). A 4xx/5xx is a result, not an error."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _add_event_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", required=True, help="receiver endpoint")
    parser.add_argument("--key-file", help="file holding the base64 private key")
    parser.add_argument("--key", help="the base64 private key itself")
    parser.add_argument("--from", dest="from_number", required=True)
    parser.add_argument("--to", dest="to_number", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--event-id")
    parser.add_argument(
        "--timestamp",
        type=int,
        help="override the signed stamp, to drive the replay window",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="show the body and headers without sending",
    )


def _resolve_key(args) -> Ed25519PrivateKey:
    if args.key_file:
        with open(args.key_file, encoding="utf-8") as handle:
            return load_private_key(handle.read())
    if args.key:
        return load_private_key(args.key)
    raise SystemExit("one of --key-file or --key is required")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keygen", help="generate a keypair for a staging receiver")

    inbound = sub.add_parser("inbound", help="send one message.received event")
    _add_event_arguments(inbound)
    inbound.add_argument("--text", default="ping from the mock harness")
    inbound.add_argument("--message-id")
    inbound.add_argument(
        "--media-url",
        action="append",
        default=[],
        help="attach media, which drives the MMS-unsupported reply",
    )

    delivery = sub.add_parser("delivery", help="send one message.finalized event")
    _add_event_arguments(delivery)
    delivery.add_argument("--status", choices=DELIVERY_STATUSES, default="delivered")
    delivery.add_argument(
        "--message-id",
        required=True,
        help="the provider message id the ledger already holds",
    )
    delivery.add_argument("--parts", type=int, default=1)

    args = parser.parse_args(argv)

    if args.command == "keygen":
        private_b64, public_b64 = generate_keypair()
        print(f"private (sign with this):  {private_b64}")
        print(f"public  (ISTOTA_SMS_TELNYX_PUBLIC_KEY):  {public_b64}")
        return 0

    key = _resolve_key(args)
    if args.command == "inbound":
        payload = inbound_payload(
            from_number=args.from_number,
            to_number=args.to_number,
            profile_id=args.profile_id,
            text=args.text,
            message_id=args.message_id,
            event_id=args.event_id,
            media=[{"url": url} for url in args.media_url],
        )
    else:
        payload = delivery_payload(
            from_number=args.from_number,
            to_number=args.to_number,
            profile_id=args.profile_id,
            status=args.status,
            message_id=args.message_id,
            event_id=args.event_id,
            parts=args.parts,
        )

    body, headers = sign_payload(payload, key, timestamp=args.timestamp)
    if args.print_only:
        print(json.dumps(headers, indent=2))
        print(body.decode())
        return 0

    status, response = post(args.url, body, headers)
    print(f"http={status}")
    if response:
        print(response.decode(errors="replace")[:2000])
    # 204 is the adapter's own success for an event it acted on; 200 covers a
    # receiver that answers differently. Anything else is the interesting case.
    return 0 if status in (200, 204) else 1


if __name__ == "__main__":
    sys.exit(main())
