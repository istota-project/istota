"""Timed sharing through scheduler parking and authenticated confirmation."""

from datetime import datetime, timedelta, timezone
import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from istota import confirmations, db
from istota.config import Config, UserConfig
from istota.location import sharing
from istota.notifications import store
from istota.notifications.resolvers import confirmation
from istota.relay.requests import RequestError, text_hash
from istota.rooms import private_replies

NOW = datetime(2026, 3, 8, 12, tzinfo=timezone.utc)
#: Words only the grant preview carries: where it may and may not appear.
MARKER = "can ask where you are"


@pytest.fixture
def env(tmp_path):
    config = Config(db_path=tmp_path / "state.db", workspace_path=tmp_path / "files",
                    users={u: UserConfig() for u in ("alice", "bob", "carol")})
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Hiking").token
        db.add_web_room_member(conn, room, "bob")
    return config, room


def turn(config, room):
    with db.get_db(config.db_path) as conn:
        ident = db.create_task(conn, user_id="alice", source_type="web", prompt="Share for an hour",
                               conversation_token=room, output_target="room")
        db.add_message(conn, room, role="user", body="Share for an hour", origin_surface="web",
                       task_id=ident, author_user_id="alice")
    return ident


def request(conn, config, ident, **overrides):
    options = dict(actor_user_id="alice", task_id=ident, request_key="share",
                   duration_seconds=3600, precision="exact", now=NOW)
    return sharing.request_location_share(conn, config, **(options | overrides))


def park(env, *, private=False, success=True):
    config, room = env
    with db.get_db(config.db_path) as conn:
        if private:
            db.create_web_chat_room(conn, "alice", "Private")
    ident = turn(config, room)

    def execute(*args, **kwargs):
        with db.get_db(config.db_path) as conn:
            assert request(conn, config, ident)["status"] == "held"
        return success, "Waiting for approval.", None, None

    with patch("istota.scheduler.execute_task", side_effect=execute), patch(
            "istota.notifications.delivery.send_notification", return_value=True):
        from istota.scheduler import process_one_task
        process_one_task(config)
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, ident), dict(conn.execute(
            "SELECT * FROM location_share_grants WHERE origin_task_id=?", (ident,)).fetchone())


@pytest.mark.parametrize("private", [False, True])
def test_shared_origin_private_preview_owner_digest_and_fixed_expiry(env, private, monkeypatch):
    config, room = env
    task, grant = park(env, private=private)
    assert task.status == "pending_confirmation"
    assert grant["state"] == "held" and grant["expires_at"] is None
    preview = task.confirmation_prompt
    assert preview == (
        "Share your exact location with Hiking for 1 hour?\n\n"
        "bob can ask where you are until then. You can stop at any time; "
        "anything already posted stays in the room.")
    with db.get_db(config.db_path) as conn:
        assert confirmations.describe_title(conn, task) == confirmation.LOCATION_SHARE_TITLE
        views, _ = store.list_open(config, conn, "alice")
        assert views[0].title == confirmation.LOCATION_SHARE_TITLE
        assert store.list_open(config, conn, "bob") == ([], 0)
        rooms = private_replies.preview_rooms(conn, task)
        assert bool(rooms) == private and room not in rooms
        if private:
            assert any(preview in row[0] for row in conn.execute("SELECT body FROM messages WHERE room_token=?", (rooms[0],)))
        else:
            assert MARKER in views[0].body
        for row in conn.execute("SELECT body FROM messages WHERE room_token=?", (room,)):
            assert MARKER not in row[0]
        for row in conn.execute("SELECT payload FROM task_events WHERE task_id=?", (task.id,)):
            assert MARKER not in row[0]
        assert not task.result or MARKER not in task.result
        assert not task.execution_trace or MARKER not in task.execution_trace
        with pytest.raises(RequestError, match="confirmation_unavailable"):
            confirmations.approve(conn, task, config=config, preview_digest="wrong")
        with pytest.raises(RequestError, match="confirmation_unavailable"):
            sharing.approve_location_share(conn, config, grant_id=grant["id"], request_id=grant["request_id"],
                preview_digest=text_hash(preview), actor_user_id="bob", now=NOW)
    from istota.webui import app
    monkeypatch.setattr(app, "_config", config)
    for reader in ("alice", "bob"):
        assert MARKER not in json.dumps(app._chat_room_messages(reader, room, 50))
        events = app._room_events_batch(reader, 0)["events"]
        shared_events = [event for event in events if event.get("room_token") == room]
        assert MARKER not in json.dumps(shared_events)
    args = dict(room=rooms[0]) if private else dict(preview_digest=text_hash(preview))
    with pytest.raises(HTTPException) as denied:
        app._chat_confirm_task(task.id, "bob", **args)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException):
        app._chat_confirm_task(task.id, "alice", room=room)
    app._chat_confirm_task(task.id, "alice", **args)
    with db.get_db(config.db_path) as conn:
        active = dict(conn.execute("SELECT * FROM location_share_grants WHERE id=?", (grant["id"],)).fetchone())
        assert active["state"] == "active"
        assert datetime.fromisoformat(active["expires_at"]) - datetime.fromisoformat(active["approved_at"]) == timedelta(hours=1)
        repeat = sharing.approve_location_share(conn, config, grant_id=grant["id"], request_id=grant["request_id"],
            preview_digest=text_hash(preview), actor_user_id="alice",
            now=datetime.fromisoformat(active["approved_at"]).replace(tzinfo=timezone.utc) + timedelta(minutes=1))
        assert repeat["expires_at"] == active["expires_at"]
        with pytest.raises(RequestError, match="grant_unavailable"):
            sharing.approve_location_share(conn, config, grant_id=grant["id"], request_id=grant["request_id"],
                preview_digest=text_hash(preview), actor_user_id="alice",
                now=datetime.fromisoformat(active["expires_at"]).replace(tzinfo=timezone.utc))
        assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE id=?", (grant["request_id"],)).fetchone()[0] == "approved"
        assert store.list_open(config, conn, "alice") == ([], 0)


@pytest.mark.parametrize("action", ["decline", "cancel", "expire", "stop", "failure"])
def test_pending_grant_cannot_survive_cancellation(env, action):
    config, room = env
    task, grant = park(env, success=action != "failure")
    with db.get_db(config.db_path) as conn:
        if action == "decline":
            confirmations.decline(conn, task)
        elif action == "cancel":
            db.cancel_task(conn, task.id)
        elif action == "expire":
            conn.execute("UPDATE tasks SET updated_at=datetime('now','-3 hours') WHERE id=?", (task.id,))
            db.expire_stale_confirmations(conn, 120)
        elif action == "stop":
            sharing.revoke_location_shares(conn, actor_user_id="alice", room_token=room, now=NOW)
        state = conn.execute("SELECT state FROM location_share_grants WHERE id=?", (grant["id"],)).fetchone()[0]
        assert state in {"cancelled", "expired"}
        with pytest.raises(RequestError):
            sharing.approve_location_share(conn, config, grant_id=grant["id"], request_id=grant["request_id"],
                preview_digest=text_hash(task.confirmation_prompt or ""), actor_user_id="alice", now=NOW)


@pytest.mark.parametrize("change", ["audience", "binding", "disabled", "archive", "veto"])
def test_approval_revalidates_frozen_authority(env, change):
    config, room = env
    task, grant = park(env)
    with db.get_db(config.db_path) as conn:
        if change == "audience":
            db.add_web_room_member(conn, room, "carol")
        elif change == "binding":
            db.add_room_binding(conn, room, "talk", "talk-new")
        elif change == "disabled":
            config.users["alice"].disabled_modules = ["location"]
        elif change == "archive":
            conn.execute("UPDATE rooms SET archived=1 WHERE token=?", (room,))
        else:
            conn.execute("INSERT INTO room_policy(room_token,vetoed_at) VALUES (?,datetime('now')) "
                         "ON CONFLICT(room_token) DO UPDATE SET vetoed_at=excluded.vetoed_at", (room,))
        confirmations.approve(conn, task, config=config, preview_digest=text_hash(task.confirmation_prompt))
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (grant["id"],)).fetchone()[0] == "cancelled"
        assert db.get_task(conn, task.id).status == "cancelled"


def test_replacement_decline_preserves_old_grant_and_approval_replaces(env):
    config, _ = env
    first_task, first = park(env)
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, first_task, config=config)
        db.update_task_status(conn, first_task.id, "completed")
    second_task, _ = park(env)
    with db.get_db(config.db_path) as conn:
        confirmations.decline(conn, second_task)
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (first["id"],)).fetchone()[0] == "active"
    third_task, third = park(env)
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, third_task, config=config)
        assert conn.execute("SELECT close_reason FROM location_share_grants WHERE id=?", (first["id"],)).fetchone()[0] == "replaced"
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (third["id"],)).fetchone()[0] == "active"


def test_audience_includes_departed_transport_member_with_web_access(env, monkeypatch):
    config, room = env
    with db.get_db(config.db_path) as conn:
        db.add_room_binding(conn, room, "talk", "talk-group")
        for user in ("alice", "bob", "carol"):
            db.add_web_room_member(conn, room, user)
            db.upsert_room_participant(conn, room_token=room, surface="talk", surface_ref=user,
                                       kind="principal", user_id=user)
        conn.execute("UPDATE room_participants SET left_at=datetime('now') WHERE user_id='carol'")
        handle = db.ensure_web_chat_handle(conn, "carol", room, "Hiking")
        from istota.rooms.scopes import is_current_member
        assert not is_current_member(conn, room, "carol")
        db.add_message(conn, room, role="assistant", body="Room content", origin_surface="web")
        assert any(m.body == "Room content" for m in db.get_messages(conn, room))
    from istota.webui import app
    monkeypatch.setattr(app, "_config", config)
    transcript = asyncio.run(app.chat_room_messages(handle.id, user={"username": "carol"}))
    assert "Room content" in json.dumps(transcript)
    task, grant = park(env)
    assert "carol" in task.confirmation_prompt
    assert json.loads(grant["audience_json"]) == ["alice", "bob", "carol"]


@pytest.mark.parametrize("kind,user,ref,allowed", [
    ("agent", None, "own-bot", True), ("agent", None, "other-bot", False),
    ("guest", None, "visitor", False), ("principal", "unknown", "unknown", False),
])
def test_timed_audience_excludes_only_the_installations_bot(env, kind, user, ref, allowed):
    config, room = env
    config.talk.bot_username = "own-bot"
    ident = turn(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        db.upsert_room_participant(conn, room_token=room, surface="talk", surface_ref=ref,
                                   kind=kind, user_id=user, display_name="own-bot")
        if allowed:
            assert request(conn, config, ident)["status"] == "held"
        else:
            with pytest.raises(RequestError, match="audience_unavailable"):
                request(conn, config, ident)
            assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 0


@pytest.mark.parametrize("duration,seconds", [("5m", 300), ("1h", 3600), ("24h", 86400), ("300s", 300)])
def test_cli_timed_request_never_reads_gps(env, monkeypatch, capsys, duration, seconds):
    from istota.skills import location as cli
    config, room = env
    ident = turn(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(ident))
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setattr(sharing, "location_snapshot", lambda *a, **k: pytest.fail("timed sharing read GPS"))
    args = cli.build_parser().parse_args(["share", "--request-key", "cli", "--for", duration])
    cli.cmd_share(args)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "held" and "lat" not in result
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT duration_seconds FROM location_share_grants").fetchone()[0] == seconds
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["share", "--request-key", "cli", "--for", duration, "--last-known"])


@pytest.mark.parametrize("duration", ["4m", "25h", "forever", "-1h", "NaN", "0h"])
def test_cli_rejects_invalid_durations(duration):
    from istota.skills.location import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["share", "--request-key", "cli", "--for", duration])


def test_location_trace_hides_request_terms():
    from istota.agent.events import _private_relay_tool
    assert _private_relay_tool("Bash", {"command": "istota-skill location share --for 1h --request-key a"})


def test_replay_is_immutable_and_second_hold_rolls_back(env):
    config, room = env
    ident = turn(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        first = request(conn, config, ident)
        assert request(conn, config, ident, now=NOW + timedelta(minutes=1)) == first
        for change in ({"duration_seconds": 300}, {"precision": "area"}):
            with pytest.raises(RequestError, match="request_conflict"):
                request(conn, config, ident, **change)
        with pytest.raises(RequestError, match="confirmation_pending"):
            request(conn, config, ident, request_key="second")
        assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 1
        db.add_web_room_member(conn, room, "carol")
        replay = request(conn, config, ident)
        assert replay["status"] == "cancelled"
        assert replay["request_id"] == first["request_id"]
        assert "carol" not in first["preview"]


@pytest.mark.parametrize("change", ["actor", "author", "guest", "email", "subtask", "scheduled", "stopped", "disabled"])
def test_timed_request_refuses_unowned_or_synthetic_turn(env, change):
    config, room = env
    ident = turn(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        options = {}
        if change == "actor":
            options["actor_user_id"] = "bob"
        elif change == "author":
            conn.execute("UPDATE messages SET author_user_id='bob' WHERE task_id=?", (ident,))
        elif change == "guest":
            guest = db.upsert_room_participant(conn, room_token=room, surface="talk", surface_ref="guest", kind="guest")
            conn.execute("UPDATE tasks SET guest_participant_id=? WHERE id=?", (guest, ident))
        elif change in {"email", "scheduled"}:
            conn.execute("UPDATE tasks SET source_type=? WHERE id=?", (change, ident))
        elif change == "subtask":
            conn.execute("UPDATE tasks SET parent_task_id=? WHERE id=?", (ident, ident))
        elif change == "stopped":
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (ident,))
        else:
            config.users["alice"].disabled_modules = ["location"]
        with pytest.raises(RequestError):
            request(conn, config, ident, **options)
        assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 0


def test_private_origin_requires_target_and_preserves_frozen_terms(env):
    config, room = env
    with db.get_db(config.db_path) as conn:
        private = db.create_web_chat_room(conn, "alice", "Private").token
    ident = turn(config, private)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        with pytest.raises(RequestError, match="no_target_room"):
            request(conn, config, ident)
        held = request(conn, config, ident, room="Hiking")
        assert held["status"] == "held"
        grant = conn.execute("SELECT * FROM location_share_grants").fetchone()
        assert grant["room_token"] == room
        assert "lat" not in json.loads(conn.execute("SELECT destination FROM whatsapp_skill_requests").fetchone()[0])


def test_oversized_audience_preview_refuses_without_partial_records(env):
    config, room = env
    ident = turn(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        # Past five readers only three are named, so it takes long names.
        for i in range(100):
            user = f"room-member-{i}"
            config.users[user] = UserConfig(display_name=f"{i} " + "long-name " * 80)
            db.add_room_member(conn, room, user, acknowledged=True)
        with pytest.raises(RequestError, match="invalid_preview"):
            request(conn, config, ident)
        assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 0


@pytest.mark.parametrize("surface", ["web", "answer"])
def test_changed_approval_reports_refusal_and_commits_cancellation(env, monkeypatch, surface):
    config, room = env
    task, grant = park(env)
    with db.get_db(config.db_path) as conn:
        # Bypass invalidation to exercise approval's frozen-audience backstop.
        conn.execute("INSERT INTO room_members(room_token,user_id) VALUES (?, 'carol')", (room,))
    if surface == "web":
        from istota.webui import app
        monkeypatch.setattr(app, "_config", config)
        with pytest.raises(HTTPException) as refused:
            app._chat_confirm_task(task.id, "alice", preview_digest=text_hash(task.confirmation_prompt))
        assert refused.value.status_code == 409
    else:
        with db.get_db(config.db_path) as conn:
            ack = confirmations.apply_answer(conn, task, confirmations.parse_answer("yes"), config=config)
            assert "Confirmed" not in ack
    with db.get_db(config.db_path) as conn:
        assert db.get_task(conn, task.id).status == "cancelled"
        assert conn.execute("SELECT state FROM location_share_grants WHERE id=?", (grant["id"],)).fetchone()[0] == "cancelled"


@pytest.mark.parametrize("cancel_at", ["execution", "before_park"])
@pytest.mark.parametrize("reason", ["owner_stop", "audience_change"])
def test_cancel_during_execution_never_publishes_preview(env, cancel_at, reason):
    config, room = env
    ident = turn(config, room)

    def cancel(conn):
        if reason == "owner_stop":
            sharing.revoke_location_shares(
                conn, actor_user_id="alice", room_token=room, now=NOW,
            )
        else:
            db.add_web_room_member(conn, room, "carol")

    def execute(*args, **kwargs):
        with db.get_db(config.db_path) as conn:
            held = request(conn, config, ident)
            if cancel_at == "execution":
                cancel(conn)
        return True, json.dumps(held), None, None

    def veto_check(conn, task):
        if cancel_at == "before_park":
            cancel(conn)
        return False

    with patch("istota.scheduler.execute_task", side_effect=execute), patch(
        "istota.notifications.delivery.send_notification", return_value=True,
    ), patch("istota.rooms.veto.task_room_vetoed", side_effect=veto_check):
        from istota.scheduler import process_one_task
        process_one_task(config)

    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, ident)
        assert task.status == "cancelled"
        assert not any(MARKER in message.body for message in db.get_messages(conn, room))
        assert not any(MARKER in row[0] for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=?", (ident,),
        ))
        assert not task.result or MARKER not in task.result
        assert store.list_open(config, conn, "alice") == ([], 0)


def test_stop_settles_pending_approval_task_and_notification(env):
    config, room = env
    task, _ = park(env)
    with db.get_db(config.db_path) as conn:
        sharing.revoke_location_shares(
            conn, actor_user_id="alice", room_token=room, now=NOW,
        )
        stopped = db.get_task(conn, task.id)
        assert stopped.status == "cancelled"
        assert stopped.whatsapp_confirmation_request_id is None
        assert store.list_open(config, conn, "alice") == ([], 0)


@pytest.mark.parametrize("seconds,text", [
    (300, "5 minutes"), (900, "15 minutes"), (3600, "1 hour"), (7200, "2 hours"),
    (5400, "1 hour 30 minutes"), (3660, "1 hour 1 minute"), (86400, "1 day"),
    (301, "5 minutes 1 second"),
])
def test_a_duration_reads_as_a_span(seconds, text):
    assert sharing._duration_text(seconds) == text


@pytest.mark.parametrize("names,text", [
    (["Bob"], "Bob"),
    (["Bob", "Carol"], "Bob and Carol"),
    (["A", "B", "C"], "A, B and C"),
    (["A", "B", "C", "D", "E"], "A, B, C, D and E"),
    (["A", "B", "C", "D", "E", "F", "G"], "A, B, C and 4 others"),
])
def test_readers_are_named_then_counted(names, text):
    assert sharing._reader_list(names) == text


def test_the_preview_names_readers_by_display_name_without_the_owner(env):
    config, room = env
    config.users["alice"].display_name = "Alice"
    config.users["bob"].display_name = "Bob"
    config.users["carol"].display_name = "Carol"
    with db.get_db(config.db_path) as conn:
        db.add_web_room_member(conn, room, "carol")
    ident = turn(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        held = request(conn, config, ident, precision="area", duration_seconds=5400)
    assert held["preview"] == (
        "Share your approximate location (about 1 km) with Hiking for 1 hour 30 minutes?\n\n"
        "Bob and Carol can ask where you are until then. You can stop at any time; "
        "anything already posted stays in the room.")
