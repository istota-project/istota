"""Conversation context selection using Claude CLI."""

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .db import ConversationMessage, TalkMessage
from .talk import clean_message_content

logger = logging.getLogger("istota.context")


def select_relevant_context(
    current_prompt: str,
    history: list[ConversationMessage],
    config: Config,
) -> list[ConversationMessage]:
    """
    Select which previous messages are relevant to the current request.

    Hybrid approach:
    - Recent N messages (always_include_recent) are always included without selection.
    - Older messages beyond that are triaged by a selection model.
    - If selection is disabled or history is short, all messages are included.

    Returns a filtered list of ConversationMessages in chronological order.
    On any selection error, falls back to the guaranteed recent messages.
    """
    if not history:
        return []

    # If selection is disabled, include all messages
    if not config.conversation.use_selection:
        logger.debug("Selection disabled, including all %d messages", len(history))
        return history

    # Skip selection for short histories - include all messages
    threshold = config.conversation.skip_selection_threshold
    if len(history) <= threshold:
        logger.debug(
            "Short history (%d msgs ≤ %d), including all",
            len(history),
            threshold,
        )
        return history

    # Split history into guaranteed recent and older triageable messages
    recent_count = config.conversation.always_include_recent
    if recent_count >= len(history):
        logger.debug(
            "History (%d msgs) within always_include_recent (%d), including all",
            len(history),
            recent_count,
        )
        return history

    guaranteed_recent = history[-recent_count:] if recent_count > 0 else []
    older_history = history[:-recent_count] if recent_count > 0 else history

    # If no older messages to triage, just return the guaranteed recent
    if not older_history:
        return guaranteed_recent

    # Triage older messages with the selection model
    selected_older = _triage_older_messages(
        current_prompt, older_history, config
    )

    # Combine: selected older + guaranteed recent (chronological order)
    selected = selected_older + guaranteed_recent

    logger.info(
        "Context: %d triaged + %d recent = %d/%d messages",
        len(selected_older),
        len(guaranteed_recent),
        len(selected),
        len(history),
    )
    return selected


def _triage_older_messages(
    current_prompt: str,
    older_history: list[ConversationMessage],
    config: Config,
) -> list[ConversationMessage]:
    """Run the selection model to triage older messages. Returns selected messages in order."""

    def _format_triage_msg(i: int, msg: ConversationMessage) -> str:
        ts = msg.created_at[:16] if msg.created_at else "unknown"
        speaker = msg.user_id if msg.user_id else "User"
        lines = f"[{i}] ({ts}) {speaker}: {msg.prompt}\nBot: {msg.result}"
        if msg.actions_taken:
            actions_line = _format_actions_line(msg.actions_taken)
            if actions_line:
                lines += f"\n{actions_line}"
        return lines

    history_text = "\n\n".join(
        _format_triage_msg(i, msg)
        for i, msg in enumerate(older_history)
    )

    selection_prompt = f"""You are helping select relevant conversation context for a chatbot.

Current user request:
{current_prompt}

OLDER messages from this conversation (the {config.conversation.always_include_recent} most recent messages are already included separately):

{history_text}

Which of these OLDER messages contain information relevant to understanding or answering the current request?

NOTE: The {config.conversation.always_include_recent} most recent messages are already included. Select which of these older messages also provide useful context.

Respond with ONLY a JSON object in this exact format:
{{"relevant_ids": [0, 2, 5]}}

Use an empty array if none of these older messages are relevant: {{"relevant_ids": []}}

Rules:
- When in doubt, INCLUDE the message — more context is better than missing context
- Include messages that could help answer or provide background for the current request
- Include messages that establish context, preferences, or facts the user might be referring to
- Include messages about ongoing topics, even if not directly referenced
- Only exclude messages that are clearly unrelated (different topic, fully resolved, trivial small talk)
- Respond with ONLY the JSON, no other text"""

    try:
        result = subprocess.run(
            [
                "claude",
                "-p", "-",
                "--model", config.conversation.selection_model,
            ],
            input=selection_prompt,
            capture_output=True,
            text=True,
            timeout=config.conversation.selection_timeout,
        )

        if result.returncode != 0:
            logger.warning(
                "Context triage failed (returncode=%d): %s",
                result.returncode,
                result.stderr or result.stdout,
            )
            return []

        # Parse JSON response — extract from code blocks or raw JSON anywhere in output
        output = result.stdout.strip()

        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", output, re.DOTALL)
        if code_block:
            output = code_block.group(1).strip()
        else:
            json_match = re.search(r"\{.*\}", output, re.DOTALL)
            if json_match:
                output = json_match.group(0)

        data = json.loads(output)
        relevant_ids = data.get("relevant_ids", [])

        if not isinstance(relevant_ids, list):
            logger.warning("Context triage returned invalid format: %s", data)
            return []

        # Filter to valid integer indices, preserve chronological order
        valid_ids = [idx for idx in relevant_ids if isinstance(idx, int) and 0 <= idx < len(older_history)]
        selected = [older_history[idx] for idx in sorted(valid_ids)]

        logger.debug(
            "Triage selected %d/%d older messages (ids: %s)",
            len(selected),
            len(older_history),
            relevant_ids,
        )
        return selected

    except subprocess.TimeoutExpired:
        logger.warning("Context triage timed out after %.1fs", config.conversation.selection_timeout)
        return []
    except json.JSONDecodeError as e:
        logger.warning("Context triage JSON parse error: %s (output: %s)", e, output[:200] if 'output' in dir() else "N/A")
        return []
    except FileNotFoundError:
        logger.error("Claude CLI not found for context triage")
        return []
    except Exception as e:
        logger.warning("Context triage error: %s", e)
        return []


def format_context_for_prompt(messages: list[ConversationMessage], truncation: int = 3000) -> str:
    """Format selected context messages for inclusion in the prompt.

    Args:
        messages: Conversation messages to format.
        truncation: Max chars per bot response. 0 to disable truncation.
    """
    if not messages:
        return ""

    # Source types that represent scheduled/background tasks — not real user messages
    _SCHEDULED_SOURCE_TYPES = {"scheduled", "cron", "briefing", "heartbeat"}

    formatted = []
    for msg in messages:
        # Use shorter timestamp format
        timestamp = msg.created_at[:16] if msg.created_at else "unknown"
        source_type = getattr(msg, "source_type", "talk") or "talk"
        if source_type in _SCHEDULED_SOURCE_TYPES:
            speaker = "Scheduled"
        elif msg.user_id:
            speaker = msg.user_id
        else:
            speaker = "User"
        formatted.append(f"[{timestamp}] {speaker}: {msg.prompt}")
        result = msg.result
        if truncation > 0 and len(result) > truncation:
            result = result[:truncation] + "...[truncated]"
        formatted.append(f"[{timestamp}] Bot: {result}")

        # Append compact actions summary if available
        if msg.actions_taken:
            actions_line = _format_actions_line(msg.actions_taken)
            if actions_line:
                formatted.append(actions_line)

    return "\n".join(formatted)


_MAX_ACTIONS = 15


def _format_actions_line(actions_json: str) -> str | None:
    """Format actions_taken JSON into a compact summary line."""
    try:
        actions: list[Any] = json.loads(actions_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not actions:
        return None
    display = actions[:_MAX_ACTIONS]
    suffix = f" +{len(actions) - _MAX_ACTIONS} more" if len(actions) > _MAX_ACTIONS else ""
    return f"[Actions: {' | '.join(str(a) for a in display)}{suffix}]"


# ---------------------------------------------------------------------------
# Talk API-based context pipeline
# ---------------------------------------------------------------------------

_REFERENCE_ID_PATTERN = re.compile(r"^istota:task:(\d+):(\w+)$")

_SCHEDULED_SOURCE_TYPES = {"scheduled", "cron", "briefing", "heartbeat"}


def _parse_reference_id(ref_id: str | None) -> tuple[int | None, str | None]:
    """Parse an istota referenceId string.

    Returns (task_id, tag) where tag is "result", "ack", or "progress".
    Returns (None, None) for non-matching or missing referenceIds.
    """
    if not ref_id:
        return None, None
    m = _REFERENCE_ID_PATTERN.match(ref_id)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def build_talk_context(
    raw_messages: list[dict],
    bot_username: str,
    task_metadata: dict[int, dict],
) -> list[TalkMessage]:
    """Convert raw Talk API messages into filtered TalkMessage list.

    Args:
        raw_messages: Messages from TalkClient.fetch_chat_history() (oldest-first).
        bot_username: Bot's Nextcloud username for identifying bot messages.
        task_metadata: Dict from get_task_metadata_for_context() mapping
            task_id -> {"actions_taken": ..., "source_type": ...}.

    Returns filtered list of TalkMessages (oldest-first), excluding system
    messages, deleted messages, ack messages, and progress messages.
    """
    result = []
    for msg in raw_messages:
        # Skip system messages
        if msg.get("messageType") == "system":
            continue

        # Skip deleted messages
        if msg.get("deleted"):
            continue

        ref_id = msg.get("referenceId") or None
        task_id, tag = _parse_reference_id(ref_id)

        # Skip ack and progress messages
        if tag in ("ack", "progress"):
            continue

        actor_id = msg.get("actorId", "")
        is_bot = actor_id == bot_username

        # Clean content (resolve placeholders)
        content = clean_message_content(msg, bot_username=bot_username if not is_bot else None)

        # Determine message role and enrich with task metadata
        actions_taken = None
        message_role = "user"
        if is_bot:
            message_role = "bot_result"
            if task_id and task_id in task_metadata:
                meta = task_metadata[task_id]
                actions_taken = meta.get("actions_taken")
                source_type = meta.get("source_type", "")
                if source_type in _SCHEDULED_SOURCE_TYPES:
                    message_role = "scheduled"

        result.append(TalkMessage(
            message_id=msg.get("id", 0),
            actor_id=actor_id,
            actor_display_name=msg.get("actorDisplayName", actor_id),
            is_bot=is_bot,
            content=content,
            timestamp=msg.get("timestamp", 0),
            actions_taken=actions_taken,
            message_role=message_role,
            task_id=task_id,
        ))

    return result


def select_relevant_talk_context(
    current_prompt: str,
    messages: list[TalkMessage],
    config: "Config",
) -> list[TalkMessage]:
    """Select relevant Talk messages for context, mirroring select_relevant_context().

    Uses the same hybrid approach: guaranteed recent messages + LLM triage of older.
    The Talk API may fetch many messages (talk_context_limit), but we only triage
    the most recent `lookback_count` to keep the selection prompt manageable.
    """
    if not messages:
        return []

    if not config.conversation.use_selection:
        return messages

    threshold = config.conversation.skip_selection_threshold
    if len(messages) <= threshold:
        return messages

    # Limit to lookback_count for triage (same as DB path)
    lookback = config.conversation.lookback_count
    if len(messages) > lookback:
        messages = messages[-lookback:]

    recent_count = config.conversation.always_include_recent
    if recent_count >= len(messages):
        return messages

    guaranteed_recent = messages[-recent_count:] if recent_count > 0 else []
    older = messages[:-recent_count] if recent_count > 0 else messages

    if not older:
        return guaranteed_recent

    selected_older = _triage_older_talk_messages(current_prompt, older, config)
    selected = selected_older + guaranteed_recent

    logger.info(
        "Talk context: %d triaged + %d recent = %d/%d messages",
        len(selected_older), len(guaranteed_recent), len(selected), len(messages),
    )
    return selected


def _triage_older_talk_messages(
    current_prompt: str,
    older: list[TalkMessage],
    config: "Config",
) -> list[TalkMessage]:
    """Run the selection model to triage older Talk messages."""

    def _format_msg(i: int, msg: TalkMessage) -> str:
        ts = datetime.fromtimestamp(msg.timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        speaker = "Bot" if msg.is_bot else msg.actor_id
        lines = f"[{i}] ({ts}) {speaker}: {msg.content}"
        if msg.actions_taken:
            actions_line = _format_actions_line(msg.actions_taken)
            if actions_line:
                lines += f"\n{actions_line}"
        return lines

    history_text = "\n\n".join(_format_msg(i, msg) for i, msg in enumerate(older))

    selection_prompt = f"""You are helping select relevant conversation context for a chatbot.

Current user request:
{current_prompt}

OLDER messages from this conversation (the {config.conversation.always_include_recent} most recent messages are already included separately):

{history_text}

Which of these OLDER messages contain information relevant to understanding or answering the current request?

NOTE: The {config.conversation.always_include_recent} most recent messages are already included. Select which of these older messages also provide useful context.

Respond with ONLY a JSON object in this exact format:
{{"relevant_ids": [0, 2, 5]}}

Use an empty array if none of these older messages are relevant: {{"relevant_ids": []}}

Rules:
- When in doubt, INCLUDE the message — more context is better than missing context
- Include messages that could help answer or provide background for the current request
- Include messages that establish context, preferences, or facts the user might be referring to
- Include messages about ongoing topics, even if not directly referenced
- Only exclude messages that are clearly unrelated (different topic, fully resolved, trivial small talk)
- Respond with ONLY the JSON, no other text"""

    try:
        result = subprocess.run(
            ["claude", "-p", "-", "--model", config.conversation.selection_model],
            input=selection_prompt,
            capture_output=True,
            text=True,
            timeout=config.conversation.selection_timeout,
        )

        if result.returncode != 0:
            logger.warning("Talk context triage failed (rc=%d)", result.returncode)
            return []

        output = result.stdout.strip()
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", output, re.DOTALL)
        if code_block:
            output = code_block.group(1).strip()
        else:
            json_match = re.search(r"\{.*\}", output, re.DOTALL)
            if json_match:
                output = json_match.group(0)

        data = json.loads(output)
        relevant_ids = data.get("relevant_ids", [])
        if not isinstance(relevant_ids, list):
            return []

        valid_ids = [idx for idx in relevant_ids if isinstance(idx, int) and 0 <= idx < len(older)]
        selected = [older[idx] for idx in sorted(valid_ids)]
        logger.debug("Talk triage selected %d/%d older messages", len(selected), len(older))
        return selected

    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception) as e:
        logger.warning("Talk context triage error: %s", e)
        return []


def format_talk_context_for_prompt(
    messages: list[TalkMessage],
    truncation: int = 3000,
) -> str:
    """Format Talk messages for inclusion in the prompt.

    Individual message format (not paired), showing all participants.
    """
    if not messages:
        return ""

    formatted = []
    for msg in messages:
        ts = datetime.fromtimestamp(msg.timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        if msg.is_bot:
            speaker = "Bot"
            content = msg.content
            if truncation > 0 and len(content) > truncation:
                content = content[:truncation] + "...[truncated]"
            formatted.append(f"[{ts}] {speaker}: {content}")
            if msg.actions_taken:
                actions_line = _format_actions_line(msg.actions_taken)
                if actions_line:
                    formatted.append(actions_line)
        else:
            if msg.message_role == "scheduled":
                speaker = "Scheduled"
            else:
                speaker = msg.actor_id or "User"
            formatted.append(f"[{ts}] {speaker}: {msg.content}")

    return "\n".join(formatted)
