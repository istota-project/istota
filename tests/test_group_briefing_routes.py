"""Group briefing routes use real sessions and current group roles."""
from uuid import uuid4

from istota import db
from istota.briefings import group_store as store
from tests.test_group_routes import configured as _configured, client as _client, session, BASE
from tests.test_group_briefing_store import payload, NOW

configured = _configured
client = _client

ROOT = BASE + "/groups/family/briefings"


async def create(client):
    response = await client.post(ROOT, json={**payload(enabled=False, channel="in_app"), "revision": 0})
    assert response.status_code == 200, response.text
    return response.json()


async def publish(client, configured):
    definition = await create(client)
    response = await client.put(ROOT + "/daily", json={**payload(channel="in_app"), "revision": definition["revision"]})
    assert response.status_code == 200, response.text
    with db.get_db(configured._config.db_path) as conn:
        run = store.enqueue_run(conn, configured._config, definition["id"], actor_id="alice", request_id=str(uuid4()), now=NOW)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        run = store.publish_run(conn, configured._config, run["id"], claim["attempt_token"], subject="Edition", body_md="PRIVATE EDITION", now=NOW)
    return definition, run


async def test_owner_succession_revision_and_creator_has_no_authority(client, configured):
    definition, run = await publish(client, configured)
    saved = {**payload(channel="in_app"), "revision": 2}
    assert (await client.put(ROOT + "/daily", json=saved)).status_code == 200
    assert (await client.put(ROOT + "/daily", json=saved)).status_code == 409
    assert (await client.put(BASE + "/groups/family/members/bob", json={"revision": 0, "role": "owner"})).status_code == 200
    assert (await client.put(BASE + "/groups/family/members/alice", json={"revision": 1, "role": "member"})).status_code == 200
    for path, body in [("/daily", saved), ("/daily/runs", {"request_id": str(uuid4()), "mode": "publish"}),
                       (f"/daily/runs/{run['id']}/retry", {})]:
        method = "PUT" if path == "/daily" else "POST"
        assert (await client.request(method, ROOT + path, json=body)).status_code == 403
    session(client, configured, "bob")
    current = (await client.get(ROOT + "/daily")).json()
    assert current["id"] == definition["id"] and current["created_by"] == "alice"
    assert (await client.put(ROOT + "/daily", json={**saved, "revision": current["revision"]})).status_code == 200
    assert (await client.get(ROOT + f"/daily/runs/{run['id']}")).json()["body_md"] == "PRIVATE EDITION"
    assert (await client.post(ROOT + f"/daily/runs/{run['id']}/retry", json={})).status_code == 200
    session(client, configured, "admin")
    assert (await client.get(ROOT)).status_code == 404
    assert (await client.put(ROOT + "/daily", json=saved)).status_code == 404


async def test_member_reader_download_preferences_and_status_are_scoped(client, configured):
    _, run = await publish(client, configured)
    preview = await client.post(ROOT + "/daily/runs", json={"request_id": str(uuid4()), "mode": "preview"})
    assert preview.status_code == 200
    session(client, configured, "bob")
    edition = (await client.get(ROOT + f"/daily/runs/{run['id']}")).json()
    assert edition["scope"] == "group" and edition["body_md"] == "PRIVATE EDITION"
    assert not {"snapshot", "research", "attempt_token", "usage"} & edition.keys()
    assert "generation" not in (await client.get(ROOT + "/daily")).json()
    assert (await client.get(ROOT + f"/daily/runs/{preview.json()['id']}")).status_code == 404
    download = await client.get(ROOT + f"/daily/runs/{run['id']}/download")
    assert download.status_code == 200 and "PRIVATE EDITION" in download.text
    assert "attachment" in download.headers["content-disposition"]
    delivery = (await client.get(ROOT + f"/daily/runs/{run['id']}/delivery")).json()
    assert {row["user_id"] for row in delivery["recipients"]} == {"bob"}
    assert "destination_json" not in str(delivery)
    assert (await client.put(ROOT + "/daily/delivery", json={"outbound_muted": True, "in_app_muted": True})).status_code == 200
    assert (await client.put(BASE + "/groups/family/briefing-delivery", json={"outbound_muted": True, "in_app_muted": False})).status_code == 200
    with db.get_db(configured._config.db_path) as conn:
        db.end_group_membership(conn, "family", "bob", ended_by="alice")
    assert (await client.get(ROOT + f"/daily/runs/{run['id']}/download")).status_code == 404


async def test_targets_csrf_body_limits_and_disabled_draft(client, configured):
    response = await client.get(BASE + "/briefings/targets")
    assert response.status_code == 200
    target = next(t for t in response.json()["targets"] if t.get("group_id") == "family")
    assert target["member_count"] == 2 and target["capabilities"]["author_briefings"]
    assert (await client.post(ROOT, json={**payload(), "revision": 0}, headers={"origin": "https://other.example.com"})).status_code == 403
    assert (await client.post(ROOT, content="x" * 70000)).status_code == 413
    assert (await client.post(ROOT, json={**payload(), "revision": 0})).status_code == 422
    await create(client)
    session(client, configured, "bob")
    assert (await client.post(ROOT, json={**payload(enabled=False), "revision": 0})).status_code == 403
    client.cookies.clear()
    assert (await client.get(ROOT)).status_code == 401


async def test_in_app_notification_is_atomic_private_and_loses_access(client, configured):
    from istota.briefings.group_delivery import queue_deliveries
    from istota.notifications import store as notifications
    _, run = await publish(client, configured)
    with db.get_db(configured._config.db_path) as conn:
        queue_deliveries(conn, configured._config, now=NOW)
        queue_deliveries(conn, configured._config, now=NOW)
        rows = conn.execute("SELECT * FROM notifications WHERE source='group_briefing'").fetchall()
        assert len(rows) == 2
        assert "PRIVATE EDITION" not in str([dict(row) for row in rows])
        assert all(row["state"] == "sent" for row in store.list_recipients(conn, run["id"]))
        views, total = notifications.list_open(configured._config, conn, "bob")
        assert total == 1
        db.end_group_membership(conn, "family", "bob", ended_by="alice")
        views, total = notifications.list_open(configured._config, conn, "bob")
        assert total == 0


async def test_retention_prunes_terminal_ledger_but_preserves_uncertain(client, configured):
    from datetime import timedelta
    _, run = await publish(client, configured)
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("UPDATE group_briefing_recipients SET state='uncertain' WHERE run_id=?", (run["id"],))
        conn.commit()
        assert store.prune_runs(conn, retention_days=90, now=NOW + timedelta(days=91)) == 0
        conn.execute("UPDATE group_briefing_recipients SET state='sent' WHERE run_id=?", (run["id"],))
        conn.commit()
        assert store.prune_runs(conn, retention_days=90, now=NOW + timedelta(days=91)) == 1
        assert store.get_run(conn, run["id"]) is None
        assert store.list_recipients(conn, run["id"]) == []
        assert store.list_definitions(conn, "family")


async def test_source_owner_approval_revision_and_retained_history(client, configured, tmp_path):
    from tests.test_briefing_source_grants import payload as contribution_payload
    from istota.notifications import store as notifications
    config = configured._config
    config.workspace_path = tmp_path / "workspace"
    raw = contribution_payload(channel="in_app")
    response = await client.post(ROOT, json={**raw, "revision": 0})
    assert response.status_code == 200, response.text
    url = ROOT + "/daily/sources/selected/approval"
    state = (await client.get(url)).json()
    consent = {"expected_definition_revision": 1, "expected_grant_revision": 0,
               "acknowledged": True, "audience_contract": state["audience_contract"]}
    assert (await client.put(url, json=consent)).status_code == 403
    session(client, configured, "bob")
    # Only the selected resource is read; no content enters responses or notices.
    root = config.workspace_root("bob")
    root.mkdir(parents=True)
    (root / "notes.md").write_text("PRIVATE FILE CANARY")
    with db.get_db(config.db_path) as conn:
        views, total = notifications.list_open(config, conn, "bob")
        assert total == 1 and "PRIVATE FILE CANARY" not in str(views)
    assert (await client.put(url, json={**consent, "acknowledged": False})).status_code == 422
    assert (await client.put(url, json={**consent, "expected_definition_revision": 0})).status_code == 409
    approved = await client.put(url, json=consent)
    assert approved.status_code == 200 and approved.json()["approved"], approved.text
    with db.get_db(config.db_path) as conn:
        assert notifications.list_open(config, conn, "bob")[1] == 0
    session(client, configured, "alice")
    assert (await client.put(ROOT + "/daily", json={**raw, "enabled": True, "revision": 1})).status_code == 200
    with db.get_db(config.db_path) as conn:
        definition = store.list_definitions(conn, "family")[0]
        run = store.enqueue_run(conn, config, definition["id"], actor_id="alice", request_id=str(uuid4()), now=NOW)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        store.publish_run(conn, config, run["id"], claim["attempt_token"], subject="Edition", body_md="Retained content", now=NOW)
    session(client, configured, "bob")
    assert (await client.request("DELETE", url, json={"expected_grant_revision": 0})).status_code == 409
    assert (await client.request("DELETE", url, json={"expected_grant_revision": 1})).status_code == 200
    assert (await client.get(ROOT + f"/daily/runs/{run['id']}" )).json()["body_md"] == "Retained content"
    session(client, configured, "alice")
    assert (await client.post(ROOT + "/daily/runs", json={"request_id": str(uuid4()), "mode": "preview"})).status_code == 409


async def test_all_users_admin_exception_and_module_independent_reading(client, configured):
    from istota.groups.system import ensure_all_users_group
    config = configured._config
    with db.get_db(config.db_path) as conn:
        group = ensure_all_users_group(conn, config)
    session(client, configured, "admin")
    url = BASE + f"/groups/{group['group_id']}/briefings"
    assert (await client.post(url, json={**payload(enabled=False, channel="in_app"), "revision": 0})).status_code == 200
    session(client, configured, "bob")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE user_profiles SET disabled_modules=? WHERE user_id=?", ('["briefings"]', "bob"))
    response = await client.get(url)
    assert response.status_code == 200
    caps = response.json()["items"][0]["capabilities"]
    assert caps["read_briefings"] and not caps["author_briefings"] and not caps["receive_briefings"]
    assert (await client.post(url, json={**payload(enabled=False, channel="in_app"), "revision": 0})).status_code == 403


async def test_preview_body_is_hidden_after_definition_changes(client, configured):
    definition = await create(client)
    config = configured._config
    with db.get_db(config.db_path) as conn:
        run = store.enqueue_run(conn, config, definition["id"], actor_id="alice", request_id=str(uuid4()), mode="preview", now=NOW)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        store.publish_run(conn, config, run["id"], claim["attempt_token"], subject="Draft", body_md="UNPUBLISHED CANARY", now=NOW)
    url = ROOT + f"/daily/runs/{run['id']}"
    assert (await client.get(url)).json()["body_md"] == "UNPUBLISHED CANARY"
    assert (await client.put(ROOT + "/daily", json={**payload(enabled=False, channel="in_app"), "revision": 1})).status_code == 200
    assert (await client.get(url)).status_code == 404
    assert (await client.get(url + "/download")).status_code == 404


async def test_notification_write_failure_stays_pending_and_mutes_prevent_notices(client, configured, monkeypatch):
    from istota.briefings.group_delivery import queue_deliveries
    from istota.notifications import store as notifications
    _, run = await publish(client, configured)
    config = configured._config
    with db.get_db(config.db_path) as conn:
        store.set_delivery_preferences(conn, config, "family", actor_id="bob", outbound_muted=False, in_app_muted=True)
        with monkeypatch.context() as patch:
            patch.setattr(notifications, "write_notification", lambda *args, **kwargs: None)
            queue_deliveries(conn, config, now=NOW)
        rows = {r["user_id"]: r for r in store.list_recipients(conn, run["id"])}
        assert rows["alice"]["state"] == "pending" and rows["bob"]["state"] == "skipped"
        db.add_group_member(conn, "family", "carol", added_by="alice")
        conn.commit()
        queue_deliveries(conn, config, now=NOW)
        assert [r[0] for r in conn.execute("SELECT user_id FROM notifications WHERE source='group_briefing'")] == ["alice"]
