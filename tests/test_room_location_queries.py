"""Room query authority, projected reads and final revocation checks."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.location import db as location_db, sharing
from istota.relay.requests import RequestError

NOW = datetime(2026, 3, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(db_path=tmp_path / "state.db", workspace_path=tmp_path / "files",
                    users={u: UserConfig(display_name=u.title()) for u in ("alice", "bob", "carol")})
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Hiking").token
        db.add_web_room_member(conn, room, "bob")
        db.add_web_room_member(conn, room, "carol")
    for user in ("alice", "bob"):
        path = config.module_db_path(user, "location")
        location_db.init_db(path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(conn, (NOW - timedelta(minutes=1)).isoformat(),
                                    lat=12.345678, lon=-45.678912, accuracy=10, wifi="private-network")
            conn.commit()
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(sharing, "datetime", Clock, raising=False)
    return config, room


def turn(config, room, actor="carol"):
    with db.get_db(config.db_path) as conn:
        task = db.create_task(conn, user_id=actor, source_type="web", prompt="Where is everyone?",
                              conversation_token=room)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
        db.add_message(conn, room, role="user", body="Where is everyone?", origin_surface="web",
                       task_id=task, author_user_id=actor)
    return task


def grant(config, room, owner="alice", precision="exact"):
    with db.get_db(config.db_path) as conn:
        _, audience, fingerprint, epoch = sharing._timed_room_terms(conn, config, room, owner)
        row = sharing._hold_location_grant(
            conn, owner_user_id=owner, origin_task_id=None, request_key="sharing",
            room_token=room, precision=precision, duration_seconds=3600, audience=audience,
            binding_fingerprint=fingerprint, room_epoch=epoch, now=NOW)
        return sharing._activate_location_grant(conn, grant_id=row["id"], now=NOW)


def read(config, task, subject="alice", actor="carol"):
    return sharing.read_room_location(config, actor_user_id=actor, task_id=task,
                                      subject_user_id=subject, now=NOW)


def read_all(config, task):
    return sharing.read_room_locations(config, actor_user_id="carol", task_id=task, now=NOW)


def test_single_and_all_read_only_active_grants_without_reader_gps(env):
    config, room = env
    active = grant(config, room)
    config.users["carol"].disabled_modules = ["location"]
    task = turn(config, room)
    result = read(config, task)
    assert set(result) == {"owner_user_id", "precision", "observed_at", "age_seconds", "accuracy_m",
                           "observation_kind", "freshness", "lat", "lon", "map_url", "expires_at"}
    assert result["lat"] == 12.345678 and result["expires_at"] == active["expires_at"]
    assert read_all(config, task) == {"locations": [result]}


@pytest.mark.parametrize("state", ["missing", "revoked", "expired", "disabled"])
def test_unavailable_is_generic_and_does_not_open_owner_database(env, monkeypatch, state):
    config, room = env
    if state != "missing":
        active = grant(config, room)
        with db.get_db(config.db_path) as conn:
            if state in {"revoked", "expired"}:
                conn.execute("UPDATE location_share_grants SET state=? WHERE id=?", (state, active["id"]))
        if state == "disabled":
            config.users["alice"].disabled_modules = ["location"]
    task = turn(config, room)
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("unauthorized module read"))
    assert read(config, task) == {"error": "location_unavailable"}
    assert read_all(config, task) == {"locations": []}


@pytest.mark.parametrize("change", ["actor", "author", "stopped", "scheduled", "email", "subtask", "guest", "private"])
def test_task_refusals_precede_owner_database_read(env, monkeypatch, change):
    config, room = env
    grant(config, room)
    if change == "private":
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "carol", "Private").token
    task = turn(config, room)
    with db.get_db(config.db_path) as conn:
        if change == "author":
            conn.execute("UPDATE messages SET author_user_id='alice' WHERE task_id=?", (task,))
        elif change == "stopped":
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (task,))
        elif change in {"scheduled", "email"}:
            conn.execute("UPDATE tasks SET source_type=? WHERE id=?", (change, task))
        elif change == "subtask":
            conn.execute("UPDATE tasks SET parent_task_id=? WHERE id=?", (task, task))
        elif change == "guest":
            guest = db.upsert_room_participant(conn, room_token=room, surface="talk", surface_ref="guest", kind="guest")
            conn.execute("UPDATE messages SET author_participant_id=? WHERE task_id=?", (guest, task))
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("unauthorized module read"))
    with pytest.raises(RequestError):
        read(config, task, actor="alice" if change == "actor" else "carol")


def test_area_and_unusable_fix_never_return_private_or_stale_fields(env):
    config, room = env
    grant(config, room, precision="area")
    grant(config, room, owner="bob")
    with location_db.connect(config.module_db_path("bob", "location")) as conn:
        conn.execute("UPDATE location_pings SET timestamp=?", ((NOW - timedelta(hours=1)).isoformat(),))
        conn.commit()
    result = read_all(config, turn(config, room))["locations"]
    assert result[0]["lat"] == 12.35 and result[0]["accuracy_m"] is None
    assert "12.345678" not in json.dumps(result)
    assert result[1] == {"owner_user_id": "bob", "error": "no_fresh_location"}


@pytest.mark.parametrize("aggregate", [False, True])
@pytest.mark.parametrize("change", ["stop", "expiry", "disable", "task", "room"])
def test_final_check_removes_earlier_observation_after_later_read(env, monkeypatch, aggregate, change):
    config, room = env
    grant(config, room)
    if aggregate:
        grant(config, room, owner="bob")
    task = turn(config, room)
    snapshot = sharing.location_snapshot
    calls = []
    def during_read(path, **kwargs):
        result = snapshot(path, **kwargs)
        calls.append(path)
        if len(calls) == (2 if aggregate else 1):
            # The second connection must commit without a framework writer
            # lock held across module I/O.
            with db.get_db(config.db_path, busy_timeout_ms=100) as conn:
                if change == "stop":
                    sharing.revoke_location_shares(conn, actor_user_id="alice", room_token=room, now=NOW)
                elif change == "disable":
                    config.users["alice"].disabled_modules = ["location"]
                elif change == "expiry":
                    conn.execute("UPDATE location_share_grants SET expires_at=? WHERE owner_user_id='alice'",
                                 (sharing._grant_timestamp(NOW + timedelta(seconds=1)),))
                    class Later:
                        @staticmethod
                        def now(tz):
                            return NOW + timedelta(seconds=1)
                    monkeypatch.setattr(sharing, "datetime", Later)
                elif change == "task":
                    conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (task,))
                else:
                    config.users["dave"] = UserConfig()
                    db.add_web_room_member(conn, room, "dave")
        return result
    monkeypatch.setattr(sharing, "location_snapshot", during_read)
    query = read_all if aggregate else read
    if change in {"room", "task"}:
        with pytest.raises(RequestError):
            query(config, task)
    elif aggregate:
        assert [r["owner_user_id"] for r in query(config, task)["locations"]] == ["bob"]
    else:
        assert query(config, task) == {"error": "location_unavailable"}


def test_room_cli_resolves_member_names_and_stops_with_disabled_module(env, monkeypatch, capsys):
    from istota.skills import room as cli
    config, room = env
    grant(config, room)
    task = turn(config, room, actor="bob")
    monkeypatch.setenv("ISTOTA_USER_ID", "bob")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task))
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setenv("LOCATION_DB_PATH", "/not/an/authority")
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setattr(cli, "datetime", sharing.datetime, raising=False)
    assert cli.main(["location", "--user", "Alice"]) is None
    assert json.loads(capsys.readouterr().out)["owner_user_id"] == "alice"
    assert cli.main(["locations"]) is None
    assert len(json.loads(capsys.readouterr().out)["locations"]) == 1
    grant(config, room, owner="bob")
    config.users["bob"].disabled_modules = ["location"]
    assert cli.main(["location-stop", "--all"]) is None
    assert json.loads(capsys.readouterr().out) == {"stopped": 1}
    assert cli.main(["location-stop"]) is None
    assert json.loads(capsys.readouterr().out) == {"stopped": 0}


def test_ambiguous_member_names_have_no_sharing_state_and_unknown_is_generic(env, monkeypatch):
    config, room = env
    config.users["alice"].display_name = config.users["bob"].display_name = "Member"
    task = turn(config, room)
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("name lookup read GPS"))
    assert read(config, task, subject="Member") == {
        "error": "ambiguous_user", "candidates": [
            {"user_id": "alice", "display_name": "Member"}, {"user_id": "bob", "display_name": "Member"}]}
    assert read(config, task, subject="outside") == {"error": "location_unavailable"}


def test_storage_error_suppresses_entire_aggregate(env, monkeypatch):
    config, room = env
    grant(config, room)
    grant(config, room, owner="bob")
    task = turn(config, room)
    snapshot = sharing.location_snapshot
    def unavailable(path, **kwargs):
        if path == config.module_db_path("bob", "location"):
            raise sqlite3.OperationalError("private path and point")
        return snapshot(path, **kwargs)
    monkeypatch.setattr(sharing, "location_snapshot", unavailable)
    with pytest.raises(RequestError, match="^location_temporarily_unavailable$"):
        read_all(config, task)


def test_exact_expiry_and_other_room_grant_never_open_module(env, monkeypatch):
    config, room = env
    active = grant(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE location_share_grants SET expires_at=? WHERE id=?",
                     (sharing._grant_timestamp(NOW), active["id"]))
        other = db.create_web_chat_room(conn, "alice", "Other").token
        db.add_web_room_member(conn, other, "carol")
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("unauthorized module read"))
    assert read(config, turn(config, room)) == {"error": "location_unavailable"}
    assert read(config, turn(config, other)) == {"error": "location_unavailable"}


def test_everyone_limit_refuses_before_module_io(env, monkeypatch):
    config, room = env
    owners = [f"member{i}" for i in range(51)]
    with db.get_db(config.db_path) as conn:
        for user in owners:
            config.users[user] = UserConfig()
            db.add_web_room_member(conn, room, user)
    for user in owners:
        grant(config, room, owner=user)
    task = turn(config, room)
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("read before limit check"))
    with pytest.raises(RequestError, match="^location_share_limit$"):
        read_all(config, task)


def test_frozen_terms_backstop_refuses_grant_without_module_io(env, monkeypatch):
    config, room = env
    active = grant(config, room)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE location_share_grants SET binding_fingerprint='old' WHERE id=?", (active["id"],))
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("read with mismatched authority"))
    assert read(config, turn(config, room)) == {"error": "location_unavailable"}


def test_unconfigured_owner_has_same_unavailable_answer_before_module_read(env, monkeypatch):
    config, room = env
    grant(config, room)
    task = turn(config, room)
    del config.users["alice"]
    monkeypatch.setattr(location_db, "connect", lambda *a: pytest.fail("read unconfigured owner"))
    assert read(config, task) == {"error": "location_unavailable"}
    assert read_all(config, task) == {"error": "location_unavailable"}
