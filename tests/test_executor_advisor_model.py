"""Executor-level advisor resolution (advisor-model spec, Stage 3)."""

from unittest.mock import patch

import pytest

from istota.config import BrainConfig, Config
from istota.db import Task
from istota.executor import _resolve_advisor, execute_task

from tests.test_executor_streaming import (
    _make_config,
    _make_task,
    _patch_executor,
    contextmanager_chain,
)


def _task(**kw) -> Task:
    return _make_task(**kw)


class TestResolveAdvisorUnit:
    """`_resolve_advisor` in isolation — no brain, no config-loader involved."""

    def test_no_config_advisor_returns_empty(self):
        config = Config()
        assert _resolve_advisor(_task(model=""), config) == ""

    def test_configured_advisor_flows_through_when_unpinned(self):
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model=""), config) == "opus"

    def test_task_model_pin_drops_the_advisor(self):
        # A per-task model pin (!model, !room model, a [[jobs]] model, an API
        # caller) drops the configured advisor: a rejected executor/advisor
        # pairing is a hard CLI error, not a downgrade.
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model="haiku"), config) == ""

    def test_whitespace_only_task_model_does_not_count_as_a_pin(self):
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model="   "), config) == "opus"

    def test_none_task_model_does_not_count_as_a_pin(self):
        config = Config(advisor_model="opus")
        assert _resolve_advisor(_task(model=None), config) == "opus"


class _FakeAnthropicBrain:
    """Minimal Brain stand-in for the executor's `brain = make_brain(...)` call,
    with the anthropic namespace so the `advisor` field actually resolves."""

    model_namespace = "anthropic"

    def __init__(self):
        self.received_reqs = []

    def resolve_model_name(self, name):
        return (name or "").strip()

    def resolve_alias(self, a):
        return None

    def execute(self, req):
        from istota.brain._types import BrainResult

        self.received_reqs.append(req)
        return BrainResult(True, "ok", stop_reason="completed")

    def list_aliases(self):
        return []

    def validate_alias_override(self, r, t):
        return []


class _FakeNativeBrain(_FakeAnthropicBrain):
    model_namespace = "openai_compat"


def _run_with_fake_brain(tmp_path, fake_brain, *, advisor_model="", task_model=""):
    config = _make_config(tmp_path)
    config.advisor_model = advisor_model
    config.brain = BrainConfig(kind="claude_code")
    config.security.sandbox_enabled = False

    patches = _patch_executor() + [
        patch("istota.executor.make_brain", return_value=fake_brain),
    ]
    with contextmanager_chain(patches):
        task = _task(source_type="cli", model=task_model)
        execute_task(task, config, [])
    return fake_brain.received_reqs[0]


class TestBrainRequestWiring:
    def test_unpinned_task_carries_configured_advisor(self, tmp_path):
        fb = _FakeAnthropicBrain()
        req = _run_with_fake_brain(tmp_path, fb, advisor_model="opus")
        assert req.advisor == "opus"

    def test_pinned_task_drops_advisor(self, tmp_path):
        fb = _FakeAnthropicBrain()
        req = _run_with_fake_brain(
            tmp_path, fb, advisor_model="opus", task_model="haiku"
        )
        assert req.advisor == ""

    def test_no_configured_advisor_stays_empty(self, tmp_path):
        fb = _FakeAnthropicBrain()
        req = _run_with_fake_brain(tmp_path, fb, advisor_model="")
        assert req.advisor == ""

    def test_native_brain_never_carries_advisor(self, tmp_path):
        # model_namespace != "anthropic" — the executor never sets `advisor`
        # regardless of config, since NativeBrain has no wire for it.
        fb = _FakeNativeBrain()
        req = _run_with_fake_brain(tmp_path, fb, advisor_model="opus")
        assert req.advisor == ""


class TestAdvisorLogLine:
    def test_logs_once_when_advisor_resolved(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="istota.executor"):
            _run_with_fake_brain(tmp_path, _FakeAnthropicBrain(), advisor_model="opus")
        matches = [r for r in caplog.records if "advisor=opus" in r.message]
        assert len(matches) == 1

    def test_no_log_line_when_no_advisor(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="istota.executor"):
            _run_with_fake_brain(tmp_path, _FakeAnthropicBrain(), advisor_model="")
        assert not any("advisor=" in r.message for r in caplog.records)


class TestFallbackDropsAdvisor:
    """`_run_fallback` strips `advisor` whenever the fallback brain's
    model_namespace isn't anthropic; an anthropic->anthropic fallback keeps it."""

    def test_anthropic_to_native_fallback_drops_advisor(self, tmp_path):
        from istota.brain._fallback import reset_availability_breaker
        from istota.brain._types import BrainResult

        reset_availability_breaker()
        primary = _FakeAnthropicBrain()
        primary.execute = lambda req: BrainResult(
            False, "usage limit", stop_reason="usage_limit"
        )
        fallback = _FakeNativeBrain()

        config = _make_config(tmp_path)
        config.advisor_model = "opus"
        config.brain = BrainConfig(kind="claude_code", fallback="native")
        config.security.sandbox_enabled = False

        def fake_make_brain(bc):
            return primary if getattr(bc, "kind", "") == "claude_code" else fallback

        patches = _patch_executor() + [
            patch("istota.executor.make_brain", side_effect=fake_make_brain),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification"),
        ]
        with contextmanager_chain(patches):
            task = _task(source_type="cli", model="")
            execute_task(task, config, [])

        assert fallback.received_reqs
        assert fallback.received_reqs[0].advisor == ""

        reset_availability_breaker()

    def test_anthropic_to_anthropic_fallback_keeps_advisor(self, tmp_path):
        from istota.brain._fallback import reset_availability_breaker
        from istota.brain._types import BrainResult

        reset_availability_breaker()
        primary = _FakeAnthropicBrain()
        primary.execute = lambda req: BrainResult(
            False, "usage limit", stop_reason="usage_limit"
        )
        fallback = _FakeAnthropicBrain()

        config = _make_config(tmp_path)
        config.advisor_model = "opus"
        config.brain = BrainConfig(kind="claude_code", fallback="tmux_claude")
        config.security.sandbox_enabled = False

        def fake_make_brain(bc):
            return primary if getattr(bc, "kind", "") == "claude_code" else fallback

        patches = _patch_executor() + [
            patch("istota.executor.make_brain", side_effect=fake_make_brain),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification"),
        ]
        with contextmanager_chain(patches):
            task = _task(source_type="cli", model="")
            execute_task(task, config, [])

        assert fallback.received_reqs
        assert fallback.received_reqs[0].advisor == "opus"

        reset_availability_breaker()
