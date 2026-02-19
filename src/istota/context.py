"""Conversation context selection using Claude CLI."""

import json
import logging
import re
import subprocess
from .config import Config
from .db import ConversationMessage

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
