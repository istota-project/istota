"""What an app may ask for: one named permission each, parsed and scoped on the host.

A capability is written ``<family>`` or ``<family>:<scope>``: ``kv:own``,
``kv:read:notes``, ``files:read:notes/aviation``, ``calendar:read``,
``calendar:read:Personal``, ``skill:markets.quote``. :class:`Capability` keeps
the family as ``name`` and the qualifier as ``scope``; ``text`` puts them back.

The scope rules here are the validation half. The bridge checks a call's
arguments against the scope stored in ``app_grants`` at call time; a calendar
name in particular is only resolved then.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from istota.sandbox.confined import valid_part
from istota.sandbox.kv_namespaces import is_reserved_namespace

#: Written to ``app_grants`` for every version without asking.
ALWAYS_GRANTED = ("kv:own", "files:own")

#: The read-only skill operations an app may name. ``apps/skill_caps.py``
#: (stage 15) supplies a handler for each, and its keys must equal this set.
SKILL_OPERATIONS = frozenset({
    "markets.quote",
    "health.trend",
    "location.current",
    "location.places",
    "memory_search.facts",
    "memory_search.timeline",
})

#: First segments of the user's tree no ``files:read`` scope may start with.
#: The bot directory is added per call, since its name comes from config.
FILES_READ_DENIED = ("memories",)

# No KV namespace rule exists elsewhere in the tree; this is the apps' own,
# deliberately narrow. A leading underscore is reserved and can never match.
_KV_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_CALENDAR_NAME_MAX = 200


class CapabilityError(ValueError):
    """A capability the host will not grant, with a sentence naming it."""


@dataclass(frozen=True)
class Capability:
    name: str
    scope: str | None = None

    @property
    def text(self) -> str:
        if self.scope is None:
            return self.name
        return f"{self.name}:{self.scope}"


def _kv_read(namespace: str, text: str) -> Capability:
    if not _KV_NAMESPACE.fullmatch(namespace) or is_reserved_namespace(namespace):
        raise CapabilityError(f"{text}: not a KV namespace an app may read.")
    if namespace.startswith("app."):
        raise CapabilityError(f"{text}: an app may not read another app's data.")
    return Capability("kv:read", namespace)


def _files_read(path: str, text: str, bot_dir: str) -> Capability:
    segments = path.split("/")
    for segment in segments:
        if not valid_part(segment) or segment.startswith(".") or any(ord(c) < 0x20 for c in segment):
            raise CapabilityError(
                f"{text}: a folder is a relative path with no empty, '.', '..' or hidden part."
            )
    # Compared without case: a case-insensitive workspace would otherwise let
    # `Memories` reach `memories`.
    first = segments[0].casefold()
    if first == bot_dir.casefold() or first in FILES_READ_DENIED:
        raise CapabilityError(f"{text}: apps may not read the {segments[0]} folder.")
    return Capability("files:read", path)


def _calendar_read(name: str, text: str) -> Capability:
    if not name.strip() or len(name) > _CALENDAR_NAME_MAX or any(ord(c) < 0x20 for c in name):
        raise CapabilityError(f"{text}: not a calendar name.")
    return Capability("calendar:read", name)


def parse_capability(text: str, *, bot_dir: str) -> Capability:
    """The capability ``text`` names, or :class:`CapabilityError`."""
    if not isinstance(text, str):
        raise CapabilityError("A capability is a string.")
    if text in ("kv:own", "files:own", "calendar:read"):
        return Capability(text)
    if text.startswith("kv:read:"):
        return _kv_read(text[len("kv:read:"):], text)
    if text.startswith("files:read:"):
        return _files_read(text[len("files:read:"):], text, bot_dir)
    if text.startswith("calendar:read:"):
        return _calendar_read(text[len("calendar:read:"):], text)
    if text.startswith("skill:"):
        operation = text[len("skill:"):]
        if operation not in SKILL_OPERATIONS:
            raise CapabilityError(f"{text}: not an operation apps may call.")
        return Capability("skill", operation)
    raise CapabilityError(f"{text}: unknown capability.")


_SCOPED_FAMILIES = (("kv:read:", "kv:read"), ("files:read:", "files:read"),
                    ("calendar:read:", "calendar:read"), ("skill:", "skill"))


def split_capability(text: str) -> Capability:
    """Family and scope of a capability ``parse_capability`` already accepted.

    No checks: for text read back from a stored manifest, where the bot
    directory the scope rules need is not to hand.
    """
    for prefix, name in _SCOPED_FAMILIES:
        if text.startswith(prefix):
            return Capability(name, text[len(prefix):])
    return Capability(text)


_SKILL_DESCRIPTIONS = {
    "markets.quote": "Look up market quotes for symbols it names",
    "health.trend": "Read your health records: biomarker trends",
    "location.current": "Read where you are now",
    "location.places": "Read your known places",
    "memory_search.facts": "Read the facts Istota has learned about you",
    "memory_search.timeline": "Read when Istota learned each fact about you",
}


def describe(cap: Capability) -> str:
    """One plain-language line for the approval screen."""
    if cap.name == "kv:own":
        return "Keep its own data"
    if cap.name == "files:own":
        return "Read and write files in its own data folder"
    if cap.name == "kv:read":
        return f"Read your data in the {cap.scope!r} store"
    if cap.name == "files:read":
        return f"Read your files in {cap.scope}"
    if cap.name == "calendar:read":
        if cap.scope is None:
            return "Read events from all your calendars"
        return f"Read events from your calendar {cap.scope!r}"
    if cap.name == "skill":
        return _SKILL_DESCRIPTIONS[cap.scope or ""]
    raise CapabilityError(f"{cap.text}: unknown capability.")


def is_sensitive(cap: Capability) -> bool:
    """Whether the approval screen shows this one under a warning."""
    return cap.name == "skill" and (cap.scope or "").startswith("health.")
