"""Declarative env var resolver for skill plugin system.

Processes EnvSpec declarations from skill.toml manifests to build
environment variables for the Claude subprocess.
"""

import json
import logging
from dataclasses import dataclass
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


def _resolve_config_path(config: object, dotted_path: str) -> object:
    """Resolve a dotted path like 'browser.api_url' against a Config object."""
    obj = config
    for part in dotted_path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _resolve_env_spec(spec: EnvSpec, ctx: EnvContext) -> str | None:
    """Resolve a single EnvSpec to an env var value, or None to skip."""
    if spec.source == "config":
        # Guard check
        if spec.when:
            guard = _resolve_config_path(ctx.config, spec.when)
            if not guard:
                return None
        val = _resolve_config_path(ctx.config, spec.config_path)
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
        for rc in ctx.user_config.resources:
            if rc.type == spec.resource_type:
                # Check named field first, then fall back to extra dict
                val = getattr(rc, spec.field, None)
                if val is None or val == "":
                    val = getattr(rc, "extra", {}).get(spec.field)
                if val:
                    return str(val)
        return None

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
    selected_skills: list[str],
    skill_index: dict[str, SkillMeta],
    ctx: EnvContext,
) -> dict[str, str]:
    """Build env vars from declarative EnvSpec entries in selected skills.

    Returns a dict of env var name -> value. Only includes resolved (non-None) values.
    """
    env = {}
    for skill_name in selected_skills:
        meta = skill_index.get(skill_name)
        if not meta or not meta.env_specs:
            continue
        for spec in meta.env_specs:
            if not spec.var:
                continue
            try:
                val = _resolve_env_spec(spec, ctx)
                if val is not None:
                    env[spec.var] = val
            except Exception as e:
                logger.warning(
                    "Failed to resolve env var %s for skill %s: %s",
                    spec.var, skill_name, e,
                )
    return env


def dispatch_setup_env_hooks(
    selected_skills: list[str],
    skill_index: dict[str, SkillMeta],
    ctx: EnvContext,
) -> dict[str, str]:
    """Call setup_env() hooks on skill Python modules that export them.

    Only called for skills that have a skill_dir (directory-based skills)
    and whose __init__.py exports a setup_env(ctx) function.

    Returns merged env vars from all hooks.
    """
    import importlib

    env = {}
    for skill_name in selected_skills:
        meta = skill_index.get(skill_name)
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
