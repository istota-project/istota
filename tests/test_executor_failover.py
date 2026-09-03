"""Direct tests for `run_with_failover`.

`tests/test_executor_fallback.py` covers the same code by driving the whole of
`execute_task`. These drive the failover loop itself, so every field of
`FailoverOutcome` can be asserted on each of its four paths, and so the stream
hand-off at a reroute is reachable without a task, a database or a brain
request the executor built.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from istota.brain._fallback import get_availability_breaker, reset_availability_breaker
from istota.brain._types import BrainRequest, BrainResult
from istota.config import BrainConfig
from istota.executor import run_with_failover
from istota.executor_stream import TaskStreamAdapter

from tests.test_executor_streaming import _make_config, _make_task


class FakeBrain:
    """A brain that returns a canned result and counts its calls."""

    def __init__(self, kind, result, *, alias_map=None):
        self.kind = kind
        self.result = result
        self.calls = 0
        self.received = []
        self._alias_map = alias_map or {}
        self.model_namespace = (
            "anthropic" if kind in ("claude_code", "tmux_claude") else "openai_compat"
        )

    def execute(self, req):
        self.calls += 1
        self.received.append(req)
        return self.result

    def resolve_model_name(self, name):
        return (name or "").strip()

    def resolve_alias(self, name):
        return self._alias_map.get(name)


class RecordingWriter:
    """Records `(kind, payload)` pairs, deduping `emit_once` like the real one."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind, payload=None):
        self.events.append((kind, payload))

    def emit_once(self, kind, payload=None):
        if any(k == kind for k, _ in self.events):
            return None
        self.events.append((kind, payload))
        return object()

    def kinds(self):
        return [kind for kind, _ in self.events]


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_availability_breaker()
    yield
    reset_availability_breaker()


@pytest.fixture(autouse=True)
def _no_alerts():
    with patch("istota.notifications.send_notification", return_value=None):
        yield


def make_request(**kwargs) -> BrainRequest:
    defaults = dict(
        prompt="do the thing",
        allowed_tools=[],
        cwd=Path("/tmp"),
        env={},
        timeout_seconds=60,
        effort="medium",
    )
    defaults.update(kwargs)
    return BrainRequest(**defaults)


def make_brain_config(**kwargs) -> BrainConfig:
    defaults = dict(
        kind="claude_code",
        fallback="native",
        fallback_cooldown_seconds=900,
    )
    defaults.update(kwargs)
    return BrainConfig(**defaults)


def run(
    tmp_path,
    *,
    primary_result,
    fallback_result=None,
    brain_config=None,
    fallback_alias_map=None,
    task=None,
    req=None,
    stream=None,
    event_writer=None,
    config=None,
):
    """Call `run_with_failover` with a stub primary and a stub fallback."""
    config = config or _make_config(tmp_path)
    brain_config = brain_config or make_brain_config()
    config.brain = brain_config
    task = task if task is not None else _make_task()
    req = req if req is not None else make_request()

    primary = FakeBrain(brain_config.kind, primary_result)
    fallback = FakeBrain(
        brain_config.fallback or "native",
        fallback_result
        if fallback_result is not None
        else BrainResult(
            True, "fallback answer", stop_reason="completed", model_used="fb-model"
        ),
        alias_map=fallback_alias_map,
    )

    with (
        patch("istota.executor.make_brain", return_value=fallback),
        patch(
            "istota.executor._native_with_user_key",
            side_effect=lambda nc, *a, **k: nc,
        ),
    ):
        outcome = run_with_failover(
            primary,
            req,
            config=config,
            brain_config=brain_config,
            task=task,
            stream=stream,
            event_writer=event_writer,
        )
    return outcome, primary, fallback


class TestThePrimarySucceeds:
    def test_every_field_describes_the_primary(self, tmp_path):
        outcome, primary, fallback = run(
            tmp_path,
            primary_result=BrainResult(
                True, "primary answer", stop_reason="completed", model_used="p-model"
            ),
        )

        assert primary.calls == 1
        assert fallback.calls == 0
        assert outcome.result.result_text == "primary answer"
        assert outcome.primary_usage_result is None
        assert outcome.ran_fallback is False
        assert outcome.usage_effort == "medium"
        assert outcome.dropped_pin is None
        assert outcome.primary_kind == "claude_code"
        assert outcome.fallback_kind == "native"


class TestThePrimaryFailsAndReroutes:
    def test_the_fallback_replaces_the_result_and_the_primary_is_held(self, tmp_path):
        outcome, primary, fallback = run(
            tmp_path,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit", model_used="p-model"
            ),
        )

        assert primary.calls == 1
        assert fallback.calls == 1
        assert outcome.result.result_text == "fallback answer"
        # Held so both attempts' usage can be written from the one call site
        # that has a connection.
        assert outcome.primary_usage_result is not None
        assert outcome.primary_usage_result.stop_reason == "usage_limit"
        assert outcome.ran_fallback is True
        assert outcome.primary_kind == "claude_code"
        assert outcome.fallback_kind == "native"

    def test_usage_effort_is_the_fallbacks_own_resolution(self, tmp_path):
        # A portable intent re-resolves in the fallback's namespace, carrying
        # its effort, so `req.effort` no longer describes the attempt that ran.
        outcome, _, fallback = run(
            tmp_path,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit"
            ),
            fallback_alias_map={"smart": ("fb-smart-model", "high")},
            task=_make_task(model="smart"),
        )

        assert outcome.usage_effort == "high"
        assert fallback.received[0].model == "fb-smart-model"
        assert outcome.dropped_pin is None

    def test_a_non_portable_pin_is_reported_as_dropped(self, tmp_path):
        outcome, _, fallback = run(
            tmp_path,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit"
            ),
            task=_make_task(model="opus"),
        )

        assert outcome.dropped_pin == "opus"
        assert outcome.ran_fallback is True

    def test_a_fallback_that_cannot_be_constructed_keeps_the_primary(self, tmp_path):
        config = _make_config(tmp_path)
        brain_config = make_brain_config()
        config.brain = brain_config
        primary = FakeBrain(
            "claude_code",
            BrainResult(False, "over quota", stop_reason="usage_limit"),
        )

        with (
            patch("istota.executor.make_brain", side_effect=RuntimeError("misconfig")),
            patch(
                "istota.executor._native_with_user_key",
                side_effect=lambda nc, *a, **k: nc,
            ),
        ):
            outcome = run_with_failover(
                primary,
                make_request(),
                config=config,
                brain_config=brain_config,
                task=_make_task(),
                stream=None,
                event_writer=None,
            )

        assert outcome.result.stop_reason == "usage_limit"
        assert outcome.primary_usage_result is None
        assert outcome.ran_fallback is False
        assert outcome.usage_effort == "medium"


class TestTheBreakerIsAlreadyOpen:
    def test_the_fallback_runs_with_no_primary_call(self, tmp_path):
        get_availability_breaker().open("claude_code", 900)

        outcome, primary, fallback = run(
            tmp_path,
            primary_result=BrainResult(True, "primary answer", stop_reason="completed"),
        )

        assert primary.calls == 0
        assert fallback.calls == 1
        assert outcome.result.result_text == "fallback answer"
        # There is no primary row to hold, which is exactly why `ran_fallback`
        # is its own field rather than `primary_usage_result is not None`.
        assert outcome.primary_usage_result is None
        assert outcome.ran_fallback is True

    def test_a_zero_cooldown_never_skips_the_primary(self, tmp_path):
        get_availability_breaker().open("claude_code", 900)

        outcome, primary, _ = run(
            tmp_path,
            primary_result=BrainResult(True, "primary answer", stop_reason="completed"),
            brain_config=make_brain_config(fallback_cooldown_seconds=0),
        )

        assert primary.calls == 1
        assert outcome.ran_fallback is False


class TestNoFallbackConfigured:
    def test_the_primary_failure_stands(self, tmp_path):
        outcome, primary, fallback = run(
            tmp_path,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit"
            ),
            brain_config=make_brain_config(fallback=""),
        )

        assert primary.calls == 1
        assert fallback.calls == 0
        assert outcome.fallback_kind is None
        assert outcome.result.stop_reason == "usage_limit"
        assert outcome.primary_usage_result is None
        assert outcome.ran_fallback is False
        assert outcome.dropped_pin is None

    def test_the_breaker_still_opens(self, tmp_path):
        # ISSUE-362: the breaker is a shared signal the direct callers read,
        # so it is not gated on a fallback existing.
        run(
            tmp_path,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit"
            ),
            brain_config=make_brain_config(fallback=""),
        )

        assert get_availability_breaker().should_skip("claude_code", 900) is True

    def test_a_self_fallback_resolves_to_none(self, tmp_path):
        outcome, primary, fallback = run(
            tmp_path,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit"
            ),
            brain_config=make_brain_config(fallback="claude_code"),
        )

        assert outcome.fallback_kind is None
        assert fallback.calls == 0
        assert outcome.ran_fallback is False


def make_stream(config, task, writer, *, stream_surface=True):
    with patch(
        "istota.transport.registry.task_is_stream_surface",
        return_value=stream_surface,
    ):
        return TaskStreamAdapter(config, task, writer)


class TestTheStreamHandOffAtAReroute:
    def test_the_buffers_settle_before_the_notice_is_emitted(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()
        order: list[str] = []

        class OrderingWriter(RecordingWriter):
            def emit_once(self, kind, payload=None):
                order.append(f"emit:{kind}")
                return super().emit_once(kind, payload)

        writer = OrderingWriter()
        stream = make_stream(config, task, writer)
        with (
            patch.object(
                stream, "flush_thinking", side_effect=lambda: order.append("thinking")
            ),
            patch.object(
                stream,
                "settle_at_tool_boundary",
                side_effect=lambda: order.append("settle"),
            ),
        ):
            run(
                tmp_path,
                config=config,
                task=task,
                primary_result=BrainResult(
                    False, "over quota", stop_reason="usage_limit"
                ),
                stream=stream,
                event_writer=writer,
            )

        # Thinking first, so the reasoning chip settles before the answer text,
        # and both before the banner — an unflushed primary tail must not open
        # the fallback's answer.
        assert order == ["thinking", "settle", "emit:brain_fallback"]

    def test_no_event_writer_means_no_notice_and_no_settle(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()
        stream = make_stream(config, task, None)
        touched: list[str] = []
        with (
            patch.object(
                stream, "flush_thinking", side_effect=lambda: touched.append("thinking")
            ),
            patch.object(
                stream,
                "settle_at_tool_boundary",
                side_effect=lambda: touched.append("settle"),
            ),
        ):
            outcome, _, _ = run(
                tmp_path,
                config=config,
                task=task,
                primary_result=BrainResult(
                    False, "over quota", stop_reason="usage_limit"
                ),
                stream=stream,
                event_writer=None,
            )

        # `_failover_notice` returns None with no writer, so `_run_fallback`
        # skips the hook entirely — and with no writer there is nothing
        # buffered to settle. The reroute itself still happens.
        assert touched == []
        assert outcome.ran_fallback is True

    def test_a_push_surface_settles_nothing_but_still_emits(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()
        writer = RecordingWriter()
        stream = make_stream(config, task, writer, stream_surface=False)
        touched: list[str] = []
        with (
            patch.object(
                stream, "flush_thinking", side_effect=lambda: touched.append("thinking")
            ),
            patch.object(
                stream,
                "settle_at_tool_boundary",
                side_effect=lambda: touched.append("settle"),
            ),
        ):
            run(
                tmp_path,
                config=config,
                task=task,
                primary_result=BrainResult(
                    False, "over quota", stop_reason="usage_limit"
                ),
                stream=stream,
                event_writer=writer,
            )

        assert touched == []
        assert "brain_fallback" in writer.kinds()

    def test_the_settle_runs_even_when_the_banner_is_deduped(self, tmp_path):
        # ISSUE-361: `emit_once` drops the second banner for a retried turn.
        # The settle is about the daemon's own buffers, not the sentence, so
        # it must still run on the attempt whose notice is suppressed.
        config = _make_config(tmp_path)
        task = _make_task()
        writer = RecordingWriter()
        writer.emit_once("brain_fallback", {"already": "seen"})
        stream = make_stream(config, task, writer)
        touched: list[str] = []
        with (
            patch.object(
                stream, "flush_thinking", side_effect=lambda: touched.append("thinking")
            ),
            patch.object(
                stream,
                "settle_at_tool_boundary",
                side_effect=lambda: touched.append("settle"),
            ),
        ):
            run(
                tmp_path,
                config=config,
                task=task,
                primary_result=BrainResult(
                    False, "over quota", stop_reason="usage_limit"
                ),
                stream=stream,
                event_writer=writer,
            )

        assert touched == ["thinking", "settle"]
        assert writer.kinds().count("brain_fallback") == 1

    def test_a_none_stream_with_a_writer_still_emits_the_notice(self, tmp_path):
        # `execute_task` always builds an adapter, so this pairing has no
        # production caller — but the signature admits `stream=None`, and the
        # guard that makes it survivable is new code the move introduced.
        # Without a case for it the guard is the one line in this stage
        # covered by nothing.
        writer = RecordingWriter()

        outcome, _, fallback = run(
            tmp_path,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit"
            ),
            stream=None,
            event_writer=writer,
        )

        assert fallback.calls == 1
        assert outcome.ran_fallback is True
        # The notice is about the reroute, not about the buffers, so it still
        # goes out with no adapter to settle.
        assert "brain_fallback" in writer.kinds()

    def test_a_notice_that_raises_does_not_cost_the_reroute(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task()

        class ExplodingWriter(RecordingWriter):
            def emit_once(self, kind, payload=None):
                raise RuntimeError("event log is gone")

        writer = ExplodingWriter()
        stream = make_stream(config, task, writer)

        outcome, _, fallback = run(
            tmp_path,
            config=config,
            task=task,
            primary_result=BrainResult(
                False, "over quota", stop_reason="usage_limit"
            ),
            stream=stream,
            event_writer=writer,
        )

        assert fallback.calls == 1
        assert outcome.result.result_text == "fallback answer"
        assert outcome.ran_fallback is True
