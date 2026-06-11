"""Skills loader CLI — on-demand skill-body disclosure (Part A).

``istota-skill skills show <name>`` prints the fully rendered documentation for
a skill (the body that progressive disclosure deferred out of the prompt).
``istota-skill skills list`` enumerates the skills the caller is allowed to
load. Both re-apply the same guards the selection path enforces
(``disabled_skills`` instance + per-user, ``admin_only`` vs the caller's admin
status, the ``skill_<name>`` experimental gate, and unmet Python dependencies)
so a deferred body can never be used to bypass them. There is intentionally no
resource gate, matching ``eligible_skill_names`` (no bundled skill declares
``resource_types`` now; the former holdouts were doc-only conventions).

Invoked server-side by the skill proxy (or directly when the proxy is off), so
``load_config()`` and the admins file are reachable here.
"""

import argparse
import json
import os
import sys


def _output_error(msg: str) -> None:
    print(json.dumps({"status": "error", "error": msg}))
    sys.exit(1)


def _load_context():
    """Resolve (config, user_id, skill_index, disabled, is_admin, enabled_features).

    Returns a dict, or calls _output_error and exits on a fatal setup problem.
    """
    from istota.config import load_config
    from istota.experimental import enabled_features_from_env
    from istota.skills._loader import load_skill_index

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _output_error("ISTOTA_USER_ID not set")

    config = load_config()
    skill_index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)

    disabled = set(config.disabled_skills)
    user_config = config.get_user(user_id)
    if user_config:
        disabled |= set(user_config.disabled_skills)

    is_admin = config.is_admin(user_id)

    # The propagated env var is authoritative for the subprocess; fall back to
    # the loaded config for the direct (proxy-off) path where it may be unset.
    enabled_features = enabled_features_from_env()
    if not enabled_features and config.experimental.features:
        enabled_features = frozenset(config.experimental.features)

    return {
        "config": config,
        "user_id": user_id,
        "skill_index": skill_index,
        "disabled": disabled,
        "is_admin": is_admin,
        "enabled_features": enabled_features,
    }


def _guard_skill(name: str, ctx: dict) -> str | None:
    """Return an error message if the caller may not load ``name``, else None."""
    from istota.skills._loader import _check_dependencies

    skill_index = ctx["skill_index"]
    meta = skill_index.get(name)
    if meta is None:
        return f"unknown skill: {name!r}"
    if name in ctx["disabled"]:
        return f"skill {name!r} is disabled"
    if meta.admin_only and not ctx["is_admin"]:
        return f"skill {name!r} is restricted to admins"
    if meta.experimental and f"skill_{name}" not in ctx["enabled_features"]:
        return f"skill {name!r} is not available"
    if not _check_dependencies(meta):
        return f"skill {name!r} is unavailable (missing dependencies)"
    # No resource gate — matches eligible_skill_names. No bundled skill declares
    # resource_types now; the former holdouts (notes/spec/todos) were doc-only
    # conventions with sensible defaults.
    return None


def _scripts_dir(config, user_id: str) -> str:
    from istota.storage import get_user_scripts_path

    scripts_nc_path = get_user_scripts_path(user_id, config.bot_dir_name)
    if config.use_mount and config.nextcloud_mount_path is not None:
        return str(config.nextcloud_mount_path / scripts_nc_path.lstrip("/"))
    return f"{config.rclone_remote}:{scripts_nc_path}"


def _render_companion_body(config, name: str, meta) -> str | None:
    """Render one companion skill's body (frontmatter stripped, placeholders
    substituted) WITHOUT the ``## Skills Reference`` wrapper — it rides under a
    delimiter beneath the primary skill. Returns None if the doc can't be read.
    """
    from istota.skills._loader import _resolve_skill_doc_path, _strip_frontmatter

    doc_path = _resolve_skill_doc_path(name, meta, config.skills_dir, config.bundled_skills_dir)
    if doc_path is None:
        return None
    try:
        content = _strip_frontmatter(doc_path.read_text()).strip()
    except OSError:
        return None
    return content.replace("{BOT_NAME}", config.bot_name).replace("{BOT_DIR}", config.bot_dir_name)


def cmd_show(args) -> None:
    import logging

    from istota.skills._loader import expand_companions, load_skills

    ctx = _load_context()
    name = args.name
    err = _guard_skill(name, ctx)
    if err:
        _output_error(err)

    config = ctx["config"]
    skill_index = ctx["skill_index"]
    body = load_skills(
        config.skills_dir,
        [name],
        config.bot_name,
        config.bot_dir_name,
        skill_index=skill_index,
        bundled_dir=config.bundled_skills_dir,
    )
    if not body:
        _output_error(f"no documentation found for skill {name!r}")

    # Append companion bodies (e.g. untrusted_input for an ingest skill) so a
    # menu-pulled skill always arrives with its guardrails in the SAME response
    # — companions are not optional and not at the model's discretion. Gate
    # filtering goes through the shared expand_companions so it can't drift from
    # selection-time companion expansion.
    parts = [body]
    meta = skill_index.get(name)
    declared = list(meta.companion_skills) if meta else []
    if declared:
        loadable = set(expand_companions(
            [name], skill_index,
            is_admin=ctx["is_admin"],
            disabled_skills=ctx["disabled"],
            enabled_experimental_features=ctx["enabled_features"],
        ))
        log = logging.getLogger("istota.skills")
        for comp in declared:
            if comp not in loadable:
                # Gated off (disabled/admin/experimental/deps) or missing from
                # the index. Never silently drop — emit a visible marker; a
                # missing safety companion is a config error.
                log.warning("skills show %s: companion %s unavailable", name, comp)
                parts.append(f"\n\n---\n<!-- companion {comp}: unavailable -->")
                continue
            cbody = _render_companion_body(config, comp, skill_index.get(comp))
            if not cbody:
                log.warning("skills show %s: companion %s body unreadable", name, comp)
                parts.append(f"\n\n---\n<!-- companion {comp}: unavailable -->")
                continue
            parts.append(f"\n\n---\n<!-- companion: {comp} -->\n\n{cbody}")

    out = "".join(parts)
    out = out.replace("{scripts_dir}", _scripts_dir(config, ctx["user_id"]))
    out = out.replace("{user_id}", ctx["user_id"])
    print(out)


def cmd_list(args) -> None:
    ctx = _load_context()
    skill_index = ctx["skill_index"]
    skills = []
    for name in sorted(skill_index):
        if _guard_skill(name, ctx) is not None:
            continue
        meta = skill_index[name]
        skills.append({
            "name": name,
            "description": meta.description,
            "cli": meta.cli,
        })
    print(json.dumps({"status": "ok", "skills": skills}, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skills", description="On-demand skill documentation loader")
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("show", help="Print full instructions for a skill")
    show.add_argument("name", help="Skill name")

    sub.add_parser("list", help="List skills you can load")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "show":
        cmd_show(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
