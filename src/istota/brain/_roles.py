"""Global operator role-override state — provider-agnostic.

Role aliases like ``fast`` / ``general`` / ``smart`` are operator preference
("on this deployment, ``smart`` should mean my expensive model"). They
sit *above* the brain layer because the same role name should mean the
same thing regardless of which brain is loaded — the brain only decides
*how* the override target string resolves to its own canonical model ID.

State here is intentionally a flat ``dict[str, str]`` of raw strings:
``{"smart": "opus-46-high"}``. Each brain's ``resolve_alias`` reads this
table and resolves the RHS through its own provider alias table, so the
operator can write provider-aware shortcuts (``opus-46-high``) and they
resolve correctly under whichever brain is active.

Lifecycle: ``set_role_overrides`` is called once at config-load time
with the parsed ``[models.roles]`` TOML table. After that, brains read
via ``get_role_overrides`` on every ``resolve_alias`` call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger("istota.brain.roles")


# Module-level reference rebound atomically by ``set_role_overrides``. We
# never mutate the dict in place (no ``clear`` + ``update`` sequence) so any
# concurrent reader that did ``snapshot = _role_overrides`` sees a coherent
# table even mid-rebind. Today rebinds only happen at single-threaded
# config-load time, but a future SIGHUP/reload feature gets safety for free.
_role_overrides: dict[str, str] = {}


def set_role_overrides(overrides: Mapping[str, str] | None) -> None:
    """Replace the role-override table with the operator's mapping.

    Non-string keys/values are dropped with a warning; empty role names and
    empty/whitespace-only targets are skipped silently (TOML parsers can
    produce these from blank lines and operator-controlled keys are not
    interesting to log). Calling with ``{}`` or ``None`` clears the table
    back to "no overrides", in which case each brain falls back to its own
    default role mapping.

    Per-entry semantic validation (collision with provider aliases, unknown
    targets) lives on the active brain via ``Brain.validate_role_override``
    and is invoked by the config loader, not here, since this module stays
    brain-agnostic.
    """
    global _role_overrides
    next_overrides: dict[str, str] = {}
    if overrides:
        for role, target in overrides.items():
            if not isinstance(role, str) or not isinstance(target, str):
                logger.warning("ignoring non-string role override: %r=%r", role, target)
                continue
            if not role.strip() or not target.strip():
                continue
            next_overrides[role.lower()] = target.strip()
    # Atomic rebind — readers see either the old table or the new one,
    # never a half-cleared mid-state.
    _role_overrides = next_overrides
    if _role_overrides:
        logger.info(
            "role overrides: %s",
            ", ".join(f"{r}={t}" for r, t in sorted(_role_overrides.items())),
        )


def get_role_overrides() -> dict[str, str]:
    """Return a copy of the live override table."""
    return dict(_role_overrides)


def get_role_override(role: str) -> str | None:
    """Return the raw override target for ``role``, or None if not overridden."""
    return _role_overrides.get(role.lower())


__all__ = [
    "get_role_override",
    "get_role_overrides",
    "set_role_overrides",
]
