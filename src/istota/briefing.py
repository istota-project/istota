"""Re-export shim — all briefing logic lives in istota.skills.briefing."""

from .skills.briefing import (  # noqa: F401
    _component_enabled,
    _fetch_calendar_events,
    _fetch_finviz_market_data,
    _fetch_market_data,
    _fetch_newsletter_content,
    _fetch_random_reminder,
    _fetch_todo_items,
    _parse_reminders,
    _strip_html,
    build_briefing_prompt,
)
