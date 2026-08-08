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
from ._types import IncomingMessage

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)

# Surfaces that *own* rooms: they lazily register an unknown token, bind it,
# add membership, and rename it from the surface. Email/REPL are not room
# surfaces — a token they carry is never turned into a room.
#
# This is deliberately narrower than "surfaces whose turns are stored". A
# mirror-only surface (email) still records its turn when the token it resolved
# to *already is* a room — see `mirror_only` in `record_inbound`.
ROOM_SURFACES = frozenset({"talk", "web"})


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
    apply_room_default: bool = True,
    priority: int = 5,
    external_id: str | None = None,
    client_msg_id: str | None = None,
    suppress_transcript_mirror: bool = False,
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
    room_surface = surface in ROOM_SURFACES and bool(room_token)

    # A non-room surface whose token already *is* a room (ISSUE-136): an email
    # threaded back into the web/Talk room it came from. Its turn belongs in that
    # room's transcript — the assistant half has always been stored there
    # (`scheduler._store_room_turn`, gated on room existence), so without this
    # the room showed a bot answer with no question above it.
    #
    # Existence-only, never creation: a fresh email thread carries a synthetic
    # token that is not a room and stays task-only, so mail the bot merely
    # receives can't mint rooms in anyone's sidebar. That also keeps this off
    # every other room side effect below — no registration, no binding, no
    # rename, no membership, no echo ledger, no room model default.
    # `suppress_transcript_mirror` withholds a turn still facing an
    # untrusted-sender gate: the row would otherwise be committed in this same
    # transaction, i.e. published to the room *before* the user is asked, and
    # `db.cancel_task` on a decline only touches `tasks` — so declining would
    # leave the content there permanently.
    mirror_only = (
        not room_surface
        and bool(room_token)
        and not suppress_transcript_mirror
        and db.get_room(conn, room_token) is not None
    )

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
        output_target=output_target,
        talk_delivery_token=delivery_token,
        model=model,
        effort=effort,
        priority=priority,
    )

    # 4. Store the user message into the canonical store — for a room surface,
    #    or for a mirror-only surface landing in an existing room — idempotently:
    #    Talk dedups a duplicate poll to the same task id, so we must not store a
    #    second user row for it.
    if (room_surface or mirror_only) and task_id is not None:
        already = conn.execute(
            "SELECT 1 FROM messages WHERE room_token = ? AND task_id = ? "
            "AND role = 'user' LIMIT 1",
            (room_token, task_id),
        ).fetchone()
        if not already:
            # Stamp the surface-native message id (Talk's message id) so the
            # canonical row knows where it exists on that surface: this feeds
            # both the echo ledger and the Talk→web read-sync cursor cap
            # (`room_max_talk_synced_message_id`).
            db.add_message(
                conn, room_token, role="user", body=text,
                origin_surface=surface, task_id=task_id,
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
        apply_room_default=not msg.model_prefix_used,
        external_id=str(msg.platform_message_id)
        if msg.platform_message_id is not None
        else None,
        suppress_transcript_mirror=msg.suppress_transcript_mirror,
    )
    return task_id
