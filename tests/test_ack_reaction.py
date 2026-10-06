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


THUMB = "\N{THUMBS UP SIGN}"
OCTOPUS = "\N{OCTOPUS}"
LAUGH = "\N{SMILING FACE WITH OPEN MOUTH AND SMILING EYES}"
PARTY = "\N{PARTY POPPER}"
OK_HAND = "\N{OK HAND SIGN}"


def _cfg(value, table=None):
    return SimpleNamespace(speech_gate=SimpleNamespace(
        ack_reaction=value, ack_reactions=table if table is not None else {},
    ))


class TestTheConfiguredReaction:
    """`ack_reaction` alone behaves exactly as in ISSUE-655."""

    def test_the_default_is_a_thumbs_up(self):
        assert ack_reaction.reactions_for(Config()) == [THUMB]
        assert ack_reaction.pick(Config(), "funny", 301) == THUMB

    def test_a_skin_tone_sequence_is_one_reaction(self):
        value = "\N{THUMBS UP SIGN}\N{EMOJI MODIFIER FITZPATRICK TYPE-4}"
        assert ack_reaction.reactions_for(_cfg(value)) == [value]

    @pytest.mark.parametrize("value", ["", "   ", None, 7])
    def test_empty_or_absent_is_off(self, value):
        assert ack_reaction.reactions_for(_cfg(value)) == []
        assert ack_reaction.pick(_cfg(value), None, 1) is None

    @pytest.mark.parametrize("value", [
        "ok", "+1", "\N{THUMBS UP SIGN} \N{THUMBS UP SIGN}",
        "\N{THUMBS UP SIGN}" * 17, "\N{THUMBS UP SIGN}\x07",
    ])
    def test_anything_that_is_not_one_emoji_is_refused(self, value, caplog):
        assert ack_reaction.reactions_for(_cfg(value)) == []
        assert ack_reaction.reactions_for(_cfg(value)) == []
        assert len([r for r in caplog.records if "ack_reaction" in r.message]) == 1


class TestThePerTypeTable:
    """ISSUE-657: `[speech_gate.ack_reactions]`, a list of emoji per type."""

    TABLE = {
        "default": [THUMB], "thanks": [THUMB, OCTOPUS], "agreement": [OK_HAND],
        "funny": [LAUGH, OCTOPUS], "celebration": [PARTY],
    }

    def test_a_type_uses_its_own_list(self):
        cfg = _cfg(THUMB, self.TABLE)
        assert ack_reaction.reactions_for(cfg, "funny") == [LAUGH, OCTOPUS]
        assert ack_reaction.pick(cfg, "celebration", 9) == PARTY
        assert ack_reaction.pick(cfg, "funny", 9) in (LAUGH, OCTOPUS)

    @pytest.mark.parametrize("table", [
        {"default": [PARTY]}, {"default": [PARTY], "funny": []},
        {"default": [PARTY], "funny": ["ok", " "]},
    ])
    def test_an_absent_empty_or_all_invalid_type_falls_back_to_default(self, table):
        assert ack_reaction.reactions_for(_cfg(THUMB, table), "funny") == [PARTY]

    def test_an_unknown_type_reads_as_default(self, caplog):
        cfg = _cfg(THUMB, {"default": [PARTY], "sarcasm": [LAUGH]})
        assert ack_reaction.reactions_for(cfg, "sarcasm") == [PARTY]
        assert ack_reaction.reactions_for(cfg, None) == [PARTY]
        # A table key that is no kind is a typo nothing reads; said once.
        assert len([r for r in caplog.records if "sarcasm" in r.getMessage()]) == 1

    def test_the_table_wins_over_ack_reaction(self):
        assert ack_reaction.reactions_for(_cfg(THUMB, {"default": [PARTY]})) == [PARTY]

    def test_ack_reaction_fills_default_when_the_table_has_none(self):
        cfg = _cfg(OCTOPUS, {"funny": [LAUGH]})
        assert ack_reaction.reactions_for(cfg, "thanks") == [OCTOPUS]
        assert ack_reaction.reactions_for(cfg, "funny") == [LAUGH]

    def test_an_invalid_entry_is_dropped_and_the_rest_survive(self, caplog):
        cfg = _cfg(THUMB, {"funny": ["lol", LAUGH, "\N{OCTOPUS} x", OCTOPUS]})
        assert ack_reaction.reactions_for(cfg, "funny") == [LAUGH, OCTOPUS]
        assert ack_reaction.reactions_for(cfg, "funny") == [LAUGH, OCTOPUS]
        warnings = [r for r in caplog.records if "ack_reaction" in r.message]
        assert len(warnings) == 2

    def test_nothing_valid_left_is_off(self):
        cfg = _cfg("nope", {"default": ["ok"], "funny": ["lol"]})
        assert ack_reaction.pick(cfg, "funny", 1) is None
        decision = GateDecision(
            True, speech_gate.RUNG_CLASSIFIER, kind=KIND_ACK, ack_type="funny",
        )
        assert not ack_reaction.should_hold(cfg, decision, can_react=True)

    def test_an_empty_ack_reaction_turns_off_a_table_too(self):
        cfg = _cfg("", self.TABLE)
        assert ack_reaction.reactions_for(cfg, "funny") == []
        decision = GateDecision(
            True, speech_gate.RUNG_CLASSIFIER, kind=KIND_ACK, ack_type="funny",
        )
        assert not ack_reaction.should_hold(cfg, decision, can_react=True)

    def test_the_same_message_always_picks_the_same_emoji(self):
        cfg = _cfg(THUMB, {"default": [THUMB, OCTOPUS, LAUGH, PARTY, OK_HAND]})
        for key in (301, "BAE5THANKS", None):
            first = ack_reaction.pick(cfg, None, key)
            assert all(ack_reaction.pick(cfg, None, key) == first for _ in range(5))
        picks = {ack_reaction.pick(cfg, None, i) for i in range(200)}
        assert len(picks) > 1


class TestTheClassifierNamesAType:
    """ISSUE-657: `ack_type` on an ack under friendly, and nowhere else."""

    @staticmethod
    def _classify(raw, disposition="friendly"):
        return speech_gate.classify(
            "window", lambda _p: raw, "fast", disposition=disposition,
        )

    @pytest.mark.parametrize("ack_type", speech_gate.ACK_TYPES)
    def test_each_known_type(self, ack_type):
        decision = self._classify(
            json.dumps({"speak": True, "kind": "ack", "ack_type": ack_type}),
        )
        assert (decision.kind, decision.ack_type) == (KIND_ACK, ack_type)

    @pytest.mark.parametrize("raw", [
        '{"speak": true, "kind": "ack", "ack_type": "sarcasm"}',
        '{"speak": true, "kind": "ack", "ack_type": 3}',
        '{"speak": true, "kind": "ack"}',
    ])
    def test_an_unknown_or_missing_type_is_default(self, raw):
        assert self._classify(raw).ack_type == speech_gate.DEFAULT_ACK_TYPE

    def test_a_reply_carries_no_type(self):
        decision = self._classify('{"speak": true, "kind": "reply", "ack_type": "funny"}')
        assert (decision.kind, decision.ack_type) == (KIND_REPLY, None)

    def test_reserved_carries_no_type(self):
        decision = self._classify(
            '{"speak": true, "kind": "ack", "ack_type": "funny"}', "reserved",
        )
        assert (decision.kind, decision.ack_type) == (KIND_REPLY, None)

    def test_an_addressed_ack_keeps_its_type(self):
        classified = self._classify('{"speak": true, "kind": "ack", "ack_type": "funny"}')
        decision = speech_gate.should_speak(
            is_multi_human=True, addressed_to_bot=True, mode="classifier",
            classified=classified,
        )
        assert (decision.rung, decision.ack_type) == (speech_gate.RUNG_ADDRESSED, "funny")

    def test_the_friendly_prompt_asks_for_the_type_and_reserved_does_not(self):
        friendly = speech_gate.build_window([], bot_name="Istota", disposition="friendly")
        reserved = speech_gate.build_window([], bot_name="Istota", disposition="reserved")
        assert all(t in friendly for t in speech_gate.ACK_TYPES)
        assert "ack_type" in friendly and "ack_type" not in reserved


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


def _ack_turn(conn, config, *, can_react=True, message_id=701, ack_type=None):
    db.register_room(conn, "grp", "alice", origin="talk", name="Family")
    return record_inbound(
        conn, config, surface="talk", surface_ref="grp", user_id="alice",
        text="Thanks!", source_type="talk", is_group_chat=True,
        addressed_to_bot=False, platform_message_id=message_id,
        classified=GateDecision(
            True, speech_gate.RUNG_CLASSIFIER, kind=KIND_ACK, ack_type=ack_type,
        ),
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


class TestTheDecisionRow:
    """ISSUE-657: `ack_type` and `reaction` beside `kind` and `reacted`."""

    @staticmethod
    def _row(conn, message_id):
        return tuple(conn.execute(
            "SELECT kind, ack_type, reacted, reaction FROM speech_gate_decisions "
            "WHERE message_id = ?", (message_id,),
        ).fetchone())

    def test_a_reacted_ack_records_its_type_and_emoji(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config, ack_type="funny")
            assert result.ack_type == "funny"
            ack_reaction.settle(
                conn, task_id=result.task_id, message_id=result.message_id,
                reacted=True, reaction=OCTOPUS,
            )
            assert self._row(conn, result.message_id) == ("ack", "funny", 1, OCTOPUS)

    def test_a_failed_reaction_records_no_emoji(self, config):
        with db.get_db(config.db_path) as conn:
            result = _ack_turn(conn, config, ack_type="thanks")
            ack_reaction.settle(
                conn, task_id=result.task_id, message_id=result.message_id,
                reacted=False, reaction=OCTOPUS,
            )
            assert self._row(conn, result.message_id) == ("ack", "thanks", 0, None)

    def test_a_reply_records_neither(self, config):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "grp", "alice", origin="talk", name="Family")
            result = record_inbound(
                conn, config, surface="talk", surface_ref="grp", user_id="alice",
                text="and Sunday?", source_type="talk", is_group_chat=True,
                addressed_to_bot=False, platform_message_id=703,
                classified=GateDecision(True, speech_gate.RUNG_CLASSIFIER),
                can_react=True,
            )
            assert self._row(conn, result.message_id) == ("reply", None, None, None)

    def test_an_upgraded_database_gains_both_columns(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE speech_gate_decisions (id INTEGER PRIMARY KEY, "
                "room_token TEXT NOT NULL, surface TEXT NOT NULL, user_id TEXT NOT NULL, "
                "message_id INTEGER, spoke INTEGER NOT NULL, rung TEXT NOT NULL, "
                "reason TEXT, model TEXT, latency_ms INTEGER, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
        db.init_db(path)
        with sqlite3.connect(path) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(speech_gate_decisions)")}
        assert {"ack_type", "reaction", "reacted", "kind"} <= columns


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
