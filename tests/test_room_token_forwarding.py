"""Old identities keep routing through current membership and epochs."""
from types import SimpleNamespace

import pytest

from istota import db, room_policy, room_scopes
from istota.config import Config
from istota.transport import routing

OLD = "web-old-room"
NEW = "rm_current_room"


@pytest.fixture
def room_db(tmp_path):
    path = tmp_path / "rooms.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        db.register_room(conn, NEW, "alice", origin="web")
        db.add_room_binding(conn, NEW, "talk", "talk-ref")
        db.add_room_binding(conn, NEW, "web", NEW)
        conn.execute("INSERT INTO room_token_migration VALUES (?, ?, ?)",
                     (OLD, NEW, "2026-01-01T00:00:00Z"))
    return path


@pytest.mark.parametrize("cross_surface", [False, True])
def test_routing_forwards_old_identity(room_db, cross_surface):
    with db.get_db(room_db) as conn:
        assert routing._canonical_room_token(conn, "email", OLD, cross_surface=cross_surface) == NEW
        assert room_scopes.canonical_token(conn, OLD) == NEW
        assert routing._canonical_room_token(conn, "email", "talk-ref", cross_surface=False) is None
        assert routing._canonical_room_token(conn, "email", "unknown", cross_surface=True) is None


def test_live_room_and_binding_win_over_mapping(room_db):
    with db.get_db(room_db) as conn:
        db.register_room(conn, OLD, "bob", origin="talk")
        assert routing._canonical_room_token(conn, "talk", OLD, cross_surface=False) == OLD
        db.register_room(conn, "other", "bob", origin="talk")
        db.add_room_binding(conn, "other", "talk", "collision")
        conn.execute("INSERT INTO room_token_migration VALUES (?, ?, ?)", ("collision", NEW, "now"))
        assert routing._canonical_room_token(conn, "talk", "collision", cross_surface=False) == "other"
        conn.execute("INSERT INTO room_token_migration VALUES (?, ?, ?)", ("dangling", "missing", "now"))
        assert routing._canonical_room_token(conn, "talk", "dangling", cross_surface=False) is None


def test_shared_room_grants_and_membership_follow_old_identity(room_db):
    with db.get_db(room_db) as conn:
        db.add_room_member(conn, NEW, "bob")
        room_policy.ensure_policy(conn, NEW)
        room_scopes.grant_scopes(conn, NEW, "alice", ["files"])
        assert db.is_room_member(conn, OLD, "alice")
        assert not db.is_room_member(conn, OLD, "outsider")
        assert db.room_is_shared(conn, OLD)
        assert room_policy.get_policy(conn, OLD).room_token == NEW
        assert room_scopes.task_withheld_scopes(conn, conversation_token=OLD, user_id="alice", skill_index={}, policy="restrict") == frozenset({"memory"})
        db.upsert_room_participant(conn, room_token=NEW, surface="talk", surface_ref="guest", kind="guest")
        assert room_policy.audience_class(conn, OLD) == room_policy.MIXED
        assert room_scopes.task_withheld_scopes(conn, conversation_token=OLD, user_id="alice", skill_index={}, policy="restrict") == frozenset({"files", "memory"})


@pytest.mark.parametrize("collision", [False, True])
def test_history_aliases_preserve_epoch_boundaries(room_db, collision):
    with db.get_db(room_db) as conn:
        task = db.create_task(conn, "old private turn", "alice", conversation_token=OLD)
        if collision:
            db.register_room(conn, "unrelated", "carol", origin="talk")
            db.add_room_binding(conn, "unrelated", "talk", OLD)
        db.add_room_member(conn, NEW, "bob")
        cutoff = db.front_stage_cutoff(conn, NEW if collision else OLD)
        assert cutoff.task_id == task
        assert OLD in db.room_ref_tokens(conn, NEW)
        if not collision:
            assert NEW in db.room_ref_tokens(conn, OLD)
        assert db.pre_cutoff_room_task_ids(conn, NEW, cutoff, [task]) == {task}


def test_old_descriptor_and_shared_delivery(room_db):
    config = Config(db_path=room_db)
    task = SimpleNamespace(id=1, source_type="scheduled", conversation_token=None)
    with db.get_db(room_db) as conn:
        db.add_room_member(conn, NEW, "bob")
    plan = routing._expand_room_destinations(config, task, token=OLD)
    assert any(d.surface == "web" for d in plan)
    leg = routing.Destination("talk", OLD)
    assert routing.refuse_shared_rooms(config, "alice", [leg], purpose="test") == []
    assert routing.refuse_shared_rooms(config, "alice", [leg], purpose="test", conversation_token=OLD) == [leg]


def test_web_handle_lookup_and_route_use_current_token(room_db, monkeypatch):
    from istota import web_app
    monkeypatch.setattr(web_app, "_config", Config(db_path=room_db))
    with db.get_db(room_db) as conn:
        handle = conn.execute("INSERT INTO web_chat_rooms (user_id, token, name) VALUES (?, ?, ?) RETURNING id", ("alice", NEW, "Room")).fetchone()[0]
        assert db.get_web_chat_room_by_token(conn, OLD).token == NEW
        conn.execute("UPDATE web_chat_rooms SET token = ? WHERE id = ?", (OLD, handle))
    assert web_app._chat_owned_room("alice", handle).token == NEW
    assert web_app._chat_owned_room("bob", handle) is None


@pytest.mark.parametrize("stored_token", ["email-thread-old", NEW])
def test_stored_email_reply_finds_migrated_thread(room_db, stored_token):
    from istota.transport.email import threads
    with db.get_db(room_db) as conn:
        db.add_room_binding(conn, NEW, "email", "<root@example.com>")
        conn.execute("INSERT INTO room_token_migration VALUES (?, ?, ?)", ("email-thread-old", NEW, "now"))
        conn.execute("INSERT INTO processed_emails (email_id, user_id, message_id, thread_id, sender_email) VALUES (?, ?, ?, ?, ?)", ("1", "alice", "<reply@example.com>", stored_token, "alice@example.com"))
        email = SimpleNamespace(references="<reply@example.com>", in_reply_to=None, message_id=None)
        room = threads.find_thread_room(conn, Config(), email)
        assert room is not None and room.token == NEW


@pytest.mark.parametrize("source", ["sms", "talk"])
def test_history_reads_old_tasks_without_crossing_epoch(room_db, source):
    with db.get_db(room_db) as conn:
        old = db.create_task(conn, "before join", "alice", source_type=source, conversation_token=OLD)
        conn.execute("UPDATE tasks SET status = 'completed', result = 'old answer' WHERE id = ?", (old,))
        db.add_room_member(conn, NEW, "bob")
        new = db.create_task(conn, "after join", "alice", source_type=source, conversation_token=NEW)
        conn.execute("UPDATE tasks SET status = 'completed', result = 'new answer' WHERE id = ?", (new,))
        assert [m.id for m in db.get_conversation_history(conn, NEW)] == [old, new]
        cutoff = db.front_stage_cutoff(conn, OLD)
        assert [m.id for m in db.get_conversation_history(conn, OLD, after=cutoff)] == [new]


async def test_chat_route_reads_migrated_handle_with_authorization(room_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from istota import web_app

    config = Config(db_path=room_db)
    monkeypatch.setattr(web_app, "_config", config)
    with db.get_db(room_db) as conn:
        handle = conn.execute("INSERT INTO web_chat_rooms (user_id, token, name) VALUES (?, ?, ?) RETURNING id", ("alice", OLD, "Room")).fetchone()[0]
        db.add_message(conn, NEW, role="system", body="current transcript", origin_surface="web")
    overrides = dict(web_app.app.dependency_overrides)
    web_app.app.dependency_overrides[web_app._require_api_auth] = lambda: {"username": "alice"}
    try:
        async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="https://example.com") as client:
            response = await client.get(f"/istota/api/chat/rooms/{handle}/messages")
            assert response.status_code == 200
            assert "current transcript" in response.text
            web_app.app.dependency_overrides[web_app._require_api_auth] = lambda: {"username": "bob"}
            assert (await client.get(f"/istota/api/chat/rooms/{handle}/messages")).status_code == 404
    finally:
        web_app.app.dependency_overrides.clear()
        web_app.app.dependency_overrides.update(overrides)


def test_caught_up_check_sees_unmirrored_old_task(room_db):
    with db.get_db(room_db) as conn:
        old = db.create_task(conn, "old question", "alice", source_type="talk", conversation_token=OLD)
        new = db.create_task(conn, "new question", "alice", source_type="talk", conversation_token=NEW)
        conn.execute("UPDATE tasks SET status = 'completed', result = 'answer'")
        for role in ("user", "assistant"):
            db.add_message(conn, NEW, role=role, body="new body", origin_surface="talk", task_id=new)
        assert not db._messages_caught_up(conn, NEW)
        assert [m.id for m in db.get_conversation_history(conn, OLD)] == [old, new]
        for role in ("user", "assistant"):
            db.add_message(conn, NEW, role=role, body="old body", origin_surface="talk", task_id=old)
        assert db._messages_caught_up(conn, NEW)
        assert {m.id for m in db.get_conversation_history(conn, OLD)} == {old, new}


def test_membership_and_grant_changes_write_only_current_identity(room_db):
    with db.get_db(room_db) as conn:
        db.add_room_member(conn, OLD, "bob")
        assert db.list_room_members(conn, NEW) == ["alice", "bob"]
        room_scopes.grant_scopes(conn, OLD, "bob", ["files"])
        assert room_scopes.granted_scopes(conn, NEW, "bob") == frozenset({"files"})
        room_scopes.revoke_scopes(conn, OLD, "bob")
        assert not room_scopes.granted_scopes(conn, NEW, "bob")
        db.remove_room_member(conn, OLD, "bob")
        assert not db.is_room_member(conn, NEW, "bob")
        assert conn.execute("SELECT 1 FROM room_members WHERE room_token = ?", (OLD,)).fetchone() is None


def test_forwarding_does_not_claim_an_existing_rooms_history(room_db):
    with db.get_db(room_db) as conn:
        db.register_room(conn, OLD, "bob", origin="web")
        assert OLD not in db.room_ref_tokens(conn, NEW)
        other = db.create_task(conn, "other room", "bob", conversation_token=OLD)
        conn.execute("UPDATE tasks SET status = 'completed', result = 'private' WHERE id = ?", (other,))
        assert db.get_conversation_history(conn, NEW) == []


@pytest.mark.parametrize("collision", [False, True])
async def test_revoked_member_cannot_read_through_old_handle(room_db, monkeypatch, collision):
    from istota import web_app

    monkeypatch.setattr(web_app, "_config", Config(db_path=room_db))
    with db.get_db(room_db) as conn:
        db.add_web_room_member(conn, NEW, "bob")
        if collision:
            db.register_room(conn, "unrelated", "carol", origin="talk")
            db.add_room_binding(conn, "unrelated", "talk", OLD)
        stale = conn.execute("INSERT INTO web_chat_rooms (user_id, token, name) VALUES (?, ?, ?) RETURNING id", ("bob", OLD, "Room")).fetchone()[0]
        current = db.ensure_web_chat_handle(conn, "bob", NEW, "Room")
        db.drop_web_room_member(conn, OLD, "bob")
        db.add_message(conn, NEW, role="system", body="after revocation", origin_surface="web")
        assert not db.is_room_member(conn, NEW, "bob")
    for handle in (stale, current.id):
        response = await web_app.chat_room_messages(handle, user={"username": "bob"})
        assert getattr(response, "status_code", 200) == 404


def test_renaming_old_handle_updates_current_room(room_db, monkeypatch):
    from istota import web_app

    monkeypatch.setattr(web_app, "_config", Config(db_path=room_db))
    with db.get_db(room_db) as conn:
        handle = conn.execute("INSERT INTO web_chat_rooms (user_id, token, name) VALUES (?, ?, ?) RETURNING id", ("alice", OLD, "Room")).fetchone()[0]
    result = web_app._chat_update_room("alice", handle, "Renamed", None)
    assert result["token"] == NEW
    with db.get_db(room_db) as conn:
        assert db.get_room(conn, NEW).name == "Renamed"


async def test_forwarded_descriptor_delivers_through_preserved_web_binding(room_db):
    from istota.transport.web import WebTransport

    config = Config(db_path=room_db)
    with db.get_db(room_db) as conn:
        conn.execute("UPDATE room_bindings SET surface_ref = ? WHERE room_token = ? AND surface = 'web'", (OLD, NEW))
    task = SimpleNamespace(id=1, source_type="scheduled", conversation_token=None)
    web = next(d for d in routing._expand_room_destinations(config, task, token=OLD) if d.surface == "web")
    assert web.channel == OLD
    message_id = await WebTransport(config).deliver(web.channel, "forwarded notice")
    assert message_id is not None
    with db.get_db(room_db) as conn:
        assert [m.body for m in db.list_system_messages(conn, NEW)] == ["forwarded notice"]


def test_delete_migrated_room_removes_old_handles_and_history(room_db):
    with db.get_db(room_db) as conn:
        stale = conn.execute("INSERT INTO web_chat_rooms (user_id, token, name) VALUES (?, ?, ?) RETURNING id", ("alice", OLD, "Room")).fetchone()[0]
        current = db.ensure_web_chat_handle(conn, "alice", NEW, "Room")
        old_task = db.create_task(conn, "old history", "alice", conversation_token=OLD)
        assert db.delete_web_chat_room(conn, current.id, "alice")
        assert db.get_web_chat_room(conn, stale) is None
        assert db.get_task(conn, old_task) is None
        assert routing.canonical_room_token(conn, OLD) is None


def test_deleted_alias_never_falls_through_to_reused_surface_ref(room_db):
    with db.get_db(room_db) as conn:
        conn.execute("DELETE FROM rooms WHERE token = ?", (NEW,))
        db.register_room(conn, "unrelated", "bob", origin="talk")
        db.add_room_binding(conn, "unrelated", "talk", OLD)
        assert routing._canonical_room_token(conn, "talk", OLD, cross_surface=False) is None
        assert room_scopes.canonical_token(conn, OLD) is None
