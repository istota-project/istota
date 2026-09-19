"""Meta's hosted Cloud API, bound to the provider seam.

**What this module is, and what it deliberately is not.** The spec's stage line
says "move the Meta-specifics into `providers/whatsapp_cloud.py`", written from
a survey that lists `transport/whatsapp/` as `__init__.py`, `_types.py`,
`outbound.py` and `webhook.py`. The tree has a fifth module the survey missed:
`client.py`, whose own docstring calls it "the PyWa boundary … the one module
in the package allowed to import PyWa". So the separation the stage asks for
already exists, under two filenames rather than one — `client.py` is the Meta
send boundary and `webhook.py` is the Meta callback boundary, both Cloud-only
(the spec's own Affected-files section keeps `webhook.py` as a file and calls
it "Cloud-only"). Relocating either one buys a large diff, no boundary that is
not already there, and an import-path change in every test that names them,
which is the one thing the stage's equivalence instrument asks not to spend.

What genuinely did not exist is the binding: common code reached Meta by
importing those modules directly. This module is that binding, and the property
it buys is checkable — `outbound.py`, which is the common send path, now imports
no Meta module at all and reaches the provider only through the adapter record.
`tests/test_whatsapp_providers.py` holds that as a drift guard.

The webhook fields are the other half and are a **declaration** at this stage
rather than a caller. `webhook_receiver`'s two routes still call `webhook.py`
directly, and deliberately: routing them through `parse_webhook` here would put
the registry's own "is this adapter fully configured" answer in front of a
request, and a Cloud deployment missing `verify_token` — which the ISSUE-058
rule says must load, be reported by doctor and be refused at *use* — would
start answering 404 where it answers 403 or 503 today. Mounting is what became
provider-aware (`config.whatsapp_webhooks_enabled`), and these two fields are
what Baileys sets to `None` when it arrives with no HTTP callback at all. They
are the same code path the route takes, so the two cannot drift, and both are
driven by tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .. import message_fingerprint
from .._types import (
    InboundWhatsAppEvent,
    WhatsAppEvent,
    WhatsAppInboundMedia,
    WhatsAppSendFailure,
    WhatsAppSendOutcome,
    WhatsAppSendRequest,
    WhatsAppWebhookRequest,
    WhatsAppWebhookResult,
)
from ..outbound import WHATSAPP_INTERACTIVE_BODY_LIMIT, WHATSAPP_TEXT_LIMIT
from ._types import WhatsAppProviderAdapter, WhatsAppProviderCaps

if TYPE_CHECKING:
    from ....config import Config

logger = logging.getLogger(__name__)

CLOUD_CAPS = WhatsAppProviderCaps(
    metered=True,
    has_service_window=True,
    supports_templates=True,
    delivery_receipts=True,
    address_field="send_id",
    service_body_limit=WHATSAPP_TEXT_LIMIT,
    interactive_body_limit=WHATSAPP_INTERACTIVE_BODY_LIMIT,
)
"""Every constraint `.claude/rules/whatsapp.md` records, declared as this
provider's own.

All four behavioural flags are true of Meta's hosted API and none of them is
true of WhatsApp: the 24-hour service window, approved templates, the monthly
attempt cap and the billing circuit are the Cloud API's rules. Declaring them
here is what lets `outbound.py`'s gate order stay provider-agnostic while
behaving, for this adapter, exactly as the hard-coded version did.

The three added at Stage 6 restate what the common path used to hard-code, and
each is Meta's number rather than WhatsApp's: `send_id` is the opaque
destination Meta itself hands back, and the two budgets are the plain-text and
interactive caps `outbound` declares. Imported from there rather than spelled
again — a second copy of 1024 would be a second thing to get wrong, and this
module is the Cloud boundary, so importing Cloud constants from the common
module is the direction that already holds.
"""

#: A client that could not be built is a message that provably never left, so
#: the failure is **definite** and the ledger records `failed`. `unknown` is
#: the one state an operator can never resolve and is not spent on a case whose
#: answer is known. The reason is local text from a fixed table, never built
#: from the exception — `client.py`'s rule, for its reason: Meta's prose, PyWa's
#: exception text and an httpx repr all carry the request URL.
_CLIENT_UNAVAILABLE_REASON = "the whatsapp cloud client could not be built"


def build_adapter(config: "Config") -> WhatsAppProviderAdapter:
    """The Cloud adapter. No I/O, no PyWa import, no session.

    `make_registry` may not do I/O on construction and this registry follows
    the same rule, so nothing here builds a client: `send` constructs one per
    call and closes it, which is the lifetime `client.py` already argues for —
    the surface is capped at a few hundred messages a month and an
    `httpx.AsyncClient` is bound to the loop it was made on. That rule is what
    lets `outbound.active_adapter` build a registry per send without the
    caching the SMS side needs, and it is pinned rather than stated.

    **No client-injection parameter**, deliberately. One existed and was
    removed: `deliver_whatsapp` injects by replacing the resolved adapter's
    `send`, so a second seam here would be a second mechanism for one job,
    exercised only by tests and diverging from the path production takes. A
    test that wants a double replaces `send` the way production does.
    """
    def parse(request: WhatsAppWebhookRequest) -> WhatsAppWebhookResult:
        from ..webhook import parse_webhook  # noqa: PLC0415

        # `parse_webhook` is the whole authenticate-then-read chain — size,
        # content type, configured secret, HMAC over the exact raw bytes,
        # decode, normalize — and it stays one call for that reason. Splitting
        # the signature check out as a precondition the caller has to remember
        # is how a parser gets fed to an unauthenticated caller. `verify` below
        # is the same predicate exposed for the seam, never a step this skips.
        events = parse_webhook(config, request.raw_body, request.headers)
        return WhatsAppWebhookResult(
            events=tuple(events),
            response_status=200,
            response_content_type=None,
            response_body=b"",
        )

    def verify(request: WhatsAppWebhookRequest) -> bool:
        from ....http_headers import header_value  # noqa: PLC0415
        from ..client import SIGNATURE_HEADER, verify_signature  # noqa: PLC0415

        return verify_signature(
            config.whatsapp.cloud.app_secret,
            request.raw_body,
            header_value(request.headers, SIGNATURE_HEADER),
        )

    async def send(request: WhatsAppSendRequest) -> WhatsAppSendOutcome:
        return await _send(config, request)

    return WhatsAppProviderAdapter(
        name="whatsapp_cloud",
        caps=CLOUD_CAPS,
        parse_webhook=parse,
        send=send,
        verify_signature=verify,
    )


async def _send(config: "Config", request: WhatsAppSendRequest):
    """One Cloud API call, with the client's whole lifetime inside it.

    The construction is guarded separately from the call, and the split is the
    same line `_send_claimed` draws one level up: everything before the first
    byte is provably a message that never left. `WhatsAppClient.__init__`
    imports PyWa before it makes a session, so a missing or renamed dependency
    raises here — which used to reach `_send_claimed`'s pre-send arm and settle
    `failed`. Returning a *definite* failure keeps that ledger outcome exactly.

    Nothing between the construction and the first byte can have sent
    anything, which is what makes `definite` honest rather than optimistic:
    `client.py` records that `WhatsApp(**kwargs)` issues no request, and the
    `httpx.AsyncClient` it wraps opens no socket until one is made.
    """
    try:
        # Inside the guard, not above it. `client.py` is the module that
        # imports PyWa, so a missing or renamed dependency raises at *this*
        # line as readily as inside `make_client` — and an import left outside
        # would escape to `_send_claimed`'s backstop and settle `unknown`,
        # which is precisely the outcome this function exists to keep as
        # `failed`.
        from ..client import make_client  # noqa: PLC0415

        client = make_client(config)
    except Exception:
        # `exc_info` kept: the cause is a dependency or configuration fault an
        # operator has to see, and a traceback prints frames rather than the
        # message body.
        logger.warning(
            "whatsapp.outbound.failed reason=client_unavailable", exc_info=True,
        )
        return WhatsAppSendFailure(True, None, _CLIENT_UNAVAILABLE_REASON)
    try:
        return await client.send(request)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Inbound media: the fetch, and the two common calls around it
# ---------------------------------------------------------------------------


def _is_pending(event: object) -> bool:
    """Whether this event names bytes the daemon still has to fetch.

    Four conditions, and each excludes a record that has already been decided:
    a delivery status is not an inbound message at all, a message that carried
    nothing has no record, a record carrying an `error` was refused upstream
    (a `group_id` message never gets one in the first place — `_inbound_event`
    resets the type above the record, so a group image is refused before the
    fetch as well as before the transaction), and one already carrying a
    `staged_path` has been staged by somebody.
    """
    if not isinstance(event, InboundWhatsAppEvent):
        return False
    media = event.media
    return (
        media is not None
        and media.error is None
        and bool(media.remote_id)
        and not media.staged_path
    )


def _media_failed(reason: str) -> WhatsAppInboundMedia:
    """A record naming a failure and no file.

    The same four fields `baileys_bridge._media_failed` sets, and deliberately
    a second constructor rather than a shared one: what the record *means* —
    `error` set, `staged_path` empty — is `WhatsAppInboundMedia`'s own
    documented contract, so the thing that would drift is already written down
    where both adapters read it, and `tests/test_whatsapp_cloud_media.py`
    pins the two against each other. Collapsing them would put a constructor
    for a `_types` record in the common module on behalf of two callers that
    each build every other record inline.
    """
    return WhatsAppInboundMedia(
        staged_path="", mime_type="", byte_count=0, attached_for_user="",
        error=reason,
    )


async def stage_cloud_media(
    config: "Config", events: Sequence[WhatsAppEvent]
) -> tuple[WhatsAppEvent, ...]:
    """Put each message's file in its user's inbox, before any lock is taken.

    The Cloud half of the ordering this whole area exists for, and the route
    calls it between `parse_webhook` and `handle_whatsapp_batch`. Meta's media
    endpoint needs the access token, so unlike Baileys the daemon is the
    fetcher here — and a Graph round trip under `BEGIN IMMEDIATE` waits out the
    30-second busy timeout against the lock the caller holds, on a router that
    under `istota serve` sits on the web app's event loop. So the bytes land on
    disk first and the transaction sees a path.

    Three calls per pending event, in the order the spec fixes:

    1. **Pre-check.** Unlocked, read-only, allowed to be stale, and *before*
       the fetch rather than after it — this is the one adapter where the check
       can keep a stranger's media off the wire entirely, which is what bounds
       the disk-fill vector a published business number carries. `None` means
       nothing is fetched at all: an unknown sender, a message id already
       claimed (which is what closes Meta's redelivery loop), a sender who has
       opted out, or a read that could not be answered.
    2. **Fetch.** `client.fetch_media` owns the per-file cap on both sides of
       the wire; this function owns the *staging ceiling*, asked before the
       round trip rather than after it.
    3. **Consume.** `media.stage_to_attachment`, which sniffs the bytes, names
       the inbox copy from its own answer and unlinks the staged file. The
       sniff is asked first, for the reason `baileys_bridge.stage_inbound_media`
       step 2 gives: a file that is not a decodable image is not an image at
       all and takes the `unsupported_type` reply the surface already had,
       while one that could not be placed is istota's own failure and says so.

    **Never raises**, which is the contract the route rests on: `_stage_one`
    carries a catch-all and everything outside it is a list rebuild. A media
    failure must cost the media and never the batch, because any exception
    reaching the route answers 503 and Meta redelivers.

    **A batch carrying no media touches nothing** — no directory is made and no
    sweep is run. Delivery-status callbacks are the common case on this
    surface, and the Baileys side draws the same line ("an event carrying no
    media never reaches here").

    **Residual: a batch that does not commit is re-staged, and each attempt
    leaves its own inbox copy.** `precheck`'s claimed-id read closes the
    redelivery loop for a message whose *acknowledgement* was lost, and its own
    docstring records that it cannot close the rollback case — a batch that
    raises rolls its claim back, so Meta's retry finds nothing written and is a
    new message as far as this read can tell. Here that means a second fetch
    and a second `stage_to_attachment`, and the inbox name carries
    `staged_name`'s random half, so the copies accumulate rather than
    colliding. Bounded by Meta's retry schedule rather than by anything here,
    and reachable two ways: a deterministic in-transaction failure (the route's
    own comment names `create_task`'s user-id guard), and a batch whose fetch
    and upload outlast Meta's callback tolerance, where the retry's pre-check
    legitimately passes before the first commit. Both are visible —
    `_report_stranded_media` names the second — and neither is silent data
    loss: the copies are in the sender's own inbox. Closing it means a stable
    per-message inbox name, which is a change to a Stage 1 rule that the
    Baileys adapter shares and whose random half exists to keep two media parts
    of one message apart.
    """
    from .. import media as media_rules  # noqa: PLC0415

    pending = [index for index, event in enumerate(events) if _is_pending(event)]
    if not pending:
        return tuple(events)

    staged = list(events)
    try:
        # Threaded like the pre-check and the copy below it, on the same
        # reason: this runs on the loop the receiver and the web UI share, and
        # a filesystem call is a filesystem call whether or not it is usually
        # a fast one.
        media_dir = await asyncio.to_thread(
            media_rules.ensure_media_dir, media_rules.default_media_dir(config),
        )
    except Exception:
        # The directory is the whole staging story, so nothing can be fetched
        # without it — but the messages still arrive, and their captions are
        # messages. `exc_info` is kept: a staging directory that cannot be made
        # private is an operator fault rather than anything off the wire.
        logger.warning(
            "whatsapp.media.staging_unavailable: the staging directory could "
            "not be prepared", exc_info=True,
        )
        for index in pending:
            staged[index] = replace(
                events[index], media=_media_failed(media_rules.MEDIA_NOT_PLACED),
            )
        return tuple(staged)

    try:
        for index in pending:
            staged[index] = await _stage_one(config, media_dir, events[index])
    finally:
        # After the consume, never before it — `_prune_parked_statuses`'
        # arrangement, and the reason `stage_inbound_media` moved its own sweep
        # into a `finally`: a sweep in front of the fetch puts the 600-second
        # window ahead of the file this call is about. Threaded, because it is
        # a directory scan plus N unlinks and `has_staging_room` already asks
        # for the same function from a thread.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(media_rules.prune_media_dir, media_dir)
    return tuple(staged)


async def _stage_one(
    config: "Config", media_dir, event: InboundWhatsAppEvent
) -> InboundWhatsAppEvent:
    """One message's file, from Meta's media id to a path in an inbox.

    Never raises. The two blocking calls — the read-only pre-check and the
    WebDAV copy — go to a thread, because this runs on the event loop the
    receiver and (under `istota serve`) the web UI share, and the copy is the
    long pole.
    """
    from .... import db, sqlite_util  # noqa: PLC0415
    from .. import media as media_rules  # noqa: PLC0415
    from ..client import WhatsAppMediaError, make_client  # noqa: PLC0415

    incoming = event.media
    if incoming is None:  # pragma: no cover - `_is_pending` is the caller's gate
        return event
    staged_path = None
    fd = None
    try:
        user_id = await asyncio.to_thread(
            media_rules.precheck,
            lambda: sqlite_util.connect_read_only(config.db_path),
            identity=event.from_user,
            message_id=event.message_id,
            provider=db.WHATSAPP_LEGACY_PROVIDER,
        )
        if user_id is None:
            logger.info(
                "whatsapp.media.unattributed message=%s: the pre-check named "
                "no user, so nothing is fetched",
                message_fingerprint(event.message_id),
            )
            return replace(
                event, media=_media_failed(media_rules.MEDIA_UNATTRIBUTED),
            )
        # Asked with the *cap* rather than with Meta's declared size, which is
        # one round trip away and would be paid for a fetch this may refuse.
        # Conservative in the direction that matters: the steady state is an
        # empty directory, so this only bites when something has filled it,
        # which is the case the ceiling exists for.
        if not await asyncio.to_thread(
            media_rules.has_staging_room, media_dir,
            incoming_bytes=media_rules.MAX_MEDIA_BYTES,
        ):
            return replace(
                event, media=_media_failed(media_rules.MEDIA_NOT_PLACED),
            )

        # `bin`, because the bytes have not been read yet and the only type in
        # hand is the one the sender chose. The staged suffix is advisory by
        # design — `stage_to_attachment` re-derives the inbox copy's from its
        # own sniff, which is what carries a HEIC past the pipeline's suffix
        # screen whatever it was staged as.
        name = media_rules.staged_name(event.message_id, "bin")
        fd = media_rules.open_staged_write(media_dir, name)
        staged_path = Path(media_dir) / name
        client = make_client(config)
        try:
            declared, written = await client.fetch_media(
                incoming.remote_id, fd, max_bytes=media_rules.MAX_MEDIA_BYTES,
            )
        finally:
            os.close(fd)
            fd = None
            await client.aclose()

        # No "can it be opened" question here, unlike the Baileys step: this
        # call wrote the file moments ago and holds the only name for it, so
        # `None` from the sniff means the bytes are not a decodable image
        # rather than that the file went missing.
        if media_rules.sniff_staged(staged_path) is None:
            media_rules.discard_staged(staged_path)
            return replace(event, media=None)
        attachment = await asyncio.to_thread(
            media_rules.stage_to_attachment, config, user_id, staged_path,
        )
        if attachment is None:
            return replace(
                event, media=_media_failed(media_rules.MEDIA_NOT_PLACED),
            )
        return replace(
            event,
            media=replace(
                incoming, staged_path=attachment, attached_for_user=user_id,
                mime_type=declared or incoming.mime_type, byte_count=written,
            ),
        )
    except WhatsAppMediaError as exc:
        # The fetcher's own refusal, already named by one of its fixed reasons.
        return replace(event, media=_media_failed(exc.reason))
    except Exception:
        # No `exc_info` and no path: a traceback here prints frames holding the
        # media URL, which carries the recipient and the token's path segment.
        logger.warning(
            "whatsapp.media.staging_failed message=%s",
            message_fingerprint(event.message_id),
        )
        return replace(event, media=_media_failed(media_rules.MEDIA_NOT_PLACED))
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if staged_path is not None:
            # The sweep is the backstop, not the mechanism: every staged file
            # istota consumes *and* every one it drops is unlinked when it is
            # decided, which is what keeps a failed fetch from occupying the
            # ceiling for ten minutes. A consumed one is already gone and
            # `discard_staged` tolerates that.
            media_rules.discard_staged(staged_path)


__all__ = ["CLOUD_CAPS", "build_adapter", "stage_cloud_media"]
