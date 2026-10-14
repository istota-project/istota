"""Room sharing through surface ingestion, scheduler approval and skill dispatch."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from istota import db
from istota.location import db as location_db
from istota.skills._loader import effective_disabled_skills, eligible_skill_names, load_skill_index
from istota.webui import app

from .test_private_replies import _config, sent as _sent_fixture
from .test_talk_trigger_path import _client, _msg
from .test_whatsapp_groups import ALICE_JID, BOB_JID, GROUP, _apply, _message, _roster
from .support.rooms import plain_talk_room
from .support.talk_double import FakeTalkClient, talk_bot_client


sent = _sent_fixture

def test_location_sharing_is_discoverable_without_reader_gps(tmp_path):
    config = _config(tmp_path)
    config.users["bob"].disabled_modules = ["location"]
    index = load_skill_index(tmp_path / "skills")
    disabled = effective_disabled_skills(config, "bob", index)
    menu = eligible_skill_names(index, exclude=set(), disabled_skills=disabled)
    assert "room" in menu
    assert not config.is_module_enabled("bob", "location")
    assert index["location"].shared_room == "private"
    assert index["room"].shared_room == "safe"
    assert "location" in index["room"].description
    assert "sharing" in index["location"].description


@pytest.mark.parametrize("surface", ["web", "talk", "whatsapp"])
@pytest.mark.parametrize("precision", ["exact", "area"])
def test_alice_approves_bob_reads_alice_stops(surface, precision, tmp_path, monkeypatch, capsys, sent):
    from istota.scheduler import process_one_task
    from istota.skills import location, room as room_cli
    from istota.transport.talk import inbound

    config = _config(tmp_path)
    config.workspace_path = tmp_path / "files"
    config.users["bob"].disabled_modules = ["location"]
    config.talk.bot_username = "istota"
    monkeypatch.setattr(app, "_config", config)
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setattr("istota.notifications.delivery.send_notification", lambda *a, **k: True)
    path = config.module_db_path("alice", "location")
    location_db.init_db(path)
    with location_db.connect(path) as conn:
        location_db.insert_ping(conn, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                                lat=12.345678, lon=-45.678912, accuracy=10, wifi="private-network")
        conn.commit()
    with db.get_db(config.db_path) as conn:
        private = db.create_web_chat_room(conn, "alice", "Private").token
        if surface == "web":
            shared = db.create_web_chat_room(conn, "alice", "Hiking").token
            db.add_web_room_member(conn, shared, "bob")
        elif surface == "talk":
            shape = plain_talk_room(conn, "alice", name="Hiking")
            shared = shape.canonical
            db.add_room_member(conn, shared, "bob")
            db.set_talk_poll_state(conn, shape.talk_ref, 50)
        else:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15557654321")
            db.latch_whatsapp_jid(conn, "bob", jid=BOB_JID)
    if surface == "whatsapp":
        _apply(config, _roster([ALICE_JID, BOB_JID], added_by=ALICE_JID, subject="Hiking"))
        with db.get_db(config.db_path) as conn:
            shared = db.resolve_room_token(conn, "whatsapp", GROUP)
    double = FakeTalkClient(config.db_path)
    monkeypatch.setattr("istota.transport.talk.get_talk_client", talk_bot_client(double))
    inbound._participant_cache.clear()

    def receive(actor, text, number):
        if surface == "web":
            status, ident = app._chat_create_web_task(actor, shared, "Istota, " + text)
            assert status == "ok"
        elif surface == "talk":
            message = _msg(msg_id=100 + number, message="{mention-user0} " + text,
                           params={"mention-user0": {"type": "user", "id": "istota", "name": "Istota"}})
            message["actorId"] = actor
            with patch("istota.transport.talk.inbound.get_talk_client", return_value=_client([message])):
                created = asyncio.run(inbound.poll_one_conversation(
                    config, shape.talk_ref, conv_type=2, display_name="Hiking"))
            (ident,) = created
        else:
            (result,) = _apply(config, _message("Istota, " + text, message_id=f"M{number}",
                              sender=ALICE_JID if actor == "alice" else BOB_JID))
            assert result.disposition == "task"
            ident = result.task_id
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, ident)
            assert task.user_id == actor and task.conversation_token == shared
            assert task.source_type == surface and task.guest_participant_id is None
            assert conn.execute("SELECT author_user_id FROM messages WHERE task_id=? AND role='user'",
                                (ident,)).fetchone()[0] == actor
        return ident

    outputs = []
    def run(ident, actor, argv, *, share=False):
        def execute(*args, **kwargs):
            monkeypatch.setenv("ISTOTA_USER_ID", actor)
            monkeypatch.setenv("ISTOTA_TASK_ID", str(ident))
            monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
            if share:
                location.cmd_share(location.build_parser().parse_args(argv))
                result = json.loads(capsys.readouterr().out)
            else:
                result = room_cli._dispatch(room_cli.build_parser().parse_args(argv))
            outputs.append(result)
            return True, json.dumps(result), None, None
        with patch("istota.scheduler.execute_task", side_effect=execute):
            assert process_one_task(config) == (ident, True)
        return outputs[-1]

    once = receive("alice", "share my location here", 0)
    posted = run(once, "alice", ["share", "--precision", precision,
                                "--request-key", "once"], share=True)
    assert posted["delivery"] == "reply" and posted["snapshot"]["precision"] == precision
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 0
    asked = receive("alice", "share my location here for an hour", 1)
    held = run(asked, "alice", ["share", "--for", "1h", "--precision", precision,
                               "--request-key", "surface-share"], share=True)
    assert held["status"] == "held" and "lat" not in held
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, asked)
        assert task.status == "pending_confirmation"
        preview = task.confirmation_prompt
        assert "Hiking" in preview and "alice" in preview and "bob" in preview
        assert conn.execute("SELECT count(*) FROM messages WHERE room_token=? AND body LIKE '%location-stop%'",
                            (shared,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM messages WHERE room_token=? AND body LIKE '%location-stop%'",
                            (private,)).fetchone()[0] == 1
    app._chat_confirm_task(asked, "alice", room=private)
    resumed = run(asked, "alice", ["share", "--for", "1h", "--precision", precision,
                                  "--request-key", "surface-share"], share=True)
    assert resumed["status"] == "approved"
    read = receive("bob", "where is Alice?", 2)
    snapshot = run(read, "bob", ["location", "--user", "Alice"])
    assert set(snapshot) == {"owner_user_id", "precision", "observed_at", "age_seconds", "accuracy_m",
                             "observation_kind", "freshness", "lat", "lon", "map_url", "expires_at"}
    assert snapshot["owner_user_id"] == "alice" and snapshot["precision"] == precision
    assert snapshot["lat"] == (12.345678 if precision == "exact" else 12.35)
    assert "wifi" not in snapshot and "private-network" not in json.dumps(snapshot)
    if precision == "area":
        assert snapshot["accuracy_m"] is None and "12.345678" not in json.dumps(snapshot)
    assert not config.module_db_path("bob", "location").exists()
    assert str(snapshot["lat"]) in json.dumps(app._chat_room_messages("bob", shared, 50))
    everyone = receive("bob", "where is everyone?", 3)
    assert run(everyone, "bob", ["locations"])["locations"][0]["owner_user_id"] == "alice"
    config.users["alice"].disabled_modules = ["location"]
    stop = receive("alice", "stop sharing my location", 4)
    assert run(stop, "alice", ["location-stop"]) == {"stopped": 1}
    later = receive("bob", "where is Alice now?", 5)
    assert run(later, "bob", ["location", "--user", "alice"]) == {"error": "location_unavailable"}
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT state FROM location_share_grants").fetchone()[0] == "revoked"
        assert any(json.dumps(snapshot) in m.body for m in db.get_messages(conn, shared))
    if surface == "whatsapp":
        messages = [request.text for request in sent if request.to == GROUP]
    elif surface == "talk":
        assert shape.canonical != shape.talk_ref
        messages = [str(call.args) for call in double.calls
                    if call.method == "send_message" and call.token == shape.talk_ref]
        assert not double.refusals
    else:
        with db.get_db(config.db_path) as conn:
            messages = [m.body for m in db.get_messages(conn, shared)]
    assert any(str(snapshot["lat"]) in text for text in messages)
    assert all("location-stop" not in text for text in messages)
    inbound._participant_cache.clear()
