"""Owner snapshots through the room post approval and delivery seam."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from istota import confirmations, db
from istota.config import Config, UserConfig
from istota.location import db as location_db
from istota.relay import requests
from istota.relay.requests import RequestError

NOW = datetime(2026, 3, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path):
    config = Config(db_path=tmp_path / "state.db", workspace_path=tmp_path / "files",
                    users={"alice": UserConfig(), "bob": UserConfig()})
    db.init_db(config.db_path)
    path = config.module_db_path("alice", "location")
    location_db.init_db(path)
    with location_db.connect(path) as conn:
        location_db.insert_ping(conn, "2026-03-08T11:59:00Z", lat=12.345678,
                                lon=-45.678912, accuracy=10, wifi="private-network")
        conn.commit()
    with db.get_db(config.db_path) as conn:
        shared = db.create_web_chat_room(conn, "alice", "Hiking").token
        db.add_web_room_member(conn, shared, "bob")
        private = db.create_web_chat_room(conn, "alice", "Private").token
    return config, shared, private, path


def turn(config, room, **kwargs):
    with db.get_db(config.db_path) as conn:
        ident = db.create_task(conn, user_id="alice", source_type="web",
                               prompt="Share my location", conversation_token=room, **kwargs)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        db.add_message(conn, room, role="user", body="Share my location", origin_surface="web",
                               task_id=ident, author_user_id="alice")
    return ident


def share(config, ident, **kwargs):
    from istota.location.sharing import share_location_snapshot

    with db.get_db(config.db_path) as conn:
        return share_location_snapshot(conn, config, actor_user_id="alice", task_id=ident,
                                       request_key="snapshot-1", now=NOW, **kwargs)


def test_current_room_returns_only_owner_projection_without_a_held_post(setup):
    config, shared, _, _ = setup
    result = share(config, turn(config, shared))
    assert result["delivery"] == "reply"
    assert set(result) == {"delivery", "snapshot"}
    assert set(result["snapshot"]) == {
        "owner_user_id", "precision", "observed_at", "age_seconds", "accuracy_m",
        "observation_kind", "freshness", "lat", "lon", "map_url",
    }
    assert result["snapshot"]["owner_user_id"] == "alice"
    assert result["snapshot"]["lat"] == 12.345678
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 0


def test_member_can_share_in_mixed_room(setup):
    config, shared, _, _ = setup
    with db.get_db(config.db_path) as conn:
        db.upsert_room_participant(conn, room_token=shared, surface="talk", surface_ref="visitor", kind="guest")
    assert share(config, turn(config, shared), precision="area")["snapshot"]["lat"] == 12.35


def test_private_post_is_frozen_replayed_and_delivered_only_after_approval(setup):
    config, shared, private, path = setup
    ident = turn(config, private, about_room_token=shared)
    held = share(config, ident)
    assert held["status"] == "held"
    assert "Location observed at 2026-03-08T11:59:00+00:00" in held["preview"]
    with location_db.connect(path) as conn:
        location_db.insert_ping(conn, NOW.isoformat(), lat=22, lon=33, accuracy=10)
        conn.commit()
    assert share(config, ident) == held
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE room_token=?", (shared,)).fetchone()[0] == 0
        row = conn.execute("SELECT * FROM whatsapp_skill_requests").fetchone()
        frozen = row["service_body"]
        assert "12.345678" in frozen and "private-network" not in frozen
        requests.park_question(conn, config, task=db.get_task(conn, ident))
        confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))
    with db.get_db(config.db_path) as conn:
        rows = conn.execute("SELECT body FROM messages WHERE room_token=?", (shared,)).fetchall()
        assert [row["body"] for row in rows] == [frozen]


@pytest.mark.parametrize("changed", [{"precision": "area"}, {"allow_last_known": True}])
def test_retry_rejects_changed_projection(setup, changed):
    config, shared, private, _ = setup
    ident = turn(config, private, about_room_token=shared)
    share(config, ident)
    with pytest.raises(RequestError, match="request_conflict"):
        share(config, ident, **changed)


@pytest.mark.parametrize("change", ["author", "missing_message", "stopped", "scheduled", "email", "subtask"])
def test_invalid_task_is_refused_before_module_read(setup, monkeypatch, change):
    config, shared, _, _ = setup
    ident = turn(config, shared)
    with db.get_db(config.db_path) as conn:
        if change == "author":
            conn.execute("UPDATE messages SET author_user_id='bob' WHERE task_id=?", (ident,))
        elif change == "missing_message":
            conn.execute("DELETE FROM messages WHERE task_id=?", (ident,))
        elif change == "stopped":
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (ident,))
        elif change in {"scheduled", "email"}:
            conn.execute("UPDATE tasks SET source_type=? WHERE id=?", (change, ident))
        else:
            conn.execute("UPDATE tasks SET parent_task_id=? WHERE id=?", (ident, ident))
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("read before authorization"))
    with pytest.raises(RequestError):
        share(config, ident)


def test_cross_room_and_missing_target_refused_before_module_read(setup, monkeypatch):
    config, shared, private, _ = setup
    with db.get_db(config.db_path) as conn:
        other = db.create_web_chat_room(conn, "alice", "Other").token
        db.add_web_room_member(conn, other, "bob")
    shared_task, private_task = turn(config, shared), turn(config, private)
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("read before target validation"))
    with pytest.raises(RequestError, match="private_turn_required"):
        share(config, shared_task, room=other)
    with pytest.raises(RequestError, match="no_target_room"):
        share(config, private_task)


def test_last_known_area_post_has_only_rounded_coordinates_and_original_time(setup):
    config, shared, private, path = setup
    with location_db.connect(path) as conn:
        conn.execute("UPDATE location_pings SET timestamp=?", ((NOW - timedelta(hours=2)).isoformat(),))
        conn.commit()
    ident = turn(config, private)
    assert share(config, ident, room="Hiking") == {"error": "no_fresh_location"}
    held = share(config, ident, room="Hiking", allow_last_known=True, precision="area")
    assert "Last known" in held["preview"] and "Approximate area" in held["preview"]
    assert "12.345678" not in held["preview"] and "accuracy" not in held["preview"].lower()
    assert "Location observed at 2026-03-08T10:00:00+00:00" in held["preview"]


def test_cli_uses_trusted_task_and_config_path_not_location_env(setup, monkeypatch, capsys):
    from istota.skills import location as cli

    config, shared, _, _ = setup
    ident = turn(config, shared)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(ident))
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setenv("LOCATION_DB_PATH", "/not/the/owners/database")
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    # Keep the CLI's clock current while using a deterministic observed time.
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(cli, "datetime", Clock)
    args = cli.build_parser().parse_args(["share", "--request-key", "snapshot-1"])
    cli.cmd_share(args)
    assert json.loads(capsys.readouterr().out)["snapshot"]["owner_user_id"] == "alice"


def test_talk_alias_resolves_to_current_shared_room(setup):
    from tests.support.rooms import promoted_room

    config, _, _, _ = setup
    with db.get_db(config.db_path) as conn:
        shape = promoted_room(conn, "alice")
        db.add_room_member(conn, shape.canonical, "bob")
    ident = turn(config, shape.canonical)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET source_type='talk', conversation_token=? WHERE id=?",
                     (shape.talk_ref, ident))
        conn.execute("UPDATE messages SET origin_surface='talk' WHERE task_id=?", (ident,))
    result = share(config, ident, room=shape.talk_ref)
    assert result["delivery"] == "reply"


def test_disabled_owner_and_guest_author_are_refused_before_module_read(setup, monkeypatch):
    config, shared, _, _ = setup
    ident = turn(config, shared)
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("read before authorization"))
    config.users["alice"].disabled_modules = ["location"]
    with pytest.raises(RequestError, match="location_unavailable"):
        share(config, ident)
    config.users["alice"].disabled_modules = []
    with db.get_db(config.db_path) as conn:
        guest = db.upsert_room_participant(conn, room_token=shared, surface="talk",
                                           surface_ref="visitor", kind="guest")
        conn.execute("UPDATE messages SET author_participant_id=? WHERE task_id=?", (guest, ident))
    with pytest.raises(RequestError, match="task_unavailable"):
        share(config, ident)


def test_retry_changed_room_is_refused_without_resampling(setup, monkeypatch):
    config, shared, private, _ = setup
    ident = turn(config, private)
    share(config, ident, room=shared)
    with db.get_db(config.db_path) as conn:
        other = db.create_web_chat_room(conn, "alice", "Other").token
        db.add_web_room_member(conn, other, "bob")
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("retry resampled location"))
    with pytest.raises(RequestError, match="request_conflict"):
        share(config, ident, room=other)


def test_email_destination_is_refused_before_module_read(setup, monkeypatch):
    config, shared, private, _ = setup
    ident = turn(config, private)
    with db.get_db(config.db_path) as conn:
        db.add_room_binding(conn, shared, "email", "thread-1")
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("read before target validation"))
    with pytest.raises(RequestError, match="email_thread"):
        share(config, ident, room=shared)


def test_storage_error_has_no_path_or_partial_payload(setup, monkeypatch):
    import sqlite3

    config, shared, _, _ = setup
    ident = turn(config, shared)

    def unavailable(*args):
        raise sqlite3.OperationalError("database unavailable: /private/storage/location.db")

    monkeypatch.setattr(location_db, "connect", unavailable)
    with pytest.raises(RequestError, match="^location_temporarily_unavailable$"):
        share(config, ident)


def test_owner_management_cli_scopes_shared_list_and_all_stop(setup, monkeypatch, capsys):
    from istota.skills import location as cli
    from tests.test_location_grants import held, activate

    config, shared, private, _ = setup
    ident = turn(config, shared)
    with db.get_db(config.db_path) as conn:
        grant, _ = held(conn, shared)
        activate(conn, grant, datetime.now(timezone.utc))
        other, _ = held(conn, private)
        activate(conn, other, datetime.now(timezone.utc))
    config.users["alice"].disabled_modules = ["location"]
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(ident))
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    cli.cmd_sharing(cli.build_parser().parse_args(["sharing"]))
    result = json.loads(capsys.readouterr().out)
    assert [s["room_token"] for s in result["shares"]] == [shared]
    cli.cmd_unshare(cli.build_parser().parse_args(["unshare", "--all"]))
    assert json.loads(capsys.readouterr().out) == {"stopped": 2}


def test_owner_management_refuses_forged_task(setup, monkeypatch):
    from istota.skills import location as cli

    config, shared, _, _ = setup
    ident = turn(config, shared)
    monkeypatch.setenv("ISTOTA_USER_ID", "bob")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(ident))
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    with pytest.raises(RequestError, match="task_unavailable"):
        cli.cmd_unshare(cli.build_parser().parse_args(["unshare", "--all"]))
