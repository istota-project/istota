"""Talk conversation polling and task creation."""

import asyncio
import logging
import time

from . import db
from .config import Config
from .talk import TalkClient, clean_message_content

logger = logging.getLogger("istota.talk_poller")


# Participant cache: token -> (participants list, timestamp)
_participant_cache: dict[str, tuple[list[dict], float]] = {}
_PARTICIPANT_CACHE_TTL = 300  # 5 minutes


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
                # Files shared in Talk are accessible in the bot's Talk folder
                attachments.append(f"Talk/{filename}")

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

    Uses asyncio.wait() with a timeout so fast rooms are processed immediately
    without waiting for slow (quiet) rooms to finish their long-poll.

    Returns list of created task IDs.
    """
    if not config.talk.enabled:
        return []

    if not config.nextcloud.url:
        return []

    client = TalkClient(config)
    created_tasks = []

    # Get all conversations the bot is part of
    try:
        conversations = await client.list_conversations()
    except Exception as e:
        logger.error("Error listing Talk conversations: %s: %s", type(e).__name__, e)
        return []

    # Build list of conversations to poll and initialize new ones
    poll_tasks = []
    conv_types: dict[str, int] = {}  # token -> conversation type
    with db.get_db(config.db_path) as conn:
        for conv in conversations:
            conversation_token = conv.get("token")
            if not conversation_token:
                continue

            # Conversation types: 1=one-to-one (DM), 2=group, 3=public, 4=changelog
            conv_type = conv.get("type")
            conv_types[conversation_token] = conv_type

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

                # !command dispatch — intercept before task creation
                if content.strip().startswith("!"):
                    from .commands import dispatch as dispatch_command

                    handled = await dispatch_command(
                        config, conn, actor_id, conversation_token, content
                    )
                    if handled:
                        continue

                # Check if this is a confirmation reply before creating a new task
                handled = await handle_confirmation_reply(
                    conn, config, actor_id, content, conversation_token
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

                # Extract reply metadata
                reply_to_talk_id = None
                reply_to_content = None
                parent = msg.get("parent")
                if isinstance(parent, dict) and parent.get("id") and not parent.get("deleted"):
                    reply_to_talk_id = parent["id"]
                    # Store parent message content as fallback
                    parent_content = parent.get("message", "")
                    if parent_content:
                        reply_to_content = parent_content[:1000]

                # Create task
                task_id = db.create_task(
                    conn,
                    prompt=prompt,
                    user_id=actor_id,
                    source_type="talk",
                    conversation_token=conversation_token,
                    is_group_chat=is_multi_user,
                    attachments=attachments if attachments else None,
                    talk_message_id=message_id,
                    reply_to_talk_id=reply_to_talk_id,
                    reply_to_content=reply_to_content,
                )

                created_tasks.append(task_id)

    return created_tasks


async def handle_confirmation_reply(
    conn,
    config: Config,
    actor_id: str,
    content: str,
    conversation_token: str,
) -> bool:
    """
    Check if a message is a confirmation reply to a pending task.

    Returns True if the message was handled as a confirmation.
    """
    # Check for affirmative/negative responses
    content_lower = content.strip().lower()
    affirmative = content_lower in ("yes", "y", "ok", "okay", "proceed", "confirm", "do it", "go ahead")
    negative = content_lower in ("no", "n", "cancel", "abort", "stop", "don't", "nevermind")

    if not (affirmative or negative):
        return False

    # Find pending confirmation task for this conversation
    pending_task = db.get_pending_confirmation(conn, conversation_token)

    if not pending_task:
        return False

    # Verify the reply is from the same user who owns the pending task
    if pending_task.user_id != actor_id:
        return False

    if affirmative:
        # Confirm the task - return to pending status for execution
        db.confirm_task(conn, pending_task.id)
        db.log_task(conn, pending_task.id, "info", "User confirmed task")
    else:
        # Cancel the task
        db.cancel_task(conn, pending_task.id)
        db.log_task(conn, pending_task.id, "info", "User cancelled task")

        # Notify user
        try:
            client = TalkClient(config)
            await client.send_message(conversation_token, "Task cancelled.")
        except Exception:
            pass

    return True
