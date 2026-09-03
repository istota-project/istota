"""ISSUE-272 — conversation-context triage is a daemon-side model call, and it has to be measured.

Triage runs on every conversational task whose older history exceeds
``skip_selection_threshold``, at both call sites, which makes it the
highest-frequency model call the daemon makes on its own behalf. It used to
spawn ``claude -p -`` directly: no ``--output-format``, no ``BrainResult``, and
so no usage row — the one origin whose absence from the totals could actually
move them.

Two inference paths reach the model, and both are covered here: the `claude`
CLI (claude_code / tmux deployments) and an injected native completer. The
fail-open contract is what everything else here must not break — a triage
hiccup adds context, it never silently drops it.
"""

import json
from unittest.mock import patch

import pytest

from istota import context
from istota.brain import claude_code
from istota.config import Config, ConversationConfig
from istota.context import select_relevant_context, select_relevant_talk_context
from istota.db import ConversationMessage, TalkMessage
from tests.support.monotonic_spy import monotonic_spy
from tests.support.sleep_spy import sleep_spy


def _config(**conv_overrides) -> Config:
    return Config(conversation=ConversationConfig(**conv_overrides))


def _msg(i: int) -> ConversationMessage:
    return ConversationMessage(
        id=i, prompt=f"q{i}", result=f"a{i}", created_at="2026-08-21 12:00",
        actions_taken=None, source_type="talk", user_id="alice",
    )


def _history(n: int) -> list[ConversationMessage]:
    return [_msg(i) for i in range(1, n + 1)]


def _talk(i: int) -> TalkMessage:
    return TalkMessage(
        message_id=i, actor_id="alice", actor_display_name="Alice", is_bot=False,
        content=f"msg {i}", timestamp=100 + i, actions_taken=None,
        message_role="user", task_id=None,
    )


def _envelope(answer: str) -> str:
    """A genuine `--output-format json` envelope (CLI 2.1.238 single-object shape).

    Same fixture family as `tests/test_claude_code_usage_capture.py` — the
    numbers below are what the assertions read back, so a triage row's tokens
    have to survive the whole path from CLI stdout to the usage sink.
    """
    return json.dumps({
        "is_error": False,
        "duration_api_ms": 812,
        "num_turns": 1,
        "stop_reason": "end_turn",
        "session_id": "00000000-0000-0000-0000-000000000000",
        "total_cost_usd": 0.0004215,
        "usage": {"input_tokens": 17, "output_tokens": 9, "service_tier": "standard"},
        "modelUsage": {
            "claude-haiku-4-5-20251001": {
                "inputTokens": 1200,
                "outputTokens": 24,
                "cacheReadInputTokens": 800,
                "cacheCreationInputTokens": 0,
                "costUSD": 0.0004215,
                "contextWindow": 200000,
                "maxOutputTokens": 32000,
            }
        },
        "permission_denials": [],
        "subtype": "success",
        "api_error_status": None,
        "result": answer,
        "type": "result",
        "duration_ms": 900,
        "uuid": "00000000-0000-0000-0000-000000000001",
    })


class _Sink:
    """Records what the triage path reports it spent."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, usage, **kwargs):
        self.calls.append({"usage": usage, **kwargs})


def _completed(stdout: str):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class TestCliTriageUsage:
    """The `claude` CLI path now runs through ClaudeCodeBrain, so it inherits the
    envelope parse and the per-attempt usage the brain already builds."""

    @patch("istota.brain.claude_code.subprocess.run")
    def test_the_answer_survives_the_envelope(self, mock_run):
        """The whole reason a flag couldn't just be bolted on: `_parse_relevant_ids`
        hunts for `{...}` greedily, so an unextracted envelope parses as the
        envelope, yields no `relevant_ids`, and fails open on every task."""
        mock_run.return_value = _completed(_envelope('{"relevant_ids": [0]}'))
        history = _history(5)

        result = select_relevant_context(
            "test", history, _config(skip_selection_threshold=2, always_include_recent=2)
        )

        # Triaged older message 0, plus the two guaranteed recent — not all five.
        assert result == [history[0], history[3], history[4]]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_it_reports_what_it_spent(self, mock_run):
        mock_run.return_value = _completed(_envelope('{"relevant_ids": []}'))
        sink = _Sink()

        select_relevant_context(
            "test", _history(5),
            _config(skip_selection_threshold=2, always_include_recent=2),
            on_usage=sink,
        )

        assert len(sink.calls) == 1
        usage = sink.calls[0]["usage"]
        assert usage.billed_input_tokens == 1200
        assert usage.output_tokens == 24
        assert usage.cache_read_tokens == 800
        assert usage.cost_usd == pytest.approx(0.0004215)
        assert sink.calls[0]["brain_kind"] == "claude_code"
        assert sink.calls[0]["success"] is True

    @patch("istota.brain.claude_code.subprocess.run")
    def test_the_talk_call_site_reports_too(self, mock_run):
        """Both call sites run the same triage; measuring one of them would
        undercount by whatever share of traffic arrives over Talk."""
        mock_run.return_value = _completed(_envelope('{"relevant_ids": []}'))
        sink = _Sink()

        select_relevant_talk_context(
            "test", [_talk(i) for i in range(1, 6)],
            _config(skip_selection_threshold=2, always_include_recent=2),
            on_usage=sink,
        )

        assert len(sink.calls) == 1
        assert sink.calls[0]["usage"].output_tokens == 24

    @patch("istota.brain.claude_code.subprocess.run")
    def test_the_argv_asks_for_the_envelope_and_stays_tool_less(self, mock_run):
        mock_run.return_value = _completed(_envelope('{"relevant_ids": []}'))

        select_relevant_context(
            "test", _history(5),
            _config(skip_selection_threshold=2, always_include_recent=2),
        )

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["claude", "-p", "-"]
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--model" in cmd
        # Text-only: no tools, so no permission bypass and no tool flags.
        assert "--dangerously-skip-permissions" not in cmd
        assert "--disallowedTools" not in cmd

    @patch("istota.brain.claude_code.subprocess.run")
    def test_it_closes_the_settings_file_advisor_channel(self, mock_run):
        """A host `~/.claude/settings.json` carrying `advisorModel` is honored in
        `-p` mode. The old bare spawn never set the disable var, so an operator
        who had that key was paying for an advisor on every conversational turn
        and had no row saying so."""
        mock_run.return_value = _completed(_envelope('{"relevant_ids": []}'))

        select_relevant_context(
            "test", _history(5),
            _config(skip_selection_threshold=2, always_include_recent=2),
        )

        env = mock_run.call_args.kwargs["env"]
        assert env.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") == "1"


class TestTriageFailsOpen:
    """Fail-open is the property most likely to be broken by accident here,
    because breaking it produces no error — just a quietly larger prompt."""

    @patch("istota.brain.claude_code.subprocess.run")
    def test_a_failing_sink_does_not_cost_the_selection(self, mock_run):
        """Telemetry must never turn a working triage into a fail-open one."""
        mock_run.return_value = _completed(_envelope('{"relevant_ids": [0]}'))
        history = _history(5)

        def _boom(usage, **kwargs):
            raise RuntimeError("db gone")

        result = select_relevant_context(
            "test", history,
            _config(skip_selection_threshold=2, always_include_recent=2),
            on_usage=_boom,
        )

        assert result == [history[0], history[3], history[4]]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_a_failed_attempt_still_reports_its_spend(self, mock_run):
        """A run that reached the model and then failed spent real tokens.
        Dropping them writes no row for exactly the case worth seeing."""
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=_envelope("Something went wrong"), stderr="",
        )
        sink = _Sink()
        history = _history(5)

        result = select_relevant_context(
            "test", history,
            _config(skip_selection_threshold=2, always_include_recent=2),
            on_usage=sink,
        )

        assert len(sink.calls) == 1
        assert sink.calls[0]["usage"].output_tokens == 24
        assert sink.calls[0]["success"] is False
        assert result == history  # fail open

    @patch("istota.brain.claude_code.subprocess.run")
    def test_no_sink_is_fine(self, mock_run):
        mock_run.return_value = _completed(_envelope('{"relevant_ids": []}'))
        history = _history(5)

        result = select_relevant_context(
            "test", history,
            _config(skip_selection_threshold=2, always_include_recent=2),
        )

        assert result == history[-2:]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_an_injected_completer_still_wins_over_the_cli(self, mock_run):
        history = _history(5)

        result = select_relevant_context(
            "test", history,
            _config(skip_selection_threshold=2, always_include_recent=2),
            completer=lambda _p: '{"relevant_ids": [0]}',
        )

        mock_run.assert_not_called()
        assert result == [history[0], history[3], history[4]]


class TestTriageBudget:
    """`selection_timeout` bounds the whole triage, not one attempt.

    Routing through the brain brought its 3-attempt retry ladder along. That is
    right for the task-less origins that share the path — nobody waits on a
    nightly OCR pass — and wrong for triage, which sits between the user's
    message and the main brain call, holds a worker slot, and whose failure mode
    is free. The deadline `cancel_check` is what bounds it.
    """

    @patch("istota.brain.claude_code.subprocess.run")
    def test_the_backoff_stops_once_the_budget_is_spent(self, mock_run, monkeypatch):
        """Without the deadline this is three spawns and a two-minute sleep no
        cancel can land on: `_interruptible_sleep` polls `cancel_check`, and
        triage used to supply none."""
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="API Error: 503 {}\nRetry-After: 60",
        )
        history = _history(5)
        slept = sleep_spy(monkeypatch, claude_code)
        # A budget already spent: the first backoff must not sleep at all.
        ticks = iter([0.0] + [10_000.0] * 40)
        monotonic_spy(monkeypatch, context, lambda: next(ticks))
        result = select_relevant_context(
            "test", history,
            _config(skip_selection_threshold=2, always_include_recent=2),
        )

        assert mock_run.call_count == 1
        assert slept == []
        assert result == history  # fail open

    @patch("istota.brain.claude_code.subprocess.run")
    def test_a_retry_inside_the_budget_still_happens(self, mock_run, monkeypatch):
        """The budget bounds the ladder; it does not delete it. A transient
        failure that resolves on the second attempt is the case where retrying
        beats failing open."""
        import subprocess

        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="API Error: 503 {}",
            ),
            _completed(_envelope('{"relevant_ids": [0]}')),
        ]
        history = _history(5)

        sleep_spy(monkeypatch, claude_code, record=False)
        result = select_relevant_context(
            "test", history,
            _config(skip_selection_threshold=2, always_include_recent=2),
        )

        assert mock_run.call_count == 2
        assert result == [history[0], history[3], history[4]]

    @patch("istota.brain.claude_code.subprocess.run")
    def test_a_timeout_is_a_timeout_not_an_error(self, mock_run):
        """A `TimeoutExpired` used to unwind past the retry loop into
        `_execute`'s generic handler, which logs a full stack trace at ERROR and
        reports `stop_reason="error"`. Benign here — it happens once per
        conversational task on a slow provider — and merely noisy for the other
        origins on this path."""
        import subprocess

        from istota.brain import BrainRequest, ClaudeCodeBrain

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        req = BrainRequest(
            prompt="hi", allowed_tools=[], cwd="/tmp", env={},
            timeout_seconds=30, streaming=False,
        )
        result = ClaudeCodeBrain().execute(req)

        assert result.success is False
        assert result.stop_reason == "timeout"
        assert mock_run.call_count == 1  # a timeout is not retried

    @patch("istota.brain.claude_code.subprocess.run")
    def test_a_timeout_fails_open(self, mock_run):
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        history = _history(5)

        result = select_relevant_context(
            "test", history,
            _config(skip_selection_threshold=2, always_include_recent=2),
        )

        assert result == history
