"""Personal push eligibility through real profile, room and dispatch seams."""

import pytest

from istota import db, user_profiles
from tests.test_web_notifications import (  # noqa: F401
    ORIGIN, _db_path, _login, client,
)


async def test_category_preferences_round_trip_without_changing_inbox(client):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.put(
        "/istota/api/settings/profile", cookies=cookies, headers=ORIGIN,
        json={"push_inbox_categories": {"confirmation": False}},
    )
    assert response.status_code == 200
    profile = (await client.get("/istota/api/settings/profile", cookies=cookies)).json()["profile"]
    assert profile["push_inbox_categories"] == {"confirmation": False}
    assert user_profiles.get_profile(_db_path(), "alice").push_inbox_categories == {"confirmation": False}


@pytest.mark.parametrize("value", [None, [], "off", {"unknown_source": False}, {"confirmation": "false"}, {"confirmation": 0}])
async def test_category_validation(client, value):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.put("/istota/api/settings/profile", cookies=cookies, headers=ORIGIN,
                                json={"push_inbox_categories": value})
    assert response.status_code == 400


async def test_categories_are_authenticated_and_registry_backed(client, monkeypatch):  # noqa: F811
    from istota.notifications import sources

    assert (await client.get("/istota/api/push/categories")).status_code == 401
    cookies = await _login(client, "alice")
    categories = (await client.get("/istota/api/push/categories", cookies=cookies)).json()["categories"]
    assert {item["id"] for item in categories} == set(sources.all_resolvers())
    assert next(item["label"] for item in categories if item["id"] == "confirmation") == "Approvals"
    registered = sources.all_resolvers()
    registered["new_source"] = object()
    monkeypatch.setattr(sources, "all_resolvers", lambda: registered)
    from istota.push.preferences import notification_categories, validate_inbox_categories
    assert {"id": "new_source", "label": "New source"} in notification_categories()
    assert validate_inbox_categories({"new_source": False}) == {"new_source": False}


async def test_profile_body_limit_and_default_restore(client):  # noqa: F811
    cookies = await _login(client, "alice")
    url = "/istota/api/settings/profile"
    assert (await client.put(url, cookies=cookies, headers=ORIGIN, content=b"x" * 65537)).status_code == 413
    assert (await client.put(url, cookies=cookies, headers=ORIGIN, json=[])).status_code == 400
    assert (await client.put(url, cookies=cookies, headers=ORIGIN, json={"push_inbox_categories": {"confirmation": False}})).status_code == 200
    assert (await client.put(url, cookies=cookies, headers=ORIGIN, json={"push_inbox_categories": {}})).status_code == 200
    assert user_profiles.get_profile(_db_path(), "alice").push_inbox_categories == {}
    other = await _login(client, "bob")
    assert (await client.get(url, cookies=other)).json()["profile"]["push_inbox_categories"] == {}


@pytest.mark.parametrize("override,expected", [(None, "off"), ("default", "off"), ("alerts", "alerts"), ("badge", "badge"), ("off", "off")])
def test_explicit_modes_override_off_account_default(override, expected):
    from istota.push.preferences import effective_room_mode
    assert effective_room_mode("off", override) == expected


async def test_room_override_is_personal_and_requires_current_membership(client):  # noqa: F811
    cookies = await _login(client, "alice")
    with db.get_db(_db_path()) as conn:
        room = db.create_web_chat_room(conn, "bob", "Shared")
        db.add_web_room_member(conn, room.token, "alice")
        personal = db.ensure_web_chat_handle(conn, "alice", room.token, room.name)
        db.add_message(conn, room.token, role="assistant", body="Unread", origin_surface="web")
    url = f"/istota/api/chat/rooms/{personal.id}"
    response = await client.patch(url, cookies=cookies, headers=ORIGIN, json={"push_notifications": "off"})
    assert response.status_code == 200
    assert response.json()["push_notifications"] == "off"
    assert (await client.patch(url, cookies=cookies, headers=ORIGIN, json={"color": "teal"})).json()["push_notifications"] == "off"
    with db.get_db(_db_path()) as conn:
        assert db.get_web_chat_room(conn, room.id).push_notifications == "default"
        assert db.count_unread_messages(conn, room.token, "web", "alice") == 1
    assert (await client.patch(f"/istota/api/chat/rooms/{room.id}", cookies=cookies, headers=ORIGIN, json={"push_notifications": "alerts"})).status_code == 404
    assert (await client.patch(url, cookies=cookies, headers=ORIGIN, json={"push_notifications": "default"})).json()["push_notifications"] == "default"
    with db.get_db(_db_path()) as conn:
        db.remove_room_member(conn, room.token, "alice")
    assert (await client.patch(url, cookies=cookies, headers=ORIGIN, json={"push_notifications": "alerts"})).status_code == 404


@pytest.mark.parametrize("mode", [None, "", "no", [], {}, False])
async def test_room_invalid_mode_is_400(client, mode):  # noqa: F811
    cookies = await _login(client, "alice")
    with db.get_db(_db_path()) as conn:
        room = db.create_web_chat_room(conn, "alice", "Work")
    assert (await client.patch(f"/istota/api/chat/rooms/{room.id}", cookies=cookies, headers=ORIGIN,
                               json={"push_notifications": mode})).status_code == 400


async def test_filtered_total_agrees_across_endpoint_dispatch_and_cli(client, monkeypatch):  # noqa: F811
    from types import SimpleNamespace
    from istota import cli
    from istota.webui import app as web
    from istota.push import apns, dispatch, preferences
    from istota.notifications import store
    from tests.test_push_dispatch import register, message, notification
    import time

    cookies = await _login(client, "alice")
    user_profiles.ensure_profile(_db_path(), "alice")
    user_profiles.update_profile(_db_path(), "alice", push_chat_alerts="off", push_inbox_categories={"confirmation": False})
    now = time.time()
    with db.get_db(_db_path()) as conn:
        register(conn)
        for mode in ("default", "alerts", "badge", "off"):
            room = db.create_web_chat_room(conn, "alice", mode)
            db.update_web_chat_room(conn, room.id, push_notifications=mode)
            message(conn, room, now - 20)
        notification(conn, now)
        store.write_notification(conn, "alice", source="task_alert", dedup_key="a", title="Alert")
        store.write_notification(conn, "bob", source="task_alert", dedup_key="b", title="Other")
        assert preferences.push_badge_count(conn, "alice") == 3
    result = (await client.get("/istota/api/notifications/count", cookies=cookies)).json()
    assert (result["open"], result["chat_unread"], result["push_badge"]) == (2, 4, 3)
    sent = []
    def send(config, device, payload, **kwargs):
        sent.append(payload)
        return apns.ApnsResult(status=200, badge=payload["aps"]["badge"], device_created_at=device["created_at"])
    monkeypatch.setattr(apns, "send", send)
    dispatch._dispatch_user(web._config, "alice", [], 999999, now)
    assert len(sent) == 1 and sent[0]["istota"]["kind"] == "chat"
    assert sent[0]["aps"]["badge"] == 3
    web._config.push.enabled = True
    monkeypatch.setattr(cli, "load_config", lambda *args: web._config)
    assert cli.cmd_push_test(SimpleNamespace(config=None, user="alice", message="Test")) == 0
    assert sent[-1]["aps"]["badge"] == 3


def test_profile_seed_merge_strict_read_and_legacy_keys(tmp_path):
    from istota.config import UserConfig
    from istota.settings_plan.profiles import snapshot_profile
    path = tmp_path / "test.db"
    db.init_db(path)
    config = UserConfig(push_inbox_categories={"confirmation": False})
    profile = user_profiles.ensure_profile(path, "alice", seed_from=config)
    assert profile.push_inbox_categories == {"confirmation": False}
    config.push_inbox_categories = {}
    user_profiles.merge_into_user_config(profile, config)
    assert config.push_inbox_categories == {"confirmation": False}
    assert snapshot_profile(profile)["profile"]["push_inbox_categories"] == {"confirmation": False}
    with db.get_db(path) as conn:
        conn.execute("UPDATE user_profiles SET push_inbox_categories=?", ('{"confirmation": false, "retired_source": false}',))
        row = conn.execute("SELECT * FROM user_profiles").fetchone()
        assert user_profiles._row_to_profile(row, strict=True).push_inbox_categories == {"confirmation": False}


async def test_managed_category_map_refuses_self_changes(client):  # noqa: F811
    cookies = await _login(client, "alice")
    user_profiles.provision_user(_db_path(), "alice", {"push_inbox_categories": {"confirmation": False}}, mode="managed")
    response = await client.put("/istota/api/settings/profile", cookies=cookies, headers=ORIGIN,
                                json={"push_inbox_categories": {}})
    assert response.status_code == 409
    assert user_profiles.get_profile(_db_path(), "alice").push_inbox_categories == {"confirmation": False}


def test_config_parsing_keeps_boolean_categories():
    from istota.config import _parse_user_data
    assert _parse_user_data({"push_inbox_categories": {"confirmation": False}}, "alice").push_inbox_categories == {"confirmation": False}
    with pytest.raises(ValueError):
        _parse_user_data({"push_inbox_categories": {"confirmation": "false"}}, "alice")


def test_category_reads_ignore_unknown_legacy_keys():
    from istota.push.preferences import stored_inbox_categories
    assert stored_inbox_categories({"confirmation": False, "retired": "garbage"}, strict=True) == {"confirmation": False}
    with pytest.raises(ValueError):
        stored_inbox_categories({"confirmation": "false"}, strict=True)


def test_filtered_badges_keep_membership_and_visibility_rules(tmp_path):
    from istota.push.preferences import push_badge_count
    path = tmp_path / "test.db"
    db.init_db(path)
    user_profiles.ensure_profile(path, "alice")
    user_profiles.update_profile(path, "alice", push_chat_alerts="off")
    with db.get_db(path) as conn:
        shared = db.create_web_chat_room(conn, "bob", "Shared")
        db.add_web_room_member(conn, shared.token, "alice")
        handle = db.ensure_web_chat_handle(conn, "alice", shared.token, "Shared")
        db.update_web_chat_room(conn, handle.id, push_notifications="badge")
        db.update_web_chat_room(conn, shared.id, push_notifications="off")
        db.add_message(conn, shared.token, role="assistant", body="Reply", origin_surface="web")
        assert push_badge_count(conn, "alice") == 1
        assert push_badge_count(conn, "bob") == 0
        assert db.total_unread_messages(conn, "bob", "web") == 1
        db.dismiss_room(conn, shared.token, "alice")
        assert push_badge_count(conn, "alice") == 0
        db.undismiss_room(conn, shared.token, "alice")
        db.set_room_archived(conn, shared.token, True)
        assert push_badge_count(conn, "alice") == 0
        db.set_room_archived(conn, shared.token, False)
        db.remove_room_member(conn, shared.token, "alice")
        assert push_badge_count(conn, "alice") == 0
