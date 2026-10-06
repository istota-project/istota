"""ISSUE-653: the speech gate's disposition, the structural facts above the
window, and the `ack` kind a friendly room's verdict may carry.

The same fixed conversation windows are built under both dispositions. A
model is not run here, so "speaks" means the prompt states the case the
window is an instance of, and the structural facts above it say what the
window shows; the classifier's answer is scripted where the path past it is
under test.
"""

import logging
import sqlite3
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, SpeechGateConfig, load_config
from istota.rooms import speech_gate
from istota.rooms.speech_gate import (
    FRIENDLY,
    KIND_ACK,
    KIND_REPLY,
    RESERVED,
    GateDecision,
    WindowTurn,
    build_window,
    classify,
    normalize_disposition,
    parse_decision,
    record_decision,
)
from istota.transport import IncomingMessage, classify_ahead, ingest_message


@pytest.fixture(autouse=True)
def _fresh_warning_latch():
    speech_gate._warned_dispositions.clear()
    yield
    speech_gate._warned_dispositions.clear()


def _human(author, text):
    return WindowTurn(author=author, text=text, is_bot=False)


def _bot(text):
    return WindowTurn(author="Istota (assistant)", text=text, is_bot=True,
                      asks=text.endswith("?"))


#: The bot answered alice, and alice thanks it.
THANKS_AFTER_ANSWER = [
    _human("alice", "what time does the pharmacy close?"),
    _bot("It closes at 6pm today."),
    _human("alice", "Thanks!"),
]
#: Two people talking, with the bot's last answer further back.
TALKING_PAST = [
    _human("alice", "what time does the pharmacy close?"),
    _bot("It closes at 6pm today."),
    _human("bob", "are you going after work?"),
    _human("alice", "yes, around five"),
]
#: Someone else reacts to the bot's answer to alice.
SOMEONE_ELSE_AFTER_ANSWER = [
    _human("alice", "what time does the pharmacy close?"),
    _bot("It closes at 6pm today."),
    _human("bob", "I'll pick you up at five then"),
]
#: The bot is named in passing.
PASSING_MENTION = [
    _human("bob", "Istota found the pharmacy hours earlier"),
    _human("alice", "nice, Istota is handy for that"),
]


def _facts(prompt):
    return {
        "before": "wrote the message just before the newest one: yes" in prompt,
        "asker": "the person Istota last answered: yes" in prompt,
    }


class TestTheStructuralFacts:
    """Stated above the window, under both dispositions, from the turns alone."""

    @pytest.mark.parametrize("disposition", [RESERVED, FRIENDLY])
    def test_thanks_right_after_the_answer(self, disposition):
        prompt = build_window(THANKS_AFTER_ANSWER, bot_name="Istota",
                              disposition=disposition)
        assert _facts(prompt) == {"before": True, "asker": True}

    @pytest.mark.parametrize("disposition", [RESERVED, FRIENDLY])
    def test_people_talking_past_the_bot(self, disposition):
        prompt = build_window(TALKING_PAST, bot_name="Istota",
                              disposition=disposition)
        # alice is the person last answered, but two turns came between.
        assert _facts(prompt) == {"before": False, "asker": True}

    def test_someone_else_after_the_answer(self):
        prompt = build_window(SOMEONE_ELSE_AFTER_ANSWER, bot_name="Istota")
        assert _facts(prompt) == {"before": True, "asker": False}

    def test_a_passing_mention_with_no_bot_turn(self):
        prompt = build_window(PASSING_MENTION, bot_name="Istota")
        assert _facts(prompt) == {"before": False, "asker": False}

    def test_an_empty_window_states_no(self):
        prompt = build_window([], bot_name="Istota")
        assert _facts(prompt) == {"before": False, "asker": False}

    def test_a_bot_answer_with_no_human_before_it_answered_nobody(self):
        turns = [_bot("Morning, the weekly summary is up."), _human("alice", "thanks")]
        assert _facts(build_window(turns, bot_name="Istota")) == {
            "before": True, "asker": False,
        }


class TestTheTwoPrompts:
    """One builder, the disposition a parameter, and the default `reserved`."""

    def test_reserved_is_the_default_and_keeps_the_three_cases(self):
        default = build_window(THANKS_AFTER_ANSWER, bot_name="Istota")
        reserved = build_window(THANKS_AFTER_ANSWER, bot_name="Istota",
                                disposition=RESERVED)
        assert default == reserved
        assert "Reply only when the newest message is addressed to Istota" in reserved
        assert "thanks" not in reserved.split("[UNTRUSTED")[0].lower()
        assert '"kind"' not in reserved

    def test_friendly_adds_the_reaction_cases_and_the_kind(self):
        prompt = build_window(THANKS_AFTER_ANSWER, bot_name="Istota",
                              disposition=FRIENDLY)
        instructions = prompt.split("[UNTRUSTED")[0]
        assert "reacts to what Istota just said" in instructions
        assert "thanks" in instructions.lower()
        assert "for example" in instructions.lower()
        assert '"kind": "reply" or "ack"' in prompt

    @pytest.mark.parametrize("disposition", [RESERVED, FRIENDLY])
    def test_both_keep_people_talking_to_each_other_silent(self, disposition):
        prompt = build_window(TALKING_PAST, bot_name="Istota",
                              disposition=disposition)
        assert "Do not reply when people are talking to each other" in prompt
        assert "mention Istota in passing" in prompt

    def test_an_unknown_disposition_builds_the_reserved_prompt(self):
        assert build_window(THANKS_AFTER_ANSWER, bot_name="Istota",
                            disposition="chatty") == build_window(
            THANKS_AFTER_ANSWER, bot_name="Istota")


class TestNormalize:
    @pytest.mark.parametrize("value,expected", [
        ("reserved", RESERVED), ("friendly", FRIENDLY), (" Friendly ", FRIENDLY),
    ])
    def test_known_values(self, value, expected):
        assert normalize_disposition(value) == expected

    @pytest.mark.parametrize("value", ["chatty", "", None, 3])
    def test_an_unknown_value_fails_closed_to_reserved_with_a_warning(
        self, value, caplog,
    ):
        with caplog.at_level(logging.WARNING, logger="istota.rooms.speech_gate"):
            assert normalize_disposition(value) == RESERVED
        assert "disposition" in caplog.text

    def test_the_quiet_form_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.rooms.speech_gate"):
            assert normalize_disposition("chatty", warn=False) == RESERVED
        assert caplog.text == ""


class TestTheKind:
    def test_the_parser_reads_ack(self):
        assert parse_decision('{"speak": true, "kind": "ack"}').kind == KIND_ACK

    @pytest.mark.parametrize("raw", [
        '{"speak": true}', '{"speak": true, "kind": "loud"}',
        '{"speak": true, "kind": 1}', '{"speak": true, "kind": "reply"}',
    ])
    def test_anything_else_is_a_reply(self, raw):
        assert parse_decision(raw).kind == KIND_REPLY

    def test_friendly_keeps_an_ack(self):
        decision = classify("w", lambda _p: '{"speak": true, "kind": "ack"}',
                            "fast", disposition=FRIENDLY)
        assert (decision.speak, decision.kind) == (True, KIND_ACK)

    def test_reserved_never_carries_an_ack(self):
        decision = classify("w", lambda _p: '{"speak": true, "kind": "ack"}',
                            "fast", disposition=RESERVED)
        assert (decision.speak, decision.kind) == (True, KIND_REPLY)

    def test_a_silent_verdict_is_never_an_ack(self):
        decision = classify("w", lambda _p: '{"speak": false, "kind": "ack"}',
                            "fast", disposition=FRIENDLY)
        assert (decision.speak, decision.kind) == (False, KIND_REPLY)


class TestTheAuditRow:
    def test_the_disposition_and_kind_are_recorded(self, db_conn):
        row_id = record_decision(
            db_conn, room_token="r", surface="talk", user_id="alice",
            message_id=1, decision=GateDecision(True, "classifier", kind=KIND_ACK),
            disposition=FRIENDLY,
        )
        row = db_conn.execute(
            "SELECT disposition, kind FROM speech_gate_decisions WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert (row["disposition"], row["kind"]) == (FRIENDLY, KIND_ACK)

    def test_a_silent_row_has_no_kind(self, db_conn):
        row_id = record_decision(
            db_conn, room_token="r", surface="talk", user_id="alice",
            message_id=1, decision=GateDecision(False, "mode_mention"),
            disposition=RESERVED,
        )
        row = db_conn.execute(
            "SELECT disposition, kind FROM speech_gate_decisions WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert (row["disposition"], row["kind"]) == (RESERVED, None)

    def test_an_upgraded_table_gains_both_columns(self, tmp_path):
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE speech_gate_decisions (id INTEGER PRIMARY KEY, "
            "room_token TEXT NOT NULL, surface TEXT NOT NULL, user_id TEXT NOT NULL, "
            "message_id INTEGER, spoke INTEGER NOT NULL, rung TEXT NOT NULL, "
            "reason TEXT, model TEXT, latency_ms INTEGER, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO speech_gate_decisions (room_token, surface, user_id, spoke, "
            "rung) VALUES ('r', 'talk', 'alice', 1, 'addressed')"
        )
        conn.commit()
        conn.close()
        db.init_db(path)
        with db.get_db(path) as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(speech_gate_decisions)")}
            old = conn.execute(
                "SELECT disposition, kind FROM speech_gate_decisions").fetchone()
        assert {"disposition", "kind"} <= cols
        assert tuple(old) == (None, None)


class TestConfig:
    def test_the_default_is_friendly(self):
        assert SpeechGateConfig().disposition == "friendly"

    def test_the_key_is_read(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[speech_gate]\nmode = "classifier"\ndisposition = "friendly"\n')
        assert load_config(p).speech_gate.disposition == "friendly"


# --- through the ingest path ----------------------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir()
    db.init_db(cfg.db_path)
    cfg.bot_name = "Istota"
    cfg.speech_gate.mode = "classifier"
    return cfg


def _seed(config, token="grp"):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "alice", origin="talk", name="Family")
        db.add_message(conn, token, role="user", body="pharmacy hours?",
                       origin_surface="talk", author_user_id="alice")
        db.add_message(conn, token, role="assistant",
                       body="It closes at 6pm today.", origin_surface="talk")


def _ask_and_ingest(config, answer, text="Thanks!", token="grp"):
    prompts = []

    def completer(prompt):
        prompts.append(prompt)
        return answer

    with patch("istota.executor.build_speech_gate_completer",
               return_value=completer):
        classified = classify_ahead(
            config, surface="talk", surface_ref=token, user_id="alice",
            text=text, is_group_chat=True, addressed_to_bot=False,
        )
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        task_id = ingest_message(conn, config, IncomingMessage(
            user_id="alice", text=text, source_type="talk", surface="talk",
            channel_token=token, is_group_chat=True, addressed_to_bot=False,
            platform_message_id=601, classified=classified,
        ))
        row = conn.execute(
            "SELECT spoke, disposition, kind FROM speech_gate_decisions "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return task_id, dict(row), prompts


class TestThroughTheIngestPath:
    def test_friendly_sends_the_friendly_prompt_and_records_the_ack(self, config):
        config.speech_gate.disposition = "friendly"
        _seed(config)
        task_id, row, prompts = _ask_and_ingest(
            config, '{"speak": true, "kind": "ack", "reason": "thanks"}',
        )
        assert task_id is not None
        assert row == {"spoke": 1, "disposition": "friendly", "kind": "ack"}
        assert "reacts to what Istota just said" in prompts[0]
        assert "the person Istota last answered: yes" in prompts[0]

    def test_reserved_sends_the_reserved_prompt(self, config):
        config.speech_gate.disposition = "reserved"
        _seed(config)
        task_id, row, prompts = _ask_and_ingest(
            config, '{"speak": false, "reason": "not addressed"}',
        )
        assert task_id is None
        assert row == {"spoke": 0, "disposition": "reserved", "kind": None}
        assert "reacts to what Istota just said" not in prompts[0]

    def test_an_unknown_setting_is_recorded_as_reserved(self, config):
        config.speech_gate.disposition = "chatty"
        _seed(config)
        _task, row, prompts = _ask_and_ingest(config, '{"speak": false}')
        assert row["disposition"] == "reserved"
        assert "reacts to what Istota just said" not in prompts[0]


class TestTheRoomsOwnDisposition:
    """ISSUE-654: a room's own setting outranks the deployment's."""

    def test_a_friendly_room_on_a_reserved_deployment(self, config):
        from istota.rooms import policy as room_policy

        _seed(config)
        with db.get_db(config.db_path) as conn:
            room_policy.set_disposition(conn, "grp", "friendly")
        task_id, row, prompts = _ask_and_ingest(
            config, '{"speak": true, "kind": "ack", "reason": "thanks"}',
        )
        assert task_id is not None
        assert row == {"spoke": 1, "disposition": "friendly", "kind": "ack"}
        assert "reacts to what Istota just said" in prompts[0]

    def test_a_reserved_room_on_a_friendly_deployment(self, config):
        from istota.rooms import policy as room_policy

        config.speech_gate.disposition = "friendly"
        _seed(config)
        with db.get_db(config.db_path) as conn:
            room_policy.set_disposition(conn, "grp", "reserved")
        _task, row, prompts = _ask_and_ingest(config, '{"speak": false}')
        assert row["disposition"] == "reserved"
        assert "reacts to what Istota just said" not in prompts[0]

    def test_a_reply_in_a_friendly_room_is_classified(self, config):
        from istota.rooms import policy as room_policy

        _seed(config)
        with db.get_db(config.db_path) as conn:
            room_policy.set_disposition(conn, "grp", "friendly")
        classified, prompts = TestAReplyToTheBot()._classify_reply(config)
        assert len(prompts) == 1 and classified.kind == KIND_ACK
        assert classified.disposition == "friendly"

    def test_a_reply_in_a_reserved_room_asks_nothing(self, config):
        from istota.rooms import policy as room_policy

        config.speech_gate.disposition = "friendly"
        _seed(config)
        with db.get_db(config.db_path) as conn:
            room_policy.set_disposition(conn, "grp", "reserved")
        classified, prompts = TestAReplyToTheBot()._classify_reply(config)
        assert (classified, prompts) == (None, [])

    def test_the_row_records_the_disposition_the_model_was_asked_under(self, config):
        """A host switching the room mid-call does not relabel the decision."""
        from istota.rooms import policy as room_policy

        _seed(config)
        with db.get_db(config.db_path) as conn:
            room_policy.set_disposition(conn, "grp", "friendly")

        def completer(prompt):
            with db.get_db(config.db_path) as conn:
                room_policy.set_disposition(conn, "grp", "reserved")
            return '{"speak": true, "kind": "ack"}'

        with patch("istota.executor.build_speech_gate_completer",
                   return_value=completer):
            classified = classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="Thanks!", is_group_chat=True, addressed_to_bot=False,
            )
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            ingest_message(conn, config, IncomingMessage(
                user_id="alice", text="Thanks!", source_type="talk", surface="talk",
                channel_token="grp", is_group_chat=True, addressed_to_bot=False,
                platform_message_id=603, classified=classified,
            ))
            row = conn.execute(
                "SELECT disposition, kind FROM speech_gate_decisions "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert tuple(row) == ("friendly", "ack")


class TestTheAckReachesTheTask:
    """A task the gate created as an `ack` is told to keep it to one line."""

    def _card(self, config, task_id):
        from istota.executor import room_card

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            return room_card(config, task, conn, withheld_scopes=None,
                             room_cli_available=True)

    def test_an_ack_task_gets_the_short_line(self, config):
        config.speech_gate.disposition = "friendly"
        _seed(config)
        task_id, _row, _p = _ask_and_ingest(
            config, '{"speak": true, "kind": "ack"}',
        )
        card = self._card(config, task_id)
        assert speech_gate.ACK_TASK_LINE in card

    def test_a_reply_task_does_not(self, config):
        config.speech_gate.disposition = "friendly"
        _seed(config)
        task_id, _row, _p = _ask_and_ingest(
            config, '{"speak": true, "kind": "reply"}',
            text="and on Sunday?",
        )
        assert speech_gate.ACK_TASK_LINE not in self._card(config, task_id)

    def test_reply_kind_for_task_reads_the_decision(self, config):
        config.speech_gate.disposition = "friendly"
        _seed(config)
        task_id, _row, _p = _ask_and_ingest(
            config, '{"speak": true, "kind": "ack"}',
        )
        with db.get_db(config.db_path) as conn:
            assert speech_gate.reply_kind_for_task(conn, task_id) == KIND_ACK
            assert speech_gate.reply_kind_for_task(conn, 99999) is None


class TestAReplyToTheBot:
    """A reply to the bot always speaks; in a friendly room it still gets a kind."""

    def test_the_ladder_takes_an_ack_from_a_classified_reply(self):
        classified = GateDecision(True, speech_gate.RUNG_CLASSIFIER, kind=KIND_ACK,
                                  model="fast")
        decision = speech_gate.should_speak(
            is_multi_human=True, addressed_to_bot=True, mode="mention",
            classified=classified,
        )
        assert (decision.speak, decision.rung, decision.kind) == (
            True, speech_gate.RUNG_ADDRESSED, KIND_ACK)

    def test_a_silent_verdict_cannot_silence_a_reply(self):
        classified = GateDecision(False, speech_gate.RUNG_CLASSIFIER)
        decision = speech_gate.should_speak(
            is_multi_human=True, addressed_to_bot=True, mode="classifier",
            classified=classified,
        )
        assert (decision.speak, decision.kind) == (True, KIND_REPLY)

    def test_a_failed_classification_is_a_plain_reply(self):
        classified = GateDecision(False, speech_gate.RUNG_FAILED)
        decision = speech_gate.should_speak(
            is_multi_human=True, addressed_to_bot=True, mode="classifier",
            classified=classified,
        )
        assert (decision.speak, decision.rung, decision.kind) == (
            True, speech_gate.RUNG_ADDRESSED, KIND_REPLY)

    def _classify_reply(self, config):
        prompts = []

        def completer(prompt):
            prompts.append(prompt)
            return '{"speak": true, "kind": "ack"}'

        with patch("istota.executor.build_speech_gate_completer",
                   return_value=completer):
            classified = classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="Thanks!", is_group_chat=True, addressed_to_bot=False,
                replied_to_bot=True,
            )
        return classified, prompts

    def test_friendly_asks_and_the_task_gets_the_short_line(self, config):
        config.speech_gate.disposition = "friendly"
        _seed(config)
        classified, prompts = self._classify_reply(config)
        assert len(prompts) == 1
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_id = ingest_message(conn, config, IncomingMessage(
                user_id="alice", text="Thanks!", source_type="talk", surface="talk",
                channel_token="grp", is_group_chat=True, addressed_to_bot=True,
                platform_message_id=602, classified=classified,
            ))
            row = conn.execute(
                "SELECT rung, kind FROM speech_gate_decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert tuple(row) == ("addressed", "ack")
        assert speech_gate.ACK_TASK_LINE in TestTheAckReachesTheTask()._card(
            config, task_id)

    def test_reserved_asks_nothing(self, config):
        config.speech_gate.disposition = "reserved"
        _seed(config)
        classified, prompts = self._classify_reply(config)
        assert (classified, prompts) == (None, [])


class TestSomebodyElsesWords:
    @pytest.mark.parametrize("reference,own", [
        (None, True), ("", True), ("istota:task:5:result", True),
        ("istota:task:5:prompt", False), ("room-post:abc", False),
    ])
    def test_is_bots_own_words(self, reference, own):
        assert speech_gate.is_bots_own_words(reference) is own

    def test_a_room_post_row_is_not_the_bots(self, db_conn):
        db.register_room(db_conn, "r1", "alice", origin="web")
        post = db.add_message(db_conn, "r1", role="assistant", body="from alice",
                              origin_surface="web", delivery_reference="room-post:x")
        answer = db.add_message(db_conn, "r1", role="assistant", body="6pm",
                                origin_surface="web")
        person = db.add_message(db_conn, "r1", role="user", body="hi",
                                origin_surface="web", author_user_id="bob")
        assert db.is_bot_message_in_room(db_conn, "r1", answer) is True
        assert db.is_bot_message_in_room(db_conn, "r1", post) is False
        assert db.is_bot_message_in_room(db_conn, "r1", person) is False
        assert db.is_bot_message_in_room(db_conn, "other", answer) is False


class TestTheWarningIsOnce:
    def test_one_warning_per_value(self, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.rooms.speech_gate"):
            normalize_disposition("once-only")
            normalize_disposition("once-only")
        assert caplog.text.count("once-only") == 1
