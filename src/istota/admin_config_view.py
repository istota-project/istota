"""Read-only, redacted rendering of the loaded `Config` for the admin UI.

Walks the `Config` dataclass tree and emits ordered sections of typed fields.
The shape is deliberately field-level rather than a TOML dump — each field
carries its dotted `key`, a `type`, and a `secret` flag — so the same payload
can back an editable form later without the frontend changing shape. Nothing
here writes; `editable` is a constant `False` and the endpoint is a GET.

**Redaction is the whole safety story of this module.** Config holds live
credentials (Nextcloud app password, IMAP/SMTP passwords, GitLab/GitHub tokens,
OAuth client secrets, the session signing key, the native-brain API key), and
an admin reading the page has no need for any of their values — only for
whether they are set. A credential-named field is therefore emitted with
`value: null` and `set: true|false`.

Matching is by field *name* against `SECRET_NAME_PATTERNS`, the same shape
`executor._CREDENTIAL_ENV_PATTERNS` uses for env stripping. A name-pattern rule
is only as good as its coverage, so `tests/test_admin_config_view.py` asserts
the coverage two ways: every known credential field redacts, and no
credential-named field anywhere in the rendered tree is exposed. A new secret
whose name does not match the patterns fails those tests rather than shipping.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

# Substring patterns marking a field name as a credential. Mirrors
# `executor._CREDENTIAL_ENV_PATTERNS`; `PASS` also catches `app_password` and a
# bare `password`, and is broad on purpose — a false positive costs an admin one
# unshown value, a false negative leaks a live secret. `AUTH` and the hyphen
# spellings exist for HTTP *header* names (`authorization`, `x-api-key`), which
# `_render_value` checks per dict key; a header dict is an auth channel.
SECRET_NAME_PATTERNS = frozenset({
    "PASSWORD", "PASS", "SECRET", "TOKEN", "API_KEY", "APIKEY",
    "PRIVATE_KEY", "CREDENTIAL", "AUTH",
})

# Fields redacted wholesale regardless of their name, because their *contents*
# are credentials even though the field itself is not named like one.
#
# `extra_headers` is the live case: `llm/openai_compat.py` merges it over the
# `Authorization` header, so it is where a non-Anthropic deployment puts its
# `x-api-key` / `api-key` / `authorization` value. Per-key redaction inside the
# dict would also catch those three spellings, but the field is an auth channel
# by construction and a header we have not thought of should not be the thing
# standing between a config page and a live provider key.
_ALWAYS_SECRET_KEYS = frozenset({
    "brain.native.extra_headers",
})

# Fields the patterns above flag but which carry no credential. Keyed on the
# full dotted path rather than the bare name, so an allowlist entry can never
# silently un-redact a same-named field in another section.
#
# Every entry is a deliberate decision that this specific setting is
# operational. The default stays fail-safe: an unrecognized `*_token` field is
# redacted until someone adds it here.
NON_SECRET_KEYS = frozenset({
    "web.token_storage",              # "ephemeral" | "encrypted" — a mode, not a token
    "web.oauth2_token_endpoint",      # a URL
    "brain.native.max_tokens",        # a count
    "brain.native.compaction_reserve_tokens",
    "brain.native.compaction_keep_recent_tokens",
    "security.passthrough_env_vars",  # a list of var *names*
    "brain.tmux.bypass_accept_marker",   # a TUI dialog substring; the config
    "brain.tmux.bypass_warning_marker",  # hotfix knob when a CLI reword lands
})

# Fields excluded from the view entirely, with the reason each is out.
_EXCLUDED_TOP_LEVEL = {
    # Per-user config is a separate surface (the Users card on the status page)
    # and dumping it here would put one user's briefings and resources in front
    # of every admin for no operational gain. Summarized as a count instead.
    "users",
    # Test-only override; meaningless to an operator.
    "bundled_skills_dir",
}

# Top-level fields rendered as a count rather than their contents: lists of
# dataclasses whose detail belongs on their own module page, not here.
_COUNT_FIELDS = {"users", "default_briefings", "briefing_shared_blocks"}


def is_secret_name(name: str) -> bool:
    """Whether a field or header name looks like a credential.

    Hyphens fold to underscores so an HTTP header (`x-api-key`) matches the same
    patterns a Python field name would.
    """
    upper = name.upper().replace("-", "_")
    return any(pattern in upper for pattern in SECRET_NAME_PATTERNS)


def is_secret_field(key: str, name: str) -> bool:
    """Whether the field at dotted ``key`` must be redacted."""
    if key in _ALWAYS_SECRET_KEYS:
        return True
    if key in NON_SECRET_KEYS:
        return False
    return is_secret_name(name)


def _is_set(value: Any) -> bool:
    """Whether a credential is configured, without inspecting its content."""
    if value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, set, dict)):
        return len(value) > 0
    return bool(value)


def _render_value(value: Any) -> tuple[Any, str]:
    """Return ``(json_safe_value, type_name)`` for a non-secret field."""
    if value is None:
        return None, "null"
    if isinstance(value, bool):
        return value, "bool"
    if isinstance(value, int):
        return value, "int"
    if isinstance(value, float):
        return value, "float"
    if isinstance(value, Path):
        return str(value), "path"
    if isinstance(value, str):
        return value, "str"
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value), "list"
    if isinstance(value, (list, tuple)):
        if any(dataclasses.is_dataclass(v) for v in value):
            return len(value), "count"
        return [_render_value(v)[0] for v in value], "list"
    if isinstance(value, dict):
        # Redact secret-named *keys* too. No shipped config field nests a
        # credential in a dict today, but the walk cannot see inside one, so a
        # future `{service: {api_key: ...}}` would otherwise render in full.
        return {
            str(k): (None if is_secret_name(str(k)) else _render_value(v)[0])
            for k, v in value.items()
        }, "dict"
    if dataclasses.is_dataclass(value):
        return None, "section"
    return str(value), "str"


def _field_entry(prefix: str, name: str, value: Any) -> dict:
    key = f"{prefix}.{name}" if prefix else name
    if is_secret_field(key, name):
        return {
            "key": key,
            "name": name,
            "value": None,
            "type": "secret",
            "secret": True,
            "set": _is_set(value),
        }
    if name in _COUNT_FIELDS:
        size = len(value) if hasattr(value, "__len__") else 0
        return {
            "key": key,
            "name": name,
            "value": size,
            "type": "count",
            "secret": False,
            "set": size > 0,
        }
    rendered, type_name = _render_value(value)
    return {
        "key": key,
        "name": name,
        "value": rendered,
        "type": type_name,
        "secret": False,
        "set": _is_set(value),
    }


def _walk(obj: Any, prefix: str, sections: list[dict], *, label: str) -> None:
    """Emit one section for ``obj``, recursing into nested dataclass fields.

    A nested dataclass becomes its own dotted section (``brain.native``) rather
    than a nested object, so the frontend renders one flat list of sections and
    a future editor addresses every field by one dotted key.
    """
    fields: list[dict] = []
    nested: list[tuple[str, Any]] = []

    for f in dataclasses.fields(obj):
        if prefix == "" and f.name in _EXCLUDED_TOP_LEVEL:
            continue
        value = getattr(obj, f.name, None)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            nested.append((f.name, value))
            continue
        fields.append(_field_entry(prefix, f.name, value))

    sections.append({"key": prefix or "general", "label": label, "fields": fields})

    for name, value in nested:
        child_prefix = f"{prefix}.{name}" if prefix else name
        _walk(value, child_prefix, sections, label=f"[{child_prefix}]")


def build_config_view(config: "Config") -> dict:
    """Render the loaded config as ordered, redacted sections."""
    sections: list[dict] = []
    _walk(config, "", sections, label="General")

    # `users` is excluded from the walk but reported as a count so an admin can
    # see the instance has users without seeing their config.
    general = sections[0]
    general["fields"].append(
        _field_entry("", "users", getattr(config, "users", {}) or {})
    )
    # General only: it is a grab-bag of ~25 unrelated top-level keys where
    # declaration order says nothing, so alphabetical is easier to scan. A
    # `[section]` keeps its declaration order, which groups related knobs.
    general["fields"].sort(key=lambda f: f["name"])

    return {
        "config_path": str(config.config_path) if config.config_path else None,
        "editable": False,
        "sections": sections,
    }
