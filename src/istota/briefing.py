"""Briefing prompt builder."""

import hashlib
import html
import logging
import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import BriefingConfig, Config

logger = logging.getLogger("istota.briefing")


def _component_enabled(components: dict, key: str) -> bool:
    """Check if a briefing component is enabled (supports both bool and dict forms)."""
    value = components.get(key)
    if value is True:
        return True
    if isinstance(value, dict):
        return value.get("enabled", False)
    return False


def _strip_html(text: str) -> str:
    """
    Strip HTML tags and decode entities from text.

    Converts HTML to plain text by:
    - Adding newlines for block elements (p, div, br, li, tr)
    - Removing all HTML tags
    - Decoding HTML entities
    - Removing invisible Unicode characters (nbsp, zero-width spaces, etc.)
    - Normalizing whitespace
    """
    if not text:
        return text

    # Remove <style> tags and their contents
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)

    # Remove @media CSS blocks that appear as raw text (after partial stripping)
    text = re.sub(r'@media[^{]*\{(?:[^{}]*\{[^}]*\})*[^}]*\}', '', text, flags=re.DOTALL)

    # Remove tracking pixel images (zero-size or 1x1 images)
    text = re.sub(r'<img[^>]*(?:width\s*=\s*["\']?[01]["\']?|height\s*=\s*["\']?[01]["\']?)[^>]*/?\s*>', '', text, flags=re.IGNORECASE)

    # Add newlines before block elements to preserve structure
    text = re.sub(r'<(p|div|br|li|tr|h[1-6])[^>]*>', r'\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|div|li|tr|h[1-6])>', r'\n', text, flags=re.IGNORECASE)

    # Remove all HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Decode HTML entities
    text = html.unescape(text)

    # Remove invisible Unicode characters commonly used in newsletters:
    # \u00a0 = non-breaking space (nbsp)
    # \u200b = zero-width space
    # \u200c = zero-width non-joiner
    # \u200d = zero-width joiner
    # \ufeff = byte order mark / zero-width no-break space
    # \u2060 = word joiner
    # \u00ad = soft hyphen
    # \u2007 = figure space
    # \u2008 = punctuation space
    # \u2009 = thin space
    # \u200a = hair space
    # \u202f = narrow no-break space
    invisible_chars = '\u00a0\u200b\u200c\u200d\ufeff\u2060\u00ad\u2007\u2008\u2009\u200a\u202f'
    text = re.sub(f'[{invisible_chars}]+', ' ', text)

    # Normalize whitespace: collapse multiple spaces/tabs but preserve newlines
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)

    # Collapse multiple newlines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def build_briefing_prompt(
    briefing: BriefingConfig,
    user_id: str,
    config: Config,
    user_timezone: str = "UTC",
) -> str:
    """
    Build an enhanced briefing prompt with pre-fetched data.

    Args:
        briefing: The briefing configuration from config.toml
        user_id: The user's ID
        config: Application configuration
        user_timezone: User's timezone string (e.g., "America/Los_Angeles")

    Returns:
        Complete prompt string for Claude Code execution
    """
    # Get current time in user's timezone
    try:
        tz = ZoneInfo(user_timezone)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    time_str = now.strftime("%Y-%m-%d %H:%M")

    # Determine briefing mode based on hour
    is_morning = now.hour < 12
    mode = "morning" if is_morning else "evening"

    components = briefing.components
    is_weekend = now.weekday() in (5, 6)
    prompt_parts = [
        f"Generate a {mode} briefing for user {user_id}.",
        f"Current time: {time_str} ({user_timezone})",
        "",
        "Include the following components:",
    ]

    # Market data - pre-fetch and include (skip quotes on weekends)
    has_market_quotes = False
    if _component_enabled(components, "markets") and not is_weekend:
        market_config = components["markets"] if isinstance(components.get("markets"), dict) else {}
        market_data = _fetch_market_data(market_config, mode)
        if market_data:
            prompt_parts.append("")
            prompt_parts.append(market_data)
            prompt_parts.append(
                "Use ONLY these yfinance quotes for the MARKETS quote lines. "
                "Do NOT substitute prices or percentages from newsletters."
            )
            has_market_quotes = True
            logger.debug("Fetched market data for %s briefing", mode)
    elif _component_enabled(components, "markets") and is_weekend:
        logger.debug("Skipping market quotes on weekend")

    # FinViz market data - enrich evening briefings with headlines, movers, etc.
    if _component_enabled(components, "markets") and not is_morning and not is_weekend:
        finviz_content = _fetch_finviz_market_data()
        if finviz_content:
            prompt_parts.append("")
            prompt_parts.append(finviz_content)
            prompt_parts.append(
                "Include FinViz data in the MARKETS section: market headlines first, "
                "then the yfinance close prices above (if available), then movers, futures, "
                "forex/bonds, economic data, and upcoming earnings. "
                "Use the pre-formatted FinViz sections as-is."
            )
            logger.debug("Fetched FinViz market data for evening briefing")

    # Newsletter emails - pre-fetch full content
    if _component_enabled(components, "news"):
        news_config = components["news"] if isinstance(components.get("news"), dict) else {}
        newsletter_content = _fetch_newsletter_content(news_config, config)
        if newsletter_content:
            prompt_parts.append("")
            prompt_parts.append(newsletter_content)
            prompt_parts.append("")
            quote_note = (
                " The MARKETS quote lines are already provided above from yfinance — "
                "do not replace them with numbers from newsletters."
                if has_market_quotes
                else ""
            )
            prompt_parts.append(
                "Summarize these newsletters. Place general/world news stories in the NEWS section "
                "and market/economic stories in the MARKETS section (after any quote data). "
                f"See the briefing skill for section format and story count targets.{quote_note}"
            )

    # Calendar events - pre-fetch with correct timezone
    if components.get("calendar"):
        calendar_content = _fetch_calendar_events(config, user_id, is_morning, user_timezone)
        if calendar_content:
            prompt_parts.append("")
            prompt_parts.append(calendar_content)
        else:
            # Fallback to agent-fetched if pre-fetch fails
            if is_morning:
                prompt_parts.append("- Today's calendar events")
            else:
                prompt_parts.append("- Tomorrow's calendar events")

    # TODO items - pre-fetch content
    if components.get("todos"):
        todo_content = _fetch_todo_items(config, user_id)
        if todo_content:
            prompt_parts.append("")
            prompt_parts.append(todo_content)
        else:
            prompt_parts.append("- Pending TODO items from their TODO file")

    # Notes files
    if _component_enabled(components, "notes"):
        prompt_parts.append(
            "- Read any shared notes files and include relevant agenda items or reminders"
        )

    # Email summary (general, not newsletters)
    if components.get("email"):
        prompt_parts.append("- Summary of unread emails")

    # Daily reminder - pre-fetch random reminder
    if _component_enabled(components, "reminders"):
        reminder = _fetch_random_reminder(config, user_id)
        if reminder:
            prompt_parts.append("")
            prompt_parts.append("## Daily Reminder (pre-selected)")
            prompt_parts.append(reminder)
            prompt_parts.append("")
            prompt_parts.append(
                "Use this EXACT text as the 💡 REMINDER section at the end of the briefing. "
                "Do NOT generate, paraphrase, or substitute your own reminder."
            )

    prompt_parts.append("")
    prompt_parts.append(
        "Format the briefing following the section format in the briefing skill reference. "
        "Use emoji-prefixed labels as section headers (not markdown headings). "
        "Output sections in the exact order shown in the briefing skill. "
        "Only include sections that have data. NO tables. "
        "CRITICAL: Your response must start with the first emoji section header (e.g. 📰 or 📈). "
        "Do NOT output any preamble, reasoning, thoughts, or commentary before or after the briefing sections."
    )

    return "\n".join(prompt_parts)


def _fetch_market_data(market_config: dict, mode: str) -> str | None:
    """
    Pre-fetch market data and format for prompt.

    Args:
        market_config: Market configuration dict
        mode: "morning" or "evening"

    Returns:
        Formatted market data string, or None if unavailable
    """
    try:
        from .skills.markets import get_futures_quotes, get_index_quotes, format_market_summary

        if mode == "morning":
            # Pre-market: show futures
            symbols = market_config.get("futures")
            quotes = get_futures_quotes(symbols)
        else:
            # Evening: show index closes
            symbols = market_config.get("indices")
            quotes = get_index_quotes(symbols)

        if not quotes:
            return None

        return format_market_summary(quotes, mode)
    except ImportError:
        # yfinance not installed - silently skip
        return None
    except Exception:
        # Market data fetch failed - silently skip
        return None


def _fetch_finviz_market_data() -> str | None:
    """
    Fetch and format FinViz market data for evening briefings.

    Returns pre-formatted text block with headlines, movers, futures,
    forex/bonds, economic data, and earnings calendar.

    Returns:
        Formatted FinViz data string, or None if unavailable.
    """
    try:
        from .skills.markets.finviz import fetch_finviz_data, format_finviz_briefing

        data = fetch_finviz_data()
        if data is None:
            return None

        formatted = format_finviz_briefing(data)
        if not formatted or "unavailable" in formatted.lower():
            return None

        return f"## FinViz Market Data (pre-fetched)\n\n{formatted}"
    except Exception as e:
        logger.warning("FinViz market data fetch failed: %s", e)
        return None


def _fetch_newsletter_content(news_config: dict, app_config: Config) -> str | None:
    """
    Pre-fetch newsletter emails and include their full content.

    Args:
        news_config: News configuration dict with sources and lookback_hours
        app_config: Application config for email settings

    Returns:
        Formatted string with newsletter content, or None if none found
    """
    try:
        from .email_poller import get_email_config
        from .skills.email import get_newsletters, read_email

        sources = news_config.get("sources", [])
        lookback_hours = news_config.get("lookback_hours", 12)

        if not sources:
            return None

        email_config = get_email_config(app_config)
        envelopes = get_newsletters(sources, lookback_hours, config=email_config)

        if not envelopes:
            return None

        lines = [f"## Newsletter content (past {lookback_hours} hours):"]

        for envelope in envelopes:
            try:
                # Fetch full email content
                email = read_email(
                    envelope.id,
                    folder=app_config.email.poll_folder,
                    config=email_config,
                )
                lines.append("")
                lines.append(f"### From: {email.sender}")
                lines.append(f"**Subject:** {email.subject}")
                lines.append("")

                # Get body, stripping HTML if needed
                body = email.body
                if body and ("<html" in body.lower() or "<body" in body.lower() or "<div" in body.lower()):
                    body = _strip_html(body)

                # Truncate very long emails to avoid overwhelming the prompt
                if len(body) > 5000:
                    body = body[:5000] + "\n\n[Content truncated...]"
                lines.append(body)
                lines.append("")
                lines.append("---")
            except Exception as e:
                logger.warning("Failed to read newsletter %s: %s", envelope.id, e)
                continue

        if len(lines) <= 1:
            # Only header, no content
            return None

        return "\n".join(lines)
    except Exception as e:
        logger.warning("Newsletter fetch failed: %s", e)
        return None


def _fetch_random_reminder(config: Config, user_id: str) -> str | None:
    """
    Fetch a reminder using shuffle-queue rotation (no repeats until all shown).

    Each reminder is shown exactly once before any repeats. When all reminders
    have been cycled through, the queue is reshuffled. If the reminders file
    content changes, the queue resets.

    Args:
        config: Application configuration (used for mount-aware file reading)
        user_id: The user's ID

    Returns:
        Next reminder in rotation, or None if not available
    """
    from . import db
    from .skills.files import read_text

    user_config = config.users.get(user_id)
    if not user_config:
        return None

    reminder_resources = [r for r in user_config.resources if r.type == "reminders_file"]
    if not reminder_resources:
        return None

    # Collect all reminders and compute content hash
    all_content = []
    all_reminders = []
    for resource in reminder_resources:
        try:
            content = read_text(config, resource.path)
            all_content.append(content or "")
            all_reminders.extend(_parse_reminders(content))
        except Exception as e:
            logger.warning("Failed to read reminders file %s: %s", resource.path, e)

    if not all_reminders:
        return None

    # Compute hash of combined content to detect changes
    content_hash = hashlib.sha256("".join(all_content).encode()).hexdigest()[:16]

    # Get or create queue state
    try:
        with db.get_db(config.db_path) as conn:
            state = db.get_reminder_state(conn, user_id)

            # Check if we need to reset the queue (content changed or queue empty)
            if state is None or state.content_hash != content_hash or not state.queue:
                # Create fresh shuffled queue
                indices = list(range(len(all_reminders)))
                random.shuffle(indices)
                queue = indices
                logger.debug(
                    "Created new reminder queue for %s: %d items", user_id, len(queue)
                )
            else:
                queue = state.queue

            # Pop the next reminder from the queue
            next_index = queue.pop(0)
            db.set_reminder_state(conn, user_id, queue, content_hash)
            conn.commit()

            return all_reminders[next_index]
    except Exception as e:
        logger.warning("Reminder state error, falling back to random: %s", e)
        return random.choice(all_reminders)


def _fetch_todo_items(config: Config, user_id: str) -> str | None:
    """
    Pre-fetch pending TODO items from the user's todo_file resources.

    Args:
        config: Application configuration
        user_id: The user's ID

    Returns:
        Formatted TODO items string, or None if not available
    """
    from .skills.files import read_text

    user_config = config.users.get(user_id)
    if not user_config:
        return None

    todo_resources = [r for r in user_config.resources if r.type == "todo_file"]
    if not todo_resources:
        return None

    all_items = []
    for resource in todo_resources:
        try:
            content = read_text(config, resource.path)
            if not content:
                continue
            # Extract pending items (lines starting with "- [ ]" or "* [ ]")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith(("- [ ]", "* [ ]")):
                    all_items.append(stripped)
        except Exception as e:
            logger.warning("Failed to read TODO file %s: %s", resource.path, e)

    if not all_items:
        return None

    lines = ["## Pending TODO Items (pre-fetched)"]
    lines.extend(all_items)
    return "\n".join(lines)


def _fetch_calendar_events(
    config: "Config", user_id: str, is_morning: bool, user_timezone: str,
) -> str | None:
    """
    Pre-fetch calendar events for the briefing with correct timezone.

    Args:
        config: Application configuration (for CalDAV credentials)
        user_id: The user's ID (for calendar ownership filtering)
        is_morning: True for today's events, False for tomorrow's
        user_timezone: User's timezone string

    Returns:
        Formatted calendar events string, or None if unavailable
    """
    if not config.caldav_url or not config.caldav_username or not config.caldav_password:
        return None

    try:
        from .skills.calendar import (
            get_caldav_client,
            get_calendars_for_user,
            get_today_events,
            get_tomorrow_events,
            format_event_for_display,
        )

        client = get_caldav_client(
            config.caldav_url, config.caldav_username, config.caldav_password,
        )
        calendars = get_calendars_for_user(client, user_id)
        if not calendars:
            return None

        day_label = "Today" if is_morning else "Tomorrow"
        fetch_fn = get_today_events if is_morning else get_tomorrow_events

        all_events = []
        for cal_name, cal_url, _writable in calendars:
            try:
                events = fetch_fn(client, cal_url, tz=user_timezone)
                for event in events:
                    all_events.append((cal_name, event))
            except Exception as e:
                logger.warning("Failed to fetch events from calendar %s: %s", cal_name, e)

        # Sort all events by start time
        all_events.sort(key=lambda x: x[1].start)

        if not all_events:
            return f"## {day_label}'s Calendar (pre-fetched)\nNo events scheduled."

        lines = [f"## {day_label}'s Calendar (pre-fetched)"]
        for cal_name, event in all_events:
            lines.append(f"- {format_event_for_display(event)} [{cal_name}]")

        return "\n".join(lines)
    except Exception as e:
        logger.warning("Calendar pre-fetch failed: %s", e)
        return None


def _parse_reminders(content: str) -> list[str]:
    """
    Parse reminders from file content.

    Splits on blank lines into blocks. Attribution lines (starting with --)
    are merged with the preceding block. Blocks containing numbered lists
    are split into individual items.

    Args:
        content: File content string

    Returns:
        List of reminder strings (may be multi-line)
    """
    raw_blocks = re.split(r"\n\s*\n", content.strip())
    # First pass: collect blocks, merging attribution lines with preceding block
    blocks = []
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        # Skip headers-only blocks and horizontal rules
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if all(l.startswith("#") or l in ("---", "***", "___") for l in lines):
            continue
        # Drop leading header lines within a block
        while lines and lines[0].startswith("#"):
            lines.pop(0)
        if not lines:
            continue
        text = "\n".join(lines)
        # Attribution line (e.g. "--Author Name" or "– Author") → append to previous
        # Require -- (double dash) or unicode dash to avoid matching bullet points (- item)
        if blocks and re.match(r"^(--|[–—])\s*\S", text):
            blocks[-1] = blocks[-1] + "\n" + text
        else:
            blocks.append(text)

    # Second pass: split blocks with multiple list items into individual reminders
    reminders = []
    for block in blocks:
        # Numbered list (multiple "N. " lines)
        numbered = re.findall(r"^\d+\.\s+.+", block, re.MULTILINE)
        if len(numbered) > 1:
            for item in numbered:
                reminders.append(item.strip())
            continue
        # Bullet list (multiple "- " lines)
        bullets = re.split(r"(?m)^(?=[-*+] )", block)
        bullets = [b.strip() for b in bullets if b.strip()]
        if len(bullets) > 1:
            for item in bullets:
                reminders.append(item)
            continue
        reminders.append(block)

    # Strip list prefixes (- item, * item, 1. item) from single-line reminders
    cleaned = []
    for r in reminders:
        r = re.sub(r"^[-*+]\s+", "", r)
        r = re.sub(r"^\d+\.\s+", "", r)
        cleaned.append(r)

    return cleaned
