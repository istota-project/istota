"""Per-kind checks for a source config written from the skill path.

The web editor saves drafts with empty configs and shows advisory hints, so it
does not call this. The skill has no hint UI: a source that can never read
anything is refused here instead of being answered ``ok``. The resolvers in
``sources/*`` stay fail-soft at read time either way.

:func:`check_source` raises :class:`SourceRefused` with ``reason``
``invalid_source`` (``field`` names the config key), ``mail_account_refused``
(the text the web returns for the same ``my_email`` account) or
``unavailable`` (a check that needs the istota config was run without one).
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlsplit

from istota.briefings.models import (
    _SLUG_RE,
    SOURCE_KINDS,
    normalize_group_email_config,
    parse_my_email_account,
)
from istota.briefings.sources.browse import BROWSE_PRESETS


class SourceRefused(ValueError):
    def __init__(self, reason: str, message: str, field: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.field = field


def _refuse(message: str, field: str | None = None) -> SourceRefused:
    return SourceRefused("invalid_source", message, field)


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _positive_number(value: object) -> bool:
    return type(value) in (int, float) and value > 0


def _picker_request(app_config) -> SimpleNamespace:
    # The picker routes read the config off `request.app.state`; they are called
    # here as they stand until they move out of the web layer.
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(istota_config=app_config)))


def _feed_options(app_config, user_id: str) -> dict:
    from istota.briefings.routes import get_feed_options

    return get_feed_options(_picker_request(app_config), user={"username": user_id})


def _shared_block_names(app_config) -> set[str]:
    from istota.briefings.routes import get_shared_block_options

    options = get_shared_block_options(_picker_request(app_config), user={})["options"]
    return {option["name"] for option in options}


def _check_rss(app_config, user_id: str, config: dict) -> None:
    ref = config.get("feed_ref")
    if (not isinstance(ref, dict) or ref.get("kind") not in ("subscription", "category")
            or not _positive_int(ref.get("value"))):
        raise _refuse(
            'rss needs feed_ref {"kind": "subscription" or "category", "value": <id>}',
            "feed_ref",
        )
    if "limit" in config and not _positive_int(config["limit"]):
        raise _refuse("limit must be a positive integer", "limit")
    if app_config is None:
        return
    options = _feed_options(app_config, user_id)
    if not options.get("available"):
        return
    listed = options["subscriptions"] if ref["kind"] == "subscription" else options["categories"]
    if ref["value"] not in {option["value"] for option in listed}:
        raise _refuse(f"no feed {ref['kind']} with id {ref['value']}", "feed_ref")


def _check_my_email(app_config, user_id: str, config: dict, stored: dict | None) -> None:
    from istota.briefings.sources.my_email import account_refusal

    try:
        parse_my_email_account(config.get("account"))
    except ValueError as e:
        raise _refuse(str(e), "account") from None
    stored_account = stored.get("account") if isinstance(stored, dict) else None
    refusal = account_refusal(app_config, user_id, config.get("account"), stored=stored_account)
    if refusal:
        raise SourceRefused("mail_account_refused", refusal, "account")


def _check_browse(config: dict) -> None:
    preset = config.get("preset") or None
    url = config.get("url") or None
    if (preset is None) == (url is None):
        raise _refuse("browse needs exactly one of preset or url")
    if preset is not None:
        if preset not in BROWSE_PRESETS:
            raise _refuse(f"unknown browse preset '{preset}'", "preset")
        return
    parsed = urlsplit(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _refuse("url must be an http or https URL", "url")


def _check_path(config: dict) -> None:
    path = config.get("path")
    if not isinstance(path, str) or not path.strip():
        raise _refuse("path must name a file", "path")


def _check_shared_block(app_config, config: dict) -> None:
    name = config.get("name")
    if not isinstance(name, str) or not _SLUG_RE.fullmatch(name):
        raise _refuse("name must be a shared block slug", "name")
    if "max_age_hours" in config and not _positive_number(config["max_age_hours"]):
        raise _refuse("max_age_hours must be a positive number", "max_age_hours")
    if app_config is None:
        raise SourceRefused("unavailable", "shared blocks are unavailable")
    if name not in _shared_block_names(app_config):
        raise _refuse(f"no shared block named '{name}'", "name")


def check_source(app_config, user_id: str, kind: str, config: dict, *, stored: dict | None = None) -> None:
    """Raise :class:`SourceRefused` for a source that cannot read anything.

    ``stored`` is the source's saved config, for an edit; a ``my_email`` account
    left as it was passes, as on the web.
    """
    if kind not in SOURCE_KINDS:
        raise _refuse(f"invalid kind '{kind}' (one of {', '.join(SOURCE_KINDS)})", "kind")
    if not isinstance(config, dict):
        raise _refuse("source config must be a JSON object")
    if kind == "rss":
        _check_rss(app_config, user_id, config)
    elif kind == "email":
        try:
            normalize_group_email_config(config)
        except ValueError as e:
            raise _refuse(str(e)) from None
    elif kind == "my_email":
        _check_my_email(app_config, user_id, config, stored)
    elif kind == "browse":
        _check_browse(config)
    elif kind in ("todos", "reminders", "notes"):
        _check_path(config)
    elif kind == "shared_block":
        _check_shared_block(app_config, config)
