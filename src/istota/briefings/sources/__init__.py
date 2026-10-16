"""Source resolvers for the briefings module.

Each resolver gathers content for one :class:`BlockSource` and returns a
:class:`GatheredSource`. Personal sources fail soft: errors and empty reads
carry provenance rather than stopping generation. Group dispatch preserves
authorization and local-read refusals, which gathering rejects before rendering.

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


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
    failure_kind: Literal["unavailable", "refused"] | None = None
    error_code: str | None = None

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
    timezone: str | None = None

    @property
    def module_config(self):
        """The ``[briefings]`` module config, or defaults."""
        from istota.config import BriefingsModuleConfig

        cfg = getattr(self.app_config, "briefings", None)
        return cfg if cfg is not None else BriefingsModuleConfig()


def resolve_source(kind: str, config: dict, ctx: SourceContext, *, source_id=None) -> GatheredSource:
    """Dispatch to the resolver for ``kind``. Never raises.

    An unknown kind returns an empty, not-ok result. Any resolver exception is
    caught here as a last-resort backstop (resolvers are already defensive).
    """
    from istota.briefings.sources.group import GroupSourceContext, resolve_group_source

    if isinstance(ctx, GroupSourceContext):
        return resolve_group_source(kind, config or {}, ctx, source_id=source_id)
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


def raise_for_refused(gathered):
    from istota.groups.policy import GroupError

    if any(source.failure_kind == "refused" for source in gathered):
        raise GroupError(422, "source_refused", "A selected source was refused")


def gather_sources(sources, gather_one):
    """Bound concurrent I/O and return results in configured order."""
    if not sources:
        return []
    by_index = {}
    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
        futures = {pool.submit(gather_one, source): i for i, source in enumerate(sources)}
        for future in as_completed(futures):
            by_index[futures[future]] = future.result()
    return [by_index[i] for i in range(len(sources))]


def _load_resolvers() -> dict:
    """Import the resolver callables lazily (avoids import cycles at module load)."""
    from istota.briefings.sources import browse, builtins, email, kv, my_email, rss

    return {
        "rss": rss.resolve,
        "email": email.resolve,
        "my_email": my_email.resolve,
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
