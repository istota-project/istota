"""ISSUE-272 — the executor's side of measuring conversation-context triage.

Two things live here. ``_build_native_completer`` is the native-brain inference
path, and it threw ``AssistantMessage.usage`` away by returning only ``.text``.
``_build_triage_usage_sink`` is what both paths report into: it writes one
``task_usage`` row per triage inference, under the ``context_triage`` origin and
with no ``task_id`` — the same shape the seven other task-less origins use, so a
triage inference never takes an ``attempt_seq`` in the task's own sequence.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import db
from istota.config import BrainConfig, Config, NativeBrainConfig
from istota.executor import _build_native_completer, _build_triage_usage_sink
from istota.llm.types import AssistantMessage, TextContent, Usage

from ._mock_provider import MockProvider


def _cfg(tmp_path, kind="claude_code"):
    c = Config()
    c.db_path = tmp_path / "istota.db"
    c.brain = BrainConfig(
        kind=kind,
        native=NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
    )
    return c


def _task(source_type="talk", user_id="alice"):
    return SimpleNamespace(id=7, source_type=source_type, user_id=user_id)


class TestNativeCompleterUsage:
    def test_it_reports_what_the_turn_spent(self):
        provider = MockProvider([
            AssistantMessage(
                content=[TextContent(text='{"relevant_ids": [1]}')],
                usage=Usage(
                    input_tokens=900, output_tokens=20,
                    cache_read_tokens=400, cache_write_tokens=0,
                    cost_usd=0.0007,
                ),
                model="claude-sonnet-4-6",
            )
        ])
        seen = []

        with patch("istota.llm.make_provider", return_value=provider):
            completer = _build_native_completer(
                NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
                30.0,
                on_usage=lambda usage, **kw: seen.append((usage, kw)),
            )
        assert completer is not None
        assert completer("prompt") == '{"relevant_ids": [1]}'

        assert len(seen) == 1
        usage, kw = seen[0]
        # OpenAI-compat `prompt_tokens` includes cached reads; the shared
        # vocabulary's billed_input excludes them.
        assert usage.billed_input_tokens == 500
        assert usage.output_tokens == 20
        assert usage.cache_read_tokens == 400
        assert usage.cost_usd == pytest.approx(0.0007)
        assert kw["brain_kind"] == "native"
        assert kw["model"] == "claude-sonnet-4-6"
        assert kw["success"] is True

    def test_a_failed_turn_that_measured_nothing_writes_no_row(self):
        """The shape `openai_compat` actually emits on an error.

        All four of its `StreamError` sites build a fresh `AssistantMessage`
        with a default `Usage()` — zeros, not what the turn spent. Reporting
        those would write `has_totals=1` rows of pure zero, and every token
        aggregate filters on `has_totals`, so a provider outage would drag this
        origin's averages down while inflating its measured-call count.

        `MockProvider` hands the scripted message straight through to
        `StreamError`, so a test that scripts usage onto an error turn proves a
        shape production cannot produce. This one scripts the real one.
        """
        provider = MockProvider([
            AssistantMessage(content=[TextContent(text="boom")], stop_reason="error")
        ])
        seen = []

        with patch("istota.llm.make_provider", return_value=provider):
            completer = _build_native_completer(
                NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
                30.0,
                on_usage=lambda usage, **kw: seen.append((usage, kw)),
            )
        assert completer("prompt") is None
        assert seen == []

    def test_a_failed_turn_that_did_measure_still_reports(self):
        """If a provider ever does attach what an errored turn spent, record it —
        the tokens were real. Guards the skip above from widening into "never
        report a failure"."""
        provider = MockProvider([
            AssistantMessage(
                content=[TextContent(text="boom")], stop_reason="error",
                usage=Usage(input_tokens=100, output_tokens=0),
            )
        ])
        seen = []

        with patch("istota.llm.make_provider", return_value=provider):
            completer = _build_native_completer(
                NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
                30.0,
                on_usage=lambda usage, **kw: seen.append((usage, kw)),
            )
        assert completer("prompt") is None
        assert len(seen) == 1
        assert seen[0][1]["success"] is False
        assert seen[0][0].billed_input_tokens == 100

    def test_a_provider_reported_free_turn_is_a_measurement(self):
        """`cost_usd = 0.0` is the provider saying the turn was free, which is
        different from it saying nothing. Zero tokens alone means unmeasured."""
        provider = MockProvider([
            AssistantMessage(
                content=[TextContent(text='{"relevant_ids": []}')],
                usage=Usage(cost_usd=0.0),
            )
        ])
        seen = []

        with patch("istota.llm.make_provider", return_value=provider):
            completer = _build_native_completer(
                NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
                30.0,
                on_usage=lambda usage, **kw: seen.append((usage, kw)),
            )
        assert completer("prompt") == '{"relevant_ids": []}'
        assert len(seen) == 1

    def test_a_failing_sink_does_not_lose_the_answer(self):
        provider = MockProvider([
            AssistantMessage(content=[TextContent(text='{"relevant_ids": []}')])
        ])

        def _boom(usage, **kw):
            raise RuntimeError("db gone")

        with patch("istota.llm.make_provider", return_value=provider):
            completer = _build_native_completer(
                NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
                30.0, on_usage=_boom,
            )
        assert completer("prompt") == '{"relevant_ids": []}'

    def test_no_sink_keeps_the_old_contract(self):
        provider = MockProvider([
            AssistantMessage(content=[TextContent(text="ok")])
        ])
        with patch("istota.llm.make_provider", return_value=provider):
            completer = _build_native_completer(
                NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"), 30.0,
            )
        assert completer("prompt") == "ok"


class TestTriageUsageSink:
    def test_it_writes_a_context_triage_row_with_no_task_id(self, tmp_path):
        from istota.usage import BrainUsage

        config = _cfg(tmp_path)
        db.init_db(config.db_path)
        sink = _build_triage_usage_sink(_task(), config)

        usage = BrainUsage(
            billed_input_tokens=1200, output_tokens=24, cache_read_tokens=800,
            cost_usd=0.0004215,
        )
        sink(usage, model="claude-haiku-4-5", brain_kind="claude_code",
             stop_reason="completed", success=True)

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT origin, user_id, source_type, task_id, output_tokens, "
                "brain_kind, success FROM task_usage"
            ).fetchall()

        assert len(rows) == 1
        row = rows[0]
        assert row["origin"] == "context_triage"
        assert row["user_id"] == "alice"
        assert row["source_type"] == "talk"
        # No task_id: a triage inference is not one of the task's own attempts,
        # and a row carrying the id would take an attempt_seq in that sequence.
        assert row["task_id"] is None
        assert row["output_tokens"] == 24
        assert row["brain_kind"] == "claude_code"
        assert row["success"] == 1

    def test_it_never_raises(self, tmp_path):
        """Telemetry must not turn a working triage into a fail-open one."""
        from istota.usage import BrainUsage

        config = _cfg(tmp_path)
        config.db_path = tmp_path / "nonexistent" / "istota.db"
        sink = _build_triage_usage_sink(_task(), config)

        sink(BrainUsage(output_tokens=1), model="m", brain_kind="claude_code",
             stop_reason="completed", success=True)
