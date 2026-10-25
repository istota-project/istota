"""An app's ``manifest.toml``: who it is, what it asks for, and the narrowest pane it works in.

Every problem is collected, so the user sees them all at once in one
:class:`ManifestError`. An unknown key is a warning rather than an error, so a
later manifest version can add keys.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

from istota.apps.capabilities import ALWAYS_GRANTED, Capability, CapabilityError, parse_capability

APP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ICON = re.compile(r"[a-z0-9-]{1,40}")
DISPLAY_RANGES = ("phone", "tablet", "desktop")
ENTRY = "index.html"
NAME_MAX = 40
DESCRIPTION_MAX = 200

_TOP_LEVEL_KEYS = frozenset({"id", "name", "icon", "entry", "capabilities", "description", "display"})
_DISPLAY_KEYS = frozenset({"narrowest", "reason"})


class ManifestError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    icon: str
    entry: str
    description: str
    capabilities: tuple[Capability, ...]
    narrowest: str
    reason: str
    warnings: tuple[str, ...] = ()


def flatten(text: str) -> str:
    """One line: every run of whitespace, line breaks included, as one space."""
    return " ".join(text.split())


def _string(data: dict, key: str, errors: list[str], *, default: str | None = None) -> str | None:
    if key not in data:
        if default is None:
            errors.append(f"manifest.toml: {key} is missing.")
        return default
    value = data[key]
    if not isinstance(value, str):
        errors.append(f"manifest.toml: {key} must be a string.")
        return None
    return value


def _capabilities(data: dict, bot_dir: str, errors: list[str]) -> tuple[Capability, ...]:
    raw = data.get("capabilities", [])
    if not isinstance(raw, list):
        errors.append("manifest.toml: capabilities must be a list of strings.")
        raw = []
    texts: list[str] = list(ALWAYS_GRANTED)
    for item in raw:
        if not isinstance(item, str):
            errors.append("manifest.toml: capabilities must be a list of strings.")
            continue
        if item not in texts:
            texts.append(item)
    caps = []
    for text in texts:
        try:
            caps.append(parse_capability(text, bot_dir=bot_dir))
        except CapabilityError as exc:
            errors.append(f"manifest.toml: {exc}")
    return tuple(caps)


def _display(data: dict, errors: list[str], warnings: list[str]) -> tuple[str, str]:
    display = data.get("display", {})
    if not isinstance(display, dict):
        errors.append("manifest.toml: display must be a table.")
        return "phone", ""
    for key in sorted(set(display) - _DISPLAY_KEYS):
        warnings.append(f"manifest.toml: unknown key display.{key} is ignored.")
    narrowest = _string(display, "narrowest", errors, default="phone")
    reason = _string(display, "reason", errors, default="")
    if narrowest is not None and narrowest not in DISPLAY_RANGES:
        errors.append("manifest.toml: display.narrowest must be phone, tablet or desktop.")
    reason = flatten(reason or "")
    if narrowest in ("tablet", "desktop") and not reason:
        errors.append("manifest.toml: display.reason is required when display.narrowest is not phone.")
    return narrowest or "phone", reason


def parse_manifest(text: str, *, folder_name: str, bot_dir: str) -> Manifest:
    """The manifest in ``text``, for the draft folder ``folder_name``. Raises :class:`ManifestError`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError([f"manifest.toml is not valid TOML: {exc}"]) from exc

    errors: list[str] = []
    warnings: list[str] = []
    for key in sorted(set(data) - _TOP_LEVEL_KEYS):
        warnings.append(f"manifest.toml: unknown key {key} is ignored.")

    app_id = _string(data, "id", errors)
    if app_id is not None:
        if not APP_ID.fullmatch(app_id):
            errors.append(
                "manifest.toml: id must be 1 to 40 lowercase letters, digits or dashes, "
                "starting with a letter or digit."
            )
        elif app_id != folder_name:
            errors.append(f"manifest.toml: id {app_id!r} must equal the folder name {folder_name!r}.")

    name = _string(data, "name", errors)
    if name is not None:
        name = flatten(name)
        if not 1 <= len(name) <= NAME_MAX:
            errors.append(f"manifest.toml: name must be 1 to {NAME_MAX} characters.")

    icon = _string(data, "icon", errors)
    if icon is not None and not ICON.fullmatch(icon):
        errors.append("manifest.toml: icon must be 1 to 40 lowercase letters, digits or dashes.")

    entry = _string(data, "entry", errors, default=ENTRY)
    if entry is not None and entry != ENTRY:
        errors.append(f"manifest.toml: entry must be {ENTRY}.")

    description = _string(data, "description", errors, default="")
    description = flatten(description or "")
    if len(description) > DESCRIPTION_MAX:
        errors.append(f"manifest.toml: description must be at most {DESCRIPTION_MAX} characters.")

    capabilities = _capabilities(data, bot_dir, errors)
    narrowest, reason = _display(data, errors, warnings)

    if errors:
        raise ManifestError(errors)
    return Manifest(
        id=app_id or "",
        name=name or "",
        icon=icon or "",
        entry=ENTRY,
        description=description,
        capabilities=capabilities,
        narrowest=narrowest,
        reason=reason,
        warnings=tuple(warnings),
    )
