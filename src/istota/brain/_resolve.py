"""One resolver for every model selection.

A selection is what a person or a config wrote: an alias (``smart``,
``opus``, an operator's own), a qualified reference (``provider/id``), a bare
id, or, in chat commands only, a pattern. ``resolve_selection`` turns it into
a ``Resolution`` (the ref, its catalogue record, the effort asked for, and
which step matched) or a ``ResolveError`` with a code. Every surface asks it:
the brains' ``resolve_alias`` family, the fallback crossing rule, doctor.

Order, after splitting a trailing ``:effort`` with ``split_effort``:

1. **Alias**: an operator override for the namespace, else ``default``, the
   tiers (``fast`` / ``general`` / ``smart``, which name a family) and the
   family shortcuts (``opus`` / ``sonnet`` / ``haiku``: the newest current
   ``anthropic`` model of that family). An override's target is itself a
   selection, resolved one level deep without operator aliases.
2. **The preferred provider's own id**: an exact match among the models of the
   provider the brain uses. This sits before the ref step so a stored bare id
   keeps running where it ran: an OpenRouter id such as
   ``anthropic/claude-opus-5`` is OpenRouter's own id on a native brain that
   uses OpenRouter, not a reference to the built-in ``anthropic`` provider.
3. **Qualified reference**: the first ``/`` segment names a configured
   provider and the rest is that provider's id. With ``for_brain``, only a
   provider the brain reaches is read this way (see below). A ref the
   provider does not list falls through to step 4 before it is refused.
4. **Bare id**: an exact, case-insensitive match on ``id`` across providers.
   Several matches prefer the brain's provider, then refuse as ambiguous.
5. **Pattern** (``allow_patterns`` only): a case-insensitive substring of
   ``id`` or ``name``, over the models ``for_brain`` can run when it is
   given. Matches narrow to the preferred provider when it has any; one
   family (or one model) picks the undated, newest one; anything wider
   refuses as ambiguous and lists candidates.

What a brain reaches is ``llm.providers.brain_can_run``: the CLI brains the
`anthropic` provider, native the providers `[brain.native] providers` lists
(its default provider always among them). With ``for_brain``, step 3 reads a
ref only for a provider the brain reaches, so a value naming another provider
is the default provider's id when it lists it (step 2) and is refused
otherwise. That holds without a discovery cache: ``anthropic/claude-opus-5``
on an OpenRouter-native brain that does not list `anthropic` is never the
`anthropic` provider's, so it never bills Anthropic. When native's default
provider is an `openrouter` one and `anthropic` is listed too, the string is
ambiguous as typed input, and is read as OpenRouter's (its ``vendor/model``
space can hold it).

A stored selection is never ambiguous (D22): rooms, jobs and tasks keep the
provider in a column of its own, and ``selection_for_task`` resolves the
stored model within that one provider (``scope_provider``), so a task never
reads a provider out of a model string. ``select_for_person`` is the write
side: it resolves typed input against the room's provider, splits a
``provider/id`` an admin typed into the two columns, and refuses a provider
change to anyone else.

Then the refusals: ``model_not_runnable`` (``for_brain`` cannot run the
ref), ``model_not_enabled`` (``enabled_only`` and the ref is outside the
enabled set; aliases are exempt, because the operator wrote them) and
``no_tools`` (``needs_tools`` and the model has no tool calling).

The resolver is strict. The brains stay lenient: a selection it cannot
resolve is still sent as written, as it always was, and the strict answer is
doctor's to report.

``clamp_effort_level`` and ``clamp_effort`` check an effort against what a
model supports (pi's rule, upward first) and give the level or its wire value.
The brains call them at their ``execute`` seam; the stored effort stays what
was asked.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from istota.llm.catalog import SHIPPED_PROVIDER, Catalogue, ModelInfo, current_catalogue
from istota.llm.providers import brain_can_run, provider_kinds

from ._aliases import split_effort
from ._roles import AliasTable, current_alias_table

#: The family shortcuts: each is the newest current `anthropic` model of it.
FAMILY_SHORTCUTS: tuple[str, ...] = ("opus", "sonnet", "haiku")

#: The default tier targets, in code: names, not ids.
DEFAULT_TIER_TARGETS: dict[str, str] = {"fast": "haiku", "general": "sonnet", "smart": "opus"}

#: The reserved alias for "the brain's own configured default".
DEFAULT_ALIAS = "default"

SOURCES = ("alias", "ref", "id", "pattern")
ERROR_CODES = (
    "unknown", "ambiguous", "model_not_runnable", "model_not_enabled", "no_tools",
    "provider_admin_only",
)

#: The brain kinds that run the `claude` CLI, and so only the `anthropic` provider.
CLI_KINDS = frozenset({"claude_code"})

#: The alias namespace each brain kind resolves names in.
BRAIN_NAMESPACES: dict[str, str] = {
    "claude_code": "anthropic",
    "native": "openai_compat",
}

_DATED_ID = re.compile(r"-\d{8}\Z")


@dataclass(frozen=True)
class Resolution:
    """A selection resolved.

    ``ref`` and ``info`` are empty for ``brain_default``: the selection named
    the brain's own configured model (``default``, or a tier on a namespace
    whose tiers mean that), which only the brain knows.
    """

    ref: str
    info: ModelInfo | None
    effort_requested: str | None
    source: str
    alias: str | None = None
    brain_default: bool = False

    @property
    def provider(self) -> str:
        return self.ref.split("/", 1)[0] if self.ref else ""


@dataclass(frozen=True)
class ResolveError:
    """Why a selection did not resolve.

    ``target`` is the alias target an override named when that is what failed,
    so a lenient caller can still send it as written.
    """

    code: str
    selection: str
    message: str
    candidates: tuple[str, ...] = ()
    effort_requested: str | None = None
    target: str | None = None


# --- the catalogue index -----------------------------------------------------

_last_index: tuple[Catalogue, dict] | None = None


def _index(catalogue: Catalogue) -> dict:
    """``{"models": (...), "by_ref": {lower ref: info}, "by_id": {lower id: [info]}}``,
    cached for the last catalogue seen (snapshots are immutable)."""
    global _last_index
    cached = _last_index
    if cached is not None and cached[0] is catalogue:
        return cached[1]
    models = catalogue.models()
    by_ref: dict[str, ModelInfo] = {}
    by_id: dict[str, list[ModelInfo]] = {}
    for info in models:
        by_ref[info.ref.lower()] = info
        by_id.setdefault(info.id.lower(), []).append(info)
    index = {"models": models, "by_ref": by_ref, "by_id": by_id}
    _last_index = (catalogue, index)
    return index


def latest_in_family(catalogue: Catalogue, family: str) -> ModelInfo | None:
    """The newest current `anthropic` model of ``family``: what a shortcut means."""
    return catalogue.latest(family, SHIPPED_PROVIDER)


# --- resolution --------------------------------------------------------------


def _error(code, selection, message, *, candidates=(), effort=None, target=None) -> ResolveError:
    return ResolveError(
        code=code, selection=selection, message=message,
        candidates=tuple(candidates)[:20], effort_requested=effort, target=target,
    )


def _family(catalogue, name, selection, effort, alias) -> Resolution | ResolveError:
    info = latest_in_family(catalogue, name)
    if info is None:
        return _error("unknown", selection, f"no current {name} model in the catalogue", effort=effort)
    return Resolution(ref=info.ref, info=info, effort_requested=effort, source="alias", alias=alias)


def _rank(info: ModelInfo) -> tuple:
    # Undated before dated, then newest, then the id for a stable answer.
    return (0 if _DATED_ID.search(info.id) else 1, info.released or "", info.id)


def _model_step(
    base: str, selection: str, effort, catalogue, *, kinds, prefer, allow_patterns, pool,
    reaches=None, scope=None,
) -> Resolution | ResolveError:
    """Steps 2 to 5 for a base with no alias meaning. ``reaches(provider)``
    says whether step 3 may read a ref of that provider; ``None`` reads any.
    ``scope`` keeps step 4 to that provider's models."""
    index = _index(catalogue)
    lowered = base.lower()

    if prefer:
        for info in index["by_id"].get(lowered, ()):
            if info.provider == prefer:
                return Resolution(ref=info.ref, info=info, effort_requested=effort, source="id")

    head, sep, tail = base.partition("/")
    reachable = reaches is None or reaches(head)
    if sep and tail and head in kinds and reachable:
        info = index["by_ref"].get(lowered)
        if info is not None:
            return Resolution(ref=info.ref, info=info, effort_requested=effort, source="ref")

    matches = list(index["by_id"].get(lowered, ()))
    if scope:
        matches = [m for m in matches if m.provider == scope]
    if len(matches) == 1:
        info = matches[0]
        return Resolution(ref=info.ref, info=info, effort_requested=effort, source="id")
    if len(matches) > 1:
        preferred = [m for m in matches if m.provider == prefer]
        if len(preferred) == 1:
            info = preferred[0]
            return Resolution(ref=info.ref, info=info, effort_requested=effort, source="id")
        return _error(
            "ambiguous", selection, f"{base!r} is an id of several providers",
            candidates=sorted(m.ref for m in matches), effort=effort,
        )

    if sep and tail and head in kinds:
        if not reachable:
            return _error(
                "model_not_runnable", selection,
                f"{base!r} does not run on this brain: {prefer or 'its provider'!r} lists no "
                f"such id, and it is not read as a {head!r} model here", effort=effort,
            )
        return _error("unknown", selection, f"provider {head!r} lists no model {tail!r}", effort=effort)

    if allow_patterns:
        found = [
            m for m in pool
            if not m.deprecated and (lowered in m.id.lower() or lowered in (m.name or "").lower())
        ]
        if prefer and any(m.provider == prefer for m in found):
            found = [m for m in found if m.provider == prefer]
        if found:
            providers = {m.provider for m in found}
            families = {m.family for m in found}
            one_family = len(providers) == 1 and len(families) == 1 and None not in families
            if len(found) == 1 or one_family:
                info = max(found, key=_rank)
                return Resolution(ref=info.ref, info=info, effort_requested=effort, source="pattern")
            return _error(
                "ambiguous", selection, f"{base!r} matches several models",
                candidates=sorted(m.ref for m in found), effort=effort,
            )

    return _error("unknown", selection, f"no model {base!r} in the catalogue", effort=effort)


def _alias_step(
    name: str, selection: str, effort, catalogue, *, aliases, namespace, kinds, prefer,
    reaches=None, keys=None, scope=None,
) -> Resolution | ResolveError | None:
    """Step 1. ``None`` when ``name`` is no alias in ``namespace``, or under
    ``keys`` (``alias_keys``) when those are given."""
    target = None
    if aliases is not None:
        target = aliases.target_in(name, keys) if keys else aliases.target(name, namespace)
    if target is not None:
        raw = target.model
        if not raw:
            # Effort-only: keep the alias's own default target.
            raw = DEFAULT_TIER_TARGETS.get(name) or (name if name in FAMILY_SHORTCUTS else "")
            if not raw:
                return Resolution(
                    ref="", info=None, effort_requested=effort or target.effort,
                    source="alias", alias=name, brain_default=True,
                )
            if namespace != BRAIN_NAMESPACES["claude_code"] and name in DEFAULT_TIER_TARGETS:
                return Resolution(
                    ref="", info=None, effort_requested=effort or target.effort,
                    source="alias", alias=name, brain_default=True,
                )
        target_base, target_effort = split_effort(raw)
        chosen_effort = effort or target.effort or target_effort
        inner = _alias_step(
            target_base.lower(), selection, chosen_effort, catalogue,
            aliases=None, namespace=namespace, kinds=kinds, prefer=prefer, reaches=reaches,
            scope=scope,
        )
        if inner is None:
            inner = _model_step(
                target_base, selection, chosen_effort, catalogue,
                kinds=kinds, prefer=prefer, allow_patterns=False, pool=(), reaches=reaches,
                scope=scope,
            )
        if isinstance(inner, ResolveError):
            return _error(
                inner.code, selection, f"alias {name!r}: {inner.message}",
                candidates=inner.candidates, effort=chosen_effort, target=target_base,
            )
        return Resolution(
            ref=inner.ref, info=inner.info, effort_requested=inner.effort_requested,
            source="alias", alias=name, brain_default=inner.brain_default,
        )

    if name == DEFAULT_ALIAS:
        return Resolution(
            ref="", info=None, effort_requested=effort, source="alias", alias=name, brain_default=True,
        )
    if name in DEFAULT_TIER_TARGETS:
        if namespace == BRAIN_NAMESPACES["claude_code"]:
            return _family(catalogue, DEFAULT_TIER_TARGETS[name], selection, effort, name)
        # On native the tiers mean its one configured model unless remapped (NB-3).
        return Resolution(
            ref="", info=None, effort_requested=effort, source="alias", alias=name, brain_default=True,
        )
    if name in FAMILY_SHORTCUTS:
        return _family(catalogue, name, selection, effort, name)
    return None


def resolve_selection(
    selection: str,
    catalogue: Catalogue | None = None,
    *,
    aliases: AliasTable | None = None,
    for_brain: str | None = None,
    enabled_only: bool = False,
    enabled: Iterable[str] | None = None,
    namespace: str | None = None,
    prefer_provider: str | None = None,
    providers: Mapping[str, object] | None = None,
    native_provider: str | None = None,
    native_reach: Iterable[str] | None = None,
    use_aliases: bool = True,
    allow_patterns: bool = False,
    needs_tools: bool = False,
    alias_provider: str | None = None,
    scope_provider: str | None = None,
) -> Resolution | ResolveError:
    """Resolve one selection; see the module docstring for the order.

    ``catalogue`` and ``aliases`` default to the process's snapshots.
    ``namespace`` defaults to ``for_brain``'s, else ``anthropic``; the
    preferred provider defaults to the brain's (``anthropic`` for the CLI
    brains, ``native_provider`` for native). ``providers`` is the configured
    providers (``id -> spec``, config or kind); the built-ins are always in it.
    ``native_reach`` is `[brain.native] providers` (``native_reach_ids``).
    ``enabled`` is the glob list ``enabled_only`` checks against.
    ``alias_provider`` reads operator aliases by provider (``alias_keys``)
    rather than by namespace. ``scope_provider`` keeps every step to that one
    provider's models: what a stored selection is resolved with, since its
    provider is in a column of its own (D22). Never raises.
    """
    text = (selection or "").strip() if isinstance(selection, str) else ""
    if not text:
        return _error("unknown", "", "no model named")
    catalogue = catalogue if catalogue is not None else current_catalogue()
    if aliases is None and use_aliases:
        aliases = current_alias_table()
    kinds = provider_kinds(providers)
    namespace = namespace or BRAIN_NAMESPACES.get(for_brain or "", "anthropic")
    if prefer_provider is None:
        if scope_provider:
            prefer_provider = scope_provider
        elif for_brain in CLI_KINDS:
            prefer_provider = SHIPPED_PROVIDER
        elif for_brain == "native":
            prefer_provider = native_provider

    native_reach = tuple(native_reach or ())

    def _runs(ref: str) -> bool:
        return brain_can_run(
            for_brain, ref, providers, native_provider=native_provider, native_reach=native_reach,
        )

    reaches = None
    if scope_provider:
        def reaches(provider_id: str) -> bool:
            return provider_id == scope_provider
    elif for_brain:
        def reaches(provider_id: str) -> bool:
            if not _runs(f"{provider_id}/-"):
                return False
            # An `anthropic/...` value on an OpenRouter-default native brain is
            # OpenRouter's id even with `anthropic` listed (see the docstring).
            return not (
                for_brain == "native" and provider_id != prefer_provider
                and kinds.get(prefer_provider or "") == "openrouter"
                and kinds.get(provider_id) == "anthropic"
            )

    base, effort = split_effort(text)
    enabled_set: frozenset[str] | None = None
    if enabled_only:
        enabled_set = frozenset(enabled_refs(catalogue, enabled or ()))

    keys = alias_keys(for_brain, alias_provider, native_provider) if alias_provider else None
    result = None
    if use_aliases:
        result = _alias_step(
            base.lower(), text, effort, catalogue,
            aliases=aliases, namespace=namespace, kinds=kinds, prefer=prefer_provider,
            reaches=reaches, keys=keys, scope=scope_provider,
        )
    if result is None:
        pool = _index(catalogue)["models"]
        if enabled_set is not None:
            pool = tuple(m for m in pool if m.ref in enabled_set)
        if for_brain:
            pool = tuple(m for m in pool if _runs(m.ref))
        if scope_provider:
            pool = tuple(m for m in pool if m.provider == scope_provider)
        result = _model_step(
            base, text, effort, catalogue,
            kinds=kinds, prefer=prefer_provider, allow_patterns=allow_patterns, pool=pool,
            reaches=reaches, scope=scope_provider,
        )
    if isinstance(result, ResolveError) or result.brain_default:
        return result

    if scope_provider and result.provider != scope_provider:
        return _error(
            "model_not_runnable", text,
            f"{result.ref} is not a model of the {scope_provider} provider", effort=result.effort_requested,
        )
    if for_brain and not _runs(result.ref):
        return _error(
            "model_not_runnable", text, f"the {for_brain} brain cannot run {result.ref}",
            effort=result.effort_requested,
        )
    if enabled_set is not None and result.source != "alias" and result.ref not in enabled_set:
        return _error(
            "model_not_enabled", text, f"{result.ref} is not one of the models you can pick",
            effort=result.effort_requested,
        )
    if needs_tools and result.info is not None and not result.info.supports_tools:
        return _error(
            "no_tools", text, f"{result.ref} does not support tool calling, which a task needs",
            effort=result.effort_requested,
        )
    return result


# --- the enabled set ---------------------------------------------------------


def enabled_refs(catalogue: Catalogue, patterns: Iterable[str]) -> tuple[str, ...]:
    """The refs ``patterns`` (``fnmatch`` globs over refs, case-insensitive)
    match. An entry may carry ``:effort``, which does not narrow the match.
    A deprecated model is matched only by an entry naming it exactly."""
    globs = []
    for raw in patterns or ():
        if isinstance(raw, str) and raw.strip():
            globs.append(split_effort(raw.strip())[0].lower())
    out = []
    for info in _index(catalogue)["models"]:
        ref = info.ref.lower()
        for glob in globs:
            if info.deprecated:
                if glob == ref:
                    out.append(info.ref)
                    break
            elif fnmatch.fnmatchcase(ref, glob):
                out.append(info.ref)
                break
    return tuple(out)


def unmatched_patterns(catalogue: Catalogue, patterns: Iterable[str]) -> list[str]:
    """The entries of ``patterns`` that match no model in ``catalogue``."""
    return [p for p in patterns or () if isinstance(p, str) and not enabled_refs(catalogue, [p])]


def default_enabled_patterns(config) -> list[str]:  # noqa: ANN001
    """What non-admins may pick when ``[models] enabled`` is not set:
    ``anthropic/*``, plus native's configured model when native has a provider."""
    patterns = [f"{SHIPPED_PROVIDER}/*"]
    native_id = native_provider_id(config)
    model = (getattr(getattr(getattr(config, "brain", None), "native", None), "model", "") or "").strip()
    if native_id and model:
        patterns.append(f"{native_id}/{split_effort(model)[0]}")
    return patterns


def enabled_patterns(config) -> list[str]:  # noqa: ANN001
    """``[models] enabled`` when set (``[]`` included), else the default."""
    configured = getattr(getattr(config, "models", None), "enabled", None)
    if configured is None:
        return default_enabled_patterns(config)
    return [p for p in configured if isinstance(p, str)]


# --- a person's selection, and what is stored ---------------------------------

#: How many candidates a refusal shown to a person lists.
SHOWN_CANDIDATES = 5


def resolve_for_person(
    selection: str,
    config,  # noqa: ANN001
    *,
    kind: str | None,
    is_admin: bool,
    allow_patterns: bool = False,
    catalogue: Catalogue | None = None,
    provider: str | None = None,
) -> Resolution | ResolveError:
    """A selection a person made for a room, a job or one task.

    An admin selects from the whole catalogue; anyone else from the enabled
    set (D9). ``kind`` is the brain that will run it, and ``provider`` the
    effective provider of the room or job (``effective_provider``), which is
    asked first and whose aliases apply. Patterns are for chat commands only
    (D10), so every other caller leaves ``allow_patterns`` off. A task needs
    tool calling, so a model without it is refused. ``select_for_person`` is
    what a writer calls: it also decides the provider column.
    """
    if provider is None:
        provider = effective_provider(config, kind, None)
    return resolve_selection(
        selection, catalogue,
        for_brain=kind,
        prefer_provider=provider,
        alias_provider=provider,
        enabled_only=not is_admin,
        enabled=None if is_admin else enabled_patterns(config),
        providers=config_providers(config),
        native_provider=native_provider_id(config),
        native_reach=native_reach_ids(config),
        allow_patterns=allow_patterns,
        needs_tools=True,
    )


def stored_selection(resolution: Resolution) -> str:
    """What a room, a job or a ``!model`` turn stores for a resolved
    selection: the alias name for an alias, else the id within its provider
    (a ref, a bare id or a pattern is stored as the id it matched; the
    provider goes in its own column, D22). Never carries ``:effort``; effort
    is its own field."""
    if resolution.source == "alias" and resolution.alias:
        return resolution.alias
    if resolution.info is not None:
        return resolution.info.id
    return resolution.ref


@dataclass(frozen=True)
class Selection:
    """A person's selection, split into what the columns store (D22).

    ``model`` is an alias name or an id within ``provider``; ``provider`` is
    the stored provider column, ``None`` for the brain's default.
    ``provider_changed`` says the selection moved the room or job to another
    provider, which only an admin may do.
    """

    model: str
    effort: str | None
    provider: str | None
    resolution: Resolution
    provider_changed: bool = False


def alias_keys(kind: str | None, provider: str | None, native_default: str | None) -> tuple[str, ...]:
    """The `[models.aliases.<name>]` keys a brain on ``provider`` reads, in order.

    The CLI brains read ``anthropic``. Native reads its provider's id; on its
    default provider it reads the old ``openai_compat`` namespace key first,
    for one release, so a config written before providers keeps its meaning.
    ``"*"`` (a flat string) is read last by every brain. An alias with no
    target under these keys has no operator target on that provider: it never
    resolves into another provider.
    """
    if kind in CLI_KINDS:
        return (SHIPPED_PROVIDER, "*")
    keys: list[str] = []
    if not provider or provider == native_default:
        keys.append(BRAIN_NAMESPACES["native"])
    if provider:
        keys.append(provider)
    keys.append("*")
    return tuple(keys)


_warned_unlisted: set[tuple[str, str]] = set()


def effective_provider(config, kind: str | None, provider: str | None) -> str | None:  # noqa: ANN001
    """The provider a room, job or task of brain ``kind`` runs on.

    The CLI brains always run on `anthropic`. Native runs on the stored
    provider when `[brain.native] providers` lists it, and on its default
    provider when none is stored. A stored provider the list no longer holds
    falls back to the default with a WARNING (once per pair and process),
    like a room's brain pin the operator stopped allowing. ``None`` for a
    kind with no provider, or a native brain with none. Never raises.
    """
    if kind in CLI_KINDS:
        return SHIPPED_PROVIDER
    if kind != "native":
        return None
    default = native_provider_id(config)
    wanted = provider.strip() if isinstance(provider, str) else ""
    if not wanted or wanted == default:
        return default
    if wanted in native_reach_ids(config):
        return wanted
    if (wanted, default or "") not in _warned_unlisted and len(_warned_unlisted) < 64:
        _warned_unlisted.add((wanted, default or ""))
        import logging

        logging.getLogger("istota.brain").warning(
            "provider %r is not in [brain.native] providers; running on the default "
            "provider %r instead", wanted, default,
        )
    return default


def stored_provider(config, kind: str | None, provider: str | None) -> str | None:  # noqa: ANN001
    """What a provider column stores for ``provider``: ``None`` for the
    brain's default provider (so the row follows a change of default) and for
    the CLI brains, else the id."""
    if kind != "native" or not provider:
        return None
    return None if provider == native_provider_id(config) else provider


def select_for_person(
    selection: str,
    config,  # noqa: ANN001
    *,
    kind: str | None,
    provider: str | None,
    is_admin: bool,
    allow_patterns: bool = False,
    catalogue: Catalogue | None = None,
) -> Selection | ResolveError:
    """A person's selection for a room, a job or one task, split for storage.

    ``provider`` is the stored provider column of the room or job (``None``
    for the default). The selection is resolved against that provider first
    (``resolve_for_person``). A ref of another provider native reaches moves
    the selection there: an admin's ``provider/id`` is split into the two
    columns, anyone else is refused (``provider_admin_only``), since the
    provider decides which key is billed. An alias stays on the room's
    provider: one whose target is another provider's model is refused, never
    resolved into it. Never raises.
    """
    current = effective_provider(config, kind, provider)
    resolved = resolve_for_person(
        selection, config, kind=kind, is_admin=is_admin,
        allow_patterns=allow_patterns, catalogue=catalogue, provider=current,
    )
    if isinstance(resolved, ResolveError):
        return resolved
    stored = stored_provider(config, kind, current)
    if resolved.brain_default or not resolved.ref:
        return Selection(
            model=stored_selection(resolved) if resolved.alias else "",
            effort=resolved.effort_requested, provider=stored, resolution=resolved,
        )
    landed = resolved.provider
    if kind != "native" or landed == current:
        return Selection(
            model=stored_selection(resolved), effort=resolved.effort_requested,
            provider=stored, resolution=resolved,
        )
    if resolved.source == "alias":
        return _error(
            "model_not_runnable", selection,
            f"alias {resolved.alias!r} names a {landed} model, and this room runs on "
            f"{current}; it is not available here",
            effort=resolved.effort_requested,
        )
    if not is_admin:
        return _error(
            "provider_admin_only", selection,
            f"{resolved.ref} is a {landed} model and this room runs on {current}; "
            "only an admin can change the provider",
            effort=resolved.effort_requested,
        )
    return Selection(
        model=stored_selection(resolved), effort=resolved.effort_requested,
        provider=stored_provider(config, kind, landed), resolution=resolved,
        provider_changed=True,
    )


def describe_resolve_error(error: ResolveError) -> str:
    """One line for a person: the refusal and up to five candidates."""
    text = error.message
    shown = error.candidates[:SHOWN_CANDIDATES]
    if shown:
        text += ". Did you mean " + ", ".join(f"`{c}`" for c in shown)
        if len(error.candidates) > len(shown):
            text += f" (and {len(error.candidates) - len(shown)} more)"
        text += "?"
    return text


def selection_for_task(
    config, selection: str | None, *, kind: str | None, provider: str | None = None,  # noqa: ANN001
) -> tuple[str, str | None]:
    """``(model, effort)`` a task row records for a stored selection (D6).

    Resolved within the one provider the task runs on (``provider``, the
    stored column; ``effective_provider`` decides), never by reading a
    provider out of the string (D22). An alias or a model resolves now, so
    ``tasks.model`` names the id within that provider that ran, and an alias
    change reaches the next task. A portable alias (the tiers,
    and any ``portable = true``) is the exception: its name is kept, because
    a fallback to a brain on another provider re-resolves it there, which a
    ref would stop. The model that ran is still recorded, as
    ``tasks.model_used`` and the usage row's ``model``. What does not resolve
    to a ref is kept as written, because the brains stay lenient: an alias
    meaning the brain's own model, an id the catalogue does not list, a ref
    the brain cannot run (native sends it to its default provider as written).
    ``effort`` is the effort the selection itself carries (``opus:high``, an
    alias's), or ``None``. No enabled-set check: what is stored was bounded
    when it was set, and an admin's pin runs for every member (D9). Never
    raises.
    """
    raw = (selection or "").strip() if isinstance(selection, str) else ""
    if not raw:
        return "", None
    try:
        scope = effective_provider(config, kind, provider)
        resolved = resolve_selection(
            raw,
            for_brain=kind,
            providers=config_providers(config),
            native_provider=native_provider_id(config),
            native_reach=native_reach_ids(config),
            alias_provider=scope,
            scope_provider=scope,
        )
    except Exception:  # noqa: BLE001 - task creation must not fail on a lookup
        return raw, None
    if isinstance(resolved, Resolution) and resolved.alias and is_portable_selection(config, resolved.alias):
        return resolved.alias, resolved.effort_requested
    if isinstance(resolved, Resolution) and not resolved.brain_default:
        return stored_selection_id(resolved), resolved.effort_requested
    base, effort = split_effort(raw)
    if isinstance(resolved, Resolution):
        return base, resolved.effort_requested
    return raw, None


def stored_selection_id(resolution: Resolution) -> str:
    """The id within its provider a resolved model runs as."""
    if resolution.info is not None:
        return resolution.info.id
    return resolution.ref.split("/", 1)[1] if "/" in resolution.ref else resolution.ref


def is_portable_selection(config, alias: str) -> bool:  # noqa: ANN001
    """Whether ``alias`` is a portable intent for ``config``: a tier, or an
    operator alias marked ``portable = true``."""
    from ._aliases import is_portable_alias
    from ._roles import parse_alias_overrides

    raw = getattr(getattr(config, "models", None), "aliases", None)
    return is_portable_alias(alias, parse_alias_overrides(raw, log=False).portable_names())


# --- effort ------------------------------------------------------------------

#: Effort levels, lowest first: the order clamping walks.
EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Supported by every reasoning model unless its map says otherwise. `xhigh`
# and `max` are supported only where the map names them (pi's rule).
_BASE_EFFORTS = frozenset({"low", "medium", "high"})


def _supported_efforts(info: ModelInfo) -> list[str]:
    effort_map = info.effort_map or {}
    supported = []
    for level in EFFORT_ORDER:
        if level in effort_map:
            if effort_map[level] is not None:
                supported.append(level)
        elif level in _BASE_EFFORTS:
            supported.append(level)
    return supported


def supported_efforts(info: ModelInfo) -> list[str]:
    """The effort levels ``info``'s model takes, lowest first; none without reasoning."""
    return _supported_efforts(info) if info.reasoning else []


def clamp_effort_level(info: ModelInfo | None, requested: str | None) -> str | None:
    """The effort level ``info``'s model runs for ``requested``, or ``None``.

    A model without reasoning gets none. An unsupported level moves up to the
    nearest supported one first, then down (D8). ``info`` of ``None`` means the
    catalogue does not know the model: there is nothing to check against, so
    the level is returned as asked, which is what a brain sent before.
    """
    level = (requested or "").strip().lower()
    if not level:
        return None
    if info is None:
        return (requested or "").strip()
    if not info.reasoning or level not in EFFORT_ORDER:
        return None
    supported = _supported_efforts(info)
    if level in supported:
        return level
    index = EFFORT_ORDER.index(level)
    for candidate in EFFORT_ORDER[index + 1:]:
        if candidate in supported:
            return candidate
    for candidate in reversed(EFFORT_ORDER[:index]):
        if candidate in supported:
            return candidate
    return None


def effort_wire_value(info: ModelInfo | None, level: str | None) -> str | None:
    """What goes on the wire for a clamped ``level``: the map's entry, else the name."""
    if not level:
        return None
    if info is not None:
        mapped = (info.effort_map or {}).get(level)
        if mapped:
            return mapped
    return level


def clamp_effort(info: ModelInfo | None, requested: str | None) -> str | None:
    """The wire value for ``requested`` on ``info``'s model, or ``None``."""
    return effort_wire_value(info, clamp_effort_level(info, requested))


def anthropic_model_info(model_id: str | None, catalogue: Catalogue | None = None) -> ModelInfo | None:
    """The catalogue record of an `anthropic` model id, or ``None`` when the
    catalogue does not list it (an empty id, a dated id, a newer model)."""
    model_id = (model_id or "").strip()
    if not model_id:
        return None
    catalogue = catalogue if catalogue is not None else current_catalogue()
    ref = f"{SHIPPED_PROVIDER}/{model_id}"
    if ref not in catalogue.refs():
        return None
    return catalogue.get(ref)


# --- config-derived inputs ---------------------------------------------------


def native_provider_id(config) -> str | None:  # noqa: ANN001
    """The provider id native's requests go to, or ``None``. Never raises."""
    brain = getattr(config, "brain", config)
    try:
        from . import native_provider_spec

        spec = native_provider_spec(brain)
    except Exception:  # noqa: BLE001 - a resolution read must not raise
        return None
    return spec.id if spec is not None else None


def native_reach_ids(config) -> tuple[str, ...]:  # noqa: ANN001
    """`[brain.native] providers`, native's default provider first: the
    providers native may send a request to. Empty when native has no
    provider. Never raises."""
    default = native_provider_id(config)
    if not default:
        return ()
    brain = getattr(config, "brain", config)
    listed = getattr(getattr(brain, "native", None), "providers", None) or ()
    out = [default]
    for provider_id in listed:
        if isinstance(provider_id, str) and provider_id.strip() and provider_id.strip() not in out:
            out.append(provider_id.strip())
    return tuple(out)


def config_providers(config) -> Mapping[str, object]:  # noqa: ANN001
    """The configured providers of ``config`` (a `Config` or a `BrainConfig`)."""
    providers = getattr(config, "providers", None)
    if providers:
        return providers
    brain = getattr(config, "brain", config)
    return getattr(brain, "provider_specs", None) or {}


def preferred_provider(namespace: str | None, config) -> str | None:  # noqa: ANN001
    """The provider whose bare ids a namespace's stored values are."""
    if namespace == BRAIN_NAMESPACES["claude_code"]:
        return SHIPPED_PROVIDER
    if namespace == BRAIN_NAMESPACES["native"]:
        return native_provider_id(config)
    return None


__all__ = [
    "BRAIN_NAMESPACES",
    "CLI_KINDS",
    "Selection",
    "alias_keys",
    "effective_provider",
    "select_for_person",
    "stored_provider",
    "stored_selection_id",
    "DEFAULT_ALIAS",
    "DEFAULT_TIER_TARGETS",
    "EFFORT_ORDER",
    "ERROR_CODES",
    "FAMILY_SHORTCUTS",
    "Resolution",
    "ResolveError",
    "anthropic_model_info",
    "clamp_effort",
    "clamp_effort_level",
    "config_providers",
    "effort_wire_value",
    "default_enabled_patterns",
    "describe_resolve_error",
    "is_portable_selection",
    "enabled_patterns",
    "enabled_refs",
    "resolve_for_person",
    "selection_for_task",
    "stored_selection",
    "latest_in_family",
    "native_provider_id",
    "native_reach_ids",
    "preferred_provider",
    "resolve_selection",
    "unmatched_patterns",
]
