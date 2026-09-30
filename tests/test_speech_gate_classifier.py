"""SG 6: the speech gate's classifier rung runs.

The classifier is asked by `ingest.classify_ahead` before the caller opens its
write transaction, and its answer is handed to `record_inbound` as
`classified`, so no model call ever holds the write lock. The completer comes
from `executor.build_speech_gate_completer`, which routes through the brain the
turn would run on and records a task-less `task_usage` row with origin
`speech_gate`. The default mode is still `mention`, where none of this runs.
"""

import json
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from istota import db, speech_gate, web_app
from istota.config import BrainConfig, Config, NativeBrainConfig
from istota.executor import build_oneshot_completer, build_speech_gate_completer
from istota.transport import IncomingMessage, classify_ahead, ingest_message
from istota.transport.ingest import record_inbound


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir()
    db.init_db(cfg.db_path)
    cfg.speech_gate.mode = "classifier"
    return cfg


class _Scripted:
    """A completer answering from a script, recording each prompt.

    Each call also asks for the database's write lock with no wait, which only
    succeeds when no transaction is holding it, i.e. when the call is not made
    from inside the caller's write transaction.
    """

    def __init__(self, db_path, *answers):
        self.db_path = db_path
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.lock_free: list[bool] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        probe = sqlite3.connect(self.db_path, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            self.lock_free.append(True)
        except sqlite3.OperationalError:
            self.lock_free.append(False)
        finally:
            probe.close()
        return self.answers.pop(0)


def _seed_room(config, token="grp"):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "alice", origin="talk", name="Family")
        db.add_message(conn, token, role="user", body="dinner tonight?",
                       origin_surface="talk", author_user_id="bob")
        db.add_message(conn, token, role="assistant",
                       body="Shall I book a table?", origin_surface="talk")


def _turn(config, text, *, classified, user_id="alice", token="grp", mid=501):
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        task_id = ingest_message(conn, config, IncomingMessage(
            user_id=user_id, text=text, source_type="talk", surface="talk",
            channel_token=token, is_group_chat=True, addressed_to_bot=False,
            platform_message_id=mid, classified=classified,
        ))
        row = conn.execute(
            "SELECT spoke, rung, reason, model FROM speech_gate_decisions "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return task_id, dict(row)


class TestTheClassifierThroughTheIngestPath:
    def test_a_yes_creates_the_task(self, config):
        _seed_room(config)
        scripted = _Scripted(
            config.db_path, '{"speak": true, "reason": "answers the offer"}',
        )
        with patch("istota.executor.build_speech_gate_completer",
                   return_value=scripted):
            classified = classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="yes please, 7pm", is_group_chat=True,
                addressed_to_bot=False,
            )

        task_id, row = _turn(config, "yes please, 7pm", classified=classified)

        assert task_id is not None
        assert row == {"spoke": 1, "rung": "classifier",
                       "reason": "answers the offer", "model": "fast"}
        assert scripted.lock_free == [True]
        prompt = scripted.prompts[0]
        # The stored turns, the bot's question flagged, and the turn being
        # decided, which is not stored yet, as the newest line.
        assert "bob: dinner tonight?" in prompt
        assert "ended with a question: yes" in prompt
        assert prompt.index("Shall I book a table?") < prompt.index(
            "alice: yes please, 7pm")

    def test_a_no_records_the_turn_without_a_task(self, config):
        _seed_room(config)
        scripted = _Scripted(config.db_path, '{"speak": false, "reason": "chat"}')
        with patch("istota.executor.build_speech_gate_completer",
                   return_value=scripted):
            classified = classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="see you there bob", is_group_chat=True,
                addressed_to_bot=False,
            )

        task_id, row = _turn(config, "see you there bob", classified=classified)

        assert task_id is None
        assert (row["spoke"], row["rung"]) == (0, "classifier")
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role='user' AND body=?",
                ("see you there bob",),
            ).fetchone()[0] == 1

    def test_unparseable_output_fails_closed(self, config):
        _seed_room(config)
        scripted = _Scripted(config.db_path, "sure, why not")
        with patch("istota.executor.build_speech_gate_completer",
                   return_value=scripted):
            classified = classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="hm", is_group_chat=True, addressed_to_bot=False,
            )

        task_id, row = _turn(config, "hm", classified=classified)

        assert task_id is None
        assert (row["spoke"], row["rung"]) == (0, "failed")

    def test_a_builder_that_raises_fails_closed(self, config):
        with patch("istota.executor.build_speech_gate_completer",
                   side_effect=RuntimeError("boom")):
            classified = classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="hm", is_group_chat=True, addressed_to_bot=False,
            )
        assert classified is not None
        assert (classified.speak, classified.rung) == (False, "failed")

    def test_the_rungs_above_still_decide_first(self, config):
        """A classifier "no" handed in cannot mute an addressed turn."""
        refused = speech_gate.GateDecision(False, speech_gate.RUNG_CLASSIFIER)
        with db.get_db(config.db_path) as conn:
            result = record_inbound(
                conn, config, surface="talk", surface_ref="grp", user_id="alice",
                text="@Istota help", is_group_chat=True, addressed_to_bot=True,
                classified=refused,
            )
        assert result.outcome == "created"

    def test_no_answer_handed_in_fails_closed(self, config):
        with db.get_db(config.db_path) as conn:
            result = record_inbound(
                conn, config, surface="talk", surface_ref="grp", user_id="alice",
                text="hm", is_group_chat=True, addressed_to_bot=False,
            )
        assert (result.outcome, result.gate_reason) == ("recorded", "failed")


class TestNothingIsAskedWhenTheRungIsUnreachable:
    @pytest.mark.parametrize("mode", ["mention", "off", "Mention "])
    def test_other_modes(self, config, mode):
        config.speech_gate.mode = mode
        with patch("istota.executor.build_speech_gate_completer") as build:
            assert classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="hm", is_group_chat=True, addressed_to_bot=False,
            ) is None
        build.assert_not_called()

    @pytest.mark.parametrize("kw", [
        {"addressed_to_bot": True, "is_group_chat": True, "surface": "talk"},
        {"addressed_to_bot": False, "is_group_chat": False, "surface": "talk"},
        {"addressed_to_bot": False, "is_group_chat": True, "surface": "email"},
    ], ids=["addressed", "one-human", "non-room-surface"])
    def test_turns_the_ladder_decides_above_it(self, config, kw):
        with patch("istota.executor.build_speech_gate_completer") as build:
            assert classify_ahead(
                config, surface_ref="grp", user_id="alice", text="hm", **kw,
            ) is None
        build.assert_not_called()


class TestTheBatchWindow:
    def test_earlier_unstored_turns_take_the_newest_places(self, config):
        _seed_room(config)
        config.speech_gate.window_messages = 3
        scripted = _Scripted(config.db_path, '{"speak": false}')
        with patch("istota.executor.build_speech_gate_completer",
                   return_value=scripted):
            classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="third", is_group_chat=True, addressed_to_bot=False,
                earlier=(("bob", "first"),),
            )
        prompt = scripted.prompts[0]
        # Three places: one stored row, then the batch turn, then this turn.
        assert "dinner tonight?" not in prompt
        assert "Shall I book a table?" in prompt
        assert prompt.index("bob: first") < prompt.index("alice: third")


def _envelope(answer):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": answer, "num_turns": 1, "stop_reason": "end_turn",
        "session_id": "00000000-0000-0000-0000-000000000000",
        "total_cost_usd": 0.0001,
        "usage": {"input_tokens": 12, "output_tokens": 5},
        "modelUsage": {"claude-haiku-4-5-20251001": {
            "inputTokens": 300, "outputTokens": 5, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": 0.0001,
            "contextWindow": 200000, "maxOutputTokens": 32000,
        }},
    })


class TestTheCompleterAndItsUsage:
    @patch("istota.brain.claude_code.subprocess.run")
    def test_the_cli_path_writes_a_speech_gate_row(self, mock_run, config):
        """claude_code: `build_oneshot_completer` answers None, so the gate
        wraps the CLI triage path itself, with its own model and timeout."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_envelope('{"speak": true}'), stderr="",
        )
        config.speech_gate.timeout_seconds = 3.0
        completer = build_speech_gate_completer(
            config, user_id="alice", source_type="talk",
        )

        assert completer("prompt") == '{"speak": true}'
        argv = mock_run.call_args.args[0]
        assert "--model" in argv
        assert mock_run.call_args.kwargs["timeout"] == 3
        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT origin, task_id, user_id, source_type, output_tokens "
                "FROM task_usage"
            ).fetchall()
        assert [tuple(r) for r in rows] == [("speech_gate", None, "alice", "talk", 5)]

    def test_the_native_path_uses_the_gate_s_model(self, config):
        config.brain = BrainConfig(
            kind="native",
            native=NativeBrainConfig(model="big/model", api_key="k"),
        )
        seen = []

        def _fake(native, timeout, *, on_usage=None):
            seen.append((native.model, timeout))
            return lambda _p: None

        config.speech_gate.model = "small/model"
        config.speech_gate.timeout_seconds = 4.0
        with patch("istota.executor._build_native_completer", side_effect=_fake), \
                patch("istota.executor._native_with_user_key",
                      side_effect=lambda nc, *a, **k: nc):
            build_speech_gate_completer(config, user_id="alice", source_type="talk")
            # An unoverridden role collapses to the configured native model.
            build_oneshot_completer(
                config, user_id="alice", source_type="talk", timeout=1.0,
                origin="speech_gate", model="fast",
            )
            # No model keeps the native brain's own, as context triage has.
            build_oneshot_completer(
                config, user_id="alice", source_type="talk", timeout=1.0,
                origin="context_triage",
            )

        assert seen == [("small/model", 4.0), ("big/model", 1.0), ("big/model", 1.0)]


class TestTheDashboardCounter:
    def test_a_speech_gate_row_is_not_counted_as_unmeasured_context(self, config):
        from istota.usage import BrainUsage

        with db.get_db(config.db_path) as conn:
            db.insert_task_usage(
                conn, usage=BrainUsage(billed_input_tokens=10, output_tokens=2),
                user_id="alice", brain_kind="claude_code", origin="speech_gate",
                task_id=None, success=True,
            )
            db.insert_task_usage(
                conn, usage=BrainUsage(billed_input_tokens=10, output_tokens=2),
                user_id="alice", brain_kind="claude_code", origin="task",
                task_id=None, success=True,
            )
            conn.commit()
            section = web_app._admin_usage_section(conn, _now())
        assert section["context_unmeasured_rows_30d"] == 1


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
