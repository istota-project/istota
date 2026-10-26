"""Checking the model fields of a proposed config, in the `apply --check` child.

`model_problems(document, config)` runs after `apply.collect` has loaded the
document, against the config that load produced, a catalogue of the shipped
file and the discovery caches (`discovery.catalogue_from_caches`, which swaps
nothing), and the document's own `[models.aliases]` parsed with no state
touched. Every model field the document sets (`schema.MODEL_FIELDS`) goes
through `brain._resolve.resolve_selection`: exact refs and aliases only, never
a pattern (D10).

Two kinds:

- `model` (blocking, in `apply.BLOCKING_KINDS`): a field only Anthropic models
  can fill names `<prefix>/...` where `<prefix>` is no configured provider.
  An Anthropic id has no `/`, so no config written before providers existed
  holds such a value. Fields a native provider can fill are never judged this
  way, because an OpenRouter id carries a `/` whose head is a vendor
  (`z-ai/glm-5.3-flash`), not a provider.
- `model_unknown` (a warning): everything else that does not resolve, an
  effort on a field that takes none, and an alias whose target does not.

Doctor's `models.catalogue` leniency is kept: a native model is not judged
while native's provider has no model list, since the brain sends it as written.
"""

from __future__ import annotations

from typing import Any

from istota.config_editor import schema


def _lookup(document: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    node = document
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return False, None
        node = node[segment]
    return True, node


def _problem(kind: str, path: list[str], message: str) -> dict:
    return {"kind": kind, "path": path, "message": message}


def model_problems(document: dict, config: Any) -> list[dict]:
    """The `model` and `model_unknown` problems of `document`, loaded as `config`.

    Never raises: a check that cannot run reports nothing rather than
    refusing a plan.
    """
    try:
        return _model_problems(document, config)
    except Exception:  # noqa: BLE001 - a failed model check must not refuse a plan
        return []


def _model_problems(document: dict, config: Any) -> list[dict]:
    from istota.brain._aliases import split_effort
    from istota.brain._resolve import (
        BRAIN_NAMESPACES,
        Resolution,
        config_providers,
        native_provider_id,
        native_reach_ids,
        preferred_provider,
        resolve_selection,
    )
    from istota.brain._roles import LEGACY_NAMESPACE, parse_alias_overrides
    from istota.llm import discovery

    catalogue = discovery.catalogue_from_caches(config)
    aliases = parse_alias_overrides(config.models.aliases, log=False)
    providers = config_providers(config)
    native_id = native_provider_id(config)
    base_kind = getattr(config.brain, "kind", "") or None
    found: list[dict] = []

    for path, field in schema.MODEL_FIELDS.items():
        present, value = _lookup(document, path)
        if not present or not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        label = ".".join(path)
        base, effort = split_effort(value)
        if effort and not field.effort:
            found.append(_problem(
                "model_unknown", list(path), f"{label} takes no effort; :{effort} is ignored",
            ))
            continue
        head = base.split("/", 1)[0].lower() if "/" in base else ""
        if head and field.sources == "anthropic" and head not in providers:
            found.append(_problem(
                "model", list(path),
                f"{label} names the provider {head!r}, which is not configured",
            ))
            continue
        kind = field.brain or base_kind
        if kind == "native" and not native_id:
            continue
        resolved = resolve_selection(
            value, catalogue, aliases=aliases, for_brain=kind,
            providers=providers, native_provider=native_id,
            native_reach=native_reach_ids(config),
        )
        if isinstance(resolved, Resolution):
            continue
        if kind == "native" and resolved.code in ("unknown", "ambiguous") and not catalogue.models(native_id):
            continue
        found.append(_problem(
            "model_unknown", list(path), f"{label} = {value[:80]!r}: {resolved.message[:200]}",
        ))

    for name in aliases.names():
        for namespace in aliases.overrides.get(name, {}):
            effective = "anthropic" if namespace == LEGACY_NAMESPACE else namespace
            if effective not in BRAIN_NAMESPACES.values():
                continue
            prefer = preferred_provider(effective, config)
            if effective == BRAIN_NAMESPACES["native"] and (not prefer or not catalogue.models(prefer)):
                continue
            resolved = resolve_selection(
                name, catalogue, aliases=aliases, namespace=effective,
                prefer_provider=prefer, providers=providers,
            )
            if isinstance(resolved, Resolution):
                continue
            found.append(_problem(
                "model_unknown", ["models", "aliases", name],
                f"[models.aliases] {name} ({namespace}): {resolved.message[:200]}",
            ))
    return found
