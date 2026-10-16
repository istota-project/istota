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


# --- the map a held post carries --------------------------------------------


MAP_TEXT = "Here\nhttps://www.openstreetmap.org/?mlat=12.345678&mlon=-45.678912"


def _request(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT * FROM whatsapp_skill_requests").fetchone()


def _release(config, ident):
    with db.get_db(config.db_path) as conn:
        requests.park_question(conn, config, task=db.get_task(conn, ident))
        confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))


def _posted(config, room):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT * FROM messages WHERE room_token=? AND role='assistant'",
                            (room,)).fetchall()


def _cards(config):
    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM location_share_cards ORDER BY id")]


def _hold(config, ident, *, text, attachment=None, key="p1"):
    from istota.rooms import private_replies

    with db.get_db(config.db_path) as conn:
        return private_replies.hold_room_post(
            conn, config, actor_user_id="alice", task_id=ident, request_key=key,
            text=text, attachment=attachment)


def _observation(**changes):
    observation = {"owner_user_id": "alice", "owner_label": "alice", "precision": "exact",
                   "lat": 12.345678, "lon": -45.678912, "accuracy_m": 10.0,
                   "observed_at": "2026-03-08T11:59:00+00:00", "freshness": "fresh",
                   "map_url": "https://www.openstreetmap.org/?mlat=12.345678&mlon=-45.678912"}
    observation.update(changes)
    return observation


def test_released_post_carries_its_frozen_map_card_in_the_rows_insert(setup, monkeypatch):
    from istota.webui import app

    config, shared, private, path = setup
    monkeypatch.setattr(app, "_config", config)
    ident = turn(config, private, about_room_token=shared)
    held = share(config, ident)
    destination = json.loads(_request(config)["destination"])
    frozen = destination["location_share"]
    assert set(frozen) == {"owner_user_id", "owner_label", "precision", "lat", "lon",
                           "accuracy_m", "observed_at", "freshness", "map_url"}
    assert frozen["lat"] == 12.345678 and frozen["owner_user_id"] == "alice"
    assert frozen["map_url"] in held["preview"]
    assert "A map of this location is posted with it (map " in held["preview"]
    assert destination["request_context"]["kind"] == "location_snapshot"
    assert _cards(config) == []
    # A newer fix after the hold does not move the approved map.
    with location_db.connect(path) as conn:
        location_db.insert_ping(conn, NOW.isoformat(), lat=22, lon=33, accuracy=10)
        conn.commit()
    _release(config, ident)
    (row,) = _posted(config, shared)
    (card,) = _cards(config)
    assert json.loads(row["location_shares"]) == [{"card_id": card["id"], **frozen}]
    assert card["request_id"] == held["request_id"] and card["task_id"] == ident
    assert card["room_token"] == shared and card["grant_id"] is None
    assert card["message_id"] == row["id"] and card["attached_at"] is not None
    assert json.loads(card["observation"]) == frozen
    # The streamed row is the inserted row, so Bob's live view carries the card.
    with db.get_db(config.db_path) as conn:
        streamed = [r for r in db.list_room_events_since(conn, "bob", since_id=0)
                    if r["role"] == "assistant"]
    (live,) = [app._cross_room_message_dict(r, "bob") for r in streamed]
    assert live["location_shares"] == [{"card_id": card["id"], **frozen}]


def test_no_map_card_holds_and_posts_the_text_alone(setup):
    config, shared, private, _ = setup
    ident = turn(config, private, about_room_token=shared)
    held = share(config, ident, map_card=False)
    assert "location_share" not in json.loads(_request(config)["destination"])
    assert "A map of this location" not in held["preview"]
    _release(config, ident)
    (row,) = _posted(config, shared)
    assert row["location_shares"] is None and _cards(config) == []


def test_text_only_post_preview_and_destination_are_unchanged(setup):
    config, shared, private, _ = setup
    ident = turn(config, private, about_room_token=shared)
    held = _hold(config, ident, text="Back by six")
    assert held["preview"] == (
        "Post this in Hiking as Istota?\n"
        "Only the message below is posted, exactly as written. "
        "Sending must start within 10 minutes of approval.\n\n"
        "Message:\nBack by six")
    row = _request(config)
    assert row["preview_digest"] == requests.text_hash(held["preview"])
    assert set(json.loads(row["destination"])) == {"kind", "room_token", "talk_ref", "label"}


def test_the_preview_digest_binds_the_observation(setup):
    config, shared, private, _ = setup
    digests = set()
    for changes in ({}, {"lat": 12.3457}, {"accuracy_m": 50.0}, {"freshness": "last_known"}):
        ident = turn(config, private, about_room_token=shared)
        _hold(config, ident, text=MAP_TEXT, attachment=_observation(**changes))
        with db.get_db(config.db_path) as conn:
            digests.add(conn.execute(
                "SELECT preview_digest FROM whatsapp_skill_requests WHERE origin_task_id=?",
                (ident,)).fetchone()[0])
    assert len(digests) == 4


def test_retry_with_another_observation_is_a_conflict(setup):
    config, shared, private, _ = setup
    ident = turn(config, private, about_room_token=shared)
    first = _hold(config, ident, text=MAP_TEXT, attachment=_observation())
    assert _hold(config, ident, text=MAP_TEXT, attachment=_observation()) == first
    for attachment in (_observation(lat=1.0), None):
        with pytest.raises(RequestError, match="request_conflict"):
            _hold(config, ident, text=MAP_TEXT, attachment=attachment)


@pytest.mark.parametrize("attachment", [
    _observation(owner_user_id="bob"),
    _observation(map_url="https://www.openstreetmap.org/?mlat=1&mlon=2"),
    {**_observation(), "ssid": "private-network"},
    {key: value for key, value in _observation().items() if key != "observed_at"},
    "not a dict",
])
def test_an_attachment_that_is_not_the_members_own_shown_map_is_refused(setup, attachment):
    config, shared, private, _ = setup
    ident = turn(config, private, about_room_token=shared)
    with pytest.raises(RequestError, match="invalid_attachment"):
        _hold(config, ident, text=MAP_TEXT, attachment=attachment)
    assert _request(config) is None


def test_an_observation_changed_after_approval_posts_nothing(setup):
    config, shared, private, _ = setup
    ident = turn(config, private, about_room_token=shared)
    share(config, ident)
    with db.get_db(config.db_path) as conn:
        requests.park_question(conn, config, task=db.get_task(conn, ident))
        confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
        row = conn.execute("SELECT id, destination FROM whatsapp_skill_requests").fetchone()
        destination = json.loads(row["destination"])
        destination["location_share"]["lat"] = 40.0
        conn.execute("UPDATE whatsapp_skill_requests SET destination=? WHERE id=?",
                     (json.dumps(destination), row["id"]))
    asyncio.run(requests.drain_requests(config))
    assert _posted(config, shared) == [] and _cards(config) == []
    assert _request(config)["state"] == "failed"
