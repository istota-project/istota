"""What the config editor may do with each key, by path.

- `READ_ONLY`: keys the editor shows and never edits, each with the reason the
  page gives and where the key is changed instead.
- `RISKY_KEYS`: editable, but able to lock admins out or change the security
  posture, so a change needs a typed confirmation. Any change to the admins
  file is risky too; it is not a config path, so it is not listed here.
- `TABLE_PATHS`: structured fields, edited as a TOML snippet (`set_toml`).
- `editor_kind`: which control a field gets.

A path is a tuple of field names. The dotted form is a display label only.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import types
import typing
from pathlib import Path
from typing import Any, Mapping

from istota import config as config_module
from istota.config import Config
from istota.config_editor import reference
from istota.webui.admin_config_view import is_secret_path

_ON_THE_HOST = "Change it on the host with `istota-stack setup --force`."


def _read_only() -> dict[tuple[str, ...], str]:
    found: dict[tuple[str, ...], str] = {}
    for name in config_module._NOT_CONFIGURATION:
        found[(name,)] = "Not a setting: istota sets this itself."
    found[("admin_users",)] = "Edited in the admins editor, which writes config/admins."
    found[("users",)] = (
        "Per-user settings are edited on the Users page. [users.*] in config.toml "
        "is seed-only and is kept as it is."
    )
    for name in reference.setup_paths():
        found[(name,)] = (
            "The container layout `istota setup` writes. Changing it would move the "
            f"stack onto an empty database. {_ON_THE_HOST}"
        )
    found[("site", "hostname")] = (
        "Must equal host.env's DOMAIN, or the stack refuses to start. Change it on "
        "the host: host.env and `provision.sh`, then `istota-stack setup --force`."
    )
    mount = (
        "The Nextcloud mount reads rclone.conf, which setup writes from this, so "
        f"a change here would leave the mount on the old account. {_ON_THE_HOST}"
    )
    found[("nextcloud", "url")] = mount
    found[("nextcloud", "username")] = mount
    found[("nextcloud", "app_password")] = mount
    found[("devbox", "network_subnet")] = (
        f"Must match the VM's egress unit, which the host renders. {_ON_THE_HOST}"
    )
    web_ui = f"This page is served by it, and a wrong value has no recovery from here. {_ON_THE_HOST}"
    found[("web", "enabled")] = web_ui
    found[("web", "port")] = web_ui
    profile = f"Selects a compose profile, which changes the services the stack runs. {_ON_THE_HOST}"
    for path in [
        ("browser", "enabled"), ("browser", "api_url"),
        ("talk", "signaling", "enabled"), ("talk", "signaling", "url"),
        ("whatsapp", "enabled"), ("whatsapp", "provider"),
    ]:
        found[path] = profile
    found[("web", "session_secret_key")] = (
        "Rotating it ends every session, this one included, while the change is "
        "applied. Rotate it on the host with `istota-stack put-files`."
    )
    return found


READ_ONLY: dict[tuple[str, ...], str] = _read_only()

RISKY_KEYS: frozenset[tuple[str, ...]] = frozenset({
    ("web", "auth"),
    ("web", "oauth2_provider"),
    ("web", "oauth2_client_id"),
    # The file credential istota_web_oauth2_client_secret.
    ("web", "oauth2_client_secret"),
    ("web", "trusted_proxy_hops"),
    ("email", "outbound_approval_floor"),
    ("email", "confirm_sender_match"),
    ("security", "sandbox_enabled"),
    ("security", "skill_proxy_enabled"),
    ("security", "credential_broker", "enabled"),
    ("brain", "kind"),
    ("brain", "fallback"),
    ("brain", "room_selectable"),
})


def _unwrap_optional(declared: Any) -> Any:
    if typing.get_origin(declared) in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(declared) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return declared


def _is_table_type(declared: Any) -> bool:
    declared = _unwrap_optional(declared)
    origin = typing.get_origin(declared)
    if declared is dict or origin is dict:
        return True
    if origin is list:
        return any(dataclasses.is_dataclass(arg) for arg in typing.get_args(declared))
    return False


def _table_paths() -> frozenset[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()

    def walk(owner: type, prefix: tuple[str, ...]) -> None:
        hints = typing.get_type_hints(owner)
        for field in dataclasses.fields(owner):
            declared = _unwrap_optional(hints.get(field.name))
            path = prefix + (field.name,)
            if dataclasses.is_dataclass(declared):
                walk(declared, path)
            elif _is_table_type(declared):
                found.add(path)

    walk(Config, ())
    return frozenset(found)


TABLE_PATHS: frozenset[tuple[str, ...]] = _table_paths()


@functools.cache
def file_credentials() -> dict[tuple[str, ...], str]:
    """Each config path with a stack secrets file, to the file's name."""
    return {
        tuple(dotted.split(".")): name
        for dotted, name in reference.credential_files().items()
        if name
    }


def credential_for(path: tuple[str, ...] | list[str]) -> dict | None:
    """How the credential at `path` is edited, or None for a plain field.

    A file credential is edited through `secrets` by its file's name, even
    when it is not secret-named (the SMS identifiers), because routing follows
    membership, not secrecy. Any other secret leaf is an inline credential,
    named by its dotted label.
    """
    from istota.config_editor.secrets import label

    path = tuple(path)
    name = file_credentials().get(path)
    if name:
        return {"kind": "file", "name": name}
    if is_secret_path(path):
        return {"kind": "inline", "name": label(path)}
    return None


def choices_for(path: tuple[str, ...] | list[str]) -> tuple[str, ...] | None:
    return config_module.FIELD_CHOICES.get(tuple(path))


def multi_choices_for(path: tuple[str, ...] | list[str]) -> tuple[str, ...] | None:
    vocabulary = config_module.MULTI_CHOICES.get(tuple(path))
    return tuple(vocabulary()) if vocabulary is not None else None


def env_override_for(
    path: tuple[str, ...] | list[str], environ: Mapping[str, str] | None = None,
) -> str | None:
    """The variable that sets `path` over the file right now, or None.

    A file credential's variable never counts: on the stack every secrets file
    is exported as one, and it is edited through the file, not the config.
    """
    environ = os.environ if environ is None else environ
    path = tuple(path)
    for var, target in config_module.ENV_OVERRIDES:
        if target == path and environ.get(var, "").strip():
            return var
    if path in file_credentials():
        return None
    for var, section, key in config_module.SECRET_ENV_OVERRIDES:
        if tuple(section.split(".")) + (key,) == path and environ.get(var, "").strip():
            return var
    return None


def _scalar_kind(declared: Any) -> str | None:
    declared = _unwrap_optional(declared)
    origin = typing.get_origin(declared) or declared
    if declared is bool:
        return "bool"
    if declared is int:
        return "int"
    if declared is float:
        return "float"
    if declared is str:
        return "str"
    if isinstance(declared, type) and issubclass(declared, Path):
        return "path"
    if origin in (list, tuple, set, frozenset):
        return "list"
    return None


def _value_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    for kind, cls in (("int", int), ("float", float), ("str", str), ("path", Path)):
        if isinstance(value, cls):
            return kind
    if isinstance(value, (list, tuple, set, frozenset)):
        return "list"
    return None


def editor_kind(path: tuple[str, ...] | list[str], declared: Any, value: Any) -> str:
    """The control for the field at `path`, its declared type `declared`.

    `bool` | `int` | `float` | `str` | `path` | `list` | `choice` |
    `multi_choice` | `secret` | `table` | `readonly`. What a live environment
    variable or a managed resource makes read-only is the view's to decide.
    """
    path = tuple(path)
    if path in READ_ONLY:
        return "readonly"
    if path in TABLE_PATHS:
        return "table"
    if credential_for(path) is not None:
        return "secret"
    if path in config_module.FIELD_CHOICES:
        return "choice"
    if path in config_module.MULTI_CHOICES:
        return "multi_choice"
    return _scalar_kind(declared) or _value_kind(value) or "readonly"
