"""The PyWa boundary.

The one module in the package allowed to import PyWa, and the one allowed to
hold a PyWa object. Everything above it works in the local records of
`_types.py`, for the reason that module's docstring gives: a PyWa update
carries the raw payload, and the raw payload carries the body, the phone number
and the BSUID.

What is deliberately **not** here, and must not arrive later: a registered
callback address, an endpoint path, a web-framework argument, a handler module,
a listener, or anything else that would let PyWa own the HTTP surface. Istota
already has
FastAPI and Uvicorn, and PyWa's handler loop catches a callback's exception and
acknowledges the webhook anyway — which is the opposite of what a failed
database transaction must do here. `tests/test_whatsapp_client.py` reads this
module's own text and fails on any of those names.

Stage 3 adds the async send methods beside the validator. The signature helper
comes first because it is what makes the endpoint safe to expose at all.
"""

from __future__ import annotations

from pywa import utils as pywa_utils

SIGNATURE_HEADER = pywa_utils.HUB_SIG
"""``X-Hub-Signature-256``, read from PyWa rather than spelled again here."""


def verify_signature(app_secret: str, raw_body: bytes, signature: str) -> bool:
    """Whether ``signature`` is Meta's HMAC over exactly these bytes.

    Three guards in front of PyWa's validator, and the first is the one that
    matters. ``webhook_updates_validator`` takes the secret as a plain string
    and computes an HMAC under whatever it is given — including ``b""``. So a
    deployment with no ``app_secret`` configured would not fail to validate;
    it would validate against a key every reader of this repository knows, and
    accept a payload any caller could sign. An absent secret is refused here,
    and the route refuses to serve at all besides.

    The second is the ``sha256=`` prefix, which PyWa strips with
    ``removeprefix`` and therefore treats as optional — so a bare hex digest
    validates there. Meta has never sent one, and accepting a format the sender
    does not produce only widens what an attacker may submit.

    The third is narrower: ``hmac.compare_digest`` raises ``TypeError`` on a
    non-ASCII ``str``, and this argument is a header value a caller chose. A
    raise out of an authentication check is a 500 where a 403 belongs.

    Bytes are compared, never text: the caller passes the exact request body,
    because a decode-and-re-encode round trip is a different byte string for
    any payload that is not already canonical UTF-8.
    """
    if not app_secret or not signature.startswith("sha256="):
        return False
    try:
        return bool(
            pywa_utils.webhook_updates_validator(app_secret, raw_body, signature)
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        return False


__all__ = ["SIGNATURE_HEADER", "verify_signature"]
