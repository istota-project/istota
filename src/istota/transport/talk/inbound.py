"""Talk conversation polling and task creation — the TalkTransport inbound body.

Owns every Talk-protocol-specific inbound step and the module-global
conversation/participant/DM caches. ``poll_talk_conversations`` self-creates its
tasks (see its atomicity note); ``TalkTransport.poll`` delegates here.
"""

import asyncio
import logging
import time
from pathlib import Path

from ... import db
from ...async_runtime import get_talk_client
from ...config import Config
from ...talk import TalkClient, clean_message_content
from .._types import WEBMIRROR_REF_PREFIX, IncomingMessage
from ..ingest import ingest_message

logger = logging.getLogger("istota.transport.talk.inbound")


# Participant cache: token -> (participants list, timestamp)
_participant_cache: dict[str, tuple[list[dict], float]] = {}
_PARTICIPANT_CACHE_TTL = 300  # 5 minutes

# Conversation list cache: avoids calling list_conversations() every poll cycle.
# Rooms change rarely; refreshing every 60s is sufficient.
_conversation_cache: tuple[list[dict], float] | None = None
_CONVERSATION_CACHE_TTL = 60  # seconds

# 1:1 DM token cache: user_id -> conversation_token (populated from list_conversations)
_dm_token_cache: dict[str, str] = {}


def get_dm_token(user_id: str) -> str | None:
    """Get the 1:1 DM conversation token for a user, if known.

    Populated automatically during Talk polling. Returns None if the
    poller hasn't run yet or the user has no 1:1 conversation with the bot.
    """
    return _dm_token_cache.get(user_id)


def extract_attachments(message: dict) -> list[str]:
    """
    Extract file attachment paths from a Talk message.

    When files are shared in Talk, they appear in the bot user's Talk folder.
    The message contains {file0}, {file1} placeholders that we replace with
    actual filenames from the message parameters.

    Returns list of relative paths like "Talk/filename.jpg".
    """
    attachments = []

    # Check for file parameters in message
    # Note: messageParameters is a dict when present, but can be an empty list when empty
    message_params = message.get("messageParameters", {})
    if not isinstance(message_params, dict):
        return attachments
    for key, value in message_params.items():
        if key.startswith("file") and isinstance(value, dict):
            # File shared in conversation
            filename = value.get("name", "")
            if filename:
                # Strip directory components to prevent path traversal
                safe_name = Path(filename).name
                if safe_name and safe_name != ".." and safe_name != ".":
                    # Files shared in Talk are accessible in the bot's Talk folder
                    attachments.append(f"Talk/{safe_name}")

    return attachments


def is_bot_mentioned(message: dict, bot_username: str) -> bool:
    """Check if the bot is directly @mentioned in a Talk message.

    Checks messageParameters for mention-user or mention-federated-user entries
    matching the bot username. Excludes mention-call (@all) to avoid responding
    to every broadcast.
    """
    message_params = message.get("messageParameters", {})
    if not isinstance(message_params, dict):
        return False

    for key, value in message_params.items():
        if not isinstance(value, dict):
            continue
        if key.startswith("mention-user") or key.startswith("mention-federated-user"):
            if value.get("id") == bot_username:
                return True
    return False


async def _get_participants(
    client: TalkClient,
    conversation_token: str,
    conv_type: int | None,
) -> list[dict]:
    """Get participants for a conversation, with TTL cache.

    Type 1 (DM) returns empty list (no lookup needed).
    Returns cached or fresh participant list from API.
    Falls back to empty list on API errors.
    """
    if conv_type == 1:
        return []

    now = time.monotonic()
    cached = _participant_cache.get(conversation_token)
    if cached is not None:
        participants, ts = cached
        if now - ts < _PARTICIPANT_CACHE_TTL:
            return participants

    try:
        participants = await client.get_participants(conversation_token)
        _participant_cache[conversation_token] = (participants, now)
        logger.debug(
            "Room %s (type=%s) has %d participants → %s",
            conversation_token, conv_type, len(participants),
            "multi-user" if len(participants) >= 3 else "DM-like",
        )
        return participants
    except Exception as e:
        logger.warning(
            "Error getting participants for %s (type=%s): %s: %s — treating as DM",
            conversation_token, conv_type, type(e).__name__, e,
        )
        return []


def _is_multi_user(participants: list[dict]) -> bool:
    """Return True if 3+ participants (requires @mention)."""
    return len(participants) >= 3


def _participant_names(participants: list[dict], exclude: str | None = None) -> list[str]:
    """Extract display names from participant list, excluding a specific actor."""
    names = []
    for p in participants:
        actor_id = p.get("actorId", "")
        if exclude and actor_id == exclude:
            continue
        name = p.get("displayName") or actor_id
        if name:
            names.append(name)
    return names


def _istota_members_for_conversation(
    conv: dict, participants: list[dict], config: Config,
) -> list[str]:
    """The istota user_ids who are human participants of a Talk conversation,
    for seeding `room_members` when the room is first registered from a poll.

    Uses the same actor→user gate as message processing (`actor_id in
    config.users`, bot excluded), so membership matches who could actually
    drive a task in the room. For a DM (type 1) the participant list is empty,
    so the other party comes from `conv['name']`. Returned sorted for a
    deterministic room owner. Empty when no istota human participates (a
    bot-only or all-guest room) — the caller then skips registration."""
    bot = config.talk.bot_username
    members: set[str] = set()
    if conv.get("type") == 1:
        other = conv.get("name", "")
        if other and other != bot and other in config.users:
            members.add(other)
    else:
        for p in participants:
            # Mirror the message-processing gate: only real users (not guests /
            # federated / bots), and only those mapped to an istota user.
            if p.get("actorType", "users") != "users":
                continue
            actor_id = p.get("actorId", "")
            if actor_id and actor_id != bot and actor_id in config.users:
                members.add(actor_id)
    return sorted(members)


async def _poll_single_conversation(
    client: TalkClient,
    conversation_token: str,
    last_message_id: int | None,
    timeout: int,
) -> tuple[str, list[dict]]:
    """
    Poll a single conversation for new messages.

    Returns (conversation_token, messages) tuple.
    """
    try:
        messages = await client.poll_messages(
            conversation_token,
            last_known_message_id=last_message_id,
            timeout=timeout,
        )
        return (conversation_token, messages)
    except Exception as e:
        logger.error("Error polling conversation %s: %s", conversation_token, e)
        return (conversation_token, [])


async def poll_talk_conversations(config: Config) -> list[int]:
    """
    Poll all Talk conversations concurrently for new messages and create tasks.

    This is the Talk transport's inbound body (``TalkTransport.poll`` delegates
    here). It owns every Talk-protocol-specific step — conversation listing +
    cache, per-room long-poll, system/own/unknown-user/unmentioned filtering,
    ``!model`` prefix parsing, ``!command`` dispatch, confirmation-reply
    handling, the per-channel active-task gate, attachment extraction, and
    cancelling superseded confirmations.

    Task creation (via ``ingest_message``) happens in the **same** ``db.get_db``
    transaction as ``set_talk_poll_state`` / ``cancel_pending_confirmations`` /
    ``!command`` dispatch, so a ``create_task`` failure rolls the whole batch
    back and the messages are re-polled next cycle rather than silently lost
    (the poll-cursor advance is never committed without the task). This is why
    Talk — like email — self-creates inside ``poll`` rather than handing
    un-ingested ``IncomingMessage``s back to a driver across a transaction
    boundary.

    Uses asyncio.wait() with a timeout so fast rooms are processed immediately
    without waiting for slow (quiet) rooms to finish their long-poll.

    Returns the list of created task IDs.
    """
    if not config.talk.enabled:
        return []

    if not config.nextcloud.url:
        return []

    global _conversation_cache

    client = get_talk_client(config)
    created: list[int] = []

    # Get all conversations, using cache to avoid blocking every cycle
    now = time.monotonic()
    cache_valid = (
        _conversation_cache is not None
        and now - _conversation_cache[1] < _CONVERSATION_CACHE_TTL
    )

    if cache_valid:
        conversations = _conversation_cache[0]
    else:
        try:
            conversations = await client.list_conversations()
            _conversation_cache = (conversations, now)
        except Exception as e:
            if _conversation_cache is not None:
                logger.debug(
                    "list_conversations failed (%s: %s), using cached list (%d rooms)",
                    type(e).__name__, e, len(_conversation_cache[0]),
                )
                conversations = _conversation_cache[0]
            else:
                logger.warning("Error listing Talk conversations: %s: %s", type(e).__name__, e)
                return []

    # Build list of conversations to poll and initialize new ones
    poll_tasks = []
    conv_types: dict[str, int] = {}  # token -> conversation type
    conv_names: dict[str, str] = {}  # token -> display name (lazy room registration)
    with db.get_db(config.db_path) as conn:
        for conv in conversations:
            conversation_token = conv.get("token")
            if not conversation_token:
                continue

            # Conversation types: 1=one-to-one (DM), 2=group, 3=public, 4=changelog
            conv_type = conv.get("type")
            conv_types[conversation_token] = conv_type
            display_name = conv.get("displayName") or conv.get("name")
            if display_name:
                conv_names[conversation_token] = display_name

            # Register the Talk room in the unified registry on first sight so
            # it surfaces in web chat even when no one has messaged the bot in
            # it yet — the task-keyed unified-rooms migration and the live
            # record_inbound path both miss a room the bot merely lurks in
            # (polled + history-cached, but never addressed): the #sysadmin
            # case. Resolve the canonical token FIRST: a *promoted* web room's
            # canonical token is its web token (the Talk token lives only in a
            # binding), so registering by the raw Talk token would create a
            # phantom duplicate origin='talk' row. Seed membership from the human
            # participants mapped to istota users (bot excluded). Only a
            # genuinely new room needs the participant fetch, so it's rare, not
            # per-poll — membership for active users is maintained below (the
            # message loop) and by record_inbound. A user who later hides the
            # room is kept out by their dismissal tombstone.
            canonical = (
                db.resolve_room_token(conn, "talk", conversation_token)
                or conversation_token
            )
            existing_room = db.get_room(conn, canonical)
            if existing_room is not None:
                # Backfill the registry title from Talk's displayName (migrated
                # rooms were folded in with NULL names; without this they'd show
                # the generic "Talk room" until their next message). Talk-origin
                # only — a web-origin (incl. promoted) room's user-set name wins.
                if (
                    existing_room.origin == "talk"
                    and display_name
                    and existing_room.name != display_name
                ):
                    db.rename_room(conn, canonical, display_name)
            elif conv_type != 4:
                # New room (skip type 4 = the "Talk updates" changelog room — a
                # system room that shouldn't surface in web chat).
                participants = await _get_participants(
                    client, conversation_token, conv_type,
                )
                member_ids = _istota_members_for_conversation(
                    conv, participants, config,
                )
                if member_ids:
                    db.register_room(
                        conn, canonical, member_ids[0],
                        origin="talk", name=display_name,
                    )
                    db.add_room_binding(conn, canonical, "talk", conversation_token)
                    for uid in member_ids[1:]:
                        db.add_room_member(conn, canonical, uid)

            # Cache 1:1 DM tokens by user ID (for notification fallback)
            if conv_type == 1:
                other_user = conv.get("name", "")
                if other_user and other_user in config.users:
                    _dm_token_cache[other_user] = conversation_token

            # Get last known message ID for this conversation
            last_message_id = db.get_talk_poll_state(conn, conversation_token)

            # First-time poll behavior depends on conversation type
            if last_message_id is None:
                if conv_type == 1:
                    # DM: fetch recent messages - the DM is initiated by messaging the bot,
                    # so there's no historical spam risk. Use 0 to trigger history fetch.
                    last_message_id = 0
                    logger.debug("First poll for DM %s - fetching message history", conversation_token)
                else:
                    # Group/public room: initialize to latest_id - 1 so the immediate
                    # poll picks up the most recent message (avoids missing the first
                    # message that triggered bot being added to the room)
                    try:
                        latest_id = await client.get_latest_message_id(conversation_token)
                        if latest_id:
                            last_message_id = latest_id - 1
                            logger.debug("First poll for room %s - starting from message %d", conversation_token, last_message_id)
                        else:
                            last_message_id = 0
                            logger.debug("First poll for room %s - no messages yet", conversation_token)
                    except Exception as e:
                        logger.error("Error initializing poll state for %s: %s", conversation_token, e)
                        continue

            # Backfill cache on first encounter
            if not db.has_cached_talk_messages(conn, conversation_token):
                try:
                    backfill_msgs = await client.fetch_chat_history(
                        conversation_token, limit=config.conversation.talk_context_limit,
                    )
                    if backfill_msgs:
                        db.upsert_talk_messages(conn, conversation_token, backfill_msgs)
                        logger.info(
                            "Backfilled %d messages for conversation %s",
                            len(backfill_msgs), conversation_token,
                        )
                except Exception as e:
                    logger.warning(
                        "Backfill failed for %s: %s — context will build from polling",
                        conversation_token, e,
                    )

            # Add to concurrent poll list
            poll_tasks.append(
                _poll_single_conversation(
                    client,
                    conversation_token,
                    last_message_id,
                    config.scheduler.talk_poll_timeout,
                )
            )

        # Reconcile the unified registry against Nextcloud: a Talk room the bot
        # is no longer in (deleted in NC, or bot removed) drops out of the
        # conversation list, so archive its registry row — otherwise it keeps
        # surfacing in the web room list forever. `conversations` is the bot's
        # *complete* room list; only reconcile when it's non-empty so a transient
        # empty/failed fetch can't mass-archive every room.
        live_talk_tokens = {
            c.get("token") for c in conversations if c.get("token")
        }
        if live_talk_tokens:
            n = db.archive_orphaned_talk_rooms(conn, live_talk_tokens)
            if n:
                logger.info("Archived %d Talk room(s) no longer in Nextcloud", n)

    if not poll_tasks:
        return []

    # Poll all conversations concurrently using long-poll for responsiveness.
    # FIRST_COMPLETED preserves instant detection (server responds immediately
    # when a message arrives) while not blocking on quiet rooms.  Once any room
    # responds, give remaining rooms a brief grace period then move on.
    tasks = [asyncio.create_task(t) for t in poll_tasks]
    done, pending = await asyncio.wait(
        tasks,
        timeout=config.scheduler.talk_poll_timeout,
        return_when=asyncio.FIRST_COMPLETED,
    )

    # If a room responded and others are still long-polling, give them a
    # short window to return before cancelling (covers near-simultaneous msgs)
    if done and pending:
        more_done, pending = await asyncio.wait(
            pending, timeout=config.scheduler.talk_poll_wait,
        )
        done |= more_done

    for t in pending:
        t.cancel()
    # Suppress CancelledError from cancelled tasks
    await asyncio.gather(*pending, return_exceptions=True)
    results = [t.result() for t in done]

    # Process results
    with db.get_db(config.db_path) as conn:
        for conversation_token, messages in results:
            if not messages:
                continue

            # Store all messages in cache (system, bot, user — context builder filters)
            db.upsert_talk_messages(conn, conversation_token, messages)

            # Process messages in order (oldest first)
            for msg in messages:
                message_id = msg.get("id")
                actor_id = msg.get("actorId", "")  # Nextcloud username
                actor_type = msg.get("actorType", "")
                message_type = msg.get("messageType", "")

                # Update poll state to this message
                if message_id:
                    db.set_talk_poll_state(conn, conversation_token, message_id)

                # Skip system messages
                if message_type == "system":
                    continue

                # Skip the Talk echo of a web-origin user turn the web process
                # posted *as the user* (post-as-user mirroring). The marker
                # travels inside the message payload, so this works even when
                # the long-poll delivers the echo before the external-id stamp
                # lands in the DB. The message is user-authored (the bot-own
                # filter below can't catch it); the poll cursor has already
                # advanced and the talk_messages cache upsert above kept it —
                # the turn is legitimately part of the conversation context.
                reference_id = msg.get("referenceId") or ""
                if isinstance(reference_id, str) and reference_id.startswith(
                    WEBMIRROR_REF_PREFIX
                ):
                    logger.debug(
                        "Skipping web-mirror echo in %s (ref=%s)",
                        conversation_token, reference_id,
                    )
                    continue

                # Skip bot's own messages
                if actor_id == config.talk.bot_username:
                    continue

                # Only process messages from users (not guests, bots, etc.)
                if actor_type != "users":
                    continue

                # Check if sender is a configured user
                if actor_id not in config.users:
                    # Unknown user - skip silently
                    continue

                # Re-engagement un-hides: any message the user posts in a room
                # they'd hidden clears their dismissal tombstone (and re-adds
                # their membership), so it resurfaces in their web list — even
                # in a multi-user room where the message is dropped just below
                # for lacking an @mention (so record_inbound is never reached).
                # Resolve to the canonical token so a promoted web room works.
                reengaged_token = (
                    db.resolve_room_token(conn, "talk", conversation_token)
                    or conversation_token
                )
                if db.get_room(conn, reengaged_token) is not None:
                    db.add_room_member(conn, reengaged_token, actor_id)
                    db.undismiss_room(conn, reengaged_token, actor_id)

                # In multi-user rooms, only respond when @mentioned
                conv_type = conv_types.get(conversation_token, 1)
                participants = await _get_participants(client, conversation_token, conv_type)
                is_multi_user = _is_multi_user(participants)
                if is_multi_user and not is_bot_mentioned(msg, config.talk.bot_username):
                    logger.debug(
                        "Skipping message from %s in multi-user room %s (no @mention)",
                        actor_id, conversation_token,
                    )
                    continue

                # Extract message content and attachments
                # In multi-user rooms, strip bot mention from prompt and resolve other mentions
                content = clean_message_content(
                    msg,
                    bot_username=config.talk.bot_username if is_multi_user else None,
                )
                attachments = extract_attachments(msg)

                # !model prefix — strip and capture per-task overrides before
                # dispatching commands or creating the task. Unknown alias →
                # post usage and stop; valid alias → carry overrides forward.
                # The active brain owns the alias namespace.
                model_override: str | None = None
                effort_override: str | None = None
                from ...brain import make_brain
                from ...commands import (
                    dispatch as dispatch_command,
                    resolve_model_prefix,
                )

                # !model prefix — strip and capture per-task overrides via the
                # shared cross-surface helper. Empty remainder is only an error
                # when there's nothing to do at all; with attachments present
                # "!model opus" is a valid "process this attachment" intent.
                brain = make_brain(config.brain)
                prefix = resolve_model_prefix(
                    content, brain, has_attachments=bool(attachments),
                )
                if prefix.usage is not None:
                    await client.send_message(conversation_token, prefix.usage)
                    continue
                if prefix.matched:
                    model_override = prefix.model
                    effort_override = prefix.effort
                    content = prefix.content

                # !command dispatch — intercept before task creation
                if content.strip().startswith("!"):
                    result = await dispatch_command(
                        config, actor_id, conversation_token, content,
                        surface="talk", conn=conn,
                    )
                    if result.handled:
                        continue

                # Extract reply metadata (before confirmation check so we can
                # match reply-to-specific confirmation prompts)
                reply_to_talk_id = None
                reply_to_content = None
                parent = msg.get("parent")
                if isinstance(parent, dict) and parent.get("id") and not parent.get("deleted"):
                    reply_to_talk_id = parent["id"]
                    # Store parent message content as fallback
                    parent_content = parent.get("message", "")
                    if parent_content:
                        reply_to_content = parent_content[:1000]

                # Check if this is a confirmation reply before creating a new task
                handled = await handle_confirmation_reply(
                    conn, config, actor_id, content, conversation_token,
                    reply_to_talk_id=reply_to_talk_id,
                )
                if handled:
                    continue

                # Per-channel gate: notify user if there's already an active fg task
                # but still queue the message (fall through to task creation)
                if db.has_active_foreground_task_for_channel(conn, conversation_token):
                    logger.debug(
                        "Channel gate: active fg task in %s, queuing message from %s",
                        conversation_token, actor_id,
                    )
                    try:
                        await client.send_message(
                            conversation_token,
                            "Still working on a previous request — I'll be with you shortly.",
                        )
                    except Exception as e:
                        logger.debug("Failed to send channel gate message: %s", e)

                # Skip empty messages (file-only shares have empty content)
                if not content.strip() and not attachments:
                    continue

                # Build prompt
                prompt = content.strip() if content.strip() else "Process the attached file(s)"

                # For group chats, prepend participant context so the bot
                # knows who else is in the room
                if is_multi_user and participants:
                    other_names = _participant_names(participants, exclude=config.talk.bot_username)
                    if other_names:
                        prompt = f"[Room participants: {', '.join(other_names)}]\n{prompt}"

                # Cancel any pending confirmations in this conversation —
                # the user has moved on by sending a new message
                cancelled = db.cancel_pending_confirmations(
                    conn, conversation_token, actor_id,
                )
                if cancelled:
                    logger.info(
                        "Cancelled %d pending confirmation(s) in %s for %s (new message)",
                        cancelled, conversation_token, actor_id,
                    )

                # Normalize into an IncomingMessage and create the task in the
                # SAME transaction as the poll-state advance above — see the
                # docstring's atomicity note.
                task_id = ingest_message(conn, config, IncomingMessage(
                    user_id=actor_id,
                    text=prompt,
                    source_type="talk",
                    surface="talk",
                    channel_token=conversation_token,
                    channel_name=conv_names.get(conversation_token),
                    is_group_chat=is_multi_user,
                    attachments=attachments if attachments else [],
                    platform_message_id=message_id,
                    reply_to_message_id=reply_to_talk_id,
                    reply_to_content=reply_to_content,
                    model=model_override,
                    effort=effort_override,
                    model_prefix_used=prefix.matched,
                ))
                if task_id is not None:
                    created.append(task_id)

    return created


async def handle_confirmation_reply(
    conn,
    config: Config,
    actor_id: str,
    content: str,
    conversation_token: str,
    reply_to_talk_id: int | None = None,
) -> bool:
    """
    Check if a message is a confirmation reply to a pending task.

    Three-path lookup:
    1. Reply to specific confirmation message (by talk_response_id)
    2. Same-conversation confirmation (existing behavior)
    3. Cross-conversation fallback by user_id (for email gates)

    Returns True if the message was handled as a confirmation.
    """
    # Check for affirmative/negative responses
    content_lower = content.strip().lower()
    # "yes trust" variants: confirm + add sender to trusted list
    trust_sender = content_lower in ("yes trust", "yes, trust", "y trust")
    affirmative = trust_sender or content_lower in ("yes", "y", "ok", "okay", "proceed", "confirm", "do it", "go ahead")
    negative = content_lower in ("no", "n", "cancel", "abort", "stop", "don't", "nevermind")

    if not (affirmative or negative):
        return False

    # Path A: reply to specific confirmation message
    pending_task = None
    if reply_to_talk_id:
        pending_task = db.get_pending_confirmation_by_response_id(conn, reply_to_talk_id)

    # Path B: same-conversation confirmation (existing behavior)
    if not pending_task:
        pending_task = db.get_pending_confirmation(conn, conversation_token)

    # Path C: cross-conversation fallback by user_id (email gates)
    if not pending_task:
        pending_task = db.get_pending_confirmation_for_user(conn, actor_id)

    if not pending_task:
        return False

    # Verify the reply is from the same user who owns the pending task
    if pending_task.user_id != actor_id:
        return False

    if affirmative:
        # Confirm the task - return to pending status for execution
        db.confirm_task(conn, pending_task.id)
        db.log_task(conn, pending_task.id, "info", "User confirmed task")

        # Trust the sender if requested and this is an email task
        if trust_sender and pending_task.source_type == "email":
            email_record = db.get_email_for_task(conn, pending_task.id)
            if email_record:
                db.add_trusted_sender(conn, actor_id, email_record.sender_email)
                db.log_task(
                    conn, pending_task.id, "info",
                    f"Trusted sender: {email_record.sender_email}",
                )
                try:
                    client = get_talk_client(config)
                    await client.send_message(
                        conversation_token,
                        f"Trusted {email_record.sender_email} — future emails will be processed automatically.",
                    )
                except Exception:
                    pass
    else:
        # Cancel the task
        db.cancel_task(conn, pending_task.id)
        db.log_task(conn, pending_task.id, "info", "User cancelled task")

        # Notify user
        try:
            client = get_talk_client(config)
            await client.send_message(conversation_token, "Task cancelled.")
        except Exception:
            pass

    return True
