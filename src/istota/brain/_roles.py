"""Global operator role-override state — provider-agnostic, per-namespace.

Role aliases like ``fast`` / ``general`` / ``smart`` are operator preference
("on this deployment, ``smart`` should mean my expensive model"). They
sit *above* the brain layer because the same role name should mean the
same thing regardless of which brain is loaded — the brain only decides
*how* the override target resolves to its own canonical model ID.

The override for a role is stored **per model namespace** so one ``smart``
abstraction can cover multiple brain families at once. A brain resolves a
role in *its own* namespace (``ClaudeCodeBrain`` / ``TmuxClaudeBrain`` →
``"anthropic"``; ``NativeBrain`` → ``"openai_compat"``), so a value written
for one namespace can never leak onto another brain's wire. This is the fix
for the cross-namespace bug: an operator writing ``smart = opus-46-high``
under ``anthropic`` and ``anthropic/claude-opus-4.8`` under ``openai_compat``
gets each brain resolving its own value.

The reserved namespace key ``"*"`` holds a *legacy* namespace-agnostic value
(from a flat ``[models.roles] role = "string"``), resolved by whichever brain
is active — exactly the pre-per-namespace behavior. At config-load time we
don't yet know which brain a given task will use (``source_type_overrides``
route per source type), so a flat string stays resolved-at-call-time rather
than being auto-expanded into a foreign-namespace slug.

A ``RoleTarget`` may carry an ``effort`` — either explicitly (an
``openai_compat`` slug can't encode effort, so it needs a field) or via an
effort-encoding provider alias resolved by the brain (``opus-high`` →
``high``). Explicit ``RoleTarget.effort`` wins over an alias-derived one.

Lifecycle: ``set_role_overrides`` is called once at config-load time with the
raw parsed ``[models.roles]`` TOML structure. After that, brains read via
``get_role_override_target(role, namespace)`` on every ``resolve_alias`` call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger("istota.brain.roles")

# The reserved namespace key for a legacy flat (namespace-agnostic) override.
LEGACY_NAMESPACE = "*"


@dataclass(frozen=True)
class RoleTarget:
    """A resolved role override target: a model name plus optional effort.

    ``model`` is a provider alias, canonical id, or endpoint slug — the brain
    resolves it through its *own* alias table (an ``anthropic`` value like
    ``opus-high`` resolves to ``claude-opus-4-8`` + effort ``high``; an
    ``openai_compat`` slug passes through verbatim). ``effort`` is an explicit
    override that wins over any effort the alias encodes.
    """

    model: str
    effort: str | None = None


# Module-level reference rebound atomically by ``set_role_overrides``. We
# never mutate the dict in place (no ``clear`` + ``update`` sequence) so any
# concurrent reader that did ``snapshot = _role_overrides`` sees a coherent
# table even mid-rebind. Today rebinds only happen at single-threaded
# config-load time, but a future SIGHUP/reload feature gets safety for free.
#
# Shape: ``role -> namespace -> RoleTarget``. The namespace key is a brain's
# ``model_namespace`` (``"anthropic"`` / ``"openai_compat"``) or the reserved
# ``"*"`` for a legacy flat value.
_role_overrides: dict[str, dict[str, RoleTarget]] = {}


def _coerce_target(role: str, namespace: str, raw: object) -> RoleTarget | None:
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
                "ignoring [models.roles] %s.%s: table missing a 'model' string",
                role, namespace,
            )
            return None
        effort = raw.get("effort")
        effort_str = effort.strip() if isinstance(effort, str) and effort.strip() else None
        return RoleTarget(model=model.strip(), effort=effort_str)
    logger.warning(
        "ignoring [models.roles] %s.%s: value %r is neither a string nor a table",
        role, namespace, raw,
    )
    return None


def set_role_overrides(overrides: Mapping[str, object] | None) -> None:
    """Replace the role-override table with the operator's mapping.

    Accepts the raw parsed ``[models.roles]`` structure, where each role maps to
    **either**:

    - a bare string (legacy flat) → stored under the ``"*"`` namespace, and
    - a per-namespace table (``{anthropic: "...", openai_compat: {...}}``) where
      each value is a bare string or an inline ``{model, effort}`` table.

    Malformed roles/values are dropped with a warning; blank role names and
    empty targets are skipped silently (TOML parsers produce these from blank
    lines and operator keys are not interesting to log). Calling with ``{}`` or
    ``None`` clears the table back to "no overrides", in which case each brain
    falls back to its own default role mapping.

    Per-entry semantic validation (collision with provider aliases, unknown
    targets) lives on the active brain via ``Brain.validate_role_override`` and
    is invoked by the config loader, not here, since this module stays
    brain-agnostic.
    """
    global _role_overrides
    next_overrides: dict[str, dict[str, RoleTarget]] = {}
    if overrides:
        for role, value in overrides.items():
            if not isinstance(role, str) or not role.strip():
                if role is not None and not isinstance(role, str):
                    logger.warning("ignoring non-string role override key: %r", role)
                continue
            role_key = role.strip().lower()
            targets: dict[str, RoleTarget] = {}
            if isinstance(value, str):
                target = _coerce_target(role_key, LEGACY_NAMESPACE, value)
                if target is not None:
                    targets[LEGACY_NAMESPACE] = target
            elif isinstance(value, Mapping):
                for namespace, raw in value.items():
                    if not isinstance(namespace, str) or not namespace.strip():
                        logger.warning(
                            "ignoring [models.roles] %s: non-string namespace key %r",
                            role_key, namespace,
                        )
                        continue
                    target = _coerce_target(role_key, namespace.strip(), raw)
                    if target is not None:
                        targets[namespace.strip()] = target
            else:
                logger.warning(
                    "ignoring [models.roles] %s: value %r is neither a string nor a table",
                    role_key, value,
                )
                continue
            if targets:
                next_overrides[role_key] = targets
    # Atomic rebind — readers see either the old table or the new one,
    # never a half-cleared mid-state.
    _role_overrides = next_overrides
    if _role_overrides:
        logger.info("role overrides: %s", _format_overrides(_role_overrides))


def _format_overrides(table: Mapping[str, Mapping[str, RoleTarget]]) -> str:
    parts: list[str] = []
    for role in sorted(table):
        for namespace in sorted(table[role]):
            rt = table[role][namespace]
            effort = f",{rt.effort}" if rt.effort else ""
            parts.append(f"{role}[{namespace}]={rt.model}{effort}")
    return ", ".join(parts)


def get_role_overrides() -> dict[str, dict[str, RoleTarget]]:
    """Return a copy of the live override table (``role -> namespace -> RoleTarget``).

    Callers that only read role *names* (``is_portable_alias`` via the raw
    config mapping, ``cron_loader`` membership) rely on the top-level keys,
    which are unchanged. A deep-ish copy of the nested dicts guards against a
    caller mutating the live table.
    """
    return {role: dict(namespaces) for role, namespaces in _role_overrides.items()}


def get_role_override_target(role: str, namespace: str) -> RoleTarget | None:
    """Return the override ``RoleTarget`` for ``role`` in ``namespace``, or None.

    Precedence: the per-namespace value (``[role][namespace]``) wins; else the
    legacy flat value (``[role]["*"]``); else None (the caller uses its brain's
    code-level default floor).
    """
    entry = _role_overrides.get(role.lower())
    if not entry:
        return None
    per_namespace = entry.get(namespace)
    if per_namespace is not None:
        return per_namespace
    return entry.get(LEGACY_NAMESPACE)


__all__ = [
    "LEGACY_NAMESPACE",
    "RoleTarget",
    "get_role_override_target",
    "get_role_overrides",
    "set_role_overrides",
]
