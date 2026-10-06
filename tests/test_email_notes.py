"""Hidden email threads, stage 4: one private note per mail.

When a note is due (`private_replies.email_note_due`), what it says
(`email_note`), and that the scheduler writes it after delivery, once,
in the host's private room. Driven through `poll_emails` and
`process_one_task`, with delivery real up to the SMTP seam, as
`test_email_thread_rooms.py` does.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from istota import db
from istota.rooms import private_replies
from istota.rooms.private_replies import email_note_due

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

HEADER_ABSENT = f"{ALICE} wrote on Dinner plans, without you on the message:"
HEADER_PRESENT = f"{ALICE} wrote on Dinner plans:"


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("host_absent", "outcome", "remark", "due"), [
    (True, "sent", "", True),
    (True, "held", "", True),
    (True, "parked", "", True),
    (True, "failed", "", True),
    (True, "none", "", True),
    (True, "none", "They mean Friday.", True),
    (False, "held", "", True),
    (False, "parked", "", True),
    (False, "failed", "", True),
    (False, "none", "Thursday clashes with your course.", True),
    (False, "none", "", False),
    (False, "none", "   ", False),
    (False, "sent", "", False),
    (False, "sent", "I told them Friday.", False),
])
def test_the_table(host_absent, outcome, remark, due):
    assert email_note_due(host_absent=host_absent, outcome=outcome, remark=remark) is due


def test_an_unknown_outcome_is_refused():
    with pytest.raises(ValueError):
        email_note_due(host_absent=True, outcome="maybe", remark="")


# ---------------------------------------------------------------------------
# Through the scheduler
# ---------------------------------------------------------------------------


def _private_room(db_path):
    with db.get_db(db_path) as conn:
        return db.create_web_chat_room(conn, HOST, "Mine").token


def _settle_first(db_path, task_ids):
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_ids[0],))


def _absent_turn(config, db_path, body="Can we move dinner to Friday?"):
    """Alice writes to the bot alone on the host's thread."""
    first = _start_thread(config)
    private = _private_room(db_path)
    _settle_first(db_path, first)
    (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,),
                       message_id="<a2@ext.example>", references=ROOT, body=body)
    return task_id, private


def _present_turn(config, db_path):
    """Alice writes to the host, naming the bot, which asks it."""
    first = _start_thread(config)
    private = _private_room(db_path)
    _settle_first(db_path, first)
    (task_id,) = _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOT,),
                       message_id="<a2@ext.example>", references=ROOT,
                       body="Zorg, can we move dinner to Friday?")
    return task_id, private


def _complete(config, result, *, task_id, deferred_body=None, send_fails=False):
    """Run the scheduler with ``result`` as the answer and, when given,
    ``deferred_body`` as the `email output` file. Pushes are captured at
    `send_notification`; mail stops at `reply_to_email`."""
    from istota.executor import task_deferred_dir
    from istota.scheduler import process_one_task

    if deferred_body is not None:
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
        out = task_deferred_dir(config, task)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"task_{task_id}_email_output.json").write_text(json.dumps(
            {"subject": "", "body": deferred_body, "format": "plain"}))
    reply_kwargs = ({"side_effect": RuntimeError("smtp down")} if send_fails
                    else {"return_value": "<out@test.com>"})
    with (
        patch("istota.scheduler.execute_task", return_value=(True, result, None, None)),
        patch("istota.transport.email.outbound.reply_to_email", **reply_kwargs) as reply,
        patch("istota.notifications.delivery.send_notification", return_value=True) as push,
    ):
        process_one_task(config)
    return reply, push


def _notes(db_path, private):
    return _rows(db_path, "SELECT body, about_room_token, delivery_reference, role "
                 "FROM messages WHERE room_token = ? ORDER BY id", (private,))


class TestTheNote:
    def test_a_host_absent_mail_answered_by_mail_writes_one_note(self, config, db_path):
        task_id, private = _absent_turn(config, db_path, body=(
            "Can we move dinner to Friday?\n\nOn Mon, 1 Jan 2026, Carol wrote:\n"
            "> Thursday at 7, Zorg booked it."
        ))
        reply, _ = _complete(config, "They mean the weekend of your first-aid course.",
                             task_id=task_id, deferred_body="Friday works.")

        reply.assert_called_once()
        (note,) = _notes(db_path, private)
        assert note["role"] == "system"
        assert note["about_room_token"] == _room_token(config)
        assert note["delivery_reference"] == f"private-pass_on:{task_id}:pass-on"
        # Header, the new text quoted (no wrapper, no quoted history), the
        # outcome, then the bot's own remark.
        assert note["body"] == (
            f"{HEADER_ABSENT}\n\n> Can we move dinner to Friday?\n\nReplied.\n\n"
            "They mean the weekend of your first-aid course."
        )

    def test_the_host_on_the_mail_and_a_sent_reply_writes_nothing(self, config, db_path):
        task_id, private = _present_turn(config, db_path)
        reply, push = _complete(config, "Told them Friday works.",
                                task_id=task_id, deferred_body="Friday works.")
        reply.assert_called_once()
        assert _notes(db_path, private) == []

    def test_the_host_on_the_mail_and_a_held_reply_writes_a_note(self, config, db_path):
        task_id, private = _present_turn(config, db_path)
        config.users[HOST].trusted_email_senders = []
        reply, _ = _complete(config, "Friday works.", task_id=task_id,
                             deferred_body="Friday works.")

        reply.assert_not_called()
        (note,) = _notes(db_path, private)
        assert note["body"] == (
            f"{HEADER_PRESENT}\n\n> Zorg, can we move dinner to Friday?\n\n"
            "Reply waiting for your approval."
        )

    def test_the_host_on_the_mail_answered_only_to_them_writes_a_note(self, config, db_path):
        task_id, private = _present_turn(config, db_path)
        reply, _ = _complete(config, "Friday clashes with your course.", task_id=task_id)
        reply.assert_not_called()
        (note,) = _notes(db_path, private)
        assert note["body"].endswith("\n\nNo reply sent.\n\nFriday clashes with your course.")

    def test_no_action_with_the_host_absent_is_the_quote_and_no_reply(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        reply, _ = _complete(config, "NO_ACTION:", task_id=task_id)
        reply.assert_not_called()
        (note,) = _notes(db_path, private)
        assert note["body"] == (
            f"{HEADER_ABSENT}\n\n> Can we move dinner to Friday?\n\nNo reply sent."
        )

    def test_a_no_action_remark_keeps_its_words_without_the_marker(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        _complete(config, "NO_ACTION: Alice is only asking Carol.", task_id=task_id)
        (note,) = _notes(db_path, private)
        assert note["body"].endswith("No reply sent.\n\nAlice is only asking Carol.")

    def test_a_reply_that_could_not_be_sent_says_so(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        _complete(config, "Friday works.", task_id=task_id, deferred_body="Friday works.",
                  send_fails=True)
        (note,) = _notes(db_path, private)
        assert note["body"].endswith("\n\nThe reply could not be sent.")

    def test_the_remark_is_dropped_when_it_is_the_mailed_body(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        _complete(config, "Friday works.", task_id=task_id, deferred_body="Friday works.")
        (note,) = _notes(db_path, private)
        assert note["body"].endswith("\n\nReplied.")

    def test_an_envelope_result_has_no_remark(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        _complete(config, json.dumps({"subject": "", "body": "Friday works.",
                                      "format": "plain"}), task_id=task_id)
        (note,) = _notes(db_path, private)
        assert note["body"].endswith("\n\nReplied.")

    def test_the_quote_is_capped(self, config, db_path):
        task_id, private = _absent_turn(config, db_path, body="x" * 600)
        _complete(config, "NO_ACTION:", task_id=task_id)
        (note,) = _notes(db_path, private)
        quoted = note["body"].split("\n\n")[1]
        assert quoted == "> " + "x" * private_replies.QUOTE_CHARS + "…"

    def test_a_parked_question_is_the_note_and_there_is_no_pass_on(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        question = "Shall I tell Alice Friday works? Please confirm."
        reply, _ = _complete(config, question, task_id=task_id)

        reply.assert_not_called()
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
        (note,) = _notes(db_path, private)
        assert note["delivery_reference"].startswith(f"private-confirmation:{task_id}:")
        assert note["body"] == (
            f"{HEADER_ABSENT}\n\n> Can we move dinner to Friday?\n\n"
            f"Question for you.\n\n{question}"
        )

    def test_a_second_delivery_adds_no_second_note(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        _complete(config, "NO_ACTION:", task_id=task_id)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
            private_replies.deliver_email_note(conn, config, task, outcome="none", remark="")
        assert len(_notes(db_path, private)) == 1
        assert len(_rows(db_path, "SELECT id FROM notifications WHERE source = 'task_alert' "
                         "AND dedup_key = ?", (f"private-note:{task_id}",))) == 1


def _parts(db_path, private):
    rows = _rows(db_path, "SELECT email_note FROM messages WHERE room_token = ? "
                 "ORDER BY id", (private,))
    return [json.loads(r["email_note"]) if r["email_note"] else None for r in rows]


class TestTheNoteParts:
    """ISSUE-644: the web renders a note from its parts, so the code-built
    header and outcome are stored apart from the model's remark."""

    def test_a_note_stores_its_parts_beside_the_body(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        _complete(config, "They mean the weekend of your course.",
                  task_id=task_id, deferred_body="Friday works.")
        (parts,) = _parts(db_path, private)
        assert parts == {
            "header": HEADER_ABSENT[:-1],
            "outcome": "Replied.",
            "remark": "They mean the weekend of your course.",
        }
        # The body is unchanged: the push surfaces and the bell read it.
        (note,) = _notes(db_path, private)
        assert note["body"].startswith(HEADER_ABSENT)

    def test_a_note_with_no_remark_stores_an_empty_one(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        _complete(config, "NO_ACTION:", task_id=task_id)
        (parts,) = _parts(db_path, private)
        assert parts == {"header": HEADER_ABSENT[:-1], "outcome": "No reply sent.",
                         "remark": ""}

    def test_a_parked_question_stores_its_parts(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        question = "Shall I tell Alice Friday works? Please confirm."
        _complete(config, question, task_id=task_id)
        (parts,) = _parts(db_path, private)
        assert parts == {"header": HEADER_ABSENT[:-1], "outcome": "Question for you.",
                         "remark": question}


# ---------------------------------------------------------------------------
# Pushes
# ---------------------------------------------------------------------------


class TestTheWebOnlyPush:
    def test_one_room_free_bell_row(self, config, db_path):
        from istota.notifications.store import ROOM_FREE_SURFACES

        task_id, _private = _absent_turn(config, db_path)
        _, push = _complete(config, "NO_ACTION:", task_id=task_id)

        (row,) = _rows(db_path, "SELECT title, body FROM notifications "
                       "WHERE source = 'task_alert' AND dedup_key = ?",
                       (f"private-note:{task_id}",))
        assert row["title"] == "Private note about Dinner plans"
        (call,) = [c for c in push.call_args_list
                   if c.kwargs.get("only_surfaces") == ROOM_FREE_SURFACES]
        assert call.args[1] == HOST


class TestTheDraftOpensTheNote:
    def test_the_held_drafts_bell_row_links_to_the_note(self, config, db_path):
        from istota.notifications import store
        from istota.notifications.sources import SAFE_PATH_RE

        task_id, private = _present_turn(config, db_path)
        config.users[HOST].trusted_email_senders = []
        _complete(config, "Friday works.", task_id=task_id, deferred_body="Friday works.")

        with db.get_db(db_path) as conn:
            rendered, _ = store.list_open(config, conn, HOST)
        (item,) = [r.to_dict() for r in rendered if r.to_dict()["source"] == "outbound_draft"]
        (link,) = [a for a in item["actions"] if a["method"] == "LINK"]
        assert link["href"] == f"/chat/r/{private}/t/{task_id}"
        assert SAFE_PATH_RE.match(link["href"])


class TestTheRemarkAndTheLink:
    @pytest.mark.parametrize(("result", "remark"), [
        ("NO_ACTION: Alice is only asking Carol.", "Alice is only asking Carol."),
        ("ACTION: replied to Ana.", "replied to Ana."),
        ("Done.\nNO_ACTION: nothing else.", "Done.\nnothing else."),
    ])
    def test_the_markers_are_stripped(self, result, remark):
        assert private_replies.email_note_remark(result, None) == remark

    def test_the_link_prefers_the_note_over_an_earlier_question(self, config, db_path):
        task_id, _private = _absent_turn(config, db_path)
        with db.get_db(db_path) as conn:
            asked = db.create_web_chat_room(conn, HOST, "Earlier").token
            noted = db.create_web_chat_room(conn, HOST, "Later").token
            db.add_message(conn, asked, role="system", body="q", origin_surface="web",
                           delivery_reference=f"private-confirmation:{task_id}:abc")
            db.add_message(conn, noted, role="system", body="n", origin_surface="web",
                           delivery_reference=f"private-pass_on:{task_id}:pass-on")
            assert private_replies.note_room_for_task(conn, task_id, HOST) == noted


# ---------------------------------------------------------------------------
# The step is recorded (#651)
# ---------------------------------------------------------------------------


def _step_lines(db_path, task_id):
    from istota.scheduler import EMAIL_NOTE_STEP_LOG

    rows = _rows(db_path, "SELECT message FROM task_logs WHERE task_id = ? ORDER BY id",
                 (task_id,))
    messages = [r["message"] for r in rows]
    return [m for m in messages if m.startswith(EMAIL_NOTE_STEP_LOG)], messages


class TestTheNoteStepIsRecorded:
    """A completed email task says its note step ran and what it decided, so
    "no note" can be asserted against a row rather than a log line or a timer."""

    def test_a_note_written_is_recorded_last(self, config, db_path):
        task_id, _private = _absent_turn(config, db_path)
        _complete(config, "NO_ACTION:", task_id=task_id)
        steps, messages = _step_lines(db_path, task_id)
        assert steps == ["Email note step: note written (none)"]
        assert messages[-1] == steps[0]

    def test_no_note_due_is_recorded(self, config, db_path):
        task_id, private = _present_turn(config, db_path)
        _complete(config, "Told them Friday works.", task_id=task_id,
                  deferred_body="Friday works.")
        assert _notes(db_path, private) == []
        steps, _ = _step_lines(db_path, task_id)
        assert steps == ["Email note step: no note due (sent)"]

    def test_a_failed_send_still_records_the_step(self, config, db_path):
        task_id, _private = _absent_turn(config, db_path)
        _complete(config, "Friday works.", task_id=task_id, deferred_body="Friday works.",
                  send_fails=True)
        steps, _ = _step_lines(db_path, task_id)
        assert steps == ["Email note step: note written (failed)"]

    def test_a_note_that_could_not_be_written_is_recorded(self, config, db_path):
        task_id, private = _absent_turn(config, db_path)
        with patch("istota.rooms.private_replies.deliver_email_note",
                   side_effect=RuntimeError("boom")):
            _complete(config, "NO_ACTION:", task_id=task_id)
        assert _notes(db_path, private) == []
        steps, _ = _step_lines(db_path, task_id)
        assert steps == ["Email note step: the note could not be written"]

    def test_a_parked_question_records_no_step(self, config, db_path):
        task_id, _private = _absent_turn(config, db_path)
        _complete(config, "Shall I tell Alice Friday works? Please confirm.",
                  task_id=task_id)
        steps, _ = _step_lines(db_path, task_id)
        assert steps == []

    def test_mail_outside_a_thread_records_that_no_note_applies(self, config, db_path):
        (task_id,) = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        _complete(config, "Nothing today.", task_id=task_id)
        steps, _ = _step_lines(db_path, task_id)
        assert steps == ["Email note step: not an email thread"]
