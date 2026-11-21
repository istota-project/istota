"""Whether a name is a timezone this server can load.

The one boolean zone check: the config loader, the setup wizard and the CRON.md
parser call it. Stdlib only, never raises.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def is_valid_timezone(name: object) -> bool:
    """Whether `name` is a non-empty string `ZoneInfo` loads, without raising for any input.

    Abbreviations (`PST`, `EDT`) are not zones `ZoneInfo` knows, and an empty name
    would load as nothing useful, so both are False.

    `OSError` is in the tuple because `ZoneInfo` resolves the name as a *path*
    under the tzdata directories, so a 300-character value raises
    `ENAMETOOLONG` rather than `ZoneInfoNotFoundError` — inside `load_config`
    that escaped as a traceback carrying the venv path.
    `ZoneInfoNotFoundError` subclasses `KeyError` and is named for the reader.
    """
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return False
    return True
