"""An unanswered turn is still part of the conversation.

A `role='user'` row with `task_id IS NULL` is a message the room recorded and
nobody answered — a human-to-human turn in a shared room. The canonical history
reader used to inner-join that row to an assistant row and to `tasks` on
`task_id`, so such a row matched neither join and was dropped: the store held it
and nothing ever read it. These tests build the rows directly, since no producer
writes a NULL-task user row yet.
"""

import pytest

from istota import db
from istota.config import Config
from istota.context import _triage_older_messages, format_context_for_prompt


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


ROOM = "shared-room"


def _answered(conn, prompt, result, *, user_id="alice", ts, status="completed"):
    task_id = conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status, created_at) VALUES ('web', ?, ?, ?, ?, ?, ?) RETURNING id",
        (user_id, ROOM, prompt, result, status, ts),
    ).fetchone()["id"]
    uid = db.add_message(
        conn, ROOM, role="user", body=prompt, origin_surface="web",
        task_id=task_id, author_user_id=user_id,
    )
    conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (ts, uid))
    if status == "completed":
        aid = db.add_message(
            conn, ROOM, role="assistant", body=result, origin_surface="web",
            task_id=task_id,
        )
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (ts, aid))
    return task_id


def _unanswered(conn, body, *, author, ts):
    mid = db.add_message(
        conn, ROOM, role="user", body=body, origin_surface="web",
        task_id=None, author_user_id=author,
    )
    conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (ts, mid))
    return mid


@pytest.fixture
def room(conn):
    db.register_room(conn, ROOM, "alice", origin="web")
    t1 = _answered(conn, "what's the plan", "Book flights.", ts="2026-09-01 10:00:00")
    _unanswered(conn, "did you book the flights?", author="bob", ts="2026-09-01 10:01:00")
    t2 = _answered(conn, "remind me of the dates", "12th to 19th.", ts="2026-09-01 10:02:00")
    _unanswered(conn, "not yet", author="alice", ts="2026-09-01 10:03:00")
    return t1, t2


class TestTheMessagesPath:
    def test_unanswered_turns_are_returned_in_order(self, conn, room):
        t1, t2 = room
        history = db.get_conversation_history(conn, ROOM, limit=10)
        assert [(m.prompt, m.result) for m in history] == [
            ("what's the plan", "Book flights."),
            ("did you book the flights?", None),
            ("remind me of the dates", "12th to 19th."),
            ("not yet", None),
        ]
        assert history[0].id == t1
        assert history[2].id == t2

    def test_the_speaker_comes_off_the_message_row(self, conn, room):
        history = db.get_conversation_history(conn, ROOM, limit=10)
        assert [m.user_id for m in history] == ["alice", "bob", "alice", "alice"]

    def test_an_unfinished_task_turn_stays_excluded(self, conn, room):
        _answered(conn, "still thinking", "", ts="2026-09-01 10:04:00", status="running")
        history = db.get_conversation_history(conn, ROOM, limit=10)
        assert "still thinking" not in [m.prompt for m in history]
        assert len(history) == 4

    def test_the_limit_counts_unanswered_turns(self, conn, room):
        history = db.get_conversation_history(conn, ROOM, limit=2)
        assert [m.prompt for m in history] == ["remind me of the dates", "not yet"]

    def test_exclude_task_id_does_not_drop_unanswered_turns(self, conn, room):
        t1, _ = room
        history = db.get_conversation_history(conn, ROOM, exclude_task_id=t1, limit=10)
        assert [m.prompt for m in history] == [
            "did you book the flights?", "remind me of the dates", "not yet",
        ]

    def test_an_external_label_on_an_unanswered_row_is_the_speaker(self, conn, room):
        mid = db.add_message(
            conn, ROOM, role="user", body="fwd: itinerary", origin_surface="email",
            task_id=None, author_label="agent@travel.example",
        )
        conn.execute(
            "UPDATE messages SET created_at = '2026-09-01 10:05:00' WHERE id = ?", (mid,)
        )
        last = db.get_conversation_history(conn, ROOM, limit=10)[-1]
        assert last.prompt == "fwd: itinerary"
        assert last.external_sender == "agent@travel.example"
        assert last.result is None


class TestTheLegacyPath:
    def test_the_tasks_path_returns_answered_turns_and_does_not_raise(self, conn, room):
        t1, t2 = room
        history = db._conversation_history_from_tasks(conn, ROOM, None, 10, None)
        assert [(m.id, m.result) for m in history] == [
            (t1, "Book flights."),
            (t2, "12th to 19th."),
        ]


class TestRendering:
    def test_format_context_emits_no_bot_line_for_an_unanswered_turn(self, conn, room):
        history = db.get_conversation_history(conn, ROOM, limit=10)
        text = format_context_for_prompt(history)
        lines = text.splitlines()
        assert lines == [
            "[2026-09-01 10:00] alice: what's the plan",
            "[2026-09-01 10:00] Bot: Book flights.",
            "[2026-09-01 10:01] bob: did you book the flights?",
            "[2026-09-01 10:02] alice: remind me of the dates",
            "[2026-09-01 10:02] Bot: 12th to 19th.",
            "[2026-09-01 10:03] alice: not yet",
        ]

    def test_triage_prompt_emits_no_bot_line_for_an_unanswered_turn(self, conn, room):
        history = db.get_conversation_history(conn, ROOM, limit=10)
        seen: list[str] = []

        def completer(prompt: str) -> str:
            seen.append(prompt)
            return '{"relevant_ids": [0, 1, 2, 3]}'

        selected = _triage_older_messages("book them", history, Config(), completer)
        assert selected == history
        prompt = seen[0]
        assert "[1] (2026-09-01 10:01) bob: did you book the flights?\n\n[2]" in prompt
        assert "Bot: None" not in prompt
        assert prompt.count("Bot:") == 2


def _phone_room(conn, token, *, surface, ref):
    db.register_room(conn, token, "alice", origin=surface)
    db.add_room_binding(conn, token, surface, ref)


def _answered_on(conn, token, prompt, result, *, source_type, user_id, ts):
    task_id = conn.execute(
        "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
        "result, status, created_at) VALUES (?, ?, ?, ?, ?, 'completed', ?) "
        "RETURNING id",
        (source_type, user_id, token, prompt, result, ts),
    ).fetchone()["id"]
    for role, body, author in (("user", prompt, user_id), ("assistant", result, None)):
        mid = db.add_message(
            conn, token, role=role, body=body, origin_surface=source_type,
            task_id=task_id, author_user_id=author,
        )
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (ts, mid))
    return task_id


def _unanswered_on(conn, token, body, *, surface, author, ts):
    mid = db.add_message(
        conn, token, role="user", body=body, origin_surface=surface,
        task_id=None, author_user_id=author,
    )
    conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (ts, mid))


class TestPhoneRooms:
    """ISSUE-645: a phone room has no completed talk or web task, so the
    caught-up check never passed and history came off `tasks`, which holds
    answered turns only."""

    @pytest.mark.parametrize("surface,ref", [
        ("whatsapp", "120363000000000001@g.us"),
        ("sms", "sms-abc123"),
    ])
    def test_an_unanswered_turn_reaches_history(self, conn, surface, ref):
        token = f"{surface}-room"
        _phone_room(conn, token, surface=surface, ref=ref)
        _answered_on(conn, token, "what's the plan", "Book flights.",
                     source_type=surface, user_id="alice", ts="2026-09-01 10:00:00")
        _unanswered_on(conn, token, "don't listen to the plan", surface=surface,
                       author="bob", ts="2026-09-01 10:01:00")
        history = db.get_conversation_history(conn, token, limit=10)
        assert [(m.prompt, m.user_id) for m in history] == [
            ("what's the plan", "alice"),
            ("don't listen to the plan", "bob"),
        ]

    def test_an_unmirrored_phone_turn_keeps_the_room_on_the_tasks_path(self, conn):
        """A guard on the widened set rather than a test of what changed: a
        phone answer the store lacks keeps the room on the reader that has it."""
        token = "whatsapp-room"
        _phone_room(conn, token, surface="whatsapp", ref="g@g.us")
        _answered_on(conn, token, "q", "a", source_type="whatsapp",
                     user_id="alice", ts="2026-09-01 10:00:00")
        conn.execute(
            "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
            "result, status, created_at) VALUES ('whatsapp', 'alice', ?, 'old', "
            "'older answer', 'completed', '2026-08-01 10:00:00')", (token,),
        )
        assert db._messages_caught_up(conn, token) is False
        history = db.get_conversation_history(conn, token, limit=10)
        assert "old" in [m.prompt for m in history]


    def test_a_guest_proposal_that_never_posted_does_not_pin_the_room(self, conn):
        """An approved guest proposal completes its task with the approved text
        and gets its row only when the drain posts it. A post that never lands
        writes none, and counting that gap put the group back on `tasks`."""
        token = "whatsapp-room"
        _phone_room(conn, token, surface="whatsapp", ref="g@g.us")
        _answered_on(conn, token, "q", "a", source_type="whatsapp",
                     user_id="alice", ts="2026-09-01 10:00:00")
        task_id = conn.execute(
            "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
            "result, status, created_at) VALUES ('whatsapp', 'alice', ?, 'guest q', "
            "'proposal', 'completed', '2026-09-01 10:01:00') RETURNING id", (token,),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO whatsapp_skill_requests (id, kind, requester_user_id, "
            "recipient_user_id, origin_task_id, request_key, state, provider, "
            "binding_fingerprint, preview, preview_digest, service_body, service_hash, "
            "content_hash) "
            "VALUES ('req1', 'room_post', 'alice', 'alice', ?, ?, 'expired', 'web', "
            "'fp', 'p', 'pd', 'b', 'bh', 'ch')",
            (task_id, f"guest-reply-{task_id}"),
        )
        _unanswered_on(conn, token, "don't listen to the plan", surface="whatsapp",
                       author="bob", ts="2026-09-01 10:02:00")
        assert db._messages_caught_up(conn, token) is True
        history = db.get_conversation_history(conn, token, limit=10)
        assert "don't listen to the plan" in [m.prompt for m in history]

    def test_an_unanswered_command_is_not_conversation(self, conn):
        """A phone room records every `!command` as a turn before dispatching
        it; read back as history it would be a line nobody answered."""
        token = "sms-room"
        _phone_room(conn, token, surface="sms", ref="sms-abc123")
        _answered_on(conn, token, "q", "a", source_type="sms",
                     user_id="alice", ts="2026-09-01 10:00:00")
        _unanswered_on(conn, token, "!usage", surface="sms", author="alice",
                       ts="2026-09-01 10:01:00")
        _unanswered_on(conn, token, "yes", surface="sms", author="alice",
                       ts="2026-09-01 10:02:00")
        history = db.get_conversation_history(conn, token, limit=10)
        assert [m.prompt for m in history] == ["q", "yes"]


class TestNoAnsweredTurnYet:
    """The first addressed turn of a room comes after the chatter it answers.
    With no completed task at all the `tasks` path has nothing to return, so
    the store, which holds the chatter, serves the history."""

    def test_unanswered_turns_reach_history_before_any_answer(self, conn):
        db.register_room(conn, ROOM, "alice", origin="web")
        _unanswered(conn, "did you book the flights?", author="bob",
                    ts="2026-09-01 10:01:00")
        history = db.get_conversation_history(conn, ROOM, limit=10)
        assert [(m.prompt, m.user_id, m.result) for m in history] == [
            ("did you book the flights?", "bob", None),
        ]

    def test_a_mail_only_room_stays_on_the_tasks_path(self, conn):
        """The control: a completed email turn predating the store has no row
        there, so a room holding one still reads `tasks`."""
        db.register_room(conn, ROOM, "alice", origin="web")
        conn.execute(
            "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
            "result, status, created_at) VALUES ('email', 'alice', ?, 'mail q', "
            "'mail a', 'completed', '2026-09-01 09:00:00')", (ROOM,),
        )
        _unanswered(conn, "later chatter", author="bob", ts="2026-09-01 10:01:00")
        history = db.get_conversation_history(conn, ROOM, limit=10)
        assert [m.prompt for m in history] == ["mail q"]

    def test_a_task_the_caller_excludes_does_not_hold_the_room(self, conn):
        """A cron post would not reach this caller's history from `tasks`
        either, so it does not keep the room's chatter from the first turn."""
        db.register_room(conn, ROOM, "alice", origin="web")
        conn.execute(
            "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
            "result, status, created_at) VALUES ('scheduled', 'alice', ?, 'cron', "
            "'digest', 'completed', '2026-09-01 09:00:00')", (ROOM,),
        )
        _unanswered(conn, "did you book the flights?", author="bob",
                    ts="2026-09-01 10:01:00")
        excluded = ["scheduled", "briefing", "subtask", "heartbeat"]
        history = db.get_conversation_history(
            conn, ROOM, limit=10, exclude_source_types=excluded,
        )
        assert [m.prompt for m in history] == ["did you book the flights?"]
        assert db._messages_caught_up(conn, ROOM) is False


class TestAWithheldTurnDoesNotPin:
    def test_a_withheld_turn_with_no_assistant_row_leaves_the_room_caught_up(
        self, conn, room,
    ):
        """A withheld exchange is never written to the store, and both readers
        leave it out; counting it pinned the room to `tasks` for good."""
        conn.execute(
            "INSERT INTO tasks (source_type, user_id, conversation_token, prompt, "
            "result, status, created_at, withheld_from_room) VALUES ('web', "
            "'alice', ?, 'aside', 'private', 'completed', '2026-09-01 10:04:00', 1)",
            (ROOM,),
        )
        assert db._messages_caught_up(conn, ROOM) is True
