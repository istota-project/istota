"""Phone-room backfill and channel-memory continuity (room-surface-model Stage 23).

A private SMS or WhatsApp conversation becomes a room at its first accepted
text. Its history before that lives on the per-user hash token
(`sms-<hash>`, `whatsapp-<hash>`), which the mint records as a permanent alias.
The scheduler's backfill replays that history into the room's transcript, and
memory recall reads the alias's `channel:` namespace. Neither may reach a
deleted room's history from the room that replaced it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from istota import db, scheduler
from istota.config import Config, MemorySearchConfig, UserConfig
from istota.transport.ingest import record_phone_turn
from istota.transport.sms import sms_conversation_token
from istota.transport.whatsapp import whatsapp_conversation_token

MARKER = "_room_backfill"
SMS = sms_conversation_token("alice")
WHATSAPP = whatsapp_conversation_token("alice")


def _config(tmp_path, **kw) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        users={"alice": UserConfig(), "bob": UserConfig()},
        **kw,
    )


def _old_turn(
    conn, token, prompt, result, *, user="alice", source="sms",
    status="completed", created="2026-09-01 10:00:00", **cols,
) -> int:
    """A task on `token` as it stood before the room existed."""
    tid = db.create_task(
        conn, prompt=prompt, user_id=user, source_type=source,
        conversation_token=token,
    )
    conn.execute(
        "UPDATE tasks SET status = ?, result = ?, created_at = ? WHERE id = ?",
        (status, result, created, tid),
    )
    for column, value in cols.items():
        conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, tid))
    return tid


def _mint(conn, config, surface="sms", user="alice", text="first text") -> str:
    ref = (sms_conversation_token if surface == "sms" else whatsapp_conversation_token)(user)
    return record_phone_turn(
        conn, config, surface=surface, surface_ref=ref, user_id=user,
        text=text, channel_name=surface.upper(),
    ).room_token


def _rows(conn, token):
    return [
        (r["role"], r["body"]) for r in conn.execute(
            "SELECT role, body FROM messages WHERE room_token = ? "
            "ORDER BY created_at, id", (token,),
        )
    ]


def _marker(conn, token, user="alice"):
    entry = db.kv_get(conn, user, MARKER, token)
    return json.loads(entry["value"]) if entry else None


class TestTheBackfill:
    def test_pre_room_history_lands_behind_the_first_text(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, SMS, "what is on friday", "The dentist at 3.",
                      created="2026-09-01 10:00:00")
            _old_turn(conn, SMS, "and saturday", "Nothing yet.",
                      created="2026-09-02 10:00:00")
            room = _mint(conn, config)
        assert scheduler.backfill_phone_rooms(config) == 4
        with db.get_db(config.db_path) as conn:
            assert _rows(conn, room) == [
                ("user", "what is on friday"),
                ("assistant", "The dentist at 3."),
                ("user", "and saturday"),
                ("assistant", "Nothing yet."),
                ("user", "first text"),
            ]
            # Nothing written under the alias itself: the room token is where
            # the transcript lives.
            assert _rows(conn, SMS) == []
            assert _marker(conn, room) == {"rows": 4}

    def test_a_second_pass_is_idempotent_and_the_marker_gates_it(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, SMS, "what is on friday", "The dentist at 3.")
            room = _mint(conn, config)
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            # The converter itself adds nothing on a re-run.
            assert db.backfill_room_messages_from_tasks(
                conn, room, alias_owner="alice",
            ) == 0
            conn.execute(
                "DELETE FROM messages WHERE room_token = ? AND role = 'assistant'",
                (room,),
            )
        # The marker, not the unique index, is what stops the second pass: a
        # row removed since is not put back.
        assert scheduler.backfill_phone_rooms(config) == 0
        with db.get_db(config.db_path) as conn:
            assert ("assistant", "The dentist at 3.") not in _rows(conn, room)

    def test_only_the_owners_own_visible_turns_are_replayed(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, SMS, "kept", "Kept answer.")
            _old_turn(conn, SMS, "withheld", "Withheld answer.", withheld_from_room=1)
            _old_turn(conn, SMS, "guest", "Guest answer.", guest_participant_id=7)
            _old_turn(conn, SMS, "someone else", "Bob answer.", user="bob")
            _old_turn(conn, SMS, "failed", None, status="failed")
            _old_turn(conn, SMS, "synthetic subtask prompt", "Subtask answer.",
                      source="subtask")
            _old_turn(conn, SMS, "cron prompt", "Cron answer.", source="scheduled")
            room = _mint(conn, config)
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            rows = _rows(conn, room)
        assert ("user", "kept") in rows and ("assistant", "Kept answer.") in rows
        # A scheduled post is the assistant half alone, as everywhere else.
        assert ("assistant", "Cron answer.") in rows
        assert ("user", "cron prompt") not in rows
        bodies = {body for _, body in rows}
        for absent in ("withheld", "Withheld answer.", "guest", "Guest answer.",
                       "someone else", "Bob answer.", "failed",
                       "synthetic subtask prompt", "Subtask answer."):
            assert absent not in bodies

    def test_a_task_still_running_at_the_mint_is_picked_up_when_it_finishes(self, tmp_path):
        """Stage 21's residual: a task created on the hash token before the
        room existed and delivered after it has no row from the delivery
        path. The backfill waits for it instead of marking the room done."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, SMS, "done before", "Earlier answer.")
            late = _old_turn(conn, SMS, "still running", None, status="running")
            room = _mint(conn, config)
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert ("assistant", "Earlier answer.") in _rows(conn, room)
            assert _marker(conn, room) is None
            conn.execute(
                "UPDATE tasks SET status = 'completed', result = 'Late answer.' "
                "WHERE id = ?", (late,),
            )
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            rows = _rows(conn, room)
            assert ("user", "still running") in rows
            assert ("assistant", "Late answer.") in rows
            assert _marker(conn, room) is not None

    def test_a_task_finishing_mid_pass_is_not_lost(self, tmp_path, monkeypatch):
        """Review finding: a task completing between the backfill read and
        the unfinished check was read by neither, and the marker then closed
        the room on it. The check now runs first; this completes the task at
        the alias lookup, which sat between the two in the old order."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            late = _old_turn(conn, SMS, "still running", None, status="running")
            room = _mint(conn, config)
        real = db.room_ref_tokens

        def finish_then_lookup(conn, token, **kw):
            with db.get_db(config.db_path) as other:
                other.execute(
                    "UPDATE tasks SET status = 'completed', result = 'Late answer.' "
                    "WHERE id = ?", (late,),
                )
            return real(conn, token, **kw)

        monkeypatch.setattr(db, "room_ref_tokens", finish_then_lookup)
        scheduler.backfill_phone_rooms(config)
        monkeypatch.setattr(db, "room_ref_tokens", real)
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert ("assistant", "Late answer.") in _rows(conn, room)

    def test_a_recreated_room_does_not_inherit_a_deleted_rooms_alias(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            original = _mint(conn, config)
            handle = db.ensure_web_chat_handle(conn, "alice", original, "SMS")
            assert db.delete_web_chat_room(conn, handle.id, "alice")
            # A turn left on the old alias after the deletion: the alias is a
            # tombstone now and names no room.
            _old_turn(conn, SMS, "orphaned", "Orphaned answer.")
            current = _mint(conn, config, text="hello again")
            assert current != original
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert _rows(conn, current) == [("user", "hello again")]
            assert _marker(conn, current) == {"rows": 0}

    def test_whatsapp_private_room_is_backfilled_and_a_group_room_is_left_alone(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, WHATSAPP, "lunch?", "Noon works.", source="whatsapp")
            private = _mint(conn, config, surface="whatsapp")
            group = db.register_room(conn, None, "alice", origin="whatsapp", name="Group").token
            db.add_room_binding(conn, group, "whatsapp", "120363000000000000@g.us")
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert ("assistant", "Noon works.") in _rows(conn, private)
            assert _rows(conn, group) == []
            assert _marker(conn, group) is None

    def test_a_failed_pass_leaves_the_room_usable_and_the_others_done(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, SMS, "sms history", "SMS answer.")
            _old_turn(conn, WHATSAPP, "wa history", "WA answer.", source="whatsapp")
            sms_room = _mint(conn, config)
            wa_room = _mint(conn, config, surface="whatsapp")
        real = db.backfill_room_messages_from_tasks

        def flaky(conn, token, **kw):
            if token == sms_room:
                raise RuntimeError("disk full")
            return real(conn, token, **kw)

        monkeypatch.setattr(db, "backfill_room_messages_from_tasks", flaky)
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert _marker(conn, sms_room) is None
            assert _rows(conn, sms_room) == [("user", "first text")]
            assert ("assistant", "WA answer.") in _rows(conn, wa_room)
            # The room still takes texts; its transcript simply starts later.
            again = _mint(conn, config, text="second text")
            assert again == sms_room
        monkeypatch.setattr(db, "backfill_room_messages_from_tasks", real)
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert ("assistant", "SMS answer.") in _rows(conn, sms_room)

    def test_a_caught_up_reader_is_not_shown_old_history_as_unread(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, SMS, "old question", "Old answer.")
            room = _mint(conn, config)
            db.set_room_read_state(
                conn, room, "web", db.room_max_message_id(conn, room), "alice",
            )
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert db.count_unread_messages(conn, room, "web", "alice") == 0

    def test_a_reader_with_unread_rows_keeps_them(self, tmp_path):
        """The control for the cursor advance: a reader who had not caught up
        is not marked read past rows they have not seen."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _old_turn(conn, SMS, "old question", "Old answer.")
            room = _mint(conn, config)
            db.set_room_read_state(conn, room, "web", 0, "alice")
            db.add_message(conn, room, role="assistant", body="Unseen reply.",
                           origin_surface="sms")
        scheduler.backfill_phone_rooms(config)
        with db.get_db(config.db_path) as conn:
            assert db.count_unread_messages(conn, room, "web", "alice") >= 1
            assert db.get_room_read_state(conn, room, "web", "alice") == 0


class TestChannelMemoryRecall:
    def _config(self, tmp_path):
        return _config(tmp_path, memory_search=MemorySearchConfig(
            enabled=True, auto_recall=True, recency_half_life_days=0,
        ))

    def _recall(self, config, conn, token, query):
        from istota.executor import _recall_memories
        task_id = db.create_task(
            conn, prompt=query, user_id="alice", source_type="sms",
            conversation_token=token,
        )
        with patch("istota.memory.search._search_vec", return_value=[]):
            return _recall_memories(config, conn, db.get_task(conn, task_id), query) or ""

    def _index(self, conn, namespace, task_id, prompt):
        from istota.memory.search import index_conversation
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            index_conversation(conn, namespace, task_id, prompt, "Noted.")

    def test_memory_indexed_before_the_room_is_recalled_in_it(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            self._index(conn, f"channel:{SMS}", 9001, "the cottonwood permit is filed")
            room = _mint(conn, config)
            assert "cottonwood permit is filed" in self._recall(
                config, conn, room, "cottonwood permit",
            )

    def test_a_deleted_rooms_memory_is_not_recalled_in_its_replacement(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            self._index(conn, f"channel:{SMS}", 9001, "the cottonwood permit is filed")
            original = _mint(conn, config)
            handle = db.ensure_web_chat_handle(conn, "alice", original, "SMS")
            assert db.delete_web_chat_room(conn, handle.id, "alice")
            current = _mint(conn, config, text="hello again")
            assert "cottonwood" not in self._recall(
                config, conn, current, "cottonwood permit",
            )

    def test_another_users_alias_is_not_recalled(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            bob_sms = sms_conversation_token("bob")
            self._index(conn, f"channel:{bob_sms}", 9002, "bob's cottonwood permit")
            _mint(conn, config, user="bob")
            room = _mint(conn, config)
            assert "bob's" not in self._recall(config, conn, room, "cottonwood permit")
