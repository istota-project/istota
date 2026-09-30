"""SG 5: `addressed_to_bot` is a field every surface sets, and unset means no.

Stage 4 defaulted it to True because every surface still dropped an
unaddressed group turn before calling in. Talk no longer does, so a caller that
says nothing is now saying "not addressed". What keeps that from muting a
direct conversation is the gate's first rung, not this default.
"""

import pytest

from istota import db, message_relays
from istota.config import Config, TalkConfig
from istota.speech_gate import GateDecision
from istota.transport import IncomingMessage, ingest_message
from istota.transport.ingest import record_inbound
from istota.transport.web import addressed_to_bot_in_text


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    cfg.bot_name = "Nova"
    cfg.talk = TalkConfig(enabled=True, bot_username="nova-bot")
    return cfg


@pytest.fixture
def gate_calls(monkeypatch):
    """Every `should_speak` call's keyword arguments; the gate always speaks."""
    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return GateDecision(True, "addressed")

    monkeypatch.setattr("istota.transport.ingest.speech_gate.should_speak", _spy)
    return calls


class TestTheDefaultIsNotAddressed:
    def test_incoming_message(self):
        msg = IncomingMessage(
            user_id="alice", text="hi", source_type="talk", surface="talk",
            channel_token="t",
        )
        assert msg.addressed_to_bot is False

    def test_record_inbound_in_a_group(self, config, db_path):
        with db.get_db(db_path) as conn:
            result = record_inbound(
                conn, config, surface="talk", surface_ref="grp", user_id="alice",
                text="lunch?", is_group_chat=True,
            )
        assert (result.outcome, result.task_id) == ("recorded", None)

    def test_a_direct_turn_is_still_answered(self, config, db_path):
        """Rung 1 is what answers a DM, whatever the surface said."""
        with db.get_db(db_path) as conn:
            result = record_inbound(
                conn, config, surface="talk", surface_ref="dm", user_id="alice",
                text="hi",
            )
        assert result.outcome == "created"

    def test_ingest_message_carries_the_field(self, config, db_path, gate_calls):
        with db.get_db(db_path) as conn:
            ingest_message(conn, config, IncomingMessage(
                user_id="alice", text="hi", source_type="talk", surface="talk",
                channel_token="grp", is_group_chat=True, addressed_to_bot=True,
            ))
        assert gate_calls[-1]["addressed_to_bot"] is True
        assert gate_calls[-1]["is_multi_human"] is True


class TestWebAddressing:
    NAMES = ("Nova", "nova-bot")

    @pytest.mark.parametrize("text", [
        "@nova what's for lunch",
        "@Nova what's for lunch",
        "nova, what's for lunch",
        "NOVA: lunch?",
        "  nova lunch?",
        "lunch? @nova",
        "hey @nova-bot lunch?",
        "nova-bot lunch?",
        "nova",
    ])
    def test_addressed(self, text):
        assert addressed_to_bot_in_text(text, self.NAMES) is True

    @pytest.mark.parametrize("text", [
        "lunch?",
        "ask nova about lunch",
        "novaon is a planet",
        "@novaon hi",
        "mail@nova.example hi",
        "",
    ])
    def test_not_addressed(self, text):
        assert addressed_to_bot_in_text(text, self.NAMES) is False

    def test_blank_names_are_ignored(self):
        assert addressed_to_bot_in_text("@ hello", ("", "  ")) is False
        assert addressed_to_bot_in_text("hello", ()) is False


class TestARelayAnswerIsAddressedByConstruction:
    """A relay answer replies to a question the bot posted, so it is addressed
    to the bot whatever room it lands in. That is what stops a gate-recorded
    answer from reaching the relay branch as a task-less ("ok", None)."""

    @pytest.mark.parametrize("surface", ["web", "talk"])
    def test_room_surfaces(self, config, db_path, gate_calls, surface):
        relay = {"id": 1, "question": None, "asker_display": "A", "asker_user_id": "a"}
        with db.get_db(db_path) as conn:
            task_id = message_relays.create_recipient_task(
                conn, config, relay, surface=surface, actor_user_id="bob",
                text="Thursday works", outcome="closed", channel="bob-room",
            )
        assert task_id is not None
        assert gate_calls[-1]["addressed_to_bot"] is True
