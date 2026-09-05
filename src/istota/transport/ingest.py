"""Shared inbound path — turn a normalized inbound message into a task.

`record_inbound` is the single inbound choke point every surface routes
through: resolve the canonical room token, lazily auto-register an unknown
room surface, echo-check, store the user message into the canonical `messages`
store, and create the task. `ingest_message` is a thin adapter over it for the
`IncomingMessage`-shaped callers (Talk, email); the web POST path calls
`record_inbound` directly (it never built an `IncomingMessage`).

Surface-specific filtering / short-circuiting (Talk's mention + command +
confirmation handling, email's untrusted-sender gate) stays inside each
transport's `poll()`; this just performs the resolve + store + create step.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .. import db
from ..surfaces import is_room_member
from ._types import IncomingMessage
from .routing import transcript_room

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


def resolve_author(
    config: "Config", user_id: str, sender_address: str | None,
) -> tuple[str | None, str | None]:
    """`(author_user_id, author_label)` for an inbound turn. Never raises.

    A surface that reports no separate sender is the istota user speaking, so
    the turn is theirs. Email reports an envelope sender, which may be the user
    mailing themselves — `external_email_sender` answers None for that, and the
    turn is again theirs — or someone else, in which case the *sanitized* label
    is the author and no user id applies.

    Sanitizing here rather than at any reader is the point of the split: the
    label reaches the store as an addr-spec or the fixed unattributed sentinel,
    so a raw `From:` header with a display name in it can never be rendered.

    Best-effort by contract. This runs inside `record_inbound`'s transaction,
    which is the inbound one — a failed attribution lookup must cost the row its
    author, never cost the user their message.

    The failure path **keeps the sender's existence** even when it cannot
    classify it, following `external_email_sender`'s own rule: under-trusting
    the principal costs an odd label, while over-trusting launders a third
    party's text into their turn. So a message that arrived with a sender falls
    back to the unattributed sentinel rather than to nothing — `(None, None)`
    renders as the room owner, which for a stranger's mail is exactly the
    mislabelling these columns exist to end.
    """
    try:
        if not sender_address:
            return (user_id or None), None
        user_config = config.users.get(user_id or "")
        own = list(user_config.email_addresses or []) if user_config else []
        label = db.external_email_sender(sender_address, own)
        if label:
            return None, label
        return (user_id or None), None
    except Exception as e:  # pragma: no cover - never fail an ingest over this
        logger.warning("author resolution failed for user %s: %s", user_id, e)
        if sender_address:
            return None, db.UNATTRIBUTED_SENDER
        return (user_id or None), None


def display_attachment_names(
    attachments: list[str] | None,
    names: list[str] | None = None,
) -> list[str] | None:
    """The labels a turn's attachment chips render, or None when it carried no
    files.

    A stored attachment's filename is not the one the user picked — the web
    upload appends a random suffix (`note.txt` → `note-a1b2c3d4.txt`) so two
    same-named uploads in a day can't collide. So a caller that still knows the
    original names (the web composer) supplies them and they win; every other
    surface falls back to the stored basename. `names` is positional and
    display-only, so a mismatched count is discarded rather than zipped — a
    label landing on the wrong file is worse than a plainer one.
    """
    if not attachments:
        return None
    if names and len(names) == len(attachments):
        return [str(n) for n in names]
    return [os.path.basename(p) for p in attachments]


def workspace_attachment_paths(
    config: "Config",
    user_id: str,
    attachments: list[str] | None,
) -> list[str | None] | None:
    """The workspace paths a turn's attachment chips can be *linked* at, or
    None when none of them can be.

    A chip should open the file it names, and the way to do that without
    minting a public share is the web app's session-scoped `/chat/files`
    endpoint — the user opening a file they already own. That endpoint takes a
    Nextcloud-style workspace path (`/Users/<uid>/…`), while the stored
    attachment is a host path, so the translation happens once here, at ingest,
    and rides the message row: the paths themselves live only on the `tasks`
    row, which retention deletes long before the transcript stops showing the
    turn.

    An attachment outside the sender's own workspace resolves to None rather
    than being dropped — the list is positional against the display names, and
    an inert chip is the intended outcome for a file the endpoint could not
    serve anyway (a Talk attachment under `/Talk`, an upload that fell back to
    the temp dir on a mountless deployment).
    """
    if not attachments:
        return None
    root = config.workspace_root(user_id)
    if root is None:  # rclone deployment — no local workspace to serve from
        return None
    real_root = os.path.normpath(str(root))
    out: list[str | None] = []
    for host_path in attachments:
        real = os.path.normpath(str(host_path))
        if real.startswith(real_root + os.sep):
            relative = real[len(real_root) + 1:].replace(os.sep, "/")
            out.append(f"/Users/{user_id}/{relative}")
        else:
            out.append(None)
    return out if any(out) else None


def _live_pin_namespace(config, conn, room_token: str, source_type: str) -> str | None:
    """The namespace an inline `!model` on this message was resolved in.

    The surfaces that accept a `!model` prefix resolve the alias through
    ``make_brain(brain_for_room(...))`` before calling in, so this repeats their
    call rather than guessing: same room, same source type, same transaction, so
    the two agree by construction. Reading ``rooms.brain`` instead would get it
    wrong in precisely the case the column exists for — ``brain_for_room``
    refuses a kind the operator has dropped from ``[brain] room_selectable``,
    and the alias then resolves in the lane's namespace (ISSUE-420).

    Imported at function scope, matching ``transport/talk/inbound.py``, which
    reaches ``brain_for_room`` the same way. **Not** because of a cycle —
    measured, ``commands`` imports no ``transport`` module and hoisting both
    imports to module scope imports cleanly in every order — but because this
    module is on the Talk poll's import path and ``commands`` pulls in the whole
    command registry behind it for a call that only a message carrying an inline
    ``!model`` ever makes.

    Never raises. ``None`` means "not established", which the executor's crossing
    rule already handles as "infer" — the answer it gave before this column.
    """
    try:
        from ..brain import model_namespace_for_kind
        from ..commands import brain_for_room

        return model_namespace_for_kind(
            brain_for_room(config, conn, room_token, source_type).kind,
        )
    except Exception:  # noqa: BLE001 — a namespace read must not fail an inbound
        logger.debug(
            "record_inbound: could not establish the inline pin's namespace",
            exc_info=True,
        )
        return None


def record_inbound(
    conn,
    config: "Config",
    *,
    surface: str,
    surface_ref: str,
    user_id: str,
    text: str,
    source_type: str | None = None,
    channel_name: str | None = None,
    is_group_chat: bool = False,
    attachments: list[str] | None = None,
    attachment_names: list[str] | None = None,
    platform_message_id: int | None = None,
    # The parent's id *on this surface* — for Talk, a Talk message id. Routes
    # to `tasks.reply_to_talk_id`. NOT the column of the same name; see
    # `reply_to_canonical_id` below, and keep the two apart.
    reply_to_message_id: int | None = None,
    # The parent's id in the canonical `messages` store. Routes to
    # `tasks.reply_to_message_id` and onto the stored user row.
    reply_to_canonical_id: int | None = None,
    reply_to_content: str | None = None,
    delivery_token: str | None = None,
    output_target: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    # An explicit per-message brain pick, or None to take the room's standing
    # default; "" escapes the room default without naming a kind. This is the
    # one place `apply_room_default` deliberately does *not* apply: that flag
    # exists so `!model default` can escape the room's model pin while
    # resolving to no override of its own, and with no `!brain` message prefix
    # there is nothing for it to be the counterpart of. Folding this fill into
    # the condition above would therefore look like tidying and would silently
    # let a `!model default` turn drop the room's brain too.
    brain: str | None = None,
    apply_room_default: bool = True,
    priority: int = 5,
    # The worker queue the resulting task lands on. Interactive surfaces leave
    # it alone; the email poller passes "background" so a flood at the public
    # `bot+user@` address cannot take the slots a live chat turn needs
    # (ISSUE-250).
    queue: str = "foreground",
    external_id: str | None = None,
    client_msg_id: str | None = None,
    suppress_transcript_mirror: bool = False,
    # Whether this turn belongs in the resolved room at all — see the flag above
    # it, which is a hold rather than a refusal.
    mirror_to_room: bool = True,
    # The message's own sender when it isn't `user_id` (email's envelope
    # sender). Raw and untrusted; sanitized here, never by a reader.
    sender_address: str | None = None,
) -> tuple[str, int | None]:
    """Resolve → echo-check → store user message → create task.

    Returns `(room_token, task_id)`. `task_id` is `None` only when the message
    is dropped as a known echo (forward-looking; structurally impossible for the
    v1 Talk+web pair, where Talk self-filters bot posts by author and web is
    never polled inbound). `room_token` is the canonical conversation token the
    task was created under.

    `client_msg_id` is the sender's own identity for this message (web chat
    mints one per send and reuses it on retry). When a stored turn already
    carries it, that turn's task is returned and nothing new is created — a
    client that could not tell "never arrived" from "answer lost" gets one turn
    rather than two.
    """
    source_type = source_type or surface

    # 1. Resolve canonical room token. With no binding, the surface_ref *is* the
    #    canonical token (origin-surface case).
    room_token = db.resolve_room_token(conn, surface, surface_ref) or surface_ref
    # Does this surface *own* rooms — register an unknown token, bind it, add
    # membership, rename from the surface? `surfaces.SURFACES` answers it;
    # `room_role == "member"` is talk and web, and email's `guest` is what keeps
    # the mirror-only path below off every one of those side effects. Not the
    # room-*view* question, which drives the outbound fan-out and which
    # `is_room_view` answers separately for the one site where the two can
    # diverge (the scheduler's confirmation mirror gate).
    room_surface = is_room_member(surface) and bool(room_token)

    # A non-room surface whose exchange belongs in a room (ISSUE-136): an email
    # threaded back into the web/Talk room it came from, or — since ISSUE-247 —
    # a first-contact email whose routing sends it to a room the user made for
    # that mail. Its turn belongs in that room's transcript alongside the answer
    # (`scheduler._store_room_turn`), so without this the room showed a bot
    # answer with no question above it.
    #
    # Existence, never creation: the resolver only ever returns a *registered*
    # room, so mail the bot merely receives can't mint rooms in anyone's
    # sidebar. That also keeps this off every other room side effect below — no
    # registration, no binding, no rename, no membership, no echo ledger, no
    # room model default. `suppress_transcript_mirror` withholds a turn still
    # facing an untrusted-sender gate: the row would otherwise be committed in
    # this same transaction, i.e. published to the room *before* the user is
    # asked, and `db.cancel_task` on a decline only touches `tasks` — so
    # declining would leave the content there permanently. `mirror_to_room` is
    # the other, permanent answer: the room is not part of this exchange, so
    # there is nothing for an approval to publish later (ISSUE-254).
    # Which room the turn is *written* to. For a room surface it is the room
    # itself. For a non-room surface it is whatever the routing resolved, which
    # is the token only when the token already is a room — a first-contact email
    # carries a thread hash and its exchange belongs in the room the user's
    # routing sends that mail to (ISSUE-247). The task keeps `room_token` as its
    # `conversation_token` either way: a thread identifier stays a thread
    # identifier, and that is what `References` matching needs it to be.
    if room_surface:
        transcript_token = room_token
    else:
        transcript_token = transcript_room(
            conn, config,
            user_id=user_id,
            source_type=source_type,
            conversation_token=room_token,
            output_target=output_target,
            talk_delivery_token=delivery_token,
        )
    mirror_only = (
        not room_surface
        and mirror_to_room
        and bool(transcript_token)
        and not suppress_transcript_mirror
    )
    # Record the permanent half of that decision on the task (ISSUE-255). The
    # poller computed it and used it twice, and it was then thrown away — so
    # every consumer keyed on `conversation_token` rather than on the transcript
    # (the history fallback, the channel memory namespace, the channel sleep
    # cycle, the two failure paths) went on treating the exchange as part of a
    # room it is deliberately absent from. `suppress_transcript_mirror` is
    # excluded on purpose: that one is a hold on a turn that *does* belong in the
    # room, and `confirmations.approve` publishes it once answered.
    #
    # **`transcript_token` is required, and is the whole difference between this
    # column and `not mirror_to_room`.** The column says "there is a room, and
    # this exchange is deliberately not part of it" — so with no room resolved
    # there is nothing to be absent from and the answer is False. The poller sets
    # `mirror_to_room=False` for *every* self-addressed thread reply, including a
    # genuine email-only thread whose `conversation_token` is a synthetic hash
    # naming no room; flagging that one would make the readers below drop the
    # thread's own prior turns from its own history, which is the only history
    # such a thread has (there is no `messages` room to fall back to).
    withheld_from_room = (
        not room_surface and not mirror_to_room and bool(transcript_token)
    )

    # The namespace `model` was resolved in, frozen onto the task beside it
    # (ISSUE-420). Two sources, and they are not interchangeable — see the two
    # branches below. Stays None for every non-room surface, which have no room
    # pin to inherit and no room brain to have resolved an inline one against.
    model_namespace: str | None = None

    if room_surface:
        # Lazy room registration on first sight (a Talk room the bot joined, a
        # web room created elsewhere). First writer wins on origin + name.
        existing = db.get_room(conn, room_token)
        if existing is None:
            db.register_room(
                conn, room_token, user_id, origin=surface, name=channel_name,
            )
        elif surface == "talk" and existing.origin == "talk":
            # Talk-side rename flows back to the registry on the next poll. Only
            # for Talk-origin rooms — a web-origin room's user-set name wins.
            if channel_name and channel_name != existing.name:
                db.rename_room(conn, room_token, channel_name)
            if existing.archived:
                # A fresh inbound means the bot is demonstrably back in this Talk
                # room, so un-hide it for all members (archive_orphaned_talk_rooms
                # globally archived it when the bot left the Nextcloud room).
                # Without this a re-joined room stays invisible to everyone even
                # though they're still members (ISSUE-134).
                db.set_room_archived(conn, room_token, False)
        db.add_room_binding(conn, room_token, surface, surface_ref)
        # Every sender is a member, so a shared (multi-human) Talk room surfaces
        # in each participant's web room list — not just the first one who
        # registered it (ISSUE-134). Idempotent; covers the already-registered
        # path where register_room above didn't run.
        db.add_room_member(conn, room_token, user_id)
        # Re-engagement un-hides: the sender posting in a room they'd previously
        # hidden clears their hide tombstone so it resurfaces in their web list.
        # Only the sender's own tombstone — another participant's hide is left
        # intact.
        db.undismiss_room(conn, room_token, user_id)

        # 2. Echo check (loop-prevention ledger) — armed by post-as-user
        #    mirroring: a web-origin row stamped with a Talk id catches the
        #    Talk echo of that mirror even when its referenceId was stripped.
        #    Rows that originated on this very surface are excluded — that's
        #    a re-polled duplicate, not a mirror, and it must reach
        #    `create_task`'s dedup (which returns the existing task id).
        if external_id is not None and db.message_has_external_id(
            conn, room_token, surface, str(external_id),
            exclude_origin=surface,
        ):
            logger.info(
                "Dropping echo of a mirrored message on %s (room=%s ext=%s)",
                surface, room_token, external_id,
            )
            return room_token, None

        # Per-room model/effort default. It lives on the shared rooms registry,
        # so this single choke point applies it uniformly to every surface
        # (Talk, web, future Matrix). An inline `!model` prefix wins: the room
        # default only fills a message that carried none. `apply_room_default`
        # is False whenever the message had an explicit `!model` prefix (set by
        # the caller from `prefix.matched`) — this covers `!model default`,
        # which resolves to no override (`model=None`) yet must still escape the
        # room default back to the instance default. When it's a real inline
        # model, `model` is already set so the fill is skipped anyway; effort
        # follows model as a unit. `existing` is None only on a room's
        # first-ever message, which has no stored default yet.
        if apply_room_default and model is None and existing is not None:
            model = existing.model
            if effort is None:
                effort = existing.effort
            # The *stored* namespace, never a fresh derivation: this id was
            # written at some earlier point and the allowlist may have moved
            # since, which is exactly the case ISSUE-420 is about. A row written
            # before the column existed carries None, and the executor's own
            # inference answers it as it did before.
            model_namespace = existing.model_namespace
        elif model:
            # An inline `!model` on this message. The caller resolved the alias
            # against the brain this room *admits*
            # (`talk/inbound.py` builds `make_brain(brain_for_room(...))`), so
            # the live derivation is the same answer by construction — and it
            # has to be live rather than stored, because nothing wrote this id
            # to the room. Same defect as the stored case one step earlier: a
            # refused room pin puts the id in the lane's namespace while
            # `tasks.brain` below still records the refused kind.
            model_namespace = _live_pin_namespace(
                config, conn, room_token, source_type,
            )

        # The room's standing brain, on the same terms and inside the same
        # guard: Talk and web, never email, matching what `model` and `effort`
        # already do (ISSUE-136 — a guest surface joins a room's transcript and
        # takes none of its settings). Frozen onto the task here so a later edit
        # to the room cannot change a task already running.
        if brain is None and existing is not None:
            brain = existing.brain

        # 2b. Idempotent replay. Checked after the room is resolved (the key is
        #     scoped to a room, so the same key in two rooms is two messages)
        #     and before the task is created, so a retry adds nothing.
        if client_msg_id:
            prior = db.find_send_by_client_msg_id(conn, room_token, client_msg_id)
            if prior is not None:
                prior_task, prior_sender = prior
                if prior_sender == user_id:
                    logger.info(
                        "Replaying prior task for client_msg_id (room=%s task=%s)",
                        room_token, prior_task,
                    )
                    return room_token, prior_task
                # A co-member of this shared room got there first with the same
                # key. It is an optimization, not a requirement, so this send
                # gives it up rather than colliding on the room-scoped unique
                # index — or being handed somebody else's task, which the
                # caller is not authorized to read anyway.
                logger.warning(
                    "client_msg_id already used by another sender in room=%s; "
                    "storing this message without one", room_token,
                )
                client_msg_id = None

    # 2c. Record a surface-native reply parent canonically too, so the web
    #     transcript renders a Talk-origin reply as a reply rather than as an
    #     ordinary message. Resolved here because this is where the conn and
    #     the canonical room token are: `IncomingMessage` stays surface-native,
    #     which is the reason the two parameters are separate in the first
    #     place. An unmirrored parent leaves it None and the citation stays
    #     Talk-only, exactly as before.
    if (
        reply_to_canonical_id is None
        and reply_to_message_id is not None
        and room_surface
    ):
        reply_to_canonical_id = db.find_message_by_external_id(
            conn, room_token, surface, str(reply_to_message_id),
        )

    # 3. Create the task.
    task_id = db.create_task(
        conn,
        prompt=text,
        user_id=user_id,
        source_type=source_type,
        conversation_token=room_token,
        is_group_chat=is_group_chat,
        attachments=attachments or None,
        talk_message_id=platform_message_id,
        # Surface-native id → the Talk column; canonical id → its own. The two
        # parameters are different namespaces for the same conceptual thing and
        # must not be merged.
        reply_to_talk_id=reply_to_message_id,
        reply_to_message_id=reply_to_canonical_id,
        reply_to_content=reply_to_content,
        withheld_from_room=withheld_from_room,
        output_target=output_target,
        talk_delivery_token=delivery_token,
        model=model,
        effort=effort,
        brain=brain,
        model_namespace=model_namespace,
        priority=priority,
        queue=queue,
    )

    # 4. Store the user message into the canonical store — for a room surface,
    #    or for a mirror-only surface landing in an existing room — idempotently:
    #    Talk dedups a duplicate poll to the same task id, so we must not store a
    #    second user row for it.
    if (room_surface or mirror_only) and task_id is not None:
        already = conn.execute(
            "SELECT 1 FROM messages WHERE room_token = ? AND task_id = ? "
            "AND role = 'user' LIMIT 1",
            (transcript_token, task_id),
        ).fetchone()
        if not already:
            author_user_id, author_label = resolve_author(
                config, user_id, sender_address,
            )
            # Stamp the surface-native message id (Talk's message id) so the
            # canonical row knows where it exists on that surface: this feeds
            # both the echo ledger and the Talk→web read-sync cursor cap
            # (`room_max_talk_synced_message_id`).
            db.add_message(
                conn, transcript_token, role="user", body=text,
                origin_surface=surface, task_id=task_id,
                author_user_id=author_user_id,
                author_label=author_label,
                external_ids=(
                    {surface: str(external_id)}
                    if external_id is not None
                    else None
                ),
                attachments=display_attachment_names(attachments, attachment_names),
                attachment_paths=workspace_attachment_paths(
                    config, user_id, attachments,
                ),
                client_msg_id=client_msg_id,
                reply_to_message_id=reply_to_canonical_id,
            )

    return room_token, task_id


def ingest_message(conn, config: "Config", msg: IncomingMessage) -> int | None:
    """Create a task from a normalized inbound message via `record_inbound`.

    Returns the task id, or `None` if the message was dropped as a known echo
    (only reachable for a room surface — email never echo-drops). On a duplicate
    Talk message (same `platform_message_id` + `channel_token`) `db.create_task`
    returns the id of the already-existing task rather than inserting twice.
    """
    _room_token, task_id = record_inbound(
        conn,
        config,
        surface=msg.surface,
        surface_ref=msg.channel_token,
        user_id=msg.user_id,
        text=msg.text,
        source_type=msg.source_type,
        channel_name=msg.channel_name,
        is_group_chat=msg.is_group_chat,
        attachments=msg.attachments or None,
        platform_message_id=msg.platform_message_id,
        reply_to_message_id=msg.reply_to_message_id,
        reply_to_content=msg.reply_to_content,
        delivery_token=msg.delivery_token,
        output_target=msg.output_target,
        model=msg.model,
        effort=msg.effort,
        brain=msg.brain,
        queue=msg.queue,
        apply_room_default=not msg.model_prefix_used,
        external_id=str(msg.platform_message_id)
        if msg.platform_message_id is not None
        else None,
        suppress_transcript_mirror=msg.suppress_transcript_mirror,
        mirror_to_room=msg.mirror_to_room,
        sender_address=msg.sender_address,
    )
    return task_id
