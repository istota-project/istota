"""Load briefing configurations from workspace BRIEFINGS.md or admin config."""

import logging
import re
from pathlib import Path

import tomli

from .config import BriefingConfig, BriefingDefaultsConfig, Config
from .storage import get_user_briefings_path

logger = logging.getLogger("istota.briefing_loader")

_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


def _load_workspace_briefings(config: Config, user_id: str) -> list[BriefingConfig] | None:
    """
    Load briefings from a user's workspace BRIEFINGS.md file.

    Returns list of BriefingConfig, or None if file doesn't exist or is unreadable.
    """
    if not config.use_mount:
        return None

    briefings_path = config.nextcloud_mount_path / get_user_briefings_path(user_id, config.bot_dir_name).lstrip("/")
    if not briefings_path.exists():
        return None

    try:
        content = briefings_path.read_text()
        match = _TOML_BLOCK_RE.search(content)
        if not match:
            return []
        toml_str = match.group(1)
        data = tomli.loads(toml_str)
    except Exception as e:
        logger.warning("Failed to parse BRIEFINGS.md for %s: %s", user_id, e)
        return None

    briefings = []
    for b in data.get("briefings", []):
        briefings.append(BriefingConfig(
            name=b.get("name", ""),
            cron=b.get("cron", ""),
            conversation_token=b.get("conversation_token", ""),
            output=b.get("output", "talk"),
            components=b.get("components", {}),
        ))

    return briefings


def _expand_boolean_components(
    components: dict,
    defaults: BriefingDefaultsConfig,
) -> dict:
    """
    Expand boolean component values using admin defaults.

    - `markets = true` → expands to defaults.markets with `enabled: true`
    - `news = true` → expands to defaults.news with `enabled: true`
    - Dict values pass through unchanged
    - Simple booleans (calendar, todos, etc.) stay as-is
    """
    expanded = {}
    for key, value in components.items():
        if isinstance(value, bool) and value:
            # Check if there's a default dict to expand from
            default_dict = getattr(defaults, key, None)
            if isinstance(default_dict, dict) and default_dict:
                expanded[key] = {"enabled": True, **default_dict}
            else:
                expanded[key] = value
        else:
            expanded[key] = value
    return expanded


def get_briefings_for_user(config: Config, user_id: str) -> list[BriefingConfig]:
    """
    Get briefings for a user with workspace > per-user config > main config precedence.

    Workspace BRIEFINGS.md overrides admin config at the briefing name level.
    Boolean component values are expanded using briefing_defaults.
    """
    user_config = config.users.get(user_id)
    admin_briefings = user_config.briefings if user_config else []

    # Try loading workspace briefings
    workspace_briefings = _load_workspace_briefings(config, user_id)

    if workspace_briefings:
        # Build lookup of admin briefings by name
        admin_by_name = {b.name: b for b in admin_briefings}

        # Workspace briefings override admin by name, add new ones
        merged_by_name = dict(admin_by_name)
        for b in workspace_briefings:
            merged_by_name[b.name] = b

        result = list(merged_by_name.values())
    else:
        result = list(admin_briefings)

    # Expand boolean components using defaults
    defaults = config.briefing_defaults
    expanded = []
    for b in result:
        new_components = _expand_boolean_components(b.components, defaults)
        if new_components != b.components:
            b = BriefingConfig(
                name=b.name,
                cron=b.cron,
                conversation_token=b.conversation_token,
                output=b.output,
                components=new_components,
            )
        expanded.append(b)

    return expanded
