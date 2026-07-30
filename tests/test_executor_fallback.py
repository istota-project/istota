"""Executor-level brain fallback (brain-fallback spec, Stage 4).

The generalized availability failover: when the primary brain is unavailable and
a fallback is configured, execute_task reruns the same attempt through the
fallback brain — no attempt increment — re-resolving the model across the
provider boundary, opening the availability breaker on persistent conditions,
firing one operator alert, and appending a visible note for a dropped pin.
"""

from unittest.mock import patch

import pytest

from istota.brain._fallback import get_availability_breaker, reset_availability_breaker
from istota.brain._types import BrainResult
from istota.config import BrainConfig
from istota.executor import FALLBACK_EXHAUSTED_MARKER, execute_task

# Reuse the streaming test harness (config/task/patches).
from tests.test_executor_streaming import (
    _make_config,
    _make_task,
    _patch_executor,
    contextmanager_chain,
)


class _FakeBrain:
    def __init__(self, kind, result, resolve_map=None, resolve_alias_map=None):
        self.kind = kind
        self.result = result
        self.calls = 0
        self.received_reqs = []
        self.resolve_calls = []
        self._resolve_map = resolve_map or {}
        # name -> (model, effort) — the fallback brain's namespace resolution.
        self._resolve_alias_map = resolve_alias_map or {}

    def execute(self, req):
        self.calls += 1
        self.received_reqs.append(req)
        return self.result

    def resolve_model_name(self, name):
        self.resolve_calls.append(name)
        return self._resolve_map.get(name, (name or "").strip())

    def resolve_alias(self, a):
        return self._resolve_alias_map.get(a)

    def list_aliases(self):
        return []

    def validate_alias_override(self, r, t):
        return []


@pytest.fixture(autouse=True)
def _reset():
    reset_availability_breaker()
    yield
    reset_availability_breaker()


def _run(
    tmp_path,
    *,
    primary_kind="claude_code",
    primary_result,
    fallback="native",
    fallback_result=None,
    fallback_resolve_map=None,
    fallback_resolve_alias_map=None,
    task_model="",
    fallback_on_transient=False,
    cooldown=900,
    n_runs=1,
):
    config = _make_config(tmp_path)
    config.brain = BrainConfig(
        kind=primary_kind,
        fallback=fallback,
        fallback_on_transient=fallback_on_transient,
        fallback_cooldown_seconds=cooldown,
    )
    config.security.sandbox_enabled = False

    fallback_kind = fallback or ("claude_code" if primary_kind == "tmux_claude" else "")
    primary = _FakeBrain(primary_kind, primary_result)
    fb = _FakeBrain(
        fallback_kind,
        fallback_result if fallback_result is not None
        else BrainResult(True, "fallback answer", stop_reason="completed", model_used="fb-model"),
        resolve_map=fallback_resolve_map or {"smart": "native-smart-model"},
        resolve_alias_map=fallback_resolve_alias_map,
    )

    def fake_make_brain(bc):
        return primary if getattr(bc, "kind", "") == primary_kind else fb

    alerts = []

    def fake_send(config, user_id, message, **kw):
        alerts.append((message, kw.get("purpose")))

    results = []
    patches = _patch_executor() + [
        patch("istota.executor.make_brain", side_effect=fake_make_brain),
        patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
        patch("istota.notifications.send_notification", side_effect=fake_send),
    ]
    with contextmanager_chain(patches):
        for _ in range(n_runs):
            task = _make_task(source_type="cli", model=task_model)
            results.append(execute_task(task, config, []))
    return results, primary, fb, alerts


class TestBasicReroute:
    def test_usage_limit_reroutes_to_fallback(self, tmp_path):
        results, primary, fb, alerts = _run(
            tmp_path,
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        success, result, _a, _t = results[0]
        assert success is True
        assert result == "fallback answer"
        assert primary.calls == 1
        assert fb.calls == 1
        # One operator alert on breaker open.
        assert len(alerts) == 1
        assert alerts[0][1] == "alert"

    def test_not_found_reroutes(self, tmp_path):
        results, primary, fb, _alerts = _run(
            tmp_path,
            primary_result=BrainResult(False, "cli missing", stop_reason="not_found"),
        )
        assert results[0][0] is True
        assert fb.calls == 1


class TestNoReroute:
    @pytest.mark.parametrize("reason", ["oom", "timeout", "error", "cancelled"])
    def test_task_level_failures_do_not_reroute(self, tmp_path, reason):
        results, primary, fb, alerts = _run(
            tmp_path,
            primary_result=BrainResult(False, "boom", stop_reason=reason),
        )
        assert fb.calls == 0
        assert primary.calls == 1
        assert results[0][0] is False
        assert alerts == []

    def test_no_fallback_configured_flows_through(self, tmp_path):
        results, primary, fb, alerts = _run(
            tmp_path,
            fallback="",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        assert fb.calls == 0
        assert results[0][0] is False
        assert alerts == []


class TestTransientGate:
    def test_transient_rerouted_by_default(self, tmp_path):
        # ISSUE-212: a persistent capacity error (529) is exactly what the
        # fallback exists to absorb, so the reroute is on out of the box.
        assert BrainConfig().fallback_on_transient is True
        results, primary, fb, _a = _run(
            tmp_path,
            fallback_on_transient=BrainConfig().fallback_on_transient,
            primary_result=BrainResult(False, "API Error: 529 Overloaded", stop_reason="transient_api_error"),
        )
        assert fb.calls == 1
        assert results[0][0] is True

    def test_transient_not_rerouted_when_disabled(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            fallback_on_transient=False,
            primary_result=BrainResult(False, "API Error: 529 {}", stop_reason="transient_api_error"),
        )
        assert fb.calls == 0

    def test_transient_rerouted_when_enabled(self, tmp_path):
        results, primary, fb, alerts = _run(
            tmp_path,
            fallback_on_transient=True,
            primary_result=BrainResult(False, "API Error: 529 {}", stop_reason="transient_api_error"),
        )
        assert fb.calls == 1
        # transient is NOT in the cooldown set → breaker stays closed, no alert.
        assert alerts == []


class TestModelResolution:
    def test_portable_alias_reresolved(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            task_model="smart",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        # smart re-resolved in fallback namespace.
        assert "smart" in fb.resolve_calls
        assert fb.received_reqs[0].model == "native-smart-model"

    def test_portable_alias_carries_fallback_effort(self, tmp_path):
        # When the fallback brain's resolve_alias yields a (slug, effort) pair,
        # both the slug and its effort reach the fallback request — a customized
        # smart falling back claude_code→native lands on a valid slug + effort.
        results, primary, fb, _a = _run(
            tmp_path,
            task_model="smart",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
            fallback_resolve_alias_map={"smart": ("anthropic/claude-opus-4.8", "high")},
        )
        assert fb.received_reqs[0].model == "anthropic/claude-opus-4.8"
        assert fb.received_reqs[0].effort == "high"

    def test_non_portable_pin_dropped(self, tmp_path, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            results, primary, fb, _a = _run(
                tmp_path,
                task_model="opus-high",
                primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
            )
        # opus-high can't cross the boundary → fallback uses its own default (empty model).
        assert fb.received_reqs[0].model == ""
        assert any("non-portable" in r.message for r in caplog.records)


class TestVisibleNote:
    def test_note_appended_for_dropped_pin(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            task_model="opus-high",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
            fallback_result=BrainResult(True, "the real answer", stop_reason="completed", model_used="native-x"),
        )
        _s, result, _a2, _t = results[0]
        assert "the real answer" in result
        assert "Ran on" in result
        assert "opus-high" in result
        assert "native-x" in result

    def test_no_note_for_portable_alias(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            task_model="smart",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
            fallback_result=BrainResult(True, "answer", stop_reason="completed", model_used="native-smart-model"),
        )
        assert "Ran on" not in results[0][1]

    def test_no_note_for_default_model(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            task_model="",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        assert "Ran on" not in results[0][1]

    def test_no_note_on_failed_fallback(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            task_model="opus-high",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
            fallback_result=BrainResult(False, "fallback also failed", stop_reason="error"),
        )
        _s, result, _a2, _t = results[0]
        assert _s is False
        assert "Ran on" not in result


class TestBothBrainsUnavailable:
    """ISSUE-212: the user must never be handed a bare provider error as the
    final message. When the fallback is *also* unavailable, the failure text
    carries a marker the scheduler turns into a legible "both unavailable"."""

    def test_marker_prefixed_when_fallback_also_unavailable(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            fallback_on_transient=True,
            primary_result=BrainResult(
                False, "API Error: 529 Overloaded", stop_reason="transient_api_error",
            ),
            fallback_result=BrainResult(
                False, "API Error: 503 Service Unavailable",
                stop_reason="transient_api_error",
            ),
        )
        success, result, _a2, _t = results[0]
        assert success is False
        assert FALLBACK_EXHAUSTED_MARKER in result
        # The underlying cause survives for the friendly formatter + the logs.
        assert "503" in result

    def test_no_marker_for_task_level_fallback_failure(self, tmp_path):
        # A fallback that timed out is a task-level outcome, not an availability
        # failure — the "both unavailable" wording would be wrong.
        results, primary, fb, _a = _run(
            tmp_path,
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
            fallback_result=BrainResult(
                False, "Task execution timed out after 30 minutes", stop_reason="timeout",
            ),
        )
        assert FALLBACK_EXHAUSTED_MARKER not in results[0][1]

    def test_no_marker_on_successful_fallback(self, tmp_path):
        results, primary, fb, _a = _run(
            tmp_path,
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        assert FALLBACK_EXHAUSTED_MARKER not in results[0][1]


class TestStickiness:
    def test_cooldown_skips_primary_on_second_task(self, tmp_path):
        results, primary, fb, alerts = _run(
            tmp_path,
            n_runs=2,
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        # First task hit the primary; second skipped it (breaker open).
        assert primary.calls == 1
        assert fb.calls == 2
        # Only one alert (breaker opened once).
        assert len(alerts) == 1

    def test_cooldown_disabled_probes_primary_each_time(self, tmp_path):
        results, primary, fb, alerts = _run(
            tmp_path,
            n_runs=2,
            cooldown=0,
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        # cooldown=0 → no stickiness: primary probed both times, no alert.
        assert primary.calls == 2
        assert fb.calls == 2
        assert alerts == []


class TestPrimaryHealthyClosesBreaker:
    def test_healthy_primary_runs_and_breaker_stays_closed(self, tmp_path):
        # A healthy primary run takes the record_success branch (elif success):
        # no fallback, no alert, breaker closed.
        results, primary, fb, alerts = _run(
            tmp_path,
            primary_result=BrainResult(True, "ok", stop_reason="completed", model_used="cc"),
        )
        assert results[0][0] is True
        assert primary.calls == 1
        assert fb.calls == 0
        assert alerts == []
        assert get_availability_breaker().should_skip("claude_code", 900) is False

    def test_healthy_primary_closes_open_breaker_after_cooldown(self, tmp_path, monkeypatch):
        # Breaker open, cooldown elapsed → task probes the primary; on success the
        # breaker is closed via record_success.
        import istota.brain._fallback as fb_mod
        clock = [0.0]
        monkeypatch.setattr(fb_mod.time, "monotonic", lambda: clock[0])
        get_availability_breaker().open("claude_code", 900)
        clock[0] = 901.0  # cooldown elapsed → should_skip False → primary probed
        results, primary, fb, _a = _run(
            tmp_path,
            cooldown=900,
            primary_result=BrainResult(True, "ok", stop_reason="completed", model_used="cc"),
        )
        assert primary.calls == 1
        assert fb.calls == 0
        # record_success cleared the entry entirely.
        assert get_availability_breaker().should_skip("claude_code", 1) is False


class TestTmuxFolding:
    def test_tmux_fallback_reruns_claude_code_breaker_closed(self, tmp_path):
        results, primary, fb, alerts = _run(
            tmp_path,
            primary_kind="tmux_claude",
            fallback="",  # effective default = claude_code
            primary_result=BrainResult(False, "not ready", stop_reason="fallback"),
        )
        assert results[0][0] is True
        assert fb.calls == 1  # claude_code fallback ran
        # "fallback" is not in the cooldown set → availability breaker stays closed.
        assert get_availability_breaker().should_skip("tmux_claude", 900) is False
