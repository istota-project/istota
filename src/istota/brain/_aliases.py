"""Portable alias layer — the cross-provider naming contract.

Role tiers (``fast`` / ``general`` / ``smart``, plus operator-defined custom
roles from ``[models.roles]``) are the *only* provider-agnostic model names.
They are semantic intents, not provider IDs: ``smart`` means "the best model
this brain has" regardless of which brain is loaded. Every brain MUST resolve
every canonical role to a real model in its own namespace (its
``DEFAULT_ROLE_TARGETS`` + role overrides) — a contract test enforces this.

Provider aliases (``opus-high``, ``sonnet``, ``haiku``, …) and raw canonical
IDs (``claude-opus-4-8``) are NOT portable: they bind to one provider and are
meaningless to a different-provider fallback brain.

The fallback path uses ``is_portable_alias`` to decide, when the primary brain
is unavailable, whether to re-resolve the same *intent* in the fallback brain's
namespace (portable → carry the role across the boundary) or drop to the
fallback brain's own default (non-portable → the explicit pin can't cross).
"""

from __future__ import annotations

from collections.abc import Mapping

# The canonical role tiers every brain must resolve. Single source of truth —
# each brain's role table imports this rather than re-declaring the names.
CANONICAL_ROLES: tuple[str, ...] = ("fast", "general", "smart")


def is_portable_alias(
    name: str | None, role_overrides: Mapping[str, str] | None = None
) -> bool:
    """True iff ``name`` is a canonical role tier or an operator-defined custom
    role — a provider-agnostic intent any brain can resolve in its own namespace.

    ``role_overrides`` is the operator ``[models.roles]`` mapping (custom role
    names beyond the three defaults count as portable too). Empty / None ``name``
    is not portable (the caller never picked a model, so there is no intent to
    carry — it uses the fallback brain's own default).
    """
    if not name or not name.strip():
        return False
    lowered = name.strip().lower()
    if lowered in CANONICAL_ROLES:
        return True
    if role_overrides:
        return any(lowered == str(role).strip().lower() for role in role_overrides)
    return False


__all__ = ["CANONICAL_ROLES", "is_portable_alias"]
