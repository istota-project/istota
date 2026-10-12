"""Read-only, redacted rendering of the loaded `Config` for the admin UI.

Walks the `Config` dataclass tree and emits ordered sections of typed fields.
The shape is deliberately field-level rather than a TOML dump — each field
carries its dotted `key`, a `type`, and a `secret` flag — so the same payload
backs the config editor's form. Each field also carries what the form needs
(its `path`, default, docs, vocabulary, credential routing, env override,
editor kind and read-only reason; `config_editor.schema` decides most of
them), and the top level carries the file's digest, the applier, who manages
each resource and the problems a check of the current file reports. Nothing
here writes, and `editable` stays `False` until the applier exists.

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
    from istota.config import Config

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
    "web.auth",
    "web.auth_enrol_ttl_hours",
    "web.auth_reset_ttl_hours",
    "web.auth_sign_in_code_ttl_minutes",
    "web.auth_min_password_length",
    "web.auth_throttle_window_seconds",
    "web.auth_throttle_max_email",
    "web.auth_throttle_max_ip",
    "web.auth_mail_link_max_email",

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
    # Process state for the live profile refresh, not configuration.
    "_profile_generation",
    "_profile_checked_at",
    "_profile_refresh_failing",
}

# Top-level fields rendered as a count rather than their contents: lists of
# dataclasses whose detail belongs on their own module page, not here.
_COUNT_FIELDS = {"users", "default_briefings", "briefing_shared_blocks"}


def _mask_phone_number(number: Any) -> str:
    """`user_profiles.mask_phone_number`, imported lazily.

    This module is imported by the web app on a request path and holds no
    package imports at module scope; `user_profiles` opens databases.
    """
    from istota.user_profiles import mask_phone_number

    return mask_phone_number(str(number or ""))


# Fields rendered through a masking function rather than in full. Not
# credentials — a redacted value would tell an operator nothing, and these are
# settings they have to be able to check against a provider portal — but
# private operational data that identifies a person and does not belong on a
# page an admin may screen-share or paste into a report.
#
# Keyed on the full dotted path for the reason `NON_SECRET_KEYS` is: a bare
# name would mask a same-named field in another section, or fail to.
#
# `sms.default_sender_number` and `sms.service_numbers` satisfy the same rule
# and are deliberately not here yet: they are the deployment's own numbers
# rather than a person's, and adding them is a behaviour change to the SMS
# surface that belongs with that surface rather than riding in on this one.
# It is a gap, not a distinction.
MASKED_KEYS = {
    "whatsapp.business_phone_number": _mask_phone_number,
}


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


def is_secret_path(path: tuple[str, ...] | list[str]) -> bool:
    """Whether the value at ``path`` in a config document must be redacted.

    The path-aware form of :func:`is_secret_field`, for a value that may sit
    inside a field: a header under ``[brain.native.extra_headers]`` or an
    element of a list. A section (a field holding a nested dataclass) decides
    nothing by its name, as in the view's own walk, so every key under
    ``[security.credential_broker]`` is not secret for being there. From the
    first non-section segment on, the path is secret when any prefix is.
    """
    from istota.config import Config

    segments = [str(segment) for segment in path]
    section: Any = Config
    for index, name in enumerate(segments):
        key = ".".join(segments[: index + 1])
        if key in _ALWAYS_SECRET_KEYS:
            return True
        field = None
        if section is not None:
            field = next((f for f in dataclasses.fields(section) if f.name == name), None)
        if field is not None and dataclasses.is_dataclass(field.type):
            section = field.type
            continue
        section = None
        if is_secret_field(key, name):
            return True
    return False


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
    masker = MASKED_KEYS.get(key)
    if masker is not None:
        return {
            "key": key,
            "name": name,
            "value": masker(value),
            "type": "str",
            "secret": False,
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


_MISSING = object()

_NO_APPLIER = {
    "kind": "none",
    "available": False,
    "reason": (
        "This install has no config applier; changes are made with "
        "`istota-stack apply` on the host."
    ),
}

#: Display names for the credential files no config key reads.
_SECRET_LABELS = {
    "anthropic_api_key": "Anthropic API key",
    "claude_code_oauth_token": "Claude Code OAuth token",
}


def _lookup(document: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    node = document
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return False, None
        node = node[segment]
    return True, node


def _type_hints(owner: type) -> dict:
    import typing

    try:
        return typing.get_type_hints(owner)
    except Exception:  # noqa: BLE001 - a hint that does not resolve falls back to the value
        return {}


def _managed_state(managed: dict | None) -> dict[str, dict]:
    """Each resource's owner. An entry that is not a table fails closed."""
    from istota.apply import MANAGED_RESOURCES

    state: dict[str, dict] = {}
    for resource in MANAGED_RESOURCES:
        entry = (managed or {}).get(resource)
        if entry is None:
            state[resource] = {"managed": False, "source": None, "at": None}
        elif isinstance(entry, dict):
            source, at = entry.get("source"), entry.get("at")
            state[resource] = {
                "managed": entry.get("managed") is not False,
                "source": source if isinstance(source, str) else None,
                "at": at if isinstance(at, str) else None,
            }
        else:
            state[resource] = {"managed": True, "source": "unreadable", "at": None}
    return state


def _managed_reason(entry: dict) -> str | None:
    if not entry["managed"]:
        return None
    if entry["source"] == "unreadable":
        return (
            "config/managed.json cannot be read, so every resource reads as managed. "
            "Re-run `istota-stack apply` on the host with the intended [meta] to repair it."
        )
    return (
        f"Managed by {entry['source'] or 'a plan'}, whose next run would revert a change "
        "made here. Change it there, or hand it to the web by removing it from that source."
    )


def _snippet(document: dict | None, path: tuple[str, ...]) -> str:
    """The value at `path` as TOML whose one top-level key is the field's name."""
    from istota.lib import toml_write

    found, value = _lookup(document, path)
    if not found:
        return ""
    try:
        return toml_write.dumps({path[-1]: value})
    except toml_write.TomlWriteError:
        return ""


def _editor_keys(
    ctx: dict, prefix: str, name: str, declared: Any, value: Any, default: Any,
) -> dict:
    """What the editor's form needs for one field, beside the read-only entry."""
    schema = ctx["schema"]
    key = f"{prefix}.{name}" if prefix else name
    path = tuple(key.split("."))
    credential = schema.credential_for(path)
    env_override = schema.env_override_for(path)
    choices = schema.choices_for(path)
    multi_choices = schema.multi_choices_for(path)

    reason = schema.READ_ONLY.get(path)
    if reason is None and env_override:
        reason = (
            f"Set by the environment variable {env_override}, which wins over "
            "config.toml, so an edit here would do nothing. Change it on the host."
        )
    if reason is None:
        resource = "secrets" if credential and credential["kind"] == "file" else "config"
        reason = ctx["managed_reasons"][resource]

    entry: dict[str, Any] = {"path": list(path)}
    if default is not _MISSING and not is_secret_field(key, name):
        entry["default"] = _field_entry(prefix, name, default)["value"]
    entry.update({
        "in_file": _lookup(ctx["document"], path)[0] if ctx["document"] is not None else False,
        "choices": list(choices) if choices is not None else None,
        "multi_choices": list(multi_choices) if multi_choices is not None else None,
        "doc": ctx["docs"].get(path),
        "credential": credential,
        "env_override": env_override,
        "editor": "readonly" if reason else schema.editor_kind(path, declared, value),
        "read_only_reason": reason,
        "risky": path in schema.RISKY_KEYS,
    })
    if path in schema.TABLE_PATHS and path not in schema.READ_ONLY:
        entry["toml"] = _snippet(ctx["redacted"], path)
    return entry


def _walk(
    obj: Any, prefix: str, sections: list[dict], *, label: str, ctx: dict, default: Any,
) -> None:
    """Emit one section for ``obj``, recursing into nested dataclass fields.

    A nested dataclass becomes its own dotted section (``brain.native``) rather
    than a nested object, so the frontend renders one flat list of sections and
    the editor addresses every field by one path. ``default`` is the same
    section of a default ``Config``, or ``None``.
    """
    fields: list[dict] = []
    nested: list[tuple[str, Any, Any]] = []
    hints = _type_hints(type(obj))

    for f in dataclasses.fields(obj):
        if prefix == "" and f.name in _EXCLUDED_TOP_LEVEL:
            continue
        value = getattr(obj, f.name, None)
        default_value = getattr(default, f.name, _MISSING) if default is not None else _MISSING
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            nested.append((f.name, value, None if default_value is _MISSING else default_value))
            continue
        entry = _field_entry(prefix, f.name, value)
        entry.update(_editor_keys(ctx, prefix, f.name, hints.get(f.name), value, default_value))
        fields.append(entry)

    sections.append({"key": prefix or "general", "label": label, "fields": fields})

    for name, value, default_value in nested:
        child_prefix = f"{prefix}.{name}" if prefix else name
        _walk(value, child_prefix, sections, label=f"[{child_prefix}]", ctx=ctx, default=default_value)


def _problems(check: Any) -> list[dict]:
    from istota.config_editor.check import problems_of

    return [] if check is None else problems_of(check)


def _value_at(config: Any, path: tuple[str, ...]) -> Any:
    node = config
    for segment in path:
        node = getattr(node, segment, None)
    return node


def _secret_files(config: "Config", ctx: dict) -> list[dict]:
    """One entry per stack secrets file: whether it is set, never its value."""
    import os

    from istota.config_editor.reference import credential_files
    from istota.setup_wizard import SECRET_NAMES

    schema = ctx["schema"]
    config_keys = {name: dotted for dotted, name in credential_files().items() if name}
    entries = []
    for name in SECRET_NAMES:
        dotted = config_keys.get(name)
        if dotted:
            path = tuple(dotted.split("."))
            is_set = _is_set(_value_at(config, path))
            reason = schema.READ_ONLY.get(path)
        else:
            # No config key reads it; `istota-secrets` exports each file as
            # the uppercase variable, in the web process too.
            path = None
            is_set = bool(os.environ.get(name.upper(), "").strip())
            reason = None
        entries.append({
            "name": name,
            "label": dotted or _SECRET_LABELS.get(name, name),
            "config_key": dotted,
            "set": is_set,
            "read_only_reason": reason or ctx["managed_reasons"]["secrets"],
            "risky": path in schema.RISKY_KEYS if path else False,
        })
    return entries


def build_config_view(
    config: "Config",
    *,
    document: str | None = None,
    document_digest: str | None = None,
    check: Any = None,
    applier: dict | None = None,
    managed: dict | None = None,
    started_at: str | None = None,
) -> dict:
    """Render the loaded config as ordered, redacted sections.

    ``document`` is the text of ``config.toml`` and ``document_digest`` its
    sha256; ``check`` the ``apply.CheckResult`` of a check of it; ``managed``
    what ``apply.read_managed(..., fail_closed=True)`` returned; ``applier``
    the applier's ``{kind, available, reason}``. Each is optional, and the
    view is the read-only one without it.
    """
    import tomllib

    from istota.config_editor import edits, schema, secrets
    from istota.config_editor.reference import field_docs

    parsed = None
    if document is not None:
        try:
            parsed = tomllib.loads(document)
        except tomllib.TOMLDecodeError:
            parsed = None
    managed_state = _managed_state(managed)
    try:
        docs = field_docs(Path(config.skills_dir).parent)
    except (OSError, TypeError):
        docs = {}
    ctx = {
        "schema": schema,
        "document": parsed,
        "redacted": secrets.redact_document(parsed) if parsed is not None else None,
        "docs": docs,
        "managed_reasons": {
            resource: _managed_reason(entry) for resource, entry in managed_state.items()
        },
    }
    try:
        default_config = type(config)()
    except Exception:  # noqa: BLE001 - a subclass needing arguments has no defaults to show
        default_config = None

    sections: list[dict] = []
    _walk(config, "", sections, label="General", ctx=ctx, default=default_config)

    # `users` is excluded from the walk but reported as a count so an admin can
    # see the instance has users without seeing their config.
    general = sections[0]
    users = getattr(config, "users", {}) or {}
    users_entry = _field_entry("", "users", users)
    users_entry.update(_editor_keys(
        ctx, "", "users", _type_hints(type(config)).get("users"), users,
        getattr(default_config, "users", _MISSING) if default_config is not None else _MISSING,
    ))
    general["fields"].append(users_entry)
    # General only: it is a grab-bag of ~25 unrelated top-level keys where
    # declaration order says nothing, so alphabetical is easier to scan. A
    # `[section]` keeps its declaration order, which groups related knobs.
    general["fields"].sort(key=lambda f: f["name"])

    return {
        "config_path": str(config.config_path) if config.config_path else None,
        "editable": False,
        "sections": sections,
        "applier": dict(applier) if applier is not None else dict(_NO_APPLIER),
        "managed": managed_state,
        "document_digest": document_digest,
        "started_at": started_at,
        "problems": _problems(check),
        "has_comments": edits.has_comments(document) if document is not None else False,
        "secrets": _secret_files(config, ctx),
        "admins": {
            "users": sorted(str(user) for user in (getattr(config, "admin_users", None) or ())),
            "read_only_reason": ctx["managed_reasons"]["admins"],
        },
    }
