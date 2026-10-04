"""Email on rooms, stage 2: the intake decision on a thread room, and what a
correspondent's turn becomes.

Intake decides in code, from the message's own headers, whether the bot is
asked (`threads.intake_facts`, `threads.thread_addressed`):

| Author        | host on message | named | asked                  |
|---------------|-----------------|-------|------------------------|
| someone else  | yes             | no    | no: recorded only      |
| someone else  | any             | yes   | yes                    |
| someone else  | no              | any   | yes, with `host_absent`|
| the host      | n/a             | any   | bot in To, or named    |

A host-absent turn with nothing to reply answers `NO_ACTION:`, which the
scheduler turns into one pass-on note in the host's private room, built from
the stored turn. Driven through `poll_emails` and `process_one_task`, the two
places the behaviour lives, as `test_email_thread_rooms.py` does.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import db
from istota.rooms import private_replies
from istota.transport.email import threads

from .test_email_thread_rooms import (
    ALICE,
    BOB,
    BOT,
    HOST,
    HOST_ADDR,
    ROOT,
    _poll,
    _room_token,
    _rows,
    _start_thread,
)
from . import test_email_thread_rooms as _base

config = _base.config
db_path = _base.db_path


def _task(db_path, task_id):
    with db.get_db(db_path) as conn:
        return db.get_task(conn, task_id)


def _last_rung(db_path):
    return _rows(db_path, "SELECT rung FROM speech_gate_decisions ORDER BY id DESC LIMIT 1")


# ---------------------------------------------------------------------------
# The four rows
# ---------------------------------------------------------------------------


class TestTheIntakeTable:
    def test_a_reply_all_the_host_is_on_and_that_does_not_name_the_bot_is_recorded(
        self, config, db_path,
    ):
        """Row 1, and the case that started it: on a thread the bot started
        every reply-all has it in To, so being in To asks nothing."""
        _start_thread(config)
        task_ids = _poll(config, sender=ALICE, to=(BOT, HOST_ADDR), cc=(BOB,),
                         message_id="<a2@ext.example>", references=ROOT,
                         body="Thursday works for me")
        assert task_ids == []
        assert _last_rung(db_path) == [{"rung": "mode_mention"}]

    def test_a_correspondent_naming_the_bot_is_asked(self, config, db_path):
        """Row 2: named, with the host on the message."""
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOB, BOT),
                           message_id="<a2@ext.example>", references=ROOT,
                           body="Zorg, is Carol free Thursday?")
        task = _task(db_path, task_id)
        assert task.user_id == HOST
        assert not task.host_absent

    def test_a_correspondent_the_host_is_not_on_is_asked_with_host_absent(
        self, config, db_path,
    ):
        """Row 3: nobody else will tell the host, so the turn runs."""
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,),
                           message_id="<a2@ext.example>", references=ROOT,
                           body="Can we move dinner to Friday?")
        task = _task(db_path, task_id)
        assert task.user_id == HOST
        assert task.host_absent
        assert task.guest_participant_id is None

    def test_the_hosts_own_mail_keeps_issue_607(self, config, db_path):
        """Row 4: the bot in To asks; in Cc and not named it listens."""
        _start_thread(config)
        (asked,) = _poll(config, sender=HOST_ADDR, to=(BOT,), cc=(ALICE, BOB),
                         message_id="<c2@test.com>", references=ROOT,
                         body="What time did we say?")
        assert not _task(db_path, asked).host_absent
        assert _poll(config, sender=HOST_ADDR, to=(ALICE,), cc=(BOB, BOT),
                     message_id="<c3@test.com>", references=ROOT,
                     body="See you all there.") == []


class TestIntakeFacts:
    def _mail(self, **fields):
        base = dict(sender=ALICE, to=(), cc=(), body="")
        base.update(fields)
        return SimpleNamespace(**base)

    def test_missing_headers_mean_the_host_is_not_on_the_message(self, config):
        facts = threads.intake_facts(config, self._mail(to=None, cc=None), HOST)
        assert facts == threads.IntakeFacts(
            author_is_host=False, host_on_message=False, named=False,
        )
        assert facts.host_absent

    def test_a_display_name_and_case_still_find_the_host(self, config):
        facts = threads.intake_facts(
            config, self._mail(cc=("Carol <CAROL@Test.com>",)), HOST,
        )
        assert facts.host_on_message and not facts.host_absent

    def test_the_bot_in_to_alone_asks_only_for_the_host(self, config):
        to_bot = self._mail(to=(BOT,), cc=(HOST_ADDR,), body="Thanks!")
        facts = threads.intake_facts(config, to_bot, HOST)
        assert not threads.thread_addressed(config, to_bot, facts)
        from_host = self._mail(sender=HOST_ADDR, to=(BOT,), body="Thanks!")
        facts = threads.intake_facts(config, from_host, HOST)
        assert facts.author_is_host
        assert threads.thread_addressed(config, from_host, facts)

    def test_a_name_in_the_quoted_history_does_not_count(self, config):
        mail = self._mail(cc=(HOST_ADDR,), body=(
            "Sounds good.\n\nOn Mon, 1 Jan 2026, Carol wrote:\n> Zorg, book it?"
        ))
        assert not threads.intake_facts(config, mail, HOST).named


# ---------------------------------------------------------------------------
# The pass-on note
# ---------------------------------------------------------------------------


QUOTED_REPLY = (
    "Can we move dinner to Friday?\n"
    "\n"
    "On Mon, 1 Jan 2026, Carol wrote:\n"
    "> Thursday at 7, Zorg booked it."
)


def _close_and_report(coro, *args, **kwargs):
    coro.close()
    return True


def _host_absent_turn(config, db_path):
    """A thread, the host's private web room, and Alice writing to the bot
    alone. The host's opening turn is settled, so the scheduler claims hers."""
    first = _start_thread(config)
    with db.get_db(db_path) as conn:
        private = db.create_web_chat_room(conn, HOST, "Mine").token
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (first[0],))
    (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,),
                       message_id="<a2@ext.example>", references=ROOT, body=QUOTED_REPLY)
    return task_id, private


def _run_scheduler(config, result):
    from istota.scheduler import process_one_task

    with (
        patch("istota.scheduler.execute_task", return_value=(True, result, None, None)),
        patch("istota.scheduler.run_coro", side_effect=_close_and_report),
        patch("istota.scheduler.post_result_to_email", return_value=True) as mail,
    ):
        process_one_task(config)
    return mail


class TestThePassOnNote:
    def test_no_action_passes_the_new_text_on_and_sends_nothing(self, config, db_path):
        task_id, private = _host_absent_turn(config, db_path)
        mail = _run_scheduler(config, "NO_ACTION: Alice is only asking Carol.")

        assert _task(db_path, task_id).status == "completed"
        mail.assert_not_called()
        (row,) = _rows(db_path, "SELECT role, body, about_room_token, delivery_reference "
                       "FROM messages WHERE room_token = ?", (private,))
        assert row["role"] == "system"
        assert row["about_room_token"] == _room_token(config)
        assert row["delivery_reference"] == f"private-pass_on:{task_id}:pass-on"
        # Built from the stored turn: the new text quoted, no wrapper, no
        # quoted history, nothing of the model's answer.
        assert row["body"] == (
            f"{ALICE} wrote on Dinner plans, without you on the message:\n\n"
            "> Can we move dinner to Friday?"
        )
        assert _rows(db_path, "SELECT id FROM messages WHERE room_token = ? "
                     "AND role = 'assistant'", (_room_token(config),)) == []

    def test_control_a_reply_is_sent_as_one(self, config, db_path):
        task_id, private = _host_absent_turn(config, db_path)
        mail = _run_scheduler(config, "Friday works too, I'll let Carol know.")

        assert _task(db_path, task_id).status == "completed"
        mail.assert_called_once()
        assert _rows(db_path, "SELECT id FROM messages WHERE room_token = ?", (private,)) == []

    def test_control_no_action_from_a_turn_the_host_is_on_is_no_pass_on(
        self, config, db_path,
    ):
        """Only a host-absent turn passes on: the host already has this mail."""
        first = _start_thread(config)
        with db.get_db(db_path) as conn:
            private = db.create_web_chat_room(conn, HOST, "Mine").token
            conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (first[0],))
        _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOT,),
              message_id="<a2@ext.example>", references=ROOT, body="Zorg, Friday?")
        _run_scheduler(config, "NO_ACTION: nothing to add.")
        assert _rows(db_path, "SELECT id FROM messages WHERE room_token = ?", (private,)) == []

    def test_with_no_private_room_it_is_a_bell_row(self, config, db_path):
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,),
                           message_id="<a2@ext.example>", references=ROOT,
                           body="Can we move dinner to Friday?")
        with db.get_db(db_path) as conn:
            delivery, body = private_replies.deliver_pass_on(
                conn, config, db.get_task(conn, task_id),
            )
            notes = conn.execute(
                "SELECT body FROM notifications WHERE source = 'task_alert'",
            ).fetchall()
        assert delivery.dest is None and delivery.notice is not None
        assert body.endswith("> Can we move dinner to Friday?")
        # The bell stores its body flattened (`flatten_body`).
        (note,) = notes
        assert note["body"].endswith("Can we move dinner to Friday?")


# ---------------------------------------------------------------------------
# The guest reply mode, and the card
# ---------------------------------------------------------------------------


class TestNoGuestModeOnEmail:
    def test_guest_reply_mode_answers_direct_whatever_the_policy_says(
        self, config, db_path,
    ):
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR,),
                           message_id="<a2@ext.example>", references=ROOT,
                           body="Zorg, Friday?")
        with db.get_db(db_path) as conn:
            from istota.rooms import policy as room_policy

            assert room_policy.ensure_policy(conn, _room_token(config)).guest_reply == "held"
            assert private_replies.guest_reply_mode(conn, db.get_task(conn, task_id)) == "direct"

    def test_the_settings_payload_marks_an_email_room(self, config, db_path):
        pytest.importorskip("fastapi")
        from istota.webui import app as web_app

        _start_thread(config)
        with db.get_db(db_path) as conn:
            view = web_app._room_sharing(
                conn, db.get_room(conn, _room_token(config)), HOST,
            )
        assert view["policy"]["email_thread"] is True


class TestTheEmailRoomCard:
    def _card(self, config, db_path, task_id):
        from istota.executor import room_card

        task = _task(db_path, task_id)
        return room_card(config, task, withheld_scopes=frozenset(), room_cli_available=True)

    def test_a_host_absent_turn_is_told_how_to_pass_it_on(self, config, db_path):
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,),
                           message_id="<a2@ext.example>", references=ROOT,
                           body="Can we move dinner to Friday?")
        card = self._card(config, db_path, task_id)
        assert "acting for 'carol' on their correspondence" in card
        assert "reaches everyone on the thread: 'carol' and 2 other people" in card
        assert "untrusted input" in card
        assert "answer `NO_ACTION:`" in card
        assert "passed on to them privately" not in card
        # The thread records the mailed body; the answer text is the host's note.
        assert "with `istota-skill email output`" in card
        assert "Your answer text is shown only to 'carol'" in card
        assert "Do not write a separate alert" in card
        # None of the shared-room card's rules apply on a thread.
        for absent in ("guest's turn", "Withheld", "room whisper", "answer-privately",
                       ALICE):
            assert absent not in card

    def test_the_hosts_own_turn_is_theirs_and_has_no_pass_on(self, config, db_path):
        _start_thread(config)
        (task_id,) = _poll(config, sender=HOST_ADDR, to=(BOT,), cc=(ALICE, BOB),
                           message_id="<c2@test.com>", references=ROOT,
                           body="What time did we say?")
        card = self._card(config, db_path, task_id)
        assert "This message is from 'carol'." in card
        assert "NO_ACTION" not in card and "untrusted input" not in card


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------


class TestTheColumn:
    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path):
        fresh = tmp_path / "fresh.db"
        db.init_db(fresh)
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute("ALTER TABLE tasks DROP COLUMN host_absent")
        raw.execute("INSERT INTO tasks (prompt, user_id, source_type) "
                    "VALUES ('p', 'carol', 'email')")
        raw.commit()
        db._run_migrations(raw)
        raw.commit()
        raw.close()
        declared = tmp_path / "declared.db"
        schema = (Path(db.__file__).parent.parent.parent / "schema.sql").read_text()
        raw = sqlite3.connect(declared)
        raw.executescript(schema)
        raw.close()

        def column(path):
            with db.get_db(path) as conn:
                return [tuple(r)[1:] for r in conn.execute("PRAGMA table_info(tasks)")
                        if r[1] == "host_absent"]

        assert column(declared) == column(old) == column(fresh) == [
            ("host_absent", "INTEGER", 1, "0", 0),
        ]
        with db.get_db(old) as conn:
            (task,) = [db.get_task(conn, row[0])
                       for row in conn.execute("SELECT id FROM tasks")]
        assert task.host_absent is False
