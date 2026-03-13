"""Re-export shim — all briefing config loading lives in istota.skills.briefing."""

from .skills.briefing import (  # noqa: F401
    _TOML_BLOCK_RE,
    _expand_boolean_components,
    _load_workspace_briefings,
    get_briefings_for_user,
)
