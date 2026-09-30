"""The inbound choke point records every turn, then asks the speech gate.

`record_inbound` stores the `role='user'` row first with no task, asks
`speech_gate.should_speak`, and creates a task only when the gate says speak,
stamping the row with its id. A turn the gate declines stays in the transcript
with `task_id IS NULL` and reaches later conversation context as an unanswered
turn. The four outcomes are named on `InboundResult`, so "echo dropped" and
"recorded, unanswered" are never the same `None`.
"""

import pytest

from istota import db
from istota.config import Config
from istota.speech_gate import GateDecision
from istota.transport import IncomingMessage, ingest_message
from istota.transport.ingest import InboundResult, record_inbound


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


def _user_rows(conn, token):
    return conn.execute(
        "SELECT id, task_id, body FROM messages "
        "WHERE room_token = ? AND role = 'user' ORDER BY id",
        (token,),
    ).fetchall()


def _decisions(conn, token):
    return conn.execute(
        "SELECT message_id, spoke, rung FROM speech_gate_decisions "
        "WHERE room_token = ? ORDER BY id",
        (token,),
    ).fetchall()


def _task_count(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _group_turn(conn, config, text="did you book the flights?", **kw):
    args = dict(
        surface="talk", surface_ref="grp", user_id="alice", text=text,
        is_group_chat=True, addressed_to_bot=False,
    )
    args.update(kw)
    return record_inbound(conn, config, **args)


class TestRecordThenDecide:
    def test_a_direct_turn_is_created_and_its_row_stamped(self, config, db_path):
        with db.get_db(db_path) as conn:
            result = record_inbound(
                conn, config, surface="talk", surface_ref="dm", user_id="alice",
                text="hi",
            )
        assert isinstance(result, InboundResult)
        assert result.outcome == "created"
        assert result.room_token == "dm"
        assert result.task_id is not None
        with db.get_db(db_path) as conn:
            rows = _user_rows(conn, "dm")
            assert [(r["id"], r["task_id"]) for r in rows] == [
                (result.message_id, result.task_id),
            ]
            assert [tuple(d) for d in _decisions(conn, "dm")] == [
                (result.message_id, 1, "not_multi_human"),
            ]

    def test_an_unaddressed_group_turn_is_recorded_without_a_task(
        self, config, db_path,
    ):
        with db.get_db(db_path) as conn:
            result = _group_turn(conn, config)
        assert result.outcome == "recorded"
        assert result.task_id is None
        assert result.message_id is not None
        assert result.gate_reason == "mode_mention"
        with db.get_db(db_path) as conn:
            rows = _user_rows(conn, "grp")
            assert [(r["id"], r["task_id"], r["body"]) for r in rows] == [
                (result.message_id, None, "did you book the flights?"),
            ]
            assert _task_count(conn) == 0
            assert [tuple(d) for d in _decisions(conn, "grp")] == [
                (result.message_id, 0, "mode_mention"),
            ]

    def test_an_addressed_group_turn_is_created(self, config, db_path):
        with db.get_db(db_path) as conn:
            result = _group_turn(conn, config, text="istota, book them",
                                 addressed_to_bot=True)
        assert result.outcome == "created"
        with db.get_db(db_path) as conn:
            rows = _user_rows(conn, "grp")
            assert [r["task_id"] for r in rows] == [result.task_id]
            assert [d["rung"] for d in _decisions(conn, "grp")] == ["addressed"]

    def test_an_unstated_address_keeps_todays_behaviour(self, config, db_path):
        # Until the surfaces state it, every caller has already dropped an
        # unaddressed turn before calling in, so the default is "addressed".
        with db.get_db(db_path) as conn:
            result = record_inbound(
                conn, config, surface="talk", surface_ref="grp", user_id="alice",
                text="@istota hi", is_group_chat=True,
            )
        assert result.outcome == "created"

    def test_mode_off_speaks_in_a_group(self, config, db_path):
        config.speech_gate.mode = "off"
        with db.get_db(db_path) as conn:
            result = _group_turn(conn, config)
        assert result.outcome == "created"

    def test_classifier_mode_with_no_completer_fails_closed(self, config, db_path):
        config.speech_gate.mode = "classifier"
        with db.get_db(db_path) as conn:
            result = _group_turn(conn, config)
        assert result.outcome == "recorded"
        assert result.gate_reason == "failed"
        with db.get_db(db_path) as conn:
            assert _task_count(conn) == 0

    def test_the_row_is_stored_before_the_gate_is_asked(
        self, config, db_path, monkeypatch,
    ):
        seen = {}

        def spy(**kwargs):
            seen["rows"] = [tuple(r) for r in _user_rows(conn, "grp")]
            return GateDecision(False, "mode_mention")

        monkeypatch.setattr("istota.transport.ingest.speech_gate.should_speak", spy)
        with db.get_db(db_path) as conn:
            result = _group_turn(conn, config)
        assert seen["rows"] == [
            (result.message_id, None, "did you book the flights?"),
        ]

    def test_a_recorded_turn_reaches_conversation_history(self, config, db_path):
        with db.get_db(db_path) as conn:
            _group_turn(conn, config, text="did you book the flights?")
            _group_turn(conn, config, text="not yet", user_id="bob")
            spoken = _group_turn(conn, config, text="istota, book them",
                                 addressed_to_bot=True)
            db.update_task_status(conn, spoken.task_id, "completed", result="Booked.")
            db.add_message(
                conn, "grp", role="assistant", body="Booked.",
                origin_surface="talk", task_id=spoken.task_id,
            )
            history = db.get_conversation_history(conn, "grp")
        assert [(m.prompt, m.result, m.user_id) for m in history] == [
            ("did you book the flights?", None, "alice"),
            ("not yet", None, "bob"),
            ("istota, book them", "Booked.", "alice"),
        ]


class TestNothingRecordedIsNeverDeclined:
    def test_a_turn_with_no_transcript_is_not_gated(self, config, db_path):
        # Email with no room stores no row; declining it would lose the message.
        with db.get_db(db_path) as conn:
            result = record_inbound(
                conn, config, surface="email", surface_ref="thread1",
                user_id="bob", text="hello", source_type="email",
                is_group_chat=True, addressed_to_bot=False,
            )
            assert _decisions(conn, "thread1") == []
        assert result.outcome == "created"
        assert result.message_id is None


class TestDroppedAndReplayed:
    def test_a_known_echo_is_dropped(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.add_room_binding(conn, "room1", "talk", "room1")
            mid = db.add_message(
                conn, "room1", role="assistant", body="bot reply",
                origin_surface="web", task_id=None,
            )
            db.set_message_external_id(conn, mid, "talk", "8888")
        with db.get_db(db_path) as conn:
            result = record_inbound(
                conn, config, surface="talk", surface_ref="room1",
                user_id="alice", text="bot reply", platform_message_id=8888,
                external_id="8888",
            )
            assert _user_rows(conn, "room1") == []
            assert _decisions(conn, "room1") == []
        assert (result.outcome, result.task_id, result.message_id) == (
            "dropped", None, None,
        )

    def test_a_client_replay_of_a_recorded_turn_writes_no_second_row(
        self, config, db_path,
    ):
        args = dict(
            surface="web", surface_ref="web-grp", user_id="alice", text="hey",
            source_type="web", is_group_chat=True, addressed_to_bot=False,
            client_msg_id="k1",
        )
        with db.get_db(db_path) as conn:
            first = record_inbound(conn, config, **args)
        with db.get_db(db_path) as conn:
            second = record_inbound(conn, config, **args)
            assert len(_user_rows(conn, "web-grp")) == 1
        assert first.outcome == "recorded"
        assert (second.outcome, second.task_id, second.message_id) == (
            "replayed", None, first.message_id,
        )

    def test_a_client_replay_of_a_created_turn_returns_its_task(
        self, config, db_path,
    ):
        args = dict(
            surface="web", surface_ref="web-dm", user_id="alice", text="hey",
            source_type="web", client_msg_id="k2",
        )
        with db.get_db(db_path) as conn:
            first = record_inbound(conn, config, **args)
        with db.get_db(db_path) as conn:
            second = record_inbound(conn, config, **args)
            assert _task_count(conn) == 1
        assert (second.outcome, second.task_id, second.message_id) == (
            "replayed", first.task_id, first.message_id,
        )

    def test_a_repolled_recorded_talk_turn_writes_no_second_row(
        self, config, db_path,
    ):
        with db.get_db(db_path) as conn:
            first = _group_turn(conn, config, platform_message_id=41,
                                external_id="41")
        with db.get_db(db_path) as conn:
            second = _group_turn(conn, config, platform_message_id=41,
                                 external_id="41")
            assert len(_user_rows(conn, "grp")) == 1
            assert len(_decisions(conn, "grp")) == 1
        assert (second.outcome, second.message_id) == ("replayed", first.message_id)

    def test_a_repolled_answered_talk_turn_returns_its_task(self, config, db_path):
        with db.get_db(db_path) as conn:
            first = record_inbound(
                conn, config, surface="talk", surface_ref="room2",
                user_id="alice", text="hi", platform_message_id=555,
            )
        with db.get_db(db_path) as conn:
            second = record_inbound(
                conn, config, surface="talk", surface_ref="room2",
                user_id="alice", text="hi", platform_message_id=555,
            )
            assert len(_user_rows(conn, "room2")) == 1
        assert (second.outcome, second.task_id) == ("replayed", first.task_id)


class TestIngestMessage:
    def test_returns_the_task_id(self, config, db_path):
        msg = IncomingMessage(
            user_id="alice", text="hello", source_type="talk",
            surface="talk", channel_token="roomZ", platform_message_id=7,
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)
            assert db.get_task(conn, task_id).conversation_token == "roomZ"
