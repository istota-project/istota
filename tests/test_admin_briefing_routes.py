"""Admin › Briefings through the host app: real sessions, real SQLite, real step-up."""
import logging

import pytest

from istota import db, user_briefings
from istota.briefings import group_store as store
from istota.config import refresh_user_profiles_if_changed
from istota.groups.system import ensure_all_users_group
from istota.webui import auth as web_auth
from tests.test_group_briefing_store import payload
from tests.test_group_routes import BASE, configured as _configured, client as _client, session

configured = _configured
client = _client

ROOT = BASE + "/admin/briefings"
STEP_UP = BASE + "/settings/step-up"
PASSWORD = "a long example passphrase"
PERSONAL_KEYS = {"id", "user_id", "owner_label", "name", "title", "cron", "timezone", "output",
                 "enabled", "held", "last_run_at"}
GROUP_KEYS = {"id", "group_id", "group_label", "system_key", "name", "title", "cron", "timezone",
              "channel", "enabled", "held", "owners", "last_published_at"}


@pytest.fixture
def seeded(configured, monkeypatch):
    """Alice's personal briefing, an orphan's, a Family and an All users definition."""
    config = configured._config
    monkeypatch.setattr(web_auth, "_N", 1024)
    for user_id in ("admin", "alice"):
        web_auth.upsert_identity(config.db_path, user_id, f"{user_id}@example.com")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE web_auth_identities SET password_hash=? WHERE user_id='admin'",
                     (web_auth.hash_password(PASSWORD),))
    refresh_user_profiles_if_changed(config)
    alice, _ = user_briefings.ensure_briefing(config.db_path, user_id="alice", name="weekly",
                                              cron="0 9 * * 1", title="Weekly review",
                                              components={"secret": "PRIVATE COMPONENT"})
    ghost, _ = user_briefings.ensure_briefing(config.db_path, user_id="ghost", name="old", cron="0 6 * * *")
    with db.get_db(config.db_path) as conn:
        family = store.save_definition(conn, config, "family", payload(channel="in_app"),
                                       actor_id="alice", expected_revision=0)
        everyone = ensure_all_users_group(conn, config)
        common = store.save_definition(conn, config, everyone["group_id"],
                                       payload(name="common", title="Common news", channel="in_app"),
                                       actor_id="admin", expected_revision=0, is_admin=True)
        gone = store.save_definition(conn, config, "family", payload(name="gone", channel="in_app"),
                                     actor_id="alice", expected_revision=0)
        store.delete_definition(conn, gone["id"], actor_id="admin")
    return {"alice": alice, "ghost": ghost, "family": family, "common": common, "everyone": everyone}


def _notifications(configured, user_id=None):
    with db.get_db(configured._config.db_path) as conn:
        sql = "SELECT * FROM notifications WHERE source = 'admin_briefing_change'"
        args = ()
        if user_id is not None:
            sql += " AND user_id = ?"
            args = (user_id,)
        return [dict(row) for row in conn.execute(sql + " ORDER BY id", args)]


async def _step_up(client, action="admin_briefing_delete"):
    response = await client.post(STEP_UP, json={"action": action})
    assert response.status_code == 200, response.text
    assert response.json()["method"] == "password"
    return {"request_id": response.json()["request_id"], "password": PASSWORD}


@pytest.fixture
def admin(client, configured, seeded):
    session(client, configured, "admin")
    return client


class TestTheList:
    async def test_lists_both_kinds_without_content(self, admin, seeded):
        response = await admin.get(ROOT)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        personal = {row["name"]: row for row in body["personal"]}
        assert set(personal) == {"weekly", "old"}
        assert all(set(row) == PERSONAL_KEYS for row in body["personal"])
        assert personal["weekly"]["owner_label"] == "Alice"
        assert personal["weekly"]["timezone"] == "UTC" and personal["weekly"]["held"] is None
        # A row whose user has no profile is listed so it can be cleaned up.
        assert personal["old"]["owner_label"] == "ghost" and personal["old"]["timezone"] is None
        group = {row["name"]: row for row in body["group"]}
        assert set(group) == {"daily", "common"}
        assert all(set(row) == GROUP_KEYS for row in body["group"])
        assert group["daily"]["group_label"] == "Family" and group["daily"]["owners"] == ["Alice"]
        assert group["common"]["system_key"] == "all_users"
        assert "PRIVATE COMPONENT" not in response.text
        assert "Write a short edition" not in response.text

    async def test_last_run_is_marked_utc(self, admin, configured):
        # SQLite stamps a naive UTC string; the browser reads a zoneless value as
        # local time, so an evening run west of UTC rendered as "just now".
        with db.get_db(configured._config.db_path) as conn:
            db.set_briefing_last_run(conn, "alice", "weekly")
            stored = db.get_briefing_last_run(conn, "alice", "weekly")
        body = (await admin.get(ROOT)).json()
        personal = {row["name"]: row for row in body["personal"]}
        assert personal["weekly"]["last_run_at"] == stored.replace(" ", "T") + "Z"
        assert personal["old"]["last_run_at"] is None

    async def test_a_non_admin_is_refused_every_route(self, client, configured, seeded):
        session(client, configured, "alice")
        alice_id, daily_id = seeded["alice"].id, seeded["family"]["id"]
        assert (await client.get(ROOT)).status_code == 403
        for path in (f"/personal/{alice_id}/hold", f"/personal/{alice_id}/resume",
                     f"/group/{daily_id}/hold", f"/group/{daily_id}/resume"):
            assert (await client.post(ROOT + path, json={})).status_code == 403, path
        for path in (f"/personal/{alice_id}", f"/group/{daily_id}"):
            response = await client.request("DELETE", ROOT + path, json={"confirm": "weekly"})
            assert response.status_code == 403, path
        refused = await client.post(STEP_UP, json={"action": "admin_briefing_delete"})
        assert refused.status_code == 403
        assert user_briefings.get_briefing(configured._config.db_path, "alice", "weekly").admin_hold_at is None

    async def test_an_empty_admins_list_refuses_everyone(self, admin, configured, seeded):
        configured._config.admin_users = set()
        assert (await admin.get(ROOT)).status_code == 403
        assert (await admin.post(ROOT + f"/personal/{seeded['alice'].id}/hold", json={})).status_code == 403

    async def test_writes_need_the_origin(self, admin, seeded):
        other = {"origin": "https://other.example.com"}
        response = await admin.post(ROOT + f"/personal/{seeded['alice'].id}/hold", json={}, headers=other)
        assert response.status_code == 403
        response = await admin.request("DELETE", ROOT + f"/group/{seeded['family']['id']}",
                                       json={"confirm": "daily"}, headers=other)
        assert response.status_code == 403


class TestHoldAndResume:
    async def test_personal_hold_is_idempotent_and_notifies_once(self, admin, configured, seeded, caplog):
        caplog.set_level(logging.INFO, logger="istota.briefings.admin_routes")
        path = ROOT + f"/personal/{seeded['alice'].id}"
        first = await admin.post(path + "/hold", json={})
        assert first.status_code == 200, first.text
        assert first.json()["held"]["by_label"] == "Admin"
        stored = user_briefings.get_briefing(configured._config.db_path, "alice", "weekly")
        assert stored.admin_hold_by == "admin"
        notices = _notifications(configured)
        assert [(n["user_id"], n["title"]) for n in notices] == [("alice", "An administrator paused your briefing")]
        assert "Weekly review" in notices[0]["body"]

        again = await admin.post(path + "/hold", json={})
        assert again.status_code == 200
        assert user_briefings.get_briefing(configured._config.db_path, "alice", "weekly").admin_hold_at == stored.admin_hold_at
        assert _notifications(configured) == notices

        resumed = await admin.post(path + "/resume", json={})
        assert resumed.status_code == 200 and resumed.json()["held"] is None
        assert [n["title"] for n in _notifications(configured, "alice")] == [
            "An administrator paused your briefing", "An administrator resumed your briefing"]
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("admin_briefing ")]
        assert lines == [
            f"admin_briefing action=hold kind=personal id={seeded['alice'].id} owner=alice admin=admin",
            f"admin_briefing action=resume kind=personal id={seeded['alice'].id} owner=alice admin=admin",
        ]
        assert "Weekly review" not in caplog.text

    async def test_unknown_ids_and_the_wrong_kind_are_404(self, admin, seeded):
        for path in ("/personal/9999/hold", "/group/9999/resume", f"/group/{_unused_group_id(seeded)}/hold"):
            response = await admin.post(ROOT + path, json={})
            assert response.status_code == 404, path
            assert response.json()["error"] == "briefing_not_found"

    async def test_a_deleted_definition_is_404(self, admin, configured, seeded):
        with db.get_db(configured._config.db_path) as conn:
            gone = conn.execute("SELECT id FROM group_briefing_configs WHERE deleted_at IS NOT NULL").fetchone()[0]
        response = await admin.post(ROOT + f"/group/{gone}/hold", json={})
        assert response.status_code == 404 and response.json()["error"] == "briefing_not_found"

    async def test_an_orphan_is_holdable_with_no_notice(self, admin, configured, seeded):
        response = await admin.post(ROOT + f"/personal/{seeded['ghost'].id}/hold", json={})
        assert response.status_code == 200
        assert response.json()["held"] is not None
        assert _notifications(configured) == []

    async def test_group_hold_tells_the_owners_and_not_the_acting_admin(self, admin, configured, seeded):
        with db.get_db(configured._config.db_path) as conn:
            db.add_group_member(conn, "family", "admin", role="owner", added_by="alice")
        path = ROOT + f"/group/{seeded['family']['id']}"
        response = await admin.post(path + "/hold", json={})
        assert response.status_code == 200, response.text
        assert response.json()["held"]["by_label"] == "Admin"
        assert sorted(response.json()["owners"]) == ["Admin", "Alice"]
        notices = _notifications(configured)
        assert [(n["user_id"], n["title"]) for n in notices] == [("alice", "An administrator paused your briefing")]
        assert "Daily news" in notices[0]["body"] and "Family" in notices[0]["body"]
        assert (await admin.post(path + "/hold", json={})).status_code == 200
        assert len(_notifications(configured)) == 1
        assert (await admin.post(path + "/resume", json={})).json()["held"] is None
        assert len(_notifications(configured, "alice")) == 2

    async def test_an_all_users_briefing_notifies_nobody(self, admin, configured, seeded):
        path = ROOT + f"/group/{seeded['common']['id']}"
        assert (await admin.post(path + "/hold", json={})).status_code == 200
        assert (await admin.post(path + "/resume", json={})).status_code == 200
        assert _notifications(configured) == []


def _unused_group_id(seeded):
    # A personal id that is no group definition's id.
    taken = {seeded["family"]["id"], seeded["common"]["id"]}
    return next(n for n in range(seeded["alice"].id + 100, seeded["alice"].id + 200) if n not in taken)


class TestDelete:
    async def test_personal_delete_needs_the_name_then_the_step_up(self, admin, configured, seeded, caplog):
        caplog.set_level(logging.INFO, logger="istota.briefings.admin_routes")
        path = ROOT + f"/personal/{seeded['alice'].id}"
        for body in ({}, {"confirm": "Weekly review"}, {"confirm": "WEEKLY"}):
            response = await admin.request("DELETE", path, json=body)
            assert response.status_code == 409, body
            assert response.json()["error"] == "confirm_required"
        missing = await admin.request("DELETE", path, json={"confirm": "weekly"})
        assert missing.status_code == 403 and missing.json()["field"] == "password"
        wrong = await admin.request("DELETE", path, json={"confirm": "weekly", "step_up": {
            **(await _step_up(admin)), "password": "not it"}})
        assert wrong.status_code == 403 and wrong.json()["reason"] == "bad"
        assert user_briefings.get_briefing(configured._config.db_path, "alice", "weekly") is not None

        response = await admin.request("DELETE", path, json={"confirm": "weekly", "step_up": await _step_up(admin)})
        assert response.status_code == 200, response.text
        assert user_briefings.get_briefing(configured._config.db_path, "alice", "weekly") is None
        assert [n["title"] for n in _notifications(configured, "alice")] == ["An administrator deleted your briefing"]
        assert f"admin_briefing action=delete kind=personal id={seeded['alice'].id} owner=alice admin=admin" in caplog.text
        assert "Weekly review" not in caplog.text
        assert (await admin.request("DELETE", path, json={"confirm": "weekly"})).status_code == 404

    async def test_a_refusal_before_the_step_up_does_not_spend_the_code(self, admin, configured, seeded):
        path = ROOT + f"/personal/{seeded['alice'].id}"
        proof = await _step_up(admin)
        refused = await admin.request("DELETE", path, json={"confirm": "nope", "step_up": proof})
        assert refused.status_code == 409
        response = await admin.request("DELETE", path, json={"confirm": "weekly", "step_up": proof})
        assert response.status_code == 200, response.text

    async def test_a_code_for_another_action_is_refused(self, admin, configured, seeded):
        path = ROOT + f"/group/{seeded['family']['id']}"
        proof = await _step_up(admin, "admin_config_change")
        response = await admin.request("DELETE", path, json={"confirm": "daily", "step_up": proof})
        assert response.status_code == 403
        with db.get_db(configured._config.db_path) as conn:
            assert store.get_definition(conn, seeded["family"]["id"])["deleted_at"] is None

    async def test_group_delete_soft_deletes_and_tells_the_owners(self, admin, configured, seeded):
        path = ROOT + f"/group/{seeded['family']['id']}"
        response = await admin.request("DELETE", path, json={"confirm": "daily", "step_up": await _step_up(admin)})
        assert response.status_code == 200, response.text
        with db.get_db(configured._config.db_path) as conn:
            assert store.get_definition(conn, seeded["family"]["id"])["deleted_by"] == "admin"
        assert [g["name"] for g in (await admin.get(ROOT)).json()["group"]] == ["common"]
        notices = _notifications(configured, "alice")
        assert [n["title"] for n in notices] == ["An administrator deleted your briefing"]
        assert (await admin.request("DELETE", path, json={"confirm": "daily"})).status_code == 404

    async def test_a_pending_migration_refuses_before_the_step_up(self, admin, configured, seeded):
        definition_id = seeded["family"]["id"]
        with db.get_db(configured._config.db_path) as conn:
            conn.execute("INSERT INTO group_briefing_migrations (legacy_key, definition_id, state, data_json) "
                         "VALUES ('default:daily', ?, 'imported', '{}')", (definition_id,))
        proof = await _step_up(admin)
        response = await admin.request("DELETE", ROOT + f"/group/{definition_id}",
                                       json={"confirm": "daily", "step_up": proof})
        assert response.status_code == 409 and response.json()["error"] == "migration_pending"
        with db.get_db(configured._config.db_path) as conn:
            used = conn.execute("SELECT used_at FROM web_auth_step_ups WHERE request_id = ?",
                                (proof["request_id"],)).fetchone()[0]
        assert used is None

    async def test_an_all_users_delete_notifies_nobody(self, admin, configured, seeded):
        response = await admin.request("DELETE", ROOT + f"/group/{seeded['common']['id']}",
                                       json={"confirm": "common", "step_up": await _step_up(admin)})
        assert response.status_code == 200, response.text
        assert _notifications(configured) == []
