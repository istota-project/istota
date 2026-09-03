"""Nextcloud Talk surface.

This package is the home for everything Talk-specific that sits above the
low-level HTTP/OCS client (``istota.talk.TalkClient``):

- ``TalkTransport`` (here) — the bidirectional seam: outbound ``deliver`` /
  ``edit`` / ``resolve_target`` (the one place outside the CLI that constructs
  ``TalkClient``) plus the ``poll`` entry point.
- ``inbound`` — the inbound body (``poll_talk_conversations`` + the
  Talk-specific filtering / `!command` dispatch / confirmation handling and the
  module-global conversation/participant/DM caches).

``deliver`` replicates the previous ``post_result_to_talk`` body (split +
sequential post + group-chat reply-threading / @mention); ``edit`` replicates
``edit_talk_message``. The scheduler's ``post_result_to_talk`` /
``edit_talk_message`` are thin shims over these methods.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

from ...async_runtime import get_talk_client
from ...talk import split_message
from .._types import IncomingMessage, TransportCapabilities
from .inbound import get_dm_token, poll_talk_conversations

if TYPE_CHECKING:
    from ... import db
    from ...config import Config
    from .._types import DeliveryOptions

logger = logging.getLogger("istota.transport.talk")

__all__ = ["TalkTransport", "poll_talk_conversations", "get_dm_token"]

# How many times one part of a message is posted before delivery is given up
# on, and how long to wait between attempts. A judgement rather than a derived
# number (ISSUE-405): production saw around two isolated `ReadTimeout`s a day on
# an idle host whose Nextcloud answered `status.php` in 16 ms, so a second
# attempt a second later is very likely to land and a third is the margin. Each
# extra attempt costs a full POST timeout on a thread that is blocking either a
# task's start (the progress ack) or its completion (the result), which is why
# the number is small: a total outage answers `ConnectError` immediately and
# costs about 49 seconds over today, while a server that accepts connections and
# then times out three times running costs about 109 seconds.
_POST_ATTEMPTS = 3
_POST_BACKOFF_SECONDS = (1.0, 3.0)

# The ladder's own wall-clock bound, checked before each retry. The attempt
# count alone does not bound the wait, because each attempt can sit for the
# client's full 15s timeout — and the heaviest caller is the log-channel
# subscriber, which posts on *every* tool call for as long as its first post
# keeps failing. Against a slow-but-reachable Nextcloud that turns a per-call
# 15s into a per-call ladder, on a thread that blocks a scheduler worker, so
# the ladder gives up on elapsed time as well as on attempts.
_POST_DEADLINE_SECONDS = 45.0

# How far back the idempotency readback looks, and how long it may take. A
# message posted seconds ago is at the very end of the room's history, so a
# wider window costs bytes on a failure path and finds nothing a narrower one
# would have missed. The timeout is much tighter than `fetch_chat_history`'s
# own 30s default for the same reason the deadline above exists.
_READBACK_LIMIT = 25
_READBACK_TIMEOUT_SECONDS = 10.0


def _is_transient(exc: BaseException) -> bool:
    """Whether `exc` is worth another attempt.

    A 404 or a 403 is an *answer* — the conversation is gone, or this account
    may not write to it — and re-posting into it buys nothing while costing a
    worker thread the wait. So the retryable set is the transport-level
    failures plus 5xx, and everything else, including every other 4xx and any
    exception that is not httpx's at all, ends delivery on the first attempt.

    429 is deliberately absent. Retrying a rate limit correctly means honouring
    `Retry-After`, a fixed one-second backoff is the wrong answer to it, and
    Nextcloud has never answered one on this path.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return isinstance(status, int) and 500 <= status <= 599
    return isinstance(exc, (
        httpx.TimeoutException,     # connect / read / write / pool
        httpx.NetworkError,         # connect / read / write / close
        httpx.RemoteProtocolError,  # the server hung up mid-response
        httpx.ProxyError,
    ))


def _request_never_sent(exc: BaseException) -> bool:
    """Whether `exc` proves Nextcloud never saw the POST.

    Only the three that fail before a connection exists: nothing was
    transmitted, so nothing can have been stored, and a re-post cannot
    duplicate. Everything else leaves the question open and has to be settled
    by the readback — a `ReadTimeout` in particular means the request went out
    and the answer did not come back, which is exactly the case where Nextcloud
    may have written the message. A `WriteTimeout` is open for the same reason:
    part of the request was on the wire.

    This is also what keeps the retry useful during an outage. There the
    readback would fail too, and a rule that demanded one before every re-post
    would give up in the one case where re-posting is provably safe.
    """
    return isinstance(exc, (
        httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
    ))


def _may_have_been_stored(exc: BaseException) -> bool:
    """Whether `exc` leaves open the possibility of a message in the room.

    The question the readback exists to answer, and it is **not** the same as
    `_is_transient`. Two failures are worth retrying and cannot have stored
    anything (the connect class); one is not worth retrying and can have stored
    something — `send_message` calls `raise_for_status()` and then
    `response.json()`, so a 2xx whose body does not parse raises after
    Nextcloud has written the message. Asking the retry predicate here would
    return `None` for a post the user can see, which is exactly the ambiguity
    this change exists to remove.

    A 4xx is the one answer that settles it without a request: the server
    rejected the post, so nothing was stored, and a readback would only cost a
    round trip against a room that has already said no.
    """
    if _request_never_sent(exc):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 400 <= status <= 499:
            return False
    return True


def _bot_actor_ids(config: "Config") -> set:
    """The `actorId`s a message of our own can carry.

    `talk.bot_username` is the field the rest of the codebase compares an
    `actorId` against (`inbound._process_conversation`, `context.build_talk_context`,
    and the synthetic bot row `scheduler` builds), so it leads. `nextcloud.username`
    is accepted too, because the two are the same string on every shipped
    deployment and are configured separately: where a deployment sets them apart
    — an LDAP or email login whose Talk `actorId` is the user id — matching only
    one of them would make the readback never recognise our own post, and the
    transport would re-post, producing the duplicate the readback exists to
    prevent. Widening to both costs nothing an attacker can reach: neither name
    is one a room member can post under.
    """
    return {
        name for name in (config.talk.bot_username, config.nextcloud.username)
        if name
    }


def _posted_message_id(
    messages: list, reference_id: str, actor_ids: set,
) -> "tuple[int | None, bool]":
    """Find our own post in a room's recent history.

    Returns ``(message_id, readable)``. ``message_id`` is the newest message
    *this account* posted carrying `reference_id`, or None. ``readable`` is
    False when an entry could not be understood at all, which the caller reads
    as "the question was not answered" rather than as "the message is not
    there".

    `messages` is oldest-first, the order `TalkClient.fetch_chat_history`
    returns after its own reverse, so the last match is the newest.

    **The actor is checked as well as the reference, and that is the security
    half rather than belt and braces.** `referenceId` is free text on Talk's
    chat API and any participant in the room can set it — the property
    `inbound._reconcile_webmirror_echo` already guards against — so matching on
    the reference alone would let a room member suppress the bot's own answer
    by claiming its key. For the same reason an actor *mismatch* is a decided
    answer and never sets `readable` False: a room member who could turn the
    question unanswerable could block delivery outright, which is the same
    denial by a different route.

    **A deleted message still counts.** The question is whether Nextcloud
    stored the post, and it did; re-posting something the user has since
    deleted is the duplicate this whole path exists to avoid.

    **This can only ever answer for a single-part message**, and the caller
    enforces that. A `referenceId` names the whole answer rather than one post:
    `deliver` splits at `max_message_length` and stamps every part with the
    same one, so a match proves *some* part is in the room and never that all
    of them are — and with one key across N parts, "part three never landed"
    and "the window returned three of five" are the same evidence. Reporting
    success on it would turn a loud, recoverable failure into a silently
    truncated answer, which is worse than the bug this retry exists to fix.

    What it also cannot see is a message from an earlier `deliver` call under
    the same key, since consecutive deliveries share one. Three references
    legitimately repeat across a task rerun or a confirmation resume —
    `:ack`, `:text` and `istota:log:` — because each of those call sites
    re-posts only when the previous call returned None, and a rerun brings a
    fresh subscriber. When it happens the caller is handed the previous run's
    message and edits that one instead of posting a new one, which is not worse
    than what it replaces: those ids exist only to be edited, and the
    alternative was a None that made every later edit a no-op. `:result` and
    `:prompt` are not exposed, because a run that posts a result completes and
    is never re-run. Narrowing further needs a per-delivery key or a message
    timestamp, and comparing timestamps across two hosts' clocks fails towards
    a duplicate, which is the wrong direction for this module.
    """
    found = None
    readable = True
    for msg in messages:
        if not isinstance(msg, dict):
            readable = False
            continue
        if msg.get("referenceId") != reference_id:
            continue
        if msg.get("actorType") != "users" or msg.get("actorId") not in actor_ids:
            continue
        msg_id = msg.get("id")
        if not isinstance(msg_id, int) or isinstance(msg_id, bool):
            # Ours by reference and actor, and we cannot name it. Every field
            # comes off `response.json()`, so the type is whatever was on the
            # wire; this is the one shape that must not read as "not there".
            readable = False
            continue
        found = msg_id
    return found, readable


class TalkTransport:
    """Bidirectional adapter over Nextcloud Talk."""

    name = "talk"
    capabilities = TransportCapabilities(
        supports_edit=True,
        supports_threading=True,
        supports_progress_ack=True,
        supports_typing=True,
        # Talk's hard per-message limit is 32000 chars; split at a round 30000
        # so the appended " (N/N)" page indicator (and any counting slack) can't
        # push a part over. Only messages past this are split at all — a normal
        # long answer posts as one message instead of many 4000-char fragments.
        max_message_length=30000,
        # Talk is a room view whose transcript lives in Nextcloud, not in our
        # `messages` table — a room fan-out has to make a real API call here.
        room_view="external",
        # An inbound Talk message registers the room, binds the surface and
        # seeds membership. The rename from `displayName` and the un-archive
        # beside them in `record_inbound` are Talk's alone, not part of what
        # `member` means — a web-origin room's user-set name wins.
        inbound_room_role="member",
        # The web process posts a web-origin turn to Talk *as the user* when it
        # holds their OAuth token, and the scheduler reposts it attributed when
        # it does not. `as_user` names the top of that ladder, not a guarantee.
        user_turn_mirror="as_user",
    )

    def __init__(self, config: "Config"):
        self._config = config

    async def poll(self) -> list[IncomingMessage]:
        """Poll Talk and create tasks.

        Like email, Talk self-creates its tasks inside ``poll_talk_conversations``
        rather than handing un-ingested ``IncomingMessage``s back to a driver:
        the create must share the ``db.get_db`` transaction with the poll-state
        advance / command dispatch / confirmation handling, or a create failure
        would advance the poll cursor past messages whose tasks were never made
        (silent message loss). So this returns an empty ``IncomingMessage`` list
        — there is nothing left for a driver to ingest. The inbound body owns
        the module-global conversation/participant/DM caches and the
        Talk-specific filtering.
        """
        await poll_talk_conversations(self._config)
        return []

    async def deliver(
        self, target: str, text: str, *,
        task: "db.Task | None" = None,
        reply_to: int | None = None,
        reference_id: str | None = None,
        threaded: bool = False,
        options: "DeliveryOptions | None" = None,
    ) -> int | None:
        """Send a message to a Talk room. Splits long messages and posts the
        parts sequentially; in group chats with ``threaded=True`` the first part
        replies to ``task.talk_message_id`` and @mentions the user.

        Returns the last posted message id, or None on failure / no target. A
        transient failure is retried a bounded number of times first, and
        ``None`` therefore means the attempts were spent and nothing was
        posted — the value ISSUE-404's undelivered-result branch keys on. A
        message the readback finds already in the room is *delivered* and comes
        back as its id, not as None.
        """
        token = target or (task.conversation_token if task is not None else None)
        if not self._config.nextcloud.url or not token:
            return None

        try:
            client = get_talk_client(self._config)
            parts = split_message(text, self.capabilities.max_message_length or 4000)
            msg_id = None
            # Whether a failed post can be settled by reading the room back.
            # Only for a message that is one post: every part carries the same
            # `reference_id`, so on a split message a match proves some part
            # landed and never that all of them did. See `_posted_message_id`.
            readback_allowed = len(parts) == 1
            for i, part in enumerate(parts):
                # In group chats, reply to the original message and @mention the
                # user for the first part only so they get a notification. Only
                # applied for final results (threaded=True), not intermediate
                # progress updates which would be too noisy.
                part_reply_to = None
                if threaded and i == 0 and task is not None and task.is_group_chat:
                    part_reply_to = task.talk_message_id
                    part = f"@{task.user_id} {part}"
                elif reply_to is not None and i == 0:
                    part_reply_to = reply_to
                msg_id = await self._post_part(
                    client, token, part,
                    reply_to=part_reply_to, reference_id=reference_id,
                    readback_allowed=readback_allowed, task=task,
                )
            return msg_id
        except Exception as e:
            task_id = task.id if task is not None else "?"
            logger.error(
                "Failed to post to Talk (task %s): %s: %r",
                task_id, type(e).__name__, e,
            )
            return None

    async def _post_part(
        self, client, token: str, part: str, *,
        reply_to: int | None, reference_id: str | None,
        readback_allowed: bool, task: "db.Task | None",
    ) -> int | None:
        """Post one part of a message, retrying a transient failure.

        Raises the failure that ended it, which ``deliver`` turns into the one
        ERROR line and a ``None`` return. Raising rather than returning None
        here keeps a part that could not be posted from being read as a part
        that was posted without an id — a real outcome of a 2xx with an
        unexpected body.

        **A failed POST is not evidence the message was not stored**, which is
        the whole reason no retry was written before this one: Nextcloud may
        have accepted and written it and merely been slow to answer, and a
        blind re-post then leaves a duplicate in the user's room, which is
        worse than the silence it replaces. So a re-post happens only where one
        of two things says it is safe: the failure proves the request never
        went out, or the room says the message is not there.

        Where neither can be established the message is held back and delivery
        ends. That direction is deliberate — the pre-fix behaviour is exactly
        no retry at all, so an unanswerable question costs nothing that was not
        already lost, while guessing the other way costs a duplicate. It is
        also what a split message gets on any failure past the connect class,
        since ``readback_allowed`` is False there and nothing can settle it.

        **The last failure is questioned too**, and that is not symmetry for
        its own sake: the readback runs at the top of an attempt, so without a
        final one the third `ReadTimeout` would return None for a message
        Nextcloud had written — the same ambiguity the design removes, moved to
        the last attempt, and the value ISSUE-404's undelivered branch reads.
        """
        task_id = task.id if task is not None else "?"
        started = time.monotonic()
        last_exc: BaseException | None = None
        unanswerable = False
        for attempt in range(_POST_ATTEMPTS):
            if last_exc is not None:
                if time.monotonic() - started >= _POST_DEADLINE_SECONDS:
                    logger.warning(
                        "Talk post to %s gave up on time for task %s after "
                        "%d attempt(s)", token, task_id, attempt,
                    )
                    break
                await asyncio.sleep(_POST_BACKOFF_SECONDS[
                    min(attempt - 1, len(_POST_BACKOFF_SECONDS) - 1)
                ])
                if _may_have_been_stored(last_exc):
                    landed, settled = await self._readback(
                        client, token, reference_id, readback_allowed, task_id,
                    )
                    if not settled:
                        unanswerable = True
                        break
                    if landed is not None:
                        return self._reuse(landed, last_exc, token, task_id)
            try:
                response = await client.send_message(
                    token, part, reply_to=reply_to, reference_id=reference_id,
                )
                return response.get("ocs", {}).get("data", {}).get("id")
            except Exception as e:
                last_exc = e
                if attempt == _POST_ATTEMPTS - 1 or not _is_transient(e):
                    break
                logger.warning(
                    "Talk post to %s failed for task %s (attempt %d/%d): %s: %r",
                    token, task_id, attempt + 1, _POST_ATTEMPTS,
                    type(e).__name__, e,
                )
        if last_exc is None:
            # Not reachable while `_POST_ATTEMPTS` is at least 1: the loop
            # returns or records a failure on every path. Kept so that setting
            # it to 0 cannot fall off the end and be read as a post that
            # produced no id.
            raise RuntimeError("Talk post loop made no attempt")
        # One last question before giving up, unless it has just been asked and
        # could not be answered. `deliver` is about to report None, and None
        # has to mean the message is not in the room.
        if not unanswerable and _may_have_been_stored(last_exc):
            landed, _ = await self._readback(
                client, token, reference_id, readback_allowed, task_id,
            )
            if landed is not None:
                return self._reuse(landed, last_exc, token, task_id)
        raise last_exc

    @staticmethod
    def _reuse(
        landed: int, exc: BaseException, token: str, task_id: object,
    ) -> int:
        logger.info(
            "Talk post for task %s landed despite %s; reusing message %s in %s "
            "rather than re-posting",
            task_id, type(exc).__name__, landed, token,
        )
        return landed

    async def _readback(
        self, client, token: str, reference_id: str | None,
        readback_allowed: bool, task_id: object,
    ) -> "tuple[int | None, bool]":
        """Ask the room whether the failed POST landed.

        Returns ``(message_id, settled)``. ``settled`` is False when the
        question could not be answered at all, and the caller must neither
        re-post on that nor report the message as delivered, because the
        failure being guarded against is a duplicate the user sees. Four ways
        to be unsettled: this send was split across several posts and one
        `referenceId` cannot speak for all of them, there is nothing to match
        on, there is no account name to attribute a message to, and the history
        read itself failed. A fifth sits in `_posted_message_id`: a history
        holding an entry this account posted under our reference that could not
        be named.

        Costs one GET, and only ever on a failure path.
        """
        if not readback_allowed:
            logger.warning(
                "Talk post to %s failed for task %s and cannot be settled: the "
                "message was split, and one reference id cannot say whether "
                "every part landed",
                token, task_id,
            )
            return None, False
        actor_ids = _bot_actor_ids(self._config)
        if not reference_id or not actor_ids:
            logger.warning(
                "Talk post to %s failed for task %s and cannot be settled: "
                "no %s to read back with",
                token, task_id,
                "reference id" if not reference_id else "bot account name",
            )
            return None, False
        try:
            history = await client.fetch_chat_history(
                token, limit=_READBACK_LIMIT, timeout=_READBACK_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(
                "Talk readback for %s in %s failed (task %s): %s: %r; "
                "treating the post as unsettled",
                reference_id, token, task_id, type(e).__name__, e,
            )
            return None, False
        landed, readable = _posted_message_id(history, reference_id, actor_ids)
        # A match answers the question whatever else the history held; only an
        # empty result has to be readable to mean "not there".
        return landed, (landed is not None or readable)

    async def edit(self, target: str, message_id: int, text: str) -> None:
        """Edit a previously posted Talk message in place. Raises on API error
        (the scheduler ``edit_talk_message`` shim catches and returns False)."""
        if not self._config.nextcloud.url or not target:
            return None
        client = get_talk_client(self._config)
        await client.edit_message(target, message_id, text)
        return None

    async def resolve_channel_name(self, token: str) -> str:
        """Resolve a Talk room token to its display name, falling back to the
        token on any OCS error / missing config. Houses the last log-path OCS
        read behind the transport seam (was a direct ``get_conversation_info``
        in ``scheduler._resolve_channel_name``)."""
        if not self._config.nextcloud.url or not token:
            return token
        try:
            client = get_talk_client(self._config)
            info = await client.get_conversation_info(token)
            return info.get("displayName") or token
        except Exception:
            logger.debug(
                "Failed to resolve Talk channel name for %s", token, exc_info=True,
            )
            return token

    async def download_attachment(self, remote_ref: str, local_path: str) -> None:
        client = get_talk_client(self._config)
        await client.download_attachment(remote_ref, local_path)

    def resolve_target(self, task: "db.Task") -> str | None:
        from ...scheduler import _talk_target_for_delivery
        return _talk_target_for_delivery(self._config, task)
