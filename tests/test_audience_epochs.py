"""Audience epochs (multiplayer Stage 14, D3).

When a room's audience grows, a new epoch starts, and front-stage readers see
only what the whole current audience was present for. A side room reads its
parent whole. These pin the table and its migration, when a join splits (a
Talk join always, a web add with `acknowledge_history` never, the first roster
observation of a room never), and each front-stage reader: the task's
conversation context from the store, from the tasks fallback and from the Talk
cache, the reply parent, memory recall, the speech gate's classifier window and
the channel sleep cycle.
"""
import sqlite3
from unittest.mock import patch

import pytest

from istota import db, speech_gate
from istota.config import (
    Config,
    ConversationConfig,
    MemorySearchConfig,
    NextcloudConfig,
    TalkConfig,
    UserConfig,
)
from istota.transport.talk.inbound import _sync_talk_roster

from .support.rooms import plain_talk_room

ALICE = {"actorType": "users", "actorId": "alice", "displayName": "Alice"}
BOB = {"actorType": "users", "actorId": "bob", "displayName": "Bob"}
MAX = {"actorType": "guests", "actorId": "max", "displayName": "Max"}
BOT = {"actorType": "users", "actorId": "bot"}


def _config(tmp_path, **kw):
    path = tmp_path / "state.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="bot",
                                  app_password="secret"),
        talk=TalkConfig(enabled=True, bot_username="bot"),
        conversation=ConversationConfig(use_selection=False),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        **kw,
    )


@pytest.fixture
def config(tmp_path):
    return _config(tmp_path)


def _turn(conn, token, user, prompt, result, *, mirror=True, source="talk"):
    """A completed, answered turn in `token`'s transcript."""
    ident = db.create_task(conn, user_id=user, source_type=source, prompt=prompt,
                           conversation_token=token)
    conn.execute(
        "UPDATE tasks SET status='completed', result=?, completed_at=datetime('now') "
        "WHERE id=?", (result, ident),
    )
    if mirror:
        db.store_turn_message(conn, token, role="user", body=prompt, task_id=ident,
                              origin_surface=source)
        db.store_turn_message(conn, token, role="assistant", body=result,
                              task_id=ident, origin_surface=source)
    return ident


def _talk_group(conn, config, token="grp"):
    """Alice and Bob in a Talk group room, its roster observed once."""
    shape = plain_talk_room(conn, "alice", token=token, name="Family")
    db.add_room_member(conn, shape.canonical, "bob", acknowledged=True)
    _sync_talk_roster(conn, config, token, [ALICE, BOB, BOT])
    return shape.canonical


def _front_stage_task(conn, token, user="alice", prompt="what did we decide?",
                      **kw):
    ident = db.create_task(conn, user_id=user, source_type="talk", prompt=prompt,
                           conversation_token=token, **kw)
    return db.get_task(conn, ident)


def _db_context(config, conn, task):
    from istota.executor import _build_db_context
    context, _ = _build_db_context(task, config, conn)
    return context or ""


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


class TestAJoinHidesEarlierTurnsFromTheFrontStage:
    def test_a_guest_joining_leaves_the_next_front_stage_task_no_earlier_turn(
        self, config,
    ):
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            _turn(conn, token, "bob", "welcome max, flights are booked", "Great.")
            context = _db_context(config, conn, _front_stage_task(conn, token))
        assert "flights are booked" in context
        assert "house sale" not in context

    def test_a_side_room_task_reads_the_earlier_turns(self, config):
        from istota.executor import build_prompt
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            side = db.ensure_side_room(conn, token, "alice")
            ident = db.create_task(conn, user_id="alice", source_type="web",
                                   prompt="what did we say before max?",
                                   conversation_token=side.token)
            composed = build_prompt(db.get_task(conn, ident), [], config, conn=conn)
        assert "house sale closes friday" in composed.user


# ---------------------------------------------------------------------------
# When a join splits
# ---------------------------------------------------------------------------


def _epochs(conn, token):
    return [dict(r) for r in conn.execute(
        "SELECT epoch, reason, person FROM room_epochs WHERE room_token = ? "
        "AND epoch > 0 ORDER BY epoch", (token,),
    ).fetchall()]


class TestWhenAJoinSplits:
    def test_the_first_roster_observation_is_a_baseline_not_a_join(self, config):
        """A room whose roster was never observed (a new room, or every room
        on the day this ships) has no record of anybody joining: whoever is on
        it the first time is the audience its transcript was written for."""
        with db.get_db(config.db_path) as conn:
            shape = plain_talk_room(conn, "alice", token="grp", name="Family")
            _turn(conn, shape.canonical, "alice", "earlier plans", "Ok.")
            _sync_talk_roster(conn, config, "grp", [ALICE, BOB, MAX, BOT])
            assert _epochs(conn, "grp") == []
            context = _db_context(config, conn, _front_stage_task(conn, "grp"))
        assert "earlier plans" in context

    def test_a_talk_join_after_the_baseline_starts_an_epoch(self, config):
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            assert _epochs(conn, token) == [
                {"epoch": 1, "reason": "talk_join", "person": "talk:guests/max"},
            ]
            # Already present: no second split.
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            assert len(_epochs(conn, token)) == 1

    def test_a_web_member_added_with_acknowledged_history_does_not_split(
        self, config,
    ):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Plans")
            _turn(conn, room.token, "alice", "the garden plan", "Ok.", source="web")
            db.add_web_room_member(conn, room.token, "bob")
            assert _epochs(conn, room.token) == []
            context = _db_context(
                config, conn, _front_stage_task(conn, room.token, user="bob"),
            )
        assert "garden plan" in context

    def test_an_unacknowledged_member_add_splits(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Plans")
            _turn(conn, room.token, "alice", "the garden plan", "Ok.", source="web")
            db.add_room_member(conn, room.token, "bob")
            assert [e["person"] for e in _epochs(conn, room.token)] == ["u:bob"]
            context = _db_context(
                config, conn, _front_stage_task(conn, room.token, user="bob"),
            )
        assert "garden plan" not in context

    def test_one_user_on_a_second_surface_is_not_a_join(self, config):
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            db.upsert_room_participant(
                conn, room_token=token, surface="web", surface_ref="bob",
                kind="principal", user_id="bob",
            )
            assert _epochs(conn, token) == []

    def test_a_private_room_never_splits(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Mine")
            db.upsert_room_participant(
                conn, room_token=room.token, surface="web", surface_ref="alice",
                kind="principal", user_id="alice",
            )
            assert _epochs(conn, room.token) == []

    def test_a_guest_who_left_no_longer_narrows_the_front_stage(self, config):
        """Epochs the whole *current* audience was present for: once the
        joiner is gone, the turns before them are shared by everyone here."""
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            _sync_talk_roster(conn, config, token, [ALICE, BOB, BOT])
            context = _db_context(config, conn, _front_stage_task(conn, token))
        assert "house sale" in context

    def test_a_member_who_left_and_came_back_splits_again(self, config):
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            _sync_talk_roster(conn, config, token, [ALICE, BOB, BOT])
            _turn(conn, token, "alice", "said while max was away", "Ok.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            context = _db_context(config, conn, _front_stage_task(conn, token))
        assert "while max was away" not in context
        with db.get_db(config.db_path) as conn:
            assert [e["epoch"] for e in _epochs(conn, token)] == [1, 2]

    def test_a_guest_speaking_before_the_roster_shows_them_splits(self, config):
        from istota.transport._types import ParticipantRef
        from istota.transport.ingest import record_inbound
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.")
            record_inbound(
                conn, config, surface="talk", surface_ref=token, user_id="",
                text="hi all", is_group_chat=True, addressed_to_bot=False,
                author=ParticipantRef(surface="talk", surface_ref="guests/max",
                                      display_name="Max"),
            )
            assert [e["person"] for e in _epochs(conn, token)] == ["talk:guests/max"]
            context = _db_context(config, conn, _front_stage_task(conn, token))
        assert "house sale" not in context
        # The joiner's own first turn is on the near side of the boundary.
        assert "hi all" in context


# ---------------------------------------------------------------------------
# Each front-stage reader
# ---------------------------------------------------------------------------


class TestTheFrontStageReaders:
    def test_the_tasks_fallback_is_limited_too(self, config):
        """A room whose store is not caught up reads history off `tasks`."""
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.",
                  mirror=False)
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            _turn(conn, token, "bob", "flights are booked", "Great.", mirror=False)
            assert not db._messages_caught_up(conn, token)
            context = _db_context(config, conn, _front_stage_task(conn, token))
        assert "flights are booked" in context
        assert "house sale" not in context

    def test_the_resurfaced_previous_tasks_are_limited_too(self, config):
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "briefing: the house sale", "Posted.",
                  source="briefing", mirror=False)
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            context = _db_context(config, conn, _front_stage_task(conn, token))
        assert "house sale" not in context

    def test_the_talk_cache_is_limited_to_messages_after_the_join(self, config):
        from istota.executor import _build_talk_api_context
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            db.upsert_talk_messages(conn, token, [
                {"id": 10, "actorId": "alice", "actorDisplayName": "Alice",
                 "actorType": "users", "message": "the house sale closes friday",
                 "messageType": "comment", "timestamp": 10},
            ])
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            db.upsert_talk_messages(conn, token, [
                {"id": 20, "actorId": "bob", "actorDisplayName": "Bob",
                 "actorType": "users", "message": "welcome max",
                 "messageType": "comment", "timestamp": 20},
            ])
            context, _ = _build_talk_api_context(
                _front_stage_task(conn, token), config, conn,
            )
        assert "welcome max" in (context or "")
        assert "house sale" not in (context or "")

    def test_a_reply_parent_from_before_the_join_is_not_pulled_in(self, config):
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            parent = _turn(conn, token, "alice", "the house sale closes friday",
                           "The sale closes at noon.")
            conn.execute("UPDATE tasks SET talk_message_id = 555 WHERE id = ?",
                         (parent,))
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            _turn(conn, token, "bob", "welcome max", "Hello Max.")
            task = _front_stage_task(conn, token, reply_to_talk_id=555)
            context = _db_context(config, conn, task)
        assert "closes at noon" not in context

    def test_a_reply_parent_after_the_join_is_still_pulled_in(self, config):
        """The control: the same reply resolves once the parent is on the
        near side of the join."""
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            parent = _turn(conn, token, "alice", "the house sale closes friday",
                           "The sale closes at noon.")
            conn.execute("UPDATE tasks SET talk_message_id = 555 WHERE id = ?",
                         (parent,))
            for i in range(30):
                _turn(conn, token, "bob", f"filler {i}", f"ok {i}")
            task = _front_stage_task(conn, token, reply_to_talk_id=555)
            context = _db_context(config, conn, task)
        assert "closes at noon" in context

    def test_memory_recall_drops_the_room_turns_before_the_join(self, tmp_path):
        from istota.executor import _recall_memories
        from istota.memory.search import index_conversation
        config = _config(tmp_path, memory_search=MemorySearchConfig(
            enabled=True, auto_recall=True, recency_half_life_days=0,
        ))
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            before = _turn(conn, token, "alice", "the cottonwood permit is filed",
                           "Noted.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            after = _turn(conn, token, "bob", "cottonwood permit renewal is due",
                          "Noted.")
            with patch("istota.memory.search.ensure_vec_table", return_value=False):
                for ident, prompt in ((before, "the cottonwood permit is filed"),
                                      (after, "cottonwood permit renewal is due")):
                    index_conversation(conn, f"channel:{token}", ident, prompt, "Noted.")
            with patch("istota.memory.search._search_vec", return_value=[]):
                recalled = _recall_memories(
                    config, conn, _front_stage_task(conn, token), "cottonwood permit",
                )
        assert "renewal is due" in (recalled or "")
        assert "is filed" not in (recalled or "")

    def test_the_classifier_window_starts_at_the_join(self, config):
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            _turn(conn, token, "bob", "welcome max", "Hello Max.")
            turns = speech_gate.load_window(
                conn, token, bot_name="Istota", window_messages=8,
                max_message_chars=400,
            )
        texts = " ".join(t.text for t in turns)
        assert "welcome max" in texts
        assert "house sale" not in texts

    def test_the_channel_sleep_cycle_reads_only_the_shared_epochs(self, config):
        from istota.memory.sleep_cycle import gather_channel_data
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            _turn(conn, token, "bob", "we agreed on the lake trip", "Noted.")
            data = gather_channel_data(config, conn, token, 24, None)
        assert "lake trip" in data
        assert "house sale" not in data

    def test_the_export_is_not_a_front_stage_reader(self, config):
        """`!export` is the member's own copy of the room, not model context."""
        with db.get_db(config.db_path) as conn:
            token = _talk_group(conn, config)
            _turn(conn, token, "alice", "the house sale closes friday", "Noted.")
            _sync_talk_roster(conn, config, token, [ALICE, BOB, MAX, BOT])
            history = db.get_conversation_history(conn, token)
        assert any("house sale" in m.prompt for m in history)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestTheMigration:
    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path, config):
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute("DROP TABLE room_epochs")
        raw.execute("DELETE FROM _migration_state WHERE name = 'room_epochs_v1'")
        raw.commit()
        raw.row_factory = sqlite3.Row
        db._run_migrations(raw)
        raw.commit()
        raw.close()
        with db.get_db(config.db_path) as fresh, db.get_db(old) as upgraded:
            a = [tuple(r) for r in fresh.execute("PRAGMA table_info(room_epochs)")]
            b = [tuple(r) for r in upgraded.execute("PRAGMA table_info(room_epochs)")]
            assert a and a == b
            index_sql = (
                "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'room_epochs' "
                "AND type = 'index' ORDER BY name"
            )
            assert list(map(tuple, fresh.execute(index_sql))) == list(
                map(tuple, upgraded.execute(index_sql))
            )
            assert upgraded.execute(
                "SELECT 1 FROM _migration_state WHERE name = 'room_epochs_v1'"
            ).fetchone() is not None
            assert upgraded.execute("SELECT COUNT(*) FROM room_epochs").fetchone()[0] == 0

    def test_the_ddl_copies_agree(self):
        from pathlib import Path
        schema = Path(db.__file__).resolve().parents[2] / "schema.sql"
        assert " ".join(db._ROOM_EPOCHS_DDL.split()) in " ".join(
            schema.read_text().split()
        )

    def test_deleting_a_room_deletes_its_epochs(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Plans")
            _turn(conn, room.token, "alice", "x", "y", source="web")
            db.add_room_member(conn, room.token, "bob")
            assert _epochs(conn, room.token)
            handle = next(h for h in db.list_web_chat_rooms(conn, "alice")
                          if h.token == room.token)
            assert db.delete_web_chat_room(conn, handle.id, "alice")
            assert conn.execute(
                "SELECT COUNT(*) FROM room_epochs WHERE room_token = ?", (room.token,),
            ).fetchone()[0] == 0
