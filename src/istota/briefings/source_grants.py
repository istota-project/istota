"""Resource-owner consent for recurring disclosure to a group's future readers."""

import json

from istota import db
from istota.briefings.group_store import (
    _json, _timestamp, briefing_capabilities, definition_sources, get_definition, supersede_definition_runs,
)
from istota.briefings.models import normalize_contribution_config
from istota.groups.policy import GroupError
from istota.groups.service import _transaction
from istota.groups.system import eligible_user_ids


def _source(definition, source_id):
    if definition:
        for source in definition_sources(definition):
            if source["id"] == source_id and source["kind"] in ("personal_file", "personal_calendar"):
                return source
    raise GroupError(404, "source_not_found", "Contribution not found")


def _resource(source):
    return {"kind": source["kind"], "config": normalize_contribution_config(source["kind"], source["config"], allow_pending=True)}


def audience_contract(definition):
    return {"version": 1, "group_id": definition["group_id"], "future_members": True,
            "retained_history": True, "recurring_use": True, "copies_cannot_recall": True}


def _owner_membership(conn, config, definition, owner_id):
    if (db.get_group(conn, definition["group_id"]) is None
            or owner_id not in eligible_user_ids(conn, config)
            or not db.is_group_member(conn, definition["group_id"], owner_id)):
        raise GroupError(409, "source_owner_unavailable", "Source owner must be a current enabled group member")
    return conn.execute("SELECT id FROM group_members WHERE group_id = ? AND user_id = ? AND ended_at IS NULL",
                        (definition["group_id"], owner_id)).fetchone()["id"]


def _active_grant(conn, definition_id, source_id):
    row = conn.execute("""SELECT * FROM group_briefing_source_grants
                          WHERE definition_id = ? AND source_id = ? AND revoked_at IS NULL""",
                       (definition_id, source_id)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["resource"] = json.loads(result.pop("resource_json"))
    result["audience_contract"] = json.loads(result.pop("audience_json"))
    return result


def _grant_revision(conn, definition_id, source_id):
    return conn.execute("SELECT COALESCE(MAX(revision), 0) FROM group_briefing_source_grants WHERE definition_id = ? AND source_id = ?",
                        (definition_id, source_id)).fetchone()[0]


def grant_snapshot(grant):
    return {"source_id": grant["source_id"], "id": grant["id"], "revision": grant["revision"]}


def resolve_source_grant(conn, config, definition, source_id):
    """Internal authorization seam for gather, publication and pending sends."""
    source = _source(definition, source_id)
    grant = _active_grant(conn, definition.get("id"), source_id)
    if grant is None:
        raise GroupError(409, "source_approval_required", "Source approval is required")
    membership = _owner_membership(conn, config, definition, source["config"]["owner_id"])
    if (grant["owner_id"] != source["config"]["owner_id"] or grant["membership_id"] != membership
            or grant["resource"] != _resource(source) or grant["channel"] != definition["channel"]
            or grant["audience_contract"] != audience_contract(definition)):
        raise GroupError(409, "source_approval_changed", "Source approval must be renewed")
    return grant


def validate_definition_grants(conn, config, definition):
    return [grant_snapshot(resolve_source_grant(conn, config, definition, source["id"]))
            for source in definition_sources(definition) if source["kind"] in ("personal_file", "personal_calendar")]


def validate_run_grants(conn, config, run):
    """Pending delivery must match the admitted grants, even after reapproval."""
    definition = get_definition(conn, run["definition_id"])
    if definition is None or validate_definition_grants(conn, config, definition) != run["snapshot"]["grants"]:
        raise GroupError(409, "source_approval_changed", "Source approval changed since admission")


def _resource_disclosure(source):
    resource = _resource(source)
    if set(resource["config"]) == {"owner_id"}:
        return resource, "not selected", "The resource owner must select one exact file or calendar before approval."
    if source["kind"] == "personal_file":
        return resource, resource["config"]["path"], "The full selected file contents may be summarized or quoted."
    fields = ", ".join(resource["config"]["fields"])
    disclosure = (f"Disclosed calendar fields: {fields}. Window: {resource['config']['lookback_days']} days back, "
                  f"{resource['config']['lookahead_days']} days ahead. "
                  + ("Private-marked events are included." if resource["config"]["include_private"] else "Private-marked events are excluded."))
    return resource, resource["config"]["calendar_url"], disclosure


def source_approval_state(conn, config, definition_id, source_id, *, actor_id, is_admin=False, resource_config=None):
    definition = get_definition(conn, definition_id)
    source = _source(definition, source_id)
    caps = briefing_capabilities(conn, config, definition["group_id"], actor_id, is_admin=is_admin)
    owner = source["config"]["owner_id"]
    if not caps["read_briefings"] or (actor_id != owner and not caps["author_briefings"]):
        raise GroupError(404, "source_not_found", "Contribution not found")
    displayed_source = source
    if resource_config is not None:
        if actor_id != owner:
            raise GroupError(403, "source_owner_required", "Only the resource owner may select a resource")
        try:
            selected = normalize_contribution_config(source["kind"], resource_config)
        except (ValueError, TypeError):
            raise GroupError(422, "invalid_resource", "Select one exact resource and disclosure") from None
        if selected["owner_id"] != owner:
            raise GroupError(403, "source_owner_required", "Only your own resource may be selected")
        displayed_source = {**source, "config": selected}
    group = db.get_group(conn, definition["group_id"])
    eligible = eligible_user_ids(conn, config)
    members = [user for user in db.list_group_members(conn, definition["group_id"]) if user in eligible]
    audience = ("everyone currently using this installation and anyone who joins later" if group["system_key"] == "all_users"
                else f"every member of {group['display_name']}, including future members")
    resource, reference, disclosure = _resource_disclosure(displayed_source)
    warning = (f"{owner}'s selected resource ({reference}) will be used on each run of this briefing for {audience} "
               f"via {definition['channel']} ({len(members)} current members). {disclosure} "
               "Future members can read retained editions. Revocation stops future use; published editions remain in group history "
               "until normal retention. Downloaded copies and copies sent by email cannot be recalled.")
    try:
        resolve_source_grant(conn, config, definition, source_id)
        approved = _resource(source) == resource
    except GroupError:
        approved = False
    return {"source_id": source_id, "owner_id": owner, "resource": resource, "approved": approved,
            "member_count": len(members), "warning": warning, "audience_contract": audience_contract(definition),
            "grant_revision": _grant_revision(conn, definition_id, source_id), "definition_revision": definition["revision"]}


def approve_source(conn, config, definition_id, source_id, *, actor_id, expected_definition_revision,
                   expected_grant_revision, acknowledged, audience_contract, resource_config=None, now=None):
    from istota.briefings.sources.contributions import verify_resource_ownership
    if acknowledged is not True:
        raise GroupError(422, "approval_acknowledgement_required", "Explicit source-sharing approval is required")
    state = source_approval_state(conn, config, definition_id, source_id, actor_id=actor_id)
    if state["owner_id"] != actor_id:
        raise GroupError(403, "source_owner_required", "Only the resource owner may approve sharing")
    try:
        same_audience = _json(audience_contract) == _json(state["audience_contract"])
    except (ValueError, TypeError):
        same_audience = False
    if (type(expected_definition_revision) is not int or type(expected_grant_revision) is not int
            or state["definition_revision"] != expected_definition_revision
            or state["grant_revision"] != expected_grant_revision
            or not same_audience):
        raise GroupError(409, "approval_conflict", "Source or audience changed; review approval again")
    resource = state["resource"]
    try:
        selected = normalize_contribution_config(resource["kind"], resource_config if resource_config is not None else resource["config"])
    except (ValueError, TypeError):
        raise GroupError(422, "invalid_resource", "Select one exact resource and disclosure") from None
    if selected["owner_id"] != actor_id:
        raise GroupError(403, "source_owner_required", "Only your own resource may be selected")
    selected_resource = {"kind": resource["kind"], "config": selected}
    definition = get_definition(conn, definition_id)
    if selected_resource != resource and definition["enabled"]:
        raise GroupError(409, "definition_disabled_required", "Save a disabled draft before selecting another resource")
    verify_resource_ownership(config, selected_resource["kind"], selected_resource["config"])
    at = _timestamp(now)
    with _transaction(conn):
        definition = get_definition(conn, definition_id)
        source = _source(definition, source_id)
        if (definition["revision"] != expected_definition_revision
                or _grant_revision(conn, definition_id, source_id) != expected_grant_revision
                or _resource(source) != resource):
            raise GroupError(409, "approval_conflict", "Source changed; review approval again")
        membership = _owner_membership(conn, config, definition, actor_id)
        previous = _active_grant(conn, definition_id, source_id)
        if previous:
            _revoke_grant(conn, previous, at)
        if selected_resource != resource:
            source["config"] = selected
            conn.execute("""UPDATE group_briefing_configs SET generation_json = ?, revision = revision + 1,
                            updated_by = ?, updated_at = ? WHERE id = ?""",
                         (_json(definition["generation"]), actor_id, at, definition_id))
            supersede_definition_runs(conn, definition_id)
        revision = _grant_revision(conn, definition_id, source_id) + 1
        conn.execute("""INSERT INTO group_briefing_source_grants
            (definition_id, source_id, owner_id, membership_id, resource_json, audience_json, channel, revision, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (definition_id, source_id, actor_id, membership, _json(selected_resource), _json(audience_contract),
                      definition["channel"], revision, at))
        return _active_grant(conn, definition_id, source_id)


def _cancel_grant_uses(conn, grant, at):
    # Match admitted identity, not a source name reused in another definition.
    runs = conn.execute("SELECT id, state, snapshot_json FROM group_briefing_runs WHERE definition_id = ?",
                        (grant["definition_id"],)).fetchall()
    for run in runs:
        if not any(g["id"] == grant["id"] for g in json.loads(run["snapshot_json"])["grants"]):
            continue
        if run["state"] in ("queued", "researching", "generating"):
            conn.execute("""UPDATE group_briefing_runs SET state = 'cancelled', error_code = 'source_approval_revoked',
                attempt_token = NULL, lease_expires_at = NULL, research_json = NULL, updated_at = ? WHERE id = ?""", (at, run["id"]))
        conn.execute("""UPDATE group_briefing_recipients SET state = 'skipped', error_code = 'source_approval_revoked',
                        updated_at = ? WHERE run_id = ? AND state IN ('pending', 'failed')""", (at, run["id"]))


def _revoke_grant(conn, grant, at):
    conn.execute("UPDATE group_briefing_source_grants SET revoked_at = ?, revision = revision + 1 WHERE id = ?",
                 (at, grant["id"]))
    _cancel_grant_uses(conn, grant, at)


def revoke_source(conn, config, definition_id, source_id, *, actor_id, expected_grant_revision, now=None):
    with _transaction(conn):
        grant = _active_grant(conn, definition_id, source_id)
        if grant is None or grant["owner_id"] != actor_id:
            raise GroupError(404, "source_not_found", "Approved contribution not found")
        if type(expected_grant_revision) is not int or grant["revision"] != expected_grant_revision:
            raise GroupError(409, "approval_conflict", "Source approval changed; reload before revoking")
        _revoke_grant(conn, grant, _timestamp(now))


def invalidate_changed_grants(conn, old, updated):
    """Called in the definition save transaction; changes cannot revive old consent."""
    if old is None:
        return
    sources = {s["id"]: s for s in definition_sources(updated) if s["kind"] in ("personal_file", "personal_calendar")}
    rows = conn.execute("SELECT source_id FROM group_briefing_source_grants WHERE definition_id = ? AND revoked_at IS NULL",
                        (old["id"],)).fetchall()
    for row in rows:
        grant = _active_grant(conn, old["id"], row["source_id"])
        source = sources.get(row["source_id"])
        if (source is None or grant["resource"] != _resource(source) or grant["channel"] != updated["channel"]
                or grant["audience_contract"] != audience_contract(updated)):
            _revoke_grant(conn, grant, _timestamp())


def revoke_owner_grants(conn, owner_id, *, group_id=None):
    """Join a membership/account mutation's transaction, without doing I/O."""
    rows = conn.execute("""SELECT g.definition_id, g.source_id FROM group_briefing_source_grants g
        JOIN group_briefing_configs d ON d.id = g.definition_id
        WHERE g.owner_id = ? AND g.revoked_at IS NULL AND (? IS NULL OR d.group_id = ?)""",
                        (owner_id, group_id, group_id)).fetchall()
    at = _timestamp()
    for row in rows:
        _revoke_grant(conn, _active_grant(conn, row["definition_id"], row["source_id"]), at)


def reconcile_source_grants(conn, config, *, registry_complete):
    """Config refresh cannot revive consent after a contributor disappears."""
    rows = conn.execute("""SELECT g.definition_id, g.source_id FROM group_briefing_source_grants g
        LEFT JOIN group_members m ON m.id = g.membership_id
        LEFT JOIN groups t ON t.group_id = m.group_id
        WHERE g.revoked_at IS NULL AND (m.ended_at IS NOT NULL OR m.id IS NULL OR t.archived_at IS NOT NULL
        OR EXISTS (SELECT 1 FROM web_auth_identities i WHERE i.user_id = g.owner_id AND i.disabled = 1))""").fetchall()
    for row in rows:
        _revoke_grant(conn, _active_grant(conn, row["definition_id"], row["source_id"]), _timestamp())
    if registry_complete:
        eligible = eligible_user_ids(conn, config)
        owners = {row["owner_id"] for row in conn.execute("SELECT DISTINCT owner_id FROM group_briefing_source_grants WHERE revoked_at IS NULL")
                  if row["owner_id"] not in eligible}
        for owner in owners:
            revoke_owner_grants(conn, owner)
