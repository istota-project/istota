"""Source resolvers for the briefings module.

Each resolver gathers content for one :class:`BlockSource` and returns a
:class:`GatheredSource`. The contract is **fail-soft**: a resolver that errors
or finds nothing returns an empty result carrying a ``provenance`` note; it
never raises to the generation pipeline. This keeps one bad source (a dead
feed, an unreachable browser, an empty inbox) from failing the whole briefing.

Four ingestion paths under one typed abstraction:

* ``rss`` — recent entries from a Feeds subscription/category (soft dep).
* ``email`` — the shared/unowned mail pool (zero-config) or a sender allowlist.
* ``browse`` — a user-defined URL (or preset) fetched via the browse skill.
* ``markets`` / ``calendar`` / ``todos`` / ``reminders`` / ``notes`` — the
  existing structured built-ins wrapped as source kinds.
* ``shared_block`` — pre-made curated content read from the shared_kv store, so
  expensive shared generation runs once globally instead of once per user.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)


@dataclass
class GatheredSource:
    """The content one source contributed to a block.

    A resolver populates *either* ``items`` (structured entries the model
    synthesizes) *or* ``text`` (pre-formatted content included near-verbatim,
    e.g. markets/calendar/browse). ``provenance`` is a short human note about
    where it came from or why it is empty; ``ok`` is False when nothing
    usable was gathered (an empty/failed source).
    """

    kind: str
    title: str
    items: list[dict] = field(default_factory=list)
    text: str = ""
    provenance: str = ""
    ok: bool = True
    # When True, the rendered content is wrapped in an untrusted-content
    # delimiter at prompt-assembly time (web-sourced curated content that a
    # trusted identity merely relayed — the content itself is untrusted).
    untrusted: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.items and not self.text.strip()


@dataclass
class SourceContext:
    """Everything a resolver may need, threaded from the generation pipeline.

    ``conn`` is a framework-DB connection (for email ownership / Feeds gating);
    it may be ``None`` when a caller has no DB access (an email source then
    fails closed — see ``email.py``). ``now`` is injectable for deterministic
    tests.
    """

    app_config: Any
    user_id: str
    conn: sqlite3.Connection | None = None
    now: datetime | None = None

    @property
    def module_config(self):
        """The ``[briefings]`` module config, or defaults."""
        from istota.config import BriefingsModuleConfig

        cfg = getattr(self.app_config, "briefings", None)
        return cfg if cfg is not None else BriefingsModuleConfig()


def resolve_source(kind: str, config: dict, ctx: SourceContext) -> GatheredSource:
    """Dispatch to the resolver for ``kind``. Never raises.

    An unknown kind returns an empty, not-ok result. Any resolver exception is
    caught here as a last-resort backstop (resolvers are already defensive).
    """
    try:
        resolver = _RESOLVERS.get(kind)
        if resolver is None:
            return GatheredSource(
                kind=kind, title=kind,
                provenance=f"(unknown source kind '{kind}')", ok=False,
            )
        return resolver(config or {}, ctx)
    except Exception as e:  # noqa: BLE001 — fail-soft backstop
        logger.warning("briefings source %s failed: %s", kind, e)
        return GatheredSource(
            kind=kind, title=kind,
            provenance=f"({kind} source error)", ok=False,
        )


def _load_resolvers() -> dict:
    """Import the resolver callables lazily (avoids import cycles at module load)."""
    from istota.briefings.sources import browse, builtins, email, kv, rss

    return {
        "rss": rss.resolve,
        "email": email.resolve,
        "browse": browse.resolve,
        "markets": builtins.resolve_markets,
        "calendar": builtins.resolve_calendar,
        "todos": builtins.resolve_todos,
        "reminders": builtins.resolve_reminders,
        "notes": builtins.resolve_notes,
        "shared_block": kv.resolve_shared_block,
    }


class _LazyResolvers:
    """Dict-like that populates on first access (breaks the import cycle:
    ``sources.__init__`` ⇄ resolver modules importing ``GatheredSource``)."""

    _cache: dict | None = None

    def get(self, kind: str):
        if self._cache is None:
            self._cache = _load_resolvers()
        return self._cache.get(kind)


_RESOLVERS = _LazyResolvers()
