"""Portable alias layer + the orthogonal ``:effort`` modifier.

Two things live here, both at the naming boundary every brain shares:

**Portable tiers.** Role tiers (``fast`` / ``general`` / ``smart``, plus
operator-declared portable custom aliases) are the *only* provider-agnostic
model names. They are semantic intents, not provider IDs: ``smart`` means "the
best model this brain has" regardless of which brain is loaded. Every brain MUST
resolve every canonical role to a real model in its own namespace (its
``DEFAULT_ALIASES`` + overrides) — a contract test enforces this. Shortcuts
(``opus`` / ``sonnet`` / ``haiku``) and raw canonical IDs (``claude-opus-4-8``)
are NOT portable: they bind to one provider and are meaningless to a
different-provider fallback brain.

The fallback path uses ``is_portable_alias`` to decide, when the primary brain
is unavailable, whether to re-resolve the same *intent* in the fallback brain's
namespace (portable → carry the tier across the boundary) or drop to the
fallback brain's own default (non-portable → the explicit pin can't cross).

**The ``:effort`` modifier.** Effort is orthogonal to model choice — a separate
axis on every surface (``task.effort``, ``!room effort``, the ``{model,
effort}`` alias-target shape). ``split_effort`` peels a trailing ``:<effort>``
off any model reference so effort composes on canonical ids, tiers, and
shortcuts alike (``opus:high``, ``smart:low``, ``claude-opus-4-8:xhigh``),
instead of being baked into a hand-maintained ``opus-high`` cross-product.
"""

from __future__ import annotations

from collections.abc import Iterable

# The canonical role tiers every brain must resolve. Single source of truth —
# each brain's alias table imports this rather than re-declaring the names.
CANONICAL_ROLES: tuple[str, ...] = ("fast", "general", "smart")

# The effort levels the ``:effort`` modifier accepts — the single source of
# truth, re-imported by ``commands._EFFORT_LEVELS`` and the brains. A model that
# doesn't support a given effort silently drops it at the wire.
EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})


def split_effort(raw: str) -> tuple[str, str | None]:
    """Split a model reference ``<base>:<effort>`` into ``(base, effort|None)``.

    A trailing ``:<suffix>`` peels off only when ``suffix`` (case-folded) is a
    known effort level and ``base`` is non-empty. Otherwise the whole string is
    returned as the base with no effort. Safe for every reference shape:

    - no colon (``claude-opus-4-8``, ``opus``) → ``(raw, None)``
    - OpenRouter slug (``anthropic/claude-sonnet-4``) → the ``/`` is untouched;
      only a real ``:effort`` tail splits (``…-4:high`` → base + ``high``)
    - bare ``:high`` (empty base) or ``opus:`` (empty/unknown suffix) → ``(raw, None)``
    - a hypothetical ``provider:model`` id whose right half isn't an effort level
      is left intact
    """
    if not raw:
        return (raw, None)
    base, sep, suffix = raw.rpartition(":")
    if sep and base and suffix.lower() in EFFORT_LEVELS:
        return (base, suffix.lower())
    return (raw, None)


def is_portable_alias(
    name: str | None, portable_names: Iterable[str] | None = None
) -> bool:
    """True iff ``name`` is a canonical role tier or a declared-portable alias —
    a provider-agnostic intent any brain can resolve in its own namespace.

    ``portable_names`` is the operator-declared portable set (custom aliases
    flagged ``portable = true`` in ``[models.aliases]``), unioned with the
    canonical tiers. A ``:effort`` modifier is stripped before the check, so
    ``smart:low`` reads as portable. Empty / None ``name`` is not portable (the
    caller never picked a model, so there is no intent to carry — it uses the
    fallback brain's own default).
    """
    if not name or not name.strip():
        return False
    base, _effort = split_effort(name.strip())
    lowered = base.strip().lower()
    if lowered in CANONICAL_ROLES:
        return True
    if portable_names:
        return lowered in {str(n).strip().lower() for n in portable_names}
    return False


__all__ = ["CANONICAL_ROLES", "EFFORT_LEVELS", "is_portable_alias", "split_effort"]
