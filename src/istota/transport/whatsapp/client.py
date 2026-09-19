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

`fetch_media` is the inbound direction and has no such ambiguity to model: a
fetch that did not complete costs the image and nothing else, so every failure
is one fixed reason out of the table below. What it does own is the *per-file
byte cap*, twice — against the size Meta declares before a byte moves, and
again against the bytes that actually arrive, because a declared size is a
claim. Past this call the daemon only ever sees a file that already exists and
the cap has no enforcement point left.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
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
_TEMPLATE_UNCONFIGURED_REASON = "no approved template name and language configured"
_TEMPLATE_LANGUAGE_REASON = "the configured template language is not a Meta code"

SIGNATURE_HEADER = pywa_utils.HUB_SIG
"""``X-Hub-Signature-256``, read from PyWa rather than spelled again here."""

#: What a media fetch may say out loud, on the same rule the send reasons
#: follow: Meta's prose, PyWa's exception text and an httpx repr all carry the
#: request URL, and a *media* URL carries the recipient's identifiers and the
#: access token's path segment besides. Every failure below is one of these
#: three, chosen by classification and never built from an exception.
MEDIA_FETCH_FAILED_REASON = "the image could not be downloaded from WhatsApp"
MEDIA_OVER_CAP_REASON = "the image was larger than this surface accepts"
MEDIA_WRITE_FAILED_REASON = "the image could not be written to disk"

#: The Meta media id, held to one ordinary URL path segment.
#:
#: `is_staged_name`'s rule applied to the other value off the wire that becomes
#: a path: PyWa interpolates this id into ``/{media_id}`` on a session whose
#: headers carry the access token, so a ``/``, a ``?`` or a ``#`` in it steers
#: a credentialed request somewhere the caller did not name. Meta signs the
#: payload the id arrives in, which makes this defence in depth rather than the
#: boundary — and the join is still where containment is decided.
_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:=-]{0,254}")

#: A declared media type, bounded for the reason `baileys_protocol` bounds its
#: own: it is a string that reaches a log line, so a newline or an ANSI escape
#: in it forges one there. Nothing branches on the value — the sniff in
#: `media.stage_to_attachment` is what decides what the bytes are.
_MAX_DECLARED_MIME_CHARS = 128

#: How much of Meta's answer is read at a time. 64 KiB is PyWa's own default
#: and the size its generator yields.
_MEDIA_CHUNK_BYTES = 64 * 1024


class WhatsAppMediaError(Exception):
    """A media fetch that did not complete, carrying one of the fixed reasons.

    An exception rather than a return value because there is no partial
    success to report: the caller has opened a descriptor and either gets the
    bytes or unlinks the file. ``reason`` is what reaches the event record, so
    it is always one of the module constants above and never text from a
    provider.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
        # The import comes first, before the session exists. It is the failure
        # a deployment actually meets — a missing or renamed PyWa — and a
        # session created before it has no owner to `aclose` it and no sync
        # close to call. What is left after this ordering is a `WhatsApp(...)`
        # that raises on its own arguments, which leaves behind a client that
        # has issued no request and therefore holds no connection and no task:
        # garbage collection reclaims it in silence. Config is validated at
        # load, so nothing shipped reaches that branch.
        from pywa_async import WhatsApp  # noqa: PLC0415

        cloud = config.whatsapp.cloud
        self._phone_number_id = cloud.phone_number_id
        self._owns_session = session is None
        self._session = session or httpx.AsyncClient(
            timeout=httpx.Timeout(float(cloud.request_timeout_seconds)),
        )
        kwargs: dict[str, object] = {
            "phone_id": cloud.phone_number_id,
            "token": cloud.access_token,
            "waba_id": cloud.waba_id,
            "app_secret": cloud.app_secret,
            "session": self._session,
        }
        if cloud.graph_api_version:
            # `v25` and `v25.0` both configured; PyWa wants the bare number.
            kwargs["api_version"] = cloud.graph_api_version.lstrip("vV")
        # No `server`, `webhook_endpoint`, `callback_url` or `verify_token`:
        # istota owns the HTTP surface and PyWa is never handed any part of it.
        self._client = WhatsApp(**kwargs)

    async def send(self, request: WhatsAppSendRequest) -> "WhatsAppSendOutcome":
        """One Cloud API call. Never raises, and never leaks provider text."""
        from pywa import errors as pywa_errors  # noqa: PLC0415

        template_language = None
        if request.kind == "template":
            if not request.template_name or not request.template_language:
                return WhatsAppSendFailure(True, None, _TEMPLATE_UNCONFIGURED_REASON)
            template_language = _template_language(request.template_language)
            if template_language is None:
                # **Definite**, and this is the case that makes resolving the
                # code up here worth a branch. PyWa's `TemplateLanguage` has no
                # `UNKNOWN` member, so an unrecognised value raises out of its
                # own `_missing_` hook — which the generic handler below would
                # read as *ambiguous*, i.e. "may have reached Meta". Nothing
                # opened a socket, and `unknown` is the one state an operator
                # can never resolve.
                return WhatsAppSendFailure(True, None, _TEMPLATE_LANGUAGE_REASON)
        try:
            sent = await self._call(request, template_language)
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

    async def fetch_media(
        self, media_id: str, dest_fd: int, *, max_bytes: int
    ) -> tuple[str, int]:
        """Stream one inbound file onto a descriptor the caller opened.

        Returns ``(declared media type, bytes written)`` and raises
        `WhatsAppMediaError` for everything else. The caller owns the
        descriptor and owns the file behind it on every path: this method does
        not close it, does not name it and does not unlink it.

        **Never `download_media`.** PyWa's own downloader derives the filename
        from ``Content-Disposition`` or from a hash of the URL — both
        server-chosen — and the daemon names its own files, for the reason
        `media.staged_name` gives. `get_media_url` plus `stream_media` are the
        only two Graph calls on this path, and
        `tests/test_whatsapp_cloud_media.py` reads this module's text to hold
        that.

        **The cap is enforced twice and the second time is the one that
        matters.** `MediaURL.file_size` is what Meta says before a byte moves,
        so refusing there costs one round trip instead of a whole download; but
        it is Meta reporting what an uploader told it, so the bytes that
        actually arrive are counted too, and the chunk that would take the
        total past the cap is never written.

        **Nothing here logs the URL, the error body or the filename.** A media
        URL carries the recipient's identifiers and the token's path segment,
        which is why the send path already turns `exc_info` off on its
        ambiguous branch; the same applies to every branch of this one.
        """
        if not _MEDIA_ID_RE.fullmatch(media_id or ""):
            logger.warning(
                "whatsapp.media.fetch_refused reason=media_id: the id is not "
                "one ordinary path segment",
            )
            raise WhatsAppMediaError(MEDIA_FETCH_FAILED_REASON)
        try:
            located = await self._client.get_media_url(media_id)
        except Exception:
            # No `exc_info` and no id: a PyWa `WhatsAppError` carries Meta's
            # prose and the response, and an httpx repr carries the Graph URL.
            logger.warning("whatsapp.media.fetch_failed reason=media_url")
            raise WhatsAppMediaError(MEDIA_FETCH_FAILED_REASON) from None
        declared = getattr(located, "mime_type", "")
        declared = (
            declared[:_MAX_DECLARED_MIME_CHARS] if isinstance(declared, str) else ""
        )
        size = _as_byte_count(getattr(located, "file_size", None))
        if size is not None and size > max_bytes:
            logger.warning(
                "whatsapp.media.refused reason=declared_size bytes=%d cap=%d",
                size, max_bytes,
            )
            raise WhatsAppMediaError(MEDIA_OVER_CAP_REASON)
        url = getattr(located, "url", None)
        if not isinstance(url, str) or not url:
            logger.warning("whatsapp.media.fetch_failed reason=no_media_url")
            raise WhatsAppMediaError(MEDIA_FETCH_FAILED_REASON)

        written = 0
        # `aclosing`, not a bare `async for`: breaking out of the loop at the
        # cap abandons PyWa's async generator with its httpx stream context
        # still open, which a mock transport never shows and a production
        # connection pool does.
        stream = self._client.stream_media(url, chunk_size=_MEDIA_CHUNK_BYTES)
        try:
            async with contextlib.aclosing(stream):
                async for chunk in stream:
                    if written + len(chunk) > max_bytes:
                        logger.warning(
                            "whatsapp.media.refused reason=stream_cap "
                            "bytes=%d cap=%d",
                            written + len(chunk), max_bytes,
                        )
                        raise WhatsAppMediaError(MEDIA_OVER_CAP_REASON)
                    _write_all(dest_fd, chunk)
                    written += len(chunk)
        except WhatsAppMediaError:
            raise
        except OSError:
            logger.warning("whatsapp.media.fetch_failed reason=write")
            raise WhatsAppMediaError(MEDIA_WRITE_FAILED_REASON) from None
        except Exception:
            logger.warning("whatsapp.media.fetch_failed reason=stream")
            raise WhatsAppMediaError(MEDIA_FETCH_FAILED_REASON) from None
        logger.info(
            "whatsapp.media.fetched declared=%s bytes=%d", declared, written,
        )
        return declared, written

    async def _call(self, request: WhatsAppSendRequest, template_language=None):
        if request.kind == "template":
            from pywa.types.templates import BodyText  # noqa: PLC0415

            # Exactly one positional body parameter, which is the shape the
            # spec requires of the operator's approved template: fixed wording
            # around one `{{1}}`. A template whose body takes none, or takes
            # more, is refused by Meta with a 4xx — definite, and correctly so:
            # it is a mismatch between the deployment's config and the account
            # state Meta owns, and only the operator can settle it.
            return await self._client.send_template(
                to=request.to,
                name=request.template_name,
                language=template_language,
                params=[BodyText.params(request.text)],
                sender=self._phone_number_id,
            )
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


def _as_byte_count(value: object) -> int | None:
    """Meta's ``file_size`` as a non-negative int, or ``None``.

    The field is documented as a number and has arrived as a string from other
    Graph endpoints, so both are read. Anything else is `None`, which means
    "Meta said nothing about the size" — the in-stream cap is what covers that
    case, and reading an unparseable value as zero would silently retire the
    cheaper gate.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _write_all(fd: int, data: bytes) -> None:
    """`os.write` until the whole chunk is on disk.

    A short write is legal and is not an error, so the obvious single call
    silently truncates a file this surface is about to hand to Pillow.
    """
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _template_language(code: str):
    """The configured language code as PyWa's enum member, or ``None``.

    `load_config` bounds the *shape* of the code and cannot bound the *set*:
    the set is whatever the installed PyWa release knows, and it moves with
    the dependency. So the lookup happens before the call and its failure is
    reported as a local misconfiguration rather than as a send that may have
    happened.

    Every exception is caught, and broadly on purpose: an unknown member here
    raises `TypeError` out of PyWa's `_missing_` hook (there is no `UNKNOWN`
    member for it to fall back to) rather than the `ValueError` an enum lookup
    normally raises, and the hook emits a `PywaUnknownEnumMemberWarning` on the
    way past that a strict session can promote to an error of a third kind.
    Suppressing that warning is deliberately *not* done: `warnings.catch_warnings`
    mutates process-global state and is documented as unsafe from a thread,
    this runs on the event loop beside every other send, and the outcome is the
    same either way — the lookup fails and the caller refuses the send.
    """
    from pywa.types.templates import TemplateLanguage  # noqa: PLC0415

    try:
        return TemplateLanguage(code)
    except Exception:
        # No `exc_info` and no code in the message: the value is operator
        # config, and the caller's `safe_reason` names the field instead.
        logger.warning("whatsapp.outbound.failed reason=template_language")
        return None


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
    "MEDIA_FETCH_FAILED_REASON",
    "MEDIA_OVER_CAP_REASON",
    "MEDIA_WRITE_FAILED_REASON",
    "SIGNATURE_HEADER",
    "WhatsAppClient",
    "WhatsAppMediaError",
    "make_client",
    "verify_signature",
]
