"""ISSUE-655: an ack answered with a reaction, the held task, and settling it."""

import json
from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config
from istota.rooms import ack_reaction
from istota.rooms import speech_gate
from istota.rooms.speech_gate import KIND_ACK, KIND_REPLY, GateDecision
from istota.transport.ingest import record_inbound


@pytest.fixture(autouse=True)
def _fresh_warning_latch():
    ack_reaction._warned.clear()
    yield
    ack_reaction._warned.clear()


def _cfg(value):
    return SimpleNamespace(speech_gate=SimpleNamespace(ack_reaction=value))


class TestTheConfiguredReaction:
    def test_the_default_is_a_thumbs_up(self):
        assert ack_reaction.reaction_for(Config()) == "\N{THUMBS UP SIGN}"

    def test_a_skin_tone_sequence_is_one_reaction(self):
        value = "\N{THUMBS UP SIGN}\N{EMOJI MODIFIER FITZPATRICK TYPE-4}"
        assert ack_reaction.reaction_for(_cfg(value)) == value

    @pytest.mark.parametrize("value", ["", "   ", None, 7])
    def test_empty_or_absent_is_off(self, value):
        assert ack_reaction.reaction_for(_cfg(value)) is None

    @pytest.mark.parametrize("value", [
        "ok", "+1", "\N{THUMBS UP SIGN} \N{THUMBS UP SIGN}",
        "\N{THUMBS UP SIGN}" * 17, "\N{THUMBS UP SIGN}\x07",
    ])
    def test_anything_that_is_not_one_emoji_is_refused(self, value, caplog):
        assert ack_reaction.reaction_for(_cfg(value)) is None
        assert ack_reaction.reaction_for(_cfg(value)) is None
        assert len([r for r in caplog.records if "ack_reaction" in r.message]) == 1


class TestShouldHold:
    ACK = GateDecision(True, speech_gate.RUNG_CLASSIFIER, kind=KIND_ACK)

    def test_an_ack_on_a_surface_that_reacts(self):
        assert ack_reaction.should_hold(Config(), self.ACK, can_react=True)

    def test_a_surface_that_cannot_react_replies(self):
        assert not ack_reaction.should_hold(Config(), self.ACK, can_react=False)

    def test_a_reply_kind_is_never_held(self):
        reply = GateDecision(True, speech_gate.RUNG_CLASSIFIER, kind=KIND_REPLY)
        assert not ack_reaction.should_hold(Config(), reply, can_react=True)

    def test_reactions_off_replies(self):
        assert not ack_reaction.should_hold(_cfg(""), self.ACK, can_react=True)

    def test_no_decision_is_never_held(self):
        assert not ack_reaction.should_hold(Config(), None, can_react=True)


# --- the held task, through record_inbound -----------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir()
    db.init_db(cfg.db_path)
    cfg.bot_name = "Istota"
    cfg.speech_gate.mode = "classifier"
    cfg.speech_gate.disposition = "friendly"
    return cfg


def _ack_turn(conn, config, *, can_react=True, message_id=701):
    db.register_room(conn, "grp", "alice", origin="talk", name="Family")
    return record_inbound(
        conn, config, surface="talk", surface_ref="grp", user_id="alice",
        text="Thanks!", source_type="talk", is_group_chat=True,
        addressed_to_bot=False, platform_message_id=message_id,
        classified=GateDecision(True, speech_gate.RUNG_CLASSIFIER, kind=KIND_ACK),
        can_react=can_react,
    )


def _task(conn, task_id):
    return conn.execute(
        "SELECT status, scheduled_for FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()


def _reacted(conn, message_id):
    return conn.execute(
        "SELECT reacted FROM speech_gate_decisions WHERE message_id = ?", (message_id,),
    ).fetchone()[0]


class TestTheHeldTask:
    def test_an_ack_is_created_held_and_unclaimable(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config)
            assert result.held_for_reaction
            assert _task(conn, result.task_id)["scheduled_for"] is not None
            assert db.claim_task(conn, "worker-1") is None

    def test_a_surface_that_cannot_react_gets_a_plain_task(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config, can_react=False)
            assert not result.held_for_reaction
            assert _task(conn, result.task_id)["scheduled_for"] is None

    def test_a_reaction_removes_the_task_and_records_it(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config)
            removed = ack_reaction.settle(
                conn, task_id=result.task_id, message_id=result.message_id,
                reacted=True,
            )
            assert removed
            assert _task(conn, result.task_id) is None
            assert conn.execute(
                "SELECT task_id FROM messages WHERE id = ?", (result.message_id,),
            ).fetchone()[0] is None
            assert _reacted(conn, result.message_id) == 1

    def test_a_failed_reaction_releases_the_task(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config)
            removed = ack_reaction.settle(
                conn, task_id=result.task_id, message_id=result.message_id,
                reacted=False,
            )
            assert not removed
            assert tuple(_task(conn, result.task_id)) == ("pending", None)
            assert _reacted(conn, result.message_id) == 0
            assert db.claim_task(conn, "worker-1").id == result.task_id

    def test_a_task_already_claimed_is_left_to_answer(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config)
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?", (result.task_id,),
            )
            assert not ack_reaction.settle(
                conn, task_id=result.task_id, message_id=result.message_id,
                reacted=True,
            )
            assert _task(conn, result.task_id)["status"] == "running"

    @pytest.mark.parametrize("reacted", [True, False])
    def test_a_task_already_on_the_retry_ladder_is_left_alone(self, config, reacted):
        """The hold ran out, a worker ran it, it failed and is waiting on its
        backoff: pending with a future `scheduled_for`, like the held row,
        but with an attempt. Settling must neither delete it nor skip the
        backoff."""
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config)
            conn.execute(
                "UPDATE tasks SET attempt_count = 1, "
                "scheduled_for = datetime('now', '+4 minutes') WHERE id = ?",
                (result.task_id,),
            )
            assert not ack_reaction.settle(
                conn, task_id=result.task_id, message_id=result.message_id,
                reacted=reacted,
            )
            task = _task(conn, result.task_id)
            assert task is not None and task["scheduled_for"] is not None

    def test_a_failed_settle_writes_nothing(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config)
            conn.execute("DROP TABLE speech_gate_decisions")
            assert not ack_reaction.settle(
                conn, task_id=result.task_id, message_id=result.message_id,
                reacted=True,
            )
            assert _task(conn, result.task_id) is not None

    def test_a_reserved_room_never_holds(self, config):
        config.speech_gate.disposition = "reserved"
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "grp", "alice", origin="talk", name="Family")
            # Under reserved `classify` never keeps an ack, so the verdict that
            # reaches the gate is a plain reply.
            verdict = speech_gate.classify(
                "window", lambda _p: '{"speak": true, "kind": "ack"}', "fast",
                disposition="reserved",
            )
            result = record_inbound(
                conn, config, surface="talk", surface_ref="grp", user_id="alice",
                text="Thanks!", source_type="talk", is_group_chat=True,
                addressed_to_bot=False, platform_message_id=702,
                classified=verdict, can_react=True,
            )
            assert result.task_id is not None and not result.held_for_reaction


class TestTheSidecarReacts:
    """ISSUE-655: `Session.react` reacts only to an original it still holds,
    keyed by that original, and a miss is a definite refusal."""

    _ORIGINAL = {"key": {"id": "IN1", "remoteJid": "g@g.us", "participant": "1@s.whatsapp.net"},
                 "message": {"conversation": "thanks!"}}

    @staticmethod
    def _react(tmp_path, payload, *, remember=None):
        from .test_whatsapp_sidecar_vendoring import PROGRAM, TestTheSidecarsInboundMedia

        remembered = ""
        if remember is not None:
            chat, message = remember
            remembered = f"m.rememberInbound({json.dumps(chat)}, {json.dumps(message)});"
        script = (
            f"const m = require({json.dumps(str(PROGRAM))});"
            "const answers = []; const sends = [];"
            "const link = {greeted: true, send: (t, f) => {"
            " answers.push(Object.assign({type: t}, f)); return true; }};"
            + remembered +
            "const s = new m.Session(link);"
            "s.sock = {sendMessage: async (to, c) => {"
            " sends.push({to, content: c}); return {key: {id: 'r' + sends.length}}; }};"
            f"s.react({json.dumps(payload)}).then(() =>"
            " process.stdout.write(JSON.stringify({answers, sends})));"
        )
        return TestTheSidecarsInboundMedia._run(script, media_dir=tmp_path)

    def _payload(self, **kw):
        payload = {"request_id": "r1", "to": "g@g.us", "message_id": "IN1",
                   "reaction": "\N{THUMBS UP SIGN}"}
        payload.update(kw)
        return payload

    def test_a_held_original_is_reacted_to_with_its_own_key(self, tmp_path):
        out = self._react(tmp_path, self._payload(), remember=("g@g.us", self._ORIGINAL))
        assert out["sends"] == [{"to": "g@g.us", "content": {"react": {
            "text": "\N{THUMBS UP SIGN}", "key": self._ORIGINAL["key"]}}}]
        assert out["answers"] == [{"type": "send_result", "request_id": "r1",
                                   "ok": True, "message_id": "r1"}]

    def test_a_miss_is_refused_definitely_and_sends_nothing(self, tmp_path):
        out = self._react(tmp_path, self._payload())
        assert out["sends"] == []
        assert out["answers"][0]["ok"] is False and out["answers"][0]["definite"] is True

    def test_an_original_from_another_chat_is_not_reacted_to(self, tmp_path):
        out = self._react(tmp_path, self._payload(), remember=("h@g.us", self._ORIGINAL))
        assert out["sends"] == []
