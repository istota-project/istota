"""Location share cards: recorded with a share, attached to the answer that carries its link."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from istota import db
from istota.location import db as location_db, sharing
from istota.webui import app

from .test_private_replies import _config


CARD_KEYS = {"card_id", "owner_user_id", "owner_label", "precision", "lat", "lon",
             "accuracy_m", "observed_at", "freshness", "map_url"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.workspace_path = tmp_path / "files"
    monkeypatch.setattr(app, "_config", config)
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setattr("istota.notifications.delivery.send_notification", lambda *a, **k: True)
    for user in ("alice", "bob"):
        path = config.module_db_path(user, "location")
        location_db.init_db(path)
        with location_db.connect(path) as conn:
            location_db.insert_ping(
                conn, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                lat=12.345678, lon=-45.678912, accuracy=10, wifi="private-network")
            conn.commit()
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Hiking").token
        db.add_web_room_member(conn, room, "bob")
    return config, room


def _turn(config, room, actor, text="where?"):
    status, ident = app._chat_create_web_task(actor, room, "Istota, " + text)
    assert status == "ok"
    return ident


def _run(config, ident, body):
    """Run one task whose model calls ``body(task_id)`` and answers with what it returns."""
    from istota.scheduler import process_one_task

    def execute(*args, **kwargs):
        return True, body(ident), None, None

    with patch("istota.scheduler.execute_task", side_effect=execute):
        assert process_one_task(config) == (ident, True)


def _share(config, ident, actor="alice", **options):
    with db.get_db(config.db_path) as conn:
        return sharing.share_location_snapshot(
            conn, config, actor_user_id=actor, task_id=ident, request_key="once",
            now=datetime.now(timezone.utc), **options)


def _grant(config, room, owner="alice", precision="exact"):
    now = datetime.now(timezone.utc)
    with db.get_db(config.db_path) as conn:
        _, audience, fingerprint, epoch = sharing._timed_room_terms(conn, config, room, owner)
        row = sharing._hold_location_grant(
            conn, owner_user_id=owner, origin_task_id=None, request_key="grant",
            room_token=room, precision=precision, duration_seconds=3600, audience=audience,
            binding_fingerprint=fingerprint, room_epoch=epoch, now=now)
        return sharing._activate_location_grant(conn, grant_id=row["id"], now=now)


def _read(config, ident, actor="bob", subject="alice"):
    return sharing.read_room_location(config, actor_user_id=actor, task_id=ident,
                                      subject_user_id=subject, now=datetime.now(timezone.utc))


def _cards(config):
    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM location_share_cards ORDER BY id")]


def _assistant_row(config, ident):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT * FROM messages WHERE task_id=? AND role='assistant'",
                            (ident,)).fetchone()


# --- schema -----------------------------------------------------------------


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_column_and_table_to_an_old_database_repeatedly(tmp_path):
    path = tmp_path / "old.db"
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER location_share_card_task_deleted")
        conn.execute("DROP TRIGGER location_share_card_room_deleted")
        conn.execute("DROP TABLE location_share_cards")
        conn.execute("ALTER TABLE messages DROP COLUMN location_shares")
        assert "location_shares" not in _columns(conn, "messages")
    db.init_db(path)
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert "location_shares" in _columns(conn, "messages")
        assert {"task_id", "request_id", "owner_user_id", "actor_user_id", "room_token",
                "grant_id", "observation", "map_url", "message_id", "image_state",
                "image_surface", "created_at", "attached_at"} <= _columns(conn, "location_share_cards")
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                            "AND name='location_share_card_task_deleted'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                            "AND name='location_share_card_room_deleted'").fetchone()
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(location_share_cards)")}
        assert {"idx_location_share_cards_task", "idx_location_share_cards_request"} <= indexes


def test_deleting_the_task_clears_the_cards_task_with_foreign_keys_off(env):
    config, room = env
    ident = _turn(config, room, "alice")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    _share(config, ident)
    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (ident,))
    (card,) = _cards(config)
    assert card["task_id"] is None


# --- recording --------------------------------------------------------------


def test_one_time_share_in_the_room_records_one_projected_card(env):
    config, room = env
    ident = _turn(config, room, "alice")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    result = _share(config, ident, precision="area")
    assert result["delivery"] == "reply"
    (card,) = _cards(config)
    assert (card["task_id"], card["owner_user_id"], card["actor_user_id"], card["room_token"]) == (
        ident, "alice", "alice", room)
    assert card["grant_id"] is None and card["message_id"] is None
    assert card["image_state"] == "none" and card["map_url"] == result["snapshot"]["map_url"]
    observation = json.loads(card["observation"])
    assert set(observation) == CARD_KEYS - {"card_id"}
    assert observation["owner_label"] == "Alice" and observation["precision"] == "area"
    assert (observation["lat"], observation["lon"]) == (12.35, -45.68)
    assert observation["accuracy_m"] is None
    assert "private-network" not in card["observation"] and "12.345678" not in card["observation"]


def test_map_off_and_private_room_shares_record_nothing(env):
    config, room = env
    ident = _turn(config, room, "alice")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    assert _share(config, ident, map_card=False)["delivery"] == "reply"
    with db.get_db(config.db_path) as conn:
        private = db.create_web_chat_room(conn, "alice", "Private").token
    held = _turn(config, private, "alice", "share my location with Hiking")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (held,))
        result = sharing.share_location_snapshot(
            conn, config, actor_user_id="alice", task_id=held, request_key="held",
            now=datetime.now(timezone.utc), room=room)
    assert result["status"] == "held"
    assert _cards(config) == []


def test_granted_single_read_records_a_card_with_its_grant_and_everyone_records_nothing(env):
    config, room = env
    active = _grant(config, room)
    ident = _turn(config, room, "bob")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    assert "locations" in sharing.read_room_locations(
        config, actor_user_id="bob", task_id=ident, now=datetime.now(timezone.utc))
    assert _cards(config) == []
    assert _read(config, ident, subject="carol") == {"error": "location_unavailable"}
    result = _read(config, ident)
    (card,) = _cards(config)
    assert card["grant_id"] == active["id"] and card["owner_user_id"] == "alice"
    assert card["actor_user_id"] == "bob" and card["task_id"] == ident
    assert card["map_url"] == result["map_url"]


def test_read_with_map_off_records_nothing(env):
    config, room = env
    _grant(config, room)
    ident = _turn(config, room, "bob")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    result = sharing.read_room_location(config, actor_user_id="bob", task_id=ident,
                                        subject_user_id="alice", now=datetime.now(timezone.utc),
                                        map_card=False)
    assert result["owner_user_id"] == "alice"
    assert _cards(config) == []


# --- attaching on final delivery ---------------------------------------------


def test_answer_with_the_link_carries_the_card_in_the_stored_and_streamed_row(env):
    config, room = env
    ident = _turn(config, room, "alice", "share my location here")
    shared = {}

    def model(task_id):
        shared.update(_share(config, task_id))
        return f"Here is where I am: {shared['snapshot']['map_url']}"

    with db.get_db(config.db_path) as conn:
        cursor = db.max_message_id(conn)
    _run(config, ident, model)

    row = _assistant_row(config, ident)
    (stored,) = json.loads(row["location_shares"])
    (card,) = _cards(config)
    assert stored["card_id"] == card["id"] and set(stored) == CARD_KEYS
    assert stored["map_url"] == shared["snapshot"]["map_url"]
    assert card["message_id"] == row["id"] and card["attached_at"]

    # The streamed row is the inserted row: Bob's live view and his reload agree.
    with db.get_db(config.db_path) as conn:
        streamed = [r for r in db.list_room_events_since(conn, "bob", since_id=cursor)
                    if r["role"] == "assistant"]
    (live,) = [app._cross_room_message_dict(r, "bob") for r in streamed]
    assert live["location_shares"] == [stored]
    history = app._chat_room_messages("bob", room, 50)
    messages = history["messages"] if isinstance(history, dict) else history
    (answer,) = [m for m in messages if m.get("role") == "assistant"]
    assert answer["location_shares"] == [stored]


def test_answer_without_the_link_gets_no_card(env):
    config, room = env
    ident = _turn(config, room, "alice", "share my location here")
    _run(config, ident, lambda task_id: (_share(config, task_id), "I shared it.")[1])
    assert _assistant_row(config, ident)["location_shares"] is None
    (card,) = _cards(config)
    assert card["message_id"] is None


def test_grant_revoked_before_delivery_drops_the_card(env):
    config, room = env
    active = _grant(config, room)
    ident = _turn(config, room, "bob", "where is Alice?")

    def model(task_id):
        result = _read(config, task_id)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE location_share_grants SET state='revoked' WHERE id=?", (active["id"],))
        return f"Alice: {result['map_url']}"

    _run(config, ident, model)
    assert _assistant_row(config, ident)["location_shares"] is None


def test_live_grant_card_attaches_and_cards_are_one_per_link_at_most_three_in_order(env):
    config, room = env
    _grant(config, room)
    ident = _turn(config, room, "bob", "where is everyone?")

    def model(task_id):
        granted = _read(config, task_id)
        urls = [granted["map_url"], granted["map_url"]]
        with db.get_db(config.db_path) as conn:
            _read(config, task_id)  # the same link again: one card for it
            for lat in (1.0, 2.0, 3.0):
                url = f"https://www.openstreetmap.org/?mlat={lat}&mlon=0"
                sharing._record_card(conn, config, task_id=task_id, actor_user_id="bob",
                                     room_token=room, snapshot={**granted, "lat": lat,
                                                                "map_url": url})
                urls.append(url)
        return "\n".join(urls)

    _run(config, ident, model)
    stored = json.loads(_assistant_row(config, ident)["location_shares"])
    cards = _cards(config)
    assert [s["card_id"] for s in stored] == [cards[0]["id"], cards[2]["id"], cards[3]["id"]]
    assert stored[0]["owner_user_id"] == "alice" and cards[0]["grant_id"]
    assert [s["lat"] for s in stored[1:]] == [1.0, 2.0]
    assert [c["message_id"] is None for c in cards] == [False, True, False, False, True]


# --- serialization ----------------------------------------------------------


def test_field_builder_passes_listed_keys_and_omits_bad_json():
    good = {k: 1 for k in CARD_KEYS} | {"raw_ping": "x"}
    assert app._location_shares_field(json.dumps([good, "junk"])) == [{k: 1 for k in CARD_KEYS}]
    assert app._location_shares_field("{not json") is None
    assert app._location_shares_field(json.dumps({"a": 1})) is None
    assert app._location_shares_field(None) is None
    assert "location_shares" not in app._assistant_message_dict({"body": "x"}, "x", "completed")


# --- the --map option -------------------------------------------------------


def _skill_env(monkeypatch, config, actor, ident):
    monkeypatch.setenv("ISTOTA_USER_ID", actor)
    monkeypatch.setenv("ISTOTA_TASK_ID", str(ident))
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))


def _running_turn(config, room, actor, text="where?"):
    ident = _turn(config, room, actor, text)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    return ident


@pytest.mark.parametrize("argv,cards", [([], 1), (["--map", "auto"], 1), (["--map", "off"], 0)])
def test_location_share_map_option(env, monkeypatch, capsys, argv, cards):
    from istota.skills import location as cli
    config, room = env
    ident = _running_turn(config, room, "alice")
    _skill_env(monkeypatch, config, "alice", ident)
    cli.cmd_share(cli.build_parser().parse_args(["share", "--request-key", "k", *argv]))
    assert json.loads(capsys.readouterr().out)["delivery"] == "reply"
    assert len(_cards(config)) == cards


def test_location_share_map_off_from_private_chat_holds_a_post_without_a_map(env, monkeypatch, capsys):
    from istota.rooms.private_replies import MAP_PREVIEW_LINE
    from istota.skills import location as cli
    config, room = env
    with db.get_db(config.db_path) as conn:
        private = db.create_web_chat_room(conn, "alice", "Private").token
    ident = _running_turn(config, private, "alice", "share my location with Hiking")
    _skill_env(monkeypatch, config, "alice", ident)
    argv = ["share", "--request-key", "k", "--room", room, "--map", "off"]
    cli.cmd_share(cli.build_parser().parse_args(argv))
    assert json.loads(capsys.readouterr().out)["status"] == "held"
    with db.get_db(config.db_path) as conn:
        destination, preview = conn.execute(
            "SELECT destination, preview FROM whatsapp_skill_requests").fetchone()
    assert "location_share" not in json.loads(destination)
    assert MAP_PREVIEW_LINE not in preview
    # The same key with the map on is a different request, not a replay of this one.
    with db.get_db(config.db_path) as conn:
        with pytest.raises(sharing.RequestError, match="request_conflict"):
            sharing.share_location_snapshot(
                conn, config, actor_user_id="alice", task_id=ident, request_key="k",
                now=datetime.now(timezone.utc), room=room)


def test_location_share_map_off_is_refused_for_a_timed_share(env, monkeypatch):
    from istota.skills import location as cli
    config, room = env
    ident = _running_turn(config, room, "alice")
    _skill_env(monkeypatch, config, "alice", ident)
    args = cli.build_parser().parse_args(["share", "--request-key", "k", "--for", "1h", "--map", "off"])
    with pytest.raises(sharing.RequestError, match="map_not_applicable"):
        cli.cmd_share(args)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM location_share_grants").fetchone()[0] == 0
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["share", "--request-key", "k", "--map", "never"])


@pytest.mark.parametrize("argv,cards", [([], 1), (["--map", "off"], 0)])
def test_room_location_map_option(env, monkeypatch, argv, cards):
    from istota.skills import room as room_cli
    config, room = env
    _grant(config, room)
    ident = _running_turn(config, room, "bob")
    _skill_env(monkeypatch, config, "bob", ident)
    result = room_cli._dispatch(room_cli.build_parser().parse_args(
        ["location", "--user", "alice", *argv]))
    assert result["owner_user_id"] == "alice"
    assert len(_cards(config)) == cards


# --- retention --------------------------------------------------------------


def test_cleanup_sweeps_unattached_cards_older_than_seven_days(env):
    from istota.scheduler import run_cleanup_checks
    config, room = env
    observation = {"owner_user_id": "alice", "owner_label": "Alice", "precision": "area",
                   "lat": 1.0, "lon": 2.0, "accuracy_m": None,
                   "observed_at": "2026-01-01T00:00:00+00:00", "freshness": "fresh",
                   "map_url": "https://www.openstreetmap.org/?mlat=1.0&mlon=2.0"}
    ids = {}
    with db.get_db(config.db_path) as conn:
        kept = db.add_message(conn, room, role="assistant", body="here", origin_surface="web")
        for name, age_days, message_id in (("old_loose", 8, None), ("old_attached", 30, kept),
                                           ("recent_loose", 6, None)):
            ids[name] = sharing._insert_card(conn, task_id=None, actor_user_id="alice",
                                             room_token=room, observation=observation)
            conn.execute("UPDATE location_share_cards SET created_at=datetime('now', ?), "
                         "message_id=? WHERE id=?", (f"-{age_days} days", message_id, ids[name]))
    run_cleanup_checks(config)
    assert {card["id"] for card in _cards(config)} == {ids["old_attached"], ids["recent_loose"]}


def test_deleting_the_room_deletes_its_attached_cards(env):
    config, room = env
    ident = _turn(config, room, "alice", "share my location here")
    _run(config, ident, lambda task_id: f"Here: {_share(config, task_id)['snapshot']['map_url']}")
    (card,) = _cards(config)
    assert card["message_id"] is not None
    with db.get_db(config.db_path) as conn:
        room_id = conn.execute("SELECT id FROM web_chat_rooms WHERE token=? AND user_id='alice'",
                               (room,)).fetchone()[0]
        assert db.delete_web_chat_room(conn, room_id, "alice")
    assert _cards(config) == []


def test_cleanup_sweeps_cards_whose_message_is_gone(env):
    config, room = env
    ident = _turn(config, room, "alice", "share my location here")
    _run(config, ident, lambda task_id: f"Here: {_share(config, task_id)['snapshot']['map_url']}")
    (card,) = _cards(config)
    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (card["message_id"],))
        assert sharing.prune_unattached_cards(conn) == 1
    assert _cards(config) == []
