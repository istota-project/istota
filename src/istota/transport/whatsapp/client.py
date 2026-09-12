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

`send` is the other half. It classifies every way a Cloud API call can end
into exactly two: **definite**, meaning Meta processed the request and refused
it, and **ambiguous**, meaning the request may have been accepted and there is
no application idempotency token that could settle it. The ledger reads that
one bit and never resends either — the distinction is what an operator is told
and nothing more, which is why erring towards ambiguous is safe and erring
towards definite is not.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from pywa import utils as pywa_utils

from ._types import WhatsAppSendFailure, WhatsAppSendRequest, WhatsAppSendResult

if TYPE_CHECKING:
    from ...config import Config
    from ._types import WhatsAppSendOutcome

logger = logging.getLogger(__name__)

#: What a failure may say out loud. Meta's own prose, PyWa's exception text and
#: an httpx repr all carry the request URL — which carries the access token's
#: path segment and the recipient — so a failure reason is chosen from this
#: table by classification and never built from an exception.
_AMBIGUOUS_REASON = "delivery outcome unknown"
_REJECTED_REASON = "meta refused the message"
_UNREADABLE_REASON = "meta returned an unreadable response"
_TEMPLATE_UNSUPPORTED_REASON = "template sends are not available"

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


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _error_code(exc: object) -> str | None:
    """Meta's documented numeric code, as a string, or ``None``.

    Only the number. `WhatsAppError` also carries `message`, `details`,
    `user_msg` and the whole raw error object, every one of which is provider
    prose that the ledger, the task log and an operator alert must not hold.
    """
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, int) else None


def _classify(exc: Exception) -> WhatsAppSendFailure:
    """A PyWa API error as a definite refusal or an ambiguous outcome.

    **4xx is definite and everything else is not.** A client-error status means
    Meta read the request, applied a documented rule and refused it, so nothing
    was queued: an expired token, a recipient outside the test number's
    allowlist, a closed window, a paused template. A 5xx is Meta failing
    *while* handling a request it may already have accepted, and an error with
    no HTTP status at all is one PyWa built without a response to read — both
    are the ambiguous case.

    `is_transient` is deliberately not consulted. It answers whether *retrying
    later* might work, which is a different question from whether this attempt
    was applied, and reading it as ambiguity would put a rate-limited send —
    which certainly did not reach anybody — into the state that means "may have
    been delivered".
    """
    try:
        status = exc.status_code  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive; PyWa builds this lazily
        status = None
    definite = isinstance(status, int) and 400 <= status < 500
    return WhatsAppSendFailure(
        definite=definite,
        error_code=_error_code(exc),
        safe_reason=_REJECTED_REASON if definite else _AMBIGUOUS_REASON,
    )


class WhatsAppClient:
    """One configured Cloud API sender, and the only holder of a PyWa object.

    Built per delivery by :func:`make_client` and closed by its caller. A
    process-wide singleton was rejected: the surface is capped at a few hundred
    messages a month, an `httpx.AsyncClient` is bound to the loop it was
    created on, and a shared one would have to be torn down from whichever of
    the scheduler's threads happened to be last — for a connection pool that
    would be cold on almost every use anyway.
    """

    def __init__(self, config: "Config", *, session: httpx.AsyncClient | None = None):
        whatsapp = config.whatsapp
        self._phone_number_id = whatsapp.phone_number_id
        self._owns_session = session is None
        self._session = session or httpx.AsyncClient(
            timeout=httpx.Timeout(float(whatsapp.request_timeout_seconds)),
        )
        from pywa_async import WhatsApp  # noqa: PLC0415

        kwargs: dict[str, object] = {
            "phone_id": whatsapp.phone_number_id,
            "token": whatsapp.access_token,
            "waba_id": whatsapp.waba_id,
            "app_secret": whatsapp.app_secret,
            "session": self._session,
        }
        if whatsapp.graph_api_version:
            # `v25` and `v25.0` both configured; PyWa wants the bare number.
            kwargs["api_version"] = whatsapp.graph_api_version.lstrip("vV")
        # No `server`, `webhook_endpoint`, `callback_url` or `verify_token`:
        # istota owns the HTTP surface and PyWa is never handed any part of it.
        self._client = WhatsApp(**kwargs)

    async def send(self, request: WhatsAppSendRequest) -> "WhatsAppSendOutcome":
        """One Cloud API call. Never raises, and never leaks provider text."""
        from pywa import errors as pywa_errors  # noqa: PLC0415

        if request.kind == "template":
            # Stage 4 owns the approved-utility-template path. A **definite**
            # refusal rather than the ambiguous default, because nothing was
            # sent and the row must say so: `unknown` means "may have reached
            # Meta", and spending that state on a code path that never opened a
            # socket would make the one state an operator cannot resolve mean
            # two different things.
            return WhatsAppSendFailure(True, None, _TEMPLATE_UNSUPPORTED_REASON)
        try:
            sent = await self._call(request)
        except pywa_errors.WhatsAppError as exc:
            failure = _classify(exc)
            logger.info(
                "whatsapp.outbound.refused definite=%s error_code=%s",
                failure.definite, failure.error_code,
            )
            return failure
        except Exception:
            # Timeout, connection loss, a PyWa parse error, anything at all.
            # The request may have reached Meta, so this is the ambiguous case
            # and the ledger records `unknown`. `exc_info` is deliberately off:
            # a traceback here prints the request URL, which carries the
            # recipient and the token's path segment.
            logger.warning("whatsapp.outbound.unknown reason=send_raised")
            return WhatsAppSendFailure(False, None, _AMBIGUOUS_REASON)
        message_id = getattr(sent, "id", None)
        if not isinstance(message_id, str) or not message_id:
            # A 200 whose body we cannot read is not a success: Meta may well
            # have queued the message, so it is ambiguous rather than failed.
            logger.warning("whatsapp.outbound.unknown reason=unreadable_response")
            return WhatsAppSendFailure(False, None, _UNREADABLE_REASON)
        return WhatsAppSendResult(message_id)

    async def _call(self, request: WhatsAppSendRequest):
        return await self._client.send_message(
            to=request.to,
            text=request.text,
            buttons=_buttons(request.buttons),
            reply_to_message_id=request.reply_to_message_id,
            sender=self._phone_number_id,
        )

    async def aclose(self) -> None:
        if self._owns_session:
            try:
                await self._session.aclose()
            except Exception:  # pragma: no cover - closing must not raise
                logger.debug("whatsapp.client.close_failed", exc_info=True)


def _buttons(pairs: tuple[tuple[str, str], ...]):
    """``(callback_data, title)`` pairs as PyWa buttons, or ``None``.

    The one place a PyWa type is built out of istota data. `None` rather than
    an empty list, because PyWa branches on the argument being falsy to decide
    whether the message is interactive at all, and an empty list would be an
    interactive message with no buttons.
    """
    if not pairs:
        return None
    from pywa.types import Button  # noqa: PLC0415

    return [Button(title=title, callback_data=data) for data, title in pairs]


def make_client(config: "Config") -> WhatsAppClient:
    """The seam every send goes through, and the one tests replace."""
    return WhatsAppClient(config)


__all__ = [
    "SIGNATURE_HEADER",
    "WhatsAppClient",
    "make_client",
    "verify_signature",
]
