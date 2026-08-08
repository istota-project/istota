"""Global operator alias-override state — provider-agnostic, per-namespace.

Model aliases (portable tiers ``fast`` / ``general`` / ``smart``, provider
shortcuts ``opus`` / ``sonnet`` / ``haiku``, and operator-defined custom names)
are operator preference: "on this deployment, ``smart`` should mean my expensive
model", or "``opus`` should pin the 4.7 release". They sit *above* the brain
layer because the same alias name should mean the same thing regardless of which
brain is loaded — the brain only decides *how* the override target resolves to
its own canonical model ID.

The override for an alias is stored **per model namespace** so one ``smart``
abstraction can cover multiple brain families at once. A brain resolves an alias
in *its own* namespace (``ClaudeCodeBrain`` / ``TmuxClaudeBrain`` →
``"anthropic"``; ``NativeBrain`` → ``"openai_compat"``), so a value written for
one namespace can never leak onto another brain's wire. This is the fix for the
cross-namespace bug: an operator writing ``smart = "opus"`` under ``anthropic``
and ``anthropic/claude-opus-4.8`` under ``openai_compat`` gets each brain
resolving its own value.

The reserved namespace key ``"*"`` holds a *legacy* namespace-agnostic value
(from a flat ``[models.aliases] name = "string"``), resolved by whichever brain
is active. At config-load time we don't yet know which brain a given task will
use (``source_type_overrides`` route per source type), so a flat string stays
resolved-at-call-time rather than being auto-expanded into a foreign-namespace
slug.

A ``RoleTarget`` may carry an ``effort`` — either explicitly (an
``openai_compat`` slug can't encode effort, so it needs a field) or via a
``:effort`` modifier on the target that the brain splits off. Explicit
``RoleTarget.effort`` wins.

The reserved per-alias key ``portable = true`` (a sibling of the namespace keys)
marks a custom alias as a cross-brain intent that survives the fallback boundary
(like the built-in tiers); it is stripped before namespace parsing and recorded
separately, read via ``get_portable_alias_names``.

Lifecycle: ``set_alias_overrides`` is called once at config-load time with the
raw parsed ``[models.aliases]`` TOML structure. After that, brains read via
``get_alias_override_target(name, namespace)`` on every ``resolve_alias`` call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger("istota.brain.aliases")

# The reserved namespace key for a legacy flat (namespace-agnostic) override.
LEGACY_NAMESPACE = "*"

# The reserved per-alias key marking a custom alias portable across the
# cross-brain fallback boundary. Sibling of the namespace keys in an
# ``[models.aliases.<name>]`` table; never itself a namespace.
PORTABLE_KEY = "portable"


@dataclass(frozen=True)
class RoleTarget:
    """A resolved alias override target: a model name plus optional effort.

    ``model`` is a provider alias, canonical id, or endpoint slug — the brain
    resolves it through its *own* alias table (an ``anthropic`` value like
    ``opus`` resolves to ``claude-opus-5``; an ``openai_compat`` slug passes
    through verbatim). ``effort`` is an explicit override that wins over any
    effort a ``:effort`` modifier on the target encodes.
    """

    model: str
    effort: str | None = None


# Module-level references rebound atomically by ``set_alias_overrides``. We
# never mutate the dicts in place (no ``clear`` + ``update`` sequence) so any
# concurrent reader that did ``snapshot = _alias_overrides`` sees a coherent
# table even mid-rebind. Today rebinds only happen at single-threaded
# config-load time, but a future SIGHUP/reload feature gets safety for free.
#
# Shape: ``name -> namespace -> RoleTarget``. The namespace key is a brain's
# ``model_namespace`` (``"anthropic"`` / ``"openai_compat"``) or the reserved
# ``"*"`` for a legacy flat value.
_alias_overrides: dict[str, dict[str, RoleTarget]] = {}

# Names an operator flagged ``portable = true`` (declared cross-brain intents).
_portable_names: set[str] = set()


def _is_truthy(value: object) -> bool:
    """Interpret a TOML ``portable`` value. Accept a bool or a common truthy
    string; anything else is falsy (a malformed value silently means "not
    portable" — the default)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return False


def _coerce_target(name: str, namespace: str, raw: object) -> RoleTarget | None:
    """Turn a raw namespace value (string or ``{model, effort}`` table) into a
    ``RoleTarget``. Returns None (with a warning) for a malformed value."""
    if isinstance(raw, str):
        model = raw.strip()
        if not model:
            return None
        return RoleTarget(model=model)
    if isinstance(raw, Mapping):
        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            logger.warning(
                "ignoring [models.aliases] %s.%s: table missing a 'model' string",
                name, namespace,
            )
            return None
        effort = raw.get("effort")
        effort_str = effort.strip() if isinstance(effort, str) and effort.strip() else None
        return RoleTarget(model=model.strip(), effort=effort_str)
    logger.warning(
        "ignoring [models.aliases] %s.%s: value %r is neither a string nor a table",
        name, namespace, raw,
    )
    return None


def set_alias_overrides(overrides: Mapping[str, object] | None) -> None:
    """Replace the alias-override table with the operator's mapping.

    Accepts the raw parsed ``[models.aliases]`` structure, where each alias maps
    to **either**:

    - a bare string (legacy flat) → stored under the ``"*"`` namespace, and
    - a per-namespace table (``{anthropic: "...", openai_compat: {...}}``) where
      each value is a bare string or an inline ``{model, effort}`` table. The
      reserved ``portable = true`` key (a sibling of the namespace keys) is
      stripped and recorded separately.

    Malformed aliases/values are dropped with a warning; blank names and empty
    targets are skipped silently. Calling with ``{}`` or ``None`` clears the
    table back to "no overrides", in which case each brain falls back to its own
    ``DEFAULT_ALIASES`` floor.

    Per-entry semantic validation (collision with a shortcut, unknown targets)
    lives on the active brain via ``Brain.validate_alias_override`` and is
    invoked by the config loader, not here, since this module stays
    brain-agnostic.
    """
    global _alias_overrides, _portable_names
    next_overrides: dict[str, dict[str, RoleTarget]] = {}
    next_portable: set[str] = set()
    if overrides:
        for name, value in overrides.items():
            if not isinstance(name, str) or not name.strip():
                if name is not None and not isinstance(name, str):
                    logger.warning("ignoring non-string alias override key: %r", name)
                continue
            name_key = name.strip().lower()
            targets: dict[str, RoleTarget] = {}
            if isinstance(value, str):
                target = _coerce_target(name_key, LEGACY_NAMESPACE, value)
                if target is not None:
                    targets[LEGACY_NAMESPACE] = target
            elif isinstance(value, Mapping):
                for namespace, raw in value.items():
                    if not isinstance(namespace, str) or not namespace.strip():
                        logger.warning(
                            "ignoring [models.aliases] %s: non-string namespace key %r",
                            name_key, namespace,
                        )
                        continue
                    ns_key = namespace.strip()
                    if ns_key.lower() == PORTABLE_KEY:
                        # Reserved flag, not a namespace — record and skip.
                        if _is_truthy(raw):
                            next_portable.add(name_key)
                        continue
                    target = _coerce_target(name_key, ns_key, raw)
                    if target is not None:
                        targets[ns_key] = target
            else:
                logger.warning(
                    "ignoring [models.aliases] %s: value %r is neither a string nor a table",
                    name_key, value,
                )
                continue
            if targets:
                next_overrides[name_key] = targets
    # Atomic rebind — readers see either the old table or the new one,
    # never a half-cleared mid-state.
    _alias_overrides = next_overrides
    _portable_names = next_portable
    if _alias_overrides:
        logger.info("alias overrides: %s", _format_overrides(_alias_overrides))
    if _portable_names:
        logger.info("portable aliases: %s", ", ".join(sorted(_portable_names)))


def _format_overrides(table: Mapping[str, Mapping[str, RoleTarget]]) -> str:
    parts: list[str] = []
    for name in sorted(table):
        for namespace in sorted(table[name]):
            rt = table[name][namespace]
            effort = f",{rt.effort}" if rt.effort else ""
            parts.append(f"{name}[{namespace}]={rt.model}{effort}")
    return ", ".join(parts)


def get_alias_overrides() -> dict[str, dict[str, RoleTarget]]:
    """Return a copy of the live override table (``name -> namespace -> RoleTarget``).

    Callers that only read alias *names* rely on the top-level keys, which are
    unchanged. A deep-ish copy of the nested dicts guards against a caller
    mutating the live table.
    """
    return {name: dict(namespaces) for name, namespaces in _alias_overrides.items()}


def get_alias_override_target(name: str, namespace: str) -> RoleTarget | None:
    """Return the override ``RoleTarget`` for ``name`` in ``namespace``, or None.

    Precedence: the per-namespace value (``[name][namespace]``) wins; else the
    legacy flat value (``[name]["*"]``); else None (the caller uses its brain's
    ``DEFAULT_ALIASES`` floor).
    """
    entry = _alias_overrides.get(name.lower())
    if not entry:
        return None
    per_namespace = entry.get(namespace)
    if per_namespace is not None:
        return per_namespace
    return entry.get(LEGACY_NAMESPACE)


def get_portable_alias_names() -> set[str]:
    """Return a copy of the operator-declared portable custom-alias names
    (``portable = true`` in ``[models.aliases]``). Unioned with CANONICAL_ROLES
    by the executor's ``config_alias_portable_names`` for the fallback check.
    """
    return set(_portable_names)


__all__ = [
    "LEGACY_NAMESPACE",
    "PORTABLE_KEY",
    "RoleTarget",
    "get_alias_override_target",
    "get_alias_overrides",
    "get_portable_alias_names",
    "set_alias_overrides",
]
