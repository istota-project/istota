"""Edition notices and source requests, with no private text in stored fallbacks."""

SOURCE = "group_briefing"


def write_edition(conn, recipient):
    from istota.briefings import group_store
    from istota.notifications.store import write_notification
    run = group_store.get_run(conn, recipient["run_id"])
    return write_notification(conn, recipient["user_id"], source=SOURCE,
        dedup_key=f"edition:{run['id']}", object_type="group_edition", object_id=str(run["id"]),
        title="A group briefing is ready", body="Open Groups to read the edition.",
        params={"group_id": run["snapshot"]["definition"]["group_id"]}, link="/groups")


def sync_approvals(conn, config, definition):
    """Reconcile requests in the definition transaction; never send source details."""
    from istota.briefings import group_store, source_grants
    from istota.groups.policy import GroupError
    from istota.notifications.store import resolve_notification, write_notification
    pending = {}
    for source in group_store.definition_sources(definition):
        if source["kind"] not in ("personal_file", "personal_calendar"):
            continue
        owner = source["config"]["owner_id"]
        if not group_store.briefing_capabilities(conn, config, definition["group_id"], owner)["read_briefings"]:
            continue
        try:
            source_grants.resolve_source_grant(conn, config, definition, source["id"])
        except GroupError:
            key = f"approval:{definition['id']}:{source['id']}:{definition['revision']}"
            pending[(owner, key)] = source["id"]
    rows = conn.execute("SELECT user_id, dedup_key FROM notifications WHERE source=? AND object_type='group_source' "
                        "AND object_id=?", (SOURCE, str(definition["id"]))).fetchall()
    existing = {(r["user_id"], r["dedup_key"]) for r in rows}
    for row in rows:
        if (row["user_id"], row["dedup_key"]) not in pending:
            resolve_notification(conn, row["user_id"], SOURCE, row["dedup_key"], by="briefing_source_changed")
    for (owner, key), source_id in pending.items():
        if (owner, key) not in existing:
            write_notification(conn, owner, source=SOURCE, dedup_key=key, object_type="group_source",
                object_id=str(definition["id"]), actionable=True, title="A group briefing requests source approval",
                body="Review the requested source and audience in Groups before approving.", link="/groups",
                params={"group_id": definition["group_id"], "source_id": source_id, "revision": definition["revision"]})


def close_approvals(conn, definition_id):
    """Close every source request for a deleted definition, in its transaction."""
    from istota.notifications.store import resolve_by_object
    users = conn.execute("SELECT DISTINCT user_id FROM notifications WHERE source=? AND object_type='group_source' "
                         "AND object_id=? AND state='open'", (SOURCE, str(definition_id))).fetchall()
    for row in users:
        resolve_by_object(conn, row["user_id"], SOURCE, "group_source", str(definition_id), by="briefing_deleted")


def close_for_member(conn, group_id, user_id):
    from istota.notifications.store import resolve_notification
    rows = conn.execute("SELECT dedup_key FROM notifications WHERE source=? AND user_id=? "
                        "AND json_extract(params, '$.group_id')=? AND state='open'",
                        (SOURCE, user_id, group_id)).fetchall()
    for row in rows:
        resolve_notification(conn, user_id, SOURCE, row["dedup_key"], by="membership_ended")


class GroupBriefingResolver:
    source = SOURCE
    auto_resolve_on_seen = False

    def resolve(self, config, conn, row):
        from istota import db
        from istota.briefings import group_store, source_grants
        from istota.groups.policy import GroupError
        from istota.notifications.sources import NotificationAction, NotificationView
        try:
            object_id = int(row.object_id)
        except (TypeError, ValueError):
            return None
        if row.object_type == "group_edition":
            run = group_store.get_run(conn, object_id)
            if not run or run["state"] != "published":
                return None
            definition = group_store.get_definition(conn, run["definition_id"])
            recipient = conn.execute("SELECT r.* FROM group_briefing_recipients r JOIN group_members m ON m.id=r.membership_id "
                                     "WHERE r.run_id=? AND r.user_id=? AND r.channel='in_app' AND m.ended_at IS NULL",
                                     (object_id, row.user_id)).fetchone()
            if not definition or not recipient:
                return None
            if group_store.delivery_preferences(conn, definition["group_id"], row.user_id,
                                                definition_id=definition["id"])["in_app_muted"]:
                return None
            title, body = "A group briefing is ready", "Open Groups to read the edition."
        elif row.object_type == "group_source":
            definition = group_store.get_definition(conn, object_id)
            if not definition or definition["revision"] != row.params.get("revision"):
                return None
            try:
                state = source_grants.source_approval_state(conn, config, definition["id"],
                    row.params.get("source_id"), actor_id=row.user_id)
            except GroupError:
                return None
            if state["owner_id"] != row.user_id or state["approved"]:
                return None
            title, body = "A group briefing requests source approval", "Review the source and audience before approving."
        else:
            return None
        if (not db.is_group_member(conn, definition["group_id"], row.user_id)
                or not group_store.briefing_capabilities(conn, config, definition["group_id"], row.user_id)["read_briefings"]):
            return None
        return NotificationView(title=title, body=body, link="/groups", actions=(
            NotificationAction(id="open", label="Open", kind="primary", method="LINK", href="/groups"),))


RESOLVER = GroupBriefingResolver()
