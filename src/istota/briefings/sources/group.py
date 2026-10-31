"""Group source authorization and gathering, without a personal runtime."""

import json
from dataclasses import dataclass, replace
from datetime import datetime

from istota import db, storage
from istota.briefings.group_store import definition_sources, get_definition
from istota.briefings.models import normalize_group_generation, normalize_group_resource_config
from istota.briefings.source_grants import validate_definition_grants
from istota.briefings.sources import (
    GatheredSource, SourceContext, gather_sources, raise_for_refused, resolve_source,
)
from istota.briefings.sources.contributions import read_contribution, read_text_file
from istota.groups.policy import GroupError


@dataclass(frozen=True)
class GroupSourceContext:
    group_id: str
    definition_id: int
    config: object
    now: datetime | None = None
    grants: tuple[dict, ...] | None = None
    definition_revision: int | None = None


def _definition(conn, ctx):
    definition = get_definition(conn, ctx.definition_id)
    if (definition is None or definition["group_id"] != ctx.group_id
            or db.get_group(conn, ctx.group_id) is None):
        raise GroupError(404, "briefing_not_found", "Group briefing not found")
    if ctx.definition_revision is not None and definition["revision"] != ctx.definition_revision:
        raise GroupError(409, "revision_conflict", "Briefing changed during source gathering")
    try:
        normalize_group_generation(definition["generation_mode"], definition["generation"])
    except (ValueError, TypeError, KeyError):
        raise GroupError(422, "source_definition_refused", "Stored source settings were refused") from None
    return definition


def _failed(kind, failure_kind, error_code):
    return GatheredSource(kind=kind, title=kind, ok=False, failure_kind=failure_kind,
                          error_code=error_code, provenance=f"({error_code})")


def _local_result(kind, result):
    status = result["status"]
    if status in ("refused", "unavailable"):
        return _failed(kind, status, result.get("error_code", "source_refused"))
    text = result.get("content", "")
    if "items" in result:
        # Calendar projections contain only approved fields, including time.
        text = "\n".join(json.dumps(item, ensure_ascii=False) for item in result["items"])
    return GatheredSource(kind=kind, title=kind, text=text, ok=bool(text.strip()), untrusted=True,
                          provenance=f"{kind}: {'missing leaf' if status == 'missing' else 'readable'}")


def _resolve_stored(source, ctx, conn, definition):
    from istota.briefings.sources import builtins, kv
    from istota.briefings.shared_blocks import SYSTEM_IDENTITY

    kind, config = source["kind"], source["config"]
    if kind == "group_memory":
        normalize_group_resource_config(kind, config)
        return _local_result(kind, storage.read_group_memory_result(ctx.config, ctx.group_id))
    if kind == "group_file":
        resource = normalize_group_resource_config(kind, config)
        root = storage._get_mount_path(ctx.config, "/") if ctx.config.has_workspace else None
        return _local_result(kind, read_text_file(root, ["Groups", ctx.group_id, *resource["path"].split("/")]))
    if kind in ("personal_file", "personal_calendar"):
        expected = next((grant for grant in ctx.grants or () if grant["source_id"] == source["id"]), None)
        if ctx.grants is not None and expected is None:
            return _failed(kind, "refused", "source_approval_required")
        return _local_result(kind, read_contribution(conn, ctx.config, ctx.definition_id, source["id"],
                                                    expected_grant=expected, now=ctx.now))
    public_ctx = SourceContext(app_config=ctx.config, user_id=SYSTEM_IDENTITY, conn=conn,
                               now=ctx.now, timezone=definition["timezone"])
    if kind == "browse":
        from istota.browser.owner import BRIEFINGS_IDENTITY, ISOLATED_PROFILE
        from istota.briefings.sources import browse
        if ctx.config.get_user(BRIEFINGS_IDENTITY) is not None:
            return _failed(kind, "refused", "briefing_browser_identity_refused")
        return browse.resolve(config, replace(public_ctx, user_id=ISOLATED_PROFILE))
    if kind not in ("shared_block", "markets", "email"):
        return _failed(kind, "refused", "source_kind_refused")
    try:
        if kind == "shared_block":
            result = kv.resolve_shared_block(config, public_ctx)
        elif kind == "email":
            from istota.briefings.sources import email
            result = email.resolve(config, public_ctx)
        else:
            result = builtins.resolve_markets(config, public_ctx, allow_browser=False)
    except Exception:
        return _failed(kind, "unavailable", "source_unavailable")
    if not result.ok and result.failure_kind is None:
        result.failure_kind = "unavailable"
        result.error_code = "source_unavailable"
    return result


def resolve_group_source(kind, config, ctx, *, source_id):
    """Only dispatch an exact stored source; errors never expose source bytes."""
    try:
        with db.get_db(ctx.config.db_path) as conn:
            definition = _definition(conn, ctx)
            source = next((s for s in definition_sources(definition) if s["id"] == source_id), None)
            if source is None or source["kind"] != kind or source["config"] != config:
                return _failed(kind, "refused", "source_identity_refused")
            return _resolve_stored(source, ctx, conn, definition)
    except GroupError:
        return _failed(kind, "refused", "source_authorization_refused")
    except Exception:
        # Unexpected local/DB failures cannot omit a selected resource.
        return _failed(kind, "refused", "source_refused")


def gather_group_sources(ctx):
    """Return ordered sources, or refuse the whole gather before rendering."""
    with db.get_db(ctx.config.db_path) as conn:
        definition = _definition(conn, ctx)
        grants = tuple(validate_definition_grants(conn, ctx.config, definition))
    if ctx.grants is not None and tuple(ctx.grants) != grants:
        raise GroupError(409, "source_approval_changed", "Source approval changed since admission")
    pinned = replace(ctx, grants=grants, definition_revision=definition["revision"])
    sources = definition_sources(definition)
    gathered = gather_sources(sources, lambda source: resolve_source(
        source["kind"], source["config"], pinned, source_id=source["id"],
    ))
    raise_for_refused(gathered)
    with db.get_db(ctx.config.db_path) as conn:
        current = _definition(conn, pinned)
        if tuple(validate_definition_grants(conn, ctx.config, current)) != grants:
            raise GroupError(409, "source_approval_changed", "Source approval changed during gathering")
    research_enabled = definition["generation"].get("research", {}).get("enabled", False)
    if sources and not research_enabled and not any(source.ok and not source.is_empty for source in gathered):
        raise GroupError(422, "sources_empty", "No selected source has usable content")
    return gathered
