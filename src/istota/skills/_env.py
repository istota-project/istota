"""Declarative env var resolver for skill plugin system.

Processes EnvSpec declarations from skill manifests to build
environment variables for the Claude subprocess.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ._types import EnvSpec, SkillMeta

logger = logging.getLogger("istota.skills_env")


@dataclass
class EnvContext:
    """Context passed to env resolution and setup_env hooks."""

    config: object  # Config
    task: object  # db.Task
    user_resources: list  # list[db.UserResource]
    user_config: object | None  # UserConfig | None
    user_temp_dir: Path
    is_admin: bool
    # Optional: list of (calendar_id, display_name, default) tuples returned
    # by CalDAV discovery. Empty when no discovery has run or the user has
    # no calendars. Consumed by EnvSpec.gate_has_discovered_calendars.
    discovered_calendars: list = field(default_factory=list)


def _resolve_config_path(config: object, dotted_path: str) -> object:
    """Resolve a dotted path like 'browser.api_url' against a Config object."""
    obj = config
    for part in dotted_path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _spec_resource_types(spec: EnvSpec) -> set[str]:
    """Return the set of resource types this spec accepts.

    Supports both ``resource_types: [a, b]`` (plural) and the legacy
    ``resource_type: a`` (singular). Either form alone works; if both are
    set, they are unioned.
    """
    accepted = set(spec.resource_types or [])
    if spec.resource_type:
        accepted.add(spec.resource_type)
    return accepted


def _resolve_env_spec(
    spec: EnvSpec,
    ctx: EnvContext,
    *,
    fallbacks_disabled: bool = False,
) -> str | None:
    """Resolve a single EnvSpec to an env var value, or None to skip.

    ``fallbacks_disabled``: when True, ``spec.fallback_var`` is ignored.
    derive_authorized_skills passes True so an instance-wide
    EnvironmentFile fallback cannot trigger per-user auto-authorization.
    """
    val = _resolve_env_spec_primary(spec, ctx)
    if val is not None:
        return val
    if not fallbacks_disabled and spec.fallback_var:
        fallback = os.environ.get(spec.fallback_var)
        if fallback:
            return fallback
    return None


def _resolve_env_spec_primary(spec: EnvSpec, ctx: EnvContext) -> str | None:
    """Primary resolution (no fallback_var)."""
    # Pre-filters: gates apply regardless of source.
    if spec.gate_user_has_resource:
        if not any(r.resource_type == spec.gate_user_has_resource
                   for r in ctx.user_resources):
            return None
    if spec.gate_has_discovered_calendars and not ctx.discovered_calendars:
        return None

    if spec.source == "config":
        # Guard check (intentionally falsy-aware: empty string / 0 / False
        # all mean "feature disabled"). ``when`` is either a string or a
        # list of strings — all listed paths must be truthy.
        if spec.when:
            paths = spec.when if isinstance(spec.when, list) else [spec.when]
            for path in paths:
                if not _resolve_config_path(ctx.config, path):
                    return None
        val = _resolve_config_path(ctx.config, spec.config_path)
        # Skip only when truly absent; downstream skills decide what to do
        # with empty strings or numeric zeros.
        if val is None:
            return None
        return str(val)

    elif spec.source == "resource":
        # First DB resource of this type, resolved through mount
        mount = getattr(ctx.config, "nextcloud_mount_path", None)
        if not mount:
            return None
        for r in ctx.user_resources:
            if r.resource_type == spec.resource_type:
                return str(mount / r.resource_path.lstrip("/"))
        return None

    elif spec.source == "resource_json":
        # All DB resources of this type as JSON array
        mount = getattr(ctx.config, "nextcloud_mount_path", None)
        if not mount:
            return None
        items = []
        for r in ctx.user_resources:
            if r.resource_type == spec.resource_type:
                items.append({
                    "name": r.display_name or "default",
                    "path": str(mount / r.resource_path.lstrip("/")),
                })
        if not items:
            return None
        return json.dumps(items)

    elif spec.source == "user_resource_config":
        # From user config [[resources]] entry
        if not ctx.user_config:
            return None
        accepted = _spec_resource_types(spec)
        for rc in ctx.user_config.resources:
            if rc.type in accepted:
                # Check named field first, then fall back to extra dict
                val = getattr(rc, spec.field, None)
                if val is None or val == "":
                    val = getattr(rc, "extra", {}).get(spec.field)
                if val:
                    return str(val)
        return None

    elif spec.source == "user_id":
        return getattr(ctx.task, "user_id", None) or None

    elif spec.source == "setup_env":
        # Value comes from the skill's setup_env(ctx) hook; the manifest
        # entry exists so derive_credential_set / derive_skill_credential_map
        # see the var (especially when sensitive=true).
        return None

    elif spec.source == "secret":
        # Per-user encrypted secret resolved from the secrets table.
        if not spec.service or not spec.key:
            return None
        from .. import secrets_store  # noqa: PLC0415

        db_path = getattr(ctx.config, "db_path", None)
        user_id = getattr(ctx.task, "user_id", None)
        if not db_path or not user_id:
            return None
        return secrets_store.get_secret(db_path, user_id, spec.service, spec.key)

    elif spec.source == "template_file":
        # Auto-create from template if missing, return path
        mount = getattr(ctx.config, "nextcloud_mount_path", None)
        if not mount:
            return None

        # Resolve user path via storage function
        from .. import storage
        path_fn = getattr(storage, spec.user_path_fn, None)
        if path_fn is None:
            logger.warning("Unknown user_path_fn: %s", spec.user_path_fn)
            return None

        task = ctx.task
        config = ctx.config
        nc_path = path_fn(task.user_id, config.bot_dir_name)
        full_path = mount / nc_path.lstrip("/")

        if not full_path.exists():
            template_obj = getattr(storage, spec.template, None)
            if template_obj is not None:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                # Some templates accept format args
                try:
                    content = template_obj.format(user_id=task.user_id)
                except (KeyError, IndexError):
                    content = template_obj
                full_path.write_text(content)

        return str(full_path)

    else:
        logger.warning("Unknown env spec source: %s (var=%s)", spec.source, spec.var)
        return None


def build_skill_env(
    skill_names: list[str],
    skill_index: dict[str, SkillMeta],
    ctx: EnvContext,
) -> dict[str, str]:
    """Resolve every EnvSpec for every skill in ``skill_names``.

    Phase 3: callers pass ``authorized_skills`` (selected ∪ auto-authorized
    via credential presence). Resolves both sensitive and non-sensitive
    specs; ``None`` results never overwrite a previously-resolved value.

    Conflict semantics: if two skills declare the same var with different
    non-None values, last-write-wins by iteration order on the input list,
    and ``logger.warning`` fires so the conflict is observable. The
    ``NC_*`` co-declaration on ``nextcloud`` and ``files`` is the common
    case; both resolve to the same config value, so no warning fires.
    """
    env: dict[str, str] = {}
    for skill_name in skill_names:
        meta = skill_index.get(skill_name)
        if not meta or not meta.env_specs:
            continue
        for spec in meta.env_specs:
            if not spec.var:
                continue
            try:
                val = _resolve_env_spec(spec, ctx)
            except Exception as e:
                logger.warning(
                    "Failed to resolve env var %s for skill %s: %s",
                    spec.var, skill_name, e,
                )
                continue
            if val is None:
                continue
            existing = env.get(spec.var)
            if existing is not None and existing != val:
                logger.warning(
                    "env_conflict skill=%s var=%s overwrote earlier value "
                    "(two manifests declared the same var with different "
                    "resolutions)",
                    skill_name, spec.var,
                )
            env[spec.var] = val
    return env


def dispatch_setup_env_hooks(
    selected_skills: list[str],
    skill_index: dict[str, SkillMeta],
    ctx: EnvContext,
) -> dict[str, str]:
    """Call setup_env() hooks on skill Python modules that export them.

    Iterates the full skill_index (not just ``selected_skills``) so hooks
    can preserve selection-independent behavior — e.g. the developer hook
    writes git credential helpers whenever the user has tokens configured,
    regardless of whether Pass 1 / Pass 2 picked the skill. Each hook is
    expected to self-gate on its config / context.

    The ``selected_skills`` argument is retained for signature
    compatibility with older callers; it is no longer consulted.

    Returns merged env vars from all hooks.
    """
    import importlib

    env = {}
    for skill_name, meta in skill_index.items():
        if not meta or not meta.skill_dir:
            continue

        # Try to import the skill's Python package
        module_name = f"istota.skills.{skill_name}"
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            continue

        setup_fn = getattr(mod, "setup_env", None)
        if setup_fn is not None:
            try:
                result = setup_fn(ctx)
                if isinstance(result, dict):
                    env.update(result)
            except Exception as e:
                logger.warning(
                    "setup_env() failed for skill %s: %s", skill_name, e,
                )
    return env
