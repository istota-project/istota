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
from istota.brain._events import TextDeltaEvent
from istota.brain._types import BrainResult
from istota.config import BrainConfig
from istota.executor import (
    FALLBACK_EXHAUSTED_MARKER,
    _run_fallback,
    execute_task,
    fallback_notice_text,
)

# Reuse the streaming test harness (config/task/patches).
from tests.test_executor_streaming import (
    _RecordingSubscriber,
    _make_config,
    _make_task,
    _patch_executor,
    _writer,
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
        # Mirrors the real brains: claude_code/tmux_claude share "anthropic",
        # native is "openai_compat".
        self.model_namespace = (
            "anthropic" if kind in ("claude_code", "tmux_claude") else "openai_compat"
        )

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
    recorder=None,
):
    config = _make_config(tmp_path)
    config.brain = BrainConfig(
        kind=primary_kind,
        fallback=fallback,
        fallback_on_transient=fallback_on_transient,
        fallback_cooldown_seconds=cooldown,
    )
    config.security.sandbox_enabled = False

    # ISSUE-362: no implicit target for any kind — the fake fallback brain is
    # named by what the config asked for, nothing else.
    fallback_kind = fallback
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
            # An event writer only where a test asked for one: the stream-surface
            # tests are the only ones that assert on events, and threading a
            # writer through the rest would put every rerouted task on the
            # streaming path for no gain.
            kw = {}
            if recorder is not None:
                kw["event_writer"] = _writer(task, config, subscriber=recorder)
            results.append(execute_task(task, config, [], **kw))
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
        # ISSUE-362: the reroute is gated on a configured fallback; the operator
        # alert is not. It fires here and says there is nothing to reroute to.
        assert len(alerts) == 1
        message, purpose = alerts[0]
        assert purpose == "alert"
        assert "no fallback configured" in message
        assert "falling back to" not in message

    def test_breaker_opens_without_a_fallback(self, tmp_path):
        """ISSUE-362: the breaker is what the sleep cycle reads, so a
        fallback-less deployment must still record its primary going down."""
        _results, _primary, fb, _alerts = _run(
            tmp_path,
            fallback="",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        assert fb.calls == 0
        assert get_availability_breaker().should_skip("claude_code", 900) is True

    def test_no_fallback_still_calls_the_primary_while_cooling_down(self, tmp_path):
        """An open breaker must not skip a primary there is nothing to replace."""
        _results, primary, fb, _alerts = _run(
            tmp_path,
            fallback="",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
            n_runs=2,
        )
        assert primary.calls == 2
        assert fb.calls == 0


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


class TestCooldownEndsAtTheQuotaReset:
    """ISSUE-374, through the task path the failure was observed on."""

    def _cache_reset_in(self, tmp_path, seconds):
        import time
        from datetime import datetime, timezone

        import istota.subscription_usage as su

        resets_at = datetime.fromtimestamp(
            time.time() + seconds, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        su.write_cache(
            su.cache_path(tmp_path),
            su.UsageSnapshot(
                fetched_at=time.time(),
                windows=(
                    su.UsageWindow(
                        key="session", label="5-hour", percent=100.0,
                        resets_at=resets_at, resets_in_seconds=int(seconds),
                    ),
                ),
                source="fetch",
            ),
        )

    def test_the_breaker_ends_at_the_reset_not_the_flat_cooldown(self, tmp_path):
        self._cache_reset_in(tmp_path, 660)
        _results, _primary, _fb, alerts = _run(
            tmp_path,
            cooldown=3600,
            primary_result=BrainResult(
                False, "session limit · resets 7pm", stop_reason="usage_limit",
            ),
        )
        remaining = get_availability_breaker().remaining("claude_code")
        assert remaining is not None
        assert 600 < remaining <= 660
        # The operator alert names the window in force, not the configured
        # ceiling — it is the one message whose job is to say when the primary
        # comes back.
        assert len(alerts) == 1
        assert "3600s" not in alerts[0][0]
        assert "for 660s" in alerts[0][0] or "for 659s" in alerts[0][0]

    def test_the_alert_names_the_flat_window_when_there_is_no_reset(self, tmp_path):
        _results, _primary, _fb, alerts = _run(
            tmp_path,
            cooldown=3600,
            primary_result=BrainResult(False, "session limit", stop_reason="usage_limit"),
        )
        assert len(alerts) == 1
        assert "for 3600s" in alerts[0][0]

    def test_the_shipped_cooldown_still_caps_a_distant_reset(self, tmp_path):
        """At the shipped 900s default the ceiling governs a five-hour window."""
        self._cache_reset_in(tmp_path, 5 * 3600)
        _run(
            tmp_path,
            cooldown=900,
            primary_result=BrainResult(False, "session limit", stop_reason="usage_limit"),
        )
        remaining = get_availability_breaker().remaining("claude_code")
        assert 890 < remaining <= 900

    def test_the_published_record_carries_the_same_window(self, tmp_path):
        import time

        from istota.brain_availability import read_unavailable

        self._cache_reset_in(tmp_path, 660)
        _run(
            tmp_path,
            cooldown=3600,
            primary_result=BrainResult(False, "session limit", stop_reason="usage_limit"),
        )
        from istota.config import Config

        # `_run` builds its own config; this one only has to name the same file.
        probe = Config(db_path=tmp_path / "test.db")
        now = time.time()
        assert read_unavailable(probe, "claude_code", now=now + 600) is not None
        # A flat hour would still be open here; the reset window is not.
        assert read_unavailable(probe, "claude_code", now=now + 700) is None

    def test_no_usage_cache_keeps_the_flat_cooldown(self, tmp_path):
        _run(
            tmp_path,
            cooldown=3600,
            primary_result=BrainResult(False, "session limit", stop_reason="usage_limit"),
        )
        remaining = get_availability_breaker().remaining("claude_code")
        assert 3590 < remaining <= 3600


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
    def test_tmux_reruns_configured_fallback_breaker_closed(self, tmp_path):
        results, primary, fb, alerts = _run(
            tmp_path,
            primary_kind="tmux_claude",
            fallback="claude_code",
            primary_result=BrainResult(False, "not ready", stop_reason="fallback"),
        )
        assert results[0][0] is True
        assert fb.calls == 1  # claude_code fallback ran
        # "fallback" is not in the cooldown set → availability breaker stays closed.
        assert get_availability_breaker().should_skip("tmux_claude", 900) is False

    @pytest.mark.parametrize(
        "fallback,expected,forbidden",
        [
            ("claude_code", "falling back.", "no fallback configured"),
            ("", "no fallback configured, so tasks keep failing.", "falling back."),
        ],
    )
    def test_the_circuit_open_alert_says_whether_anything_takes_over(
        self, tmp_path, fallback, expected, forbidden
    ):
        """ISSUE-362: the alert used to promise a reroute unconditionally.

        It reaches an operator, and on a deployment with no fallback the old
        wording told them their tasks were being served when they were failing.
        """
        with patch(
            "istota.brain.tmux_claude.consume_circuit_open_alert", return_value=True
        ):
            _results, _primary, _fb, alerts = _run(
                tmp_path,
                primary_kind="tmux_claude",
                fallback=fallback,
                primary_result=BrainResult(
                    False, "not ready", stop_reason="fallback"
                ),
            )
        circuit = [m for m, _p in alerts if "circuit opened" in m]
        assert len(circuit) == 1
        assert expected in circuit[0]
        assert forbidden not in circuit[0]

    def test_tmux_without_a_configured_fallback_does_not_reroute(self, tmp_path):
        """ISSUE-362: the implicit tmux -> claude_code target is gone.

        This is the pairing that was unconfigurable before: an empty `fallback`
        on a tmux primary now means what it says.
        """
        results, primary, fb, _alerts = _run(
            tmp_path,
            primary_kind="tmux_claude",
            fallback="",
            primary_result=BrainResult(False, "not ready", stop_reason="fallback"),
        )
        assert results[0][0] is False
        assert primary.calls == 1
        assert fb.calls == 0


class TestFallbackIsVisibleOnStreamSurfaces:
    """ISSUE-278: a fallback used to be silent on every stream surface.

    The task ran for minutes with `task_started` as its only event, so nothing
    told the reader that the primary had failed or — when a pin was dropped —
    that the answer was coming from a different model than the room is
    configured for. The executor emits a `brain_fallback` event at the moment
    it reroutes, before the fallback brain runs, so the notice lands in the
    silence rather than after it.
    """

    def test_event_emitted_when_primary_fails(self, tmp_path):
        rec = _RecordingSubscriber()
        results, _primary, fb, _a = _run(
            tmp_path,
            recorder=rec,
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        assert results[0][0] is True
        events = [e for e in rec.events if e.kind == "brain_fallback"]
        assert len(events) == 1
        p = events[0].payload
        assert p["primary"] == "claude_code"
        assert p["fallback"] == "native"
        assert p["reason"] == "usage_limit"
        assert p["text"]

    def test_event_precedes_the_fallback_run(self, tmp_path):
        """The notice is the point: emitted before the fallback brain is called,
        not after it returns. Emitting afterwards would land it at the end of
        the same silence it exists to break."""
        rec = _RecordingSubscriber()
        seen_at_execute = []

        class _Watcher(_FakeBrain):
            def execute(self, req):
                seen_at_execute.append([e.kind for e in rec.events])
                return super().execute(req)

        # Swap the fallback brain for one that snapshots the event log as it
        # starts, so ordering is asserted against the real call, not a proxy.
        import istota.executor as ex

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", fallback="native",
                                   fallback_cooldown_seconds=900)
        config.security.sandbox_enabled = False
        primary = _FakeBrain(
            "claude_code",
            BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        fb = _Watcher(
            "native",
            BrainResult(True, "answer", stop_reason="completed", model_used="fb-model"),
        )
        patches = _patch_executor() + [
            patch("istota.executor.make_brain",
                  side_effect=lambda bc: primary if getattr(bc, "kind", "") == "claude_code" else fb),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification", side_effect=lambda *a, **k: None),
        ]
        with contextmanager_chain(patches):
            task = _make_task(source_type="cli")
            ex.execute_task(task, config, [], event_writer=_writer(task, config, subscriber=rec))

        assert seen_at_execute, "fallback brain was never called"
        assert "brain_fallback" in seen_at_execute[0]

    def test_event_names_the_model_when_the_alias_carried_over(self, tmp_path):
        rec = _RecordingSubscriber()
        _run(
            tmp_path,
            recorder=rec,
            task_model="smart",
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        p = [e for e in rec.events if e.kind == "brain_fallback"][0].payload
        assert p["model"] == "native-smart-model"
        # Always present, per the payload contract in events.py.
        assert p["dropped_pin"] == ""
        assert "native-smart-model" in p["text"]

    def test_event_names_the_dropped_pin(self, tmp_path):
        """The reported case. `opus-high` can't cross the provider boundary, so
        the fallback runs on its own default and the *pin* is what the notice
        has to name — the resolved model isn't known until the run returns."""
        rec = _RecordingSubscriber()
        _run(
            tmp_path,
            recorder=rec,
            task_model="opus-high",
            primary_result=BrainResult(
                False, "API Error: 529", stop_reason="transient_api_error",
            ),
            fallback_on_transient=True,
        )
        p = [e for e in rec.events if e.kind == "brain_fallback"][0].payload
        assert p["dropped_pin"] == "opus-high"
        assert p["model"] == ""
        assert "opus-high" in p["text"]

    def test_event_emitted_on_the_cooldown_path(self, tmp_path):
        """The steady state once the breaker opens: every task for the window
        skips the primary entirely. Those runs are degraded too, so they carry
        the notice as well."""
        rec = _RecordingSubscriber()
        results, primary, fb, _a = _run(
            tmp_path,
            recorder=rec,
            n_runs=2,
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        assert primary.calls == 1 and fb.calls == 2  # second task skipped the primary
        events = [e for e in rec.events if e.kind == "brain_fallback"]
        assert len(events) == 2
        assert events[1].payload["reason"] == "cooldown"

    def test_no_event_when_no_fallback_is_taken(self, tmp_path):
        rec = _RecordingSubscriber()
        _run(
            tmp_path,
            recorder=rec,
            primary_result=BrainResult(True, "ok", stop_reason="completed", model_used="cc"),
        )
        assert [e for e in rec.events if e.kind == "brain_fallback"] == []

    def test_no_event_when_the_fallback_brain_cannot_be_built(self, tmp_path):
        """Construction failed → the primary's result stands and no reroute
        happened, so a notice would report a substitution that never occurred."""
        rec = _RecordingSubscriber()
        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", fallback="native",
                                   fallback_cooldown_seconds=900)
        config.security.sandbox_enabled = False
        primary = _FakeBrain(
            "claude_code",
            BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )

        def _make(bc):
            if getattr(bc, "kind", "") == "claude_code":
                return primary
            raise RuntimeError("misconfigured nested block")

        patches = _patch_executor() + [
            patch("istota.executor.make_brain", side_effect=_make),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification", side_effect=lambda *a, **k: None),
        ]
        with contextmanager_chain(patches):
            task = _make_task(source_type="cli")
            execute_task(task, config, [], event_writer=_writer(task, config, subscriber=rec))

        assert [e for e in rec.events if e.kind == "brain_fallback"] == []

    def test_a_failing_subscriber_does_not_break_the_fallback(self, tmp_path):
        """The notice is cosmetic; the reroute is not. A subscriber that throws
        must not cost the user the answer. (This one is caught inside
        `EventWriter.emit`, which never lets a subscriber escape — the guard in
        `_run_fallback` is for the emitter itself, tested below.)"""
        class _Boom:
            def on_event(self, event):
                raise RuntimeError("kaboom")

            def on_finish(self):
                pass

        results, _primary, fb, _a = _run(
            tmp_path,
            recorder=_Boom(),
            primary_result=BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        assert results[0][0] is True
        assert results[0][1] == "fallback answer"
        assert fb.calls == 1

    def test_a_raising_on_start_hook_does_not_break_the_fallback(self, tmp_path):
        """The guard in `_run_fallback` itself. Nothing between the hook and the
        `execute` call may turn a recoverable reroute into a failed task, so the
        hook is called defensively even though today's emitter is total."""
        from istota.brain._types import BrainRequest

        called = []

        class _Boom(_FakeBrain):
            def execute(self, req):
                called.append(req)
                return super().execute(req)

        fb = _Boom("native", BrainResult(True, "answer", stop_reason="completed"))
        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", fallback="native")
        task = _make_task(source_type="cli")
        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )

        with contextmanager_chain([
            patch("istota.executor.make_brain", return_value=fb),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
        ]):
            result, _pin, _effort = _run_fallback(
                config, config.brain, "native", task, req,
                on_start=lambda *_: 1 / 0,
            )

        assert called, "a throwing hook stopped the fallback from running"
        assert result is not None and result.success is True

    def test_the_fallback_copy_keeps_the_prepared_images(self, tmp_path):
        """`_dc.replace` names only model / effort / advisor / is_fallback, so
        every other field rides across untouched — including `images`. That is
        what lets the fallback brain make its own vision-capability decision
        instead of answering an image-bearing task blind, so it is pinned rather
        than assumed."""
        from istota.brain._types import BrainRequest, ImageInput

        fb = _FakeBrain("native", BrainResult(True, "answer", stop_reason="completed"))
        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", fallback="native")
        task = _make_task(source_type="cli")
        images = [
            ImageInput(
                path=tmp_path / "shot.png",
                media_type="image/png",
                display_name="shot.png",
            )
        ]
        req = BrainRequest(
            prompt="p",
            allowed_tools=[],
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
            images=images,
        )

        with contextmanager_chain([
            patch("istota.executor.make_brain", return_value=fb),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
        ]):
            _run_fallback(config, config.brain, "native", task, req)

        assert fb.received_reqs[0].images == images

    def test_the_reroute_settles_the_primarys_buffered_stream(self, tmp_path):
        """A reroute is a stream boundary. Whatever the primary streamed into
        the shared delta buffer must be resolved *before* the notice, or the
        fallback's first `text_delta` opens with the failed brain's abandoned
        tail — presented, under a notice saying the primary failed, as the
        fallback's own words."""
        rec = _RecordingSubscriber()
        gate = 20  # small enough that the primary's narration unlocks

        def _streaming_primary(req):
            # The primary streams past the narration gate, then fails.
            for _ in range(3):
                req.on_progress(TextDeltaEvent(text="primary-tail "))
            return BrainResult(False, "529", stop_reason="transient_api_error")

        config = _make_config(tmp_path)
        config.brain = BrainConfig(
            kind="claude_code", fallback="native", fallback_on_transient=True,
        )
        config.scheduler.stream_text_gate_chars = gate
        config.security.sandbox_enabled = False

        primary = _FakeBrain("claude_code", None)
        primary.execute = _streaming_primary
        fb = _FakeBrain(
            "native",
            BrainResult(True, "fallback answer", stop_reason="completed"),
        )

        patches = _patch_executor() + [
            patch("istota.executor.make_brain",
                  side_effect=lambda bc: primary if getattr(bc, "kind", "") == "claude_code" else fb),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
            patch("istota.transport.registry.task_is_stream_surface", return_value=True),
            patch("istota.notifications.send_notification", side_effect=lambda *a, **k: None),
        ]
        with contextmanager_chain(patches):
            task = _make_task(source_type="web")
            execute_task(task, config, [], event_writer=_writer(task, config, subscriber=rec))

        kinds = [e.kind for e in rec.events]
        assert "brain_fallback" in kinds
        notice_at = kinds.index("brain_fallback")
        # Every delta carrying the primary's text sits BEFORE the notice; none
        # after it re-emits words the failed brain produced.
        after = [
            e for e in rec.events[notice_at + 1:]
            if e.kind == "text_delta" and "primary-tail" in e.payload.get("text", "")
        ]
        assert after == [], "the primary's abandoned text leaked past the reroute"


class TestTheRequestSaysWhichBrainRunItIs:
    """ISSUE-378 — the fallback run's request is marked, so its artifacts are.

    Both runs of one rerouted attempt carry the same `task_id` and the same
    `attempt`, because a reroute deliberately does not increment either. The
    usage table tells them apart on `is_fallback`; without the same flag on the
    request, nothing a brain writes per attempt can. `NativeBrain`'s session log
    is the first consumer and the reason the field exists.
    """

    def test_the_fallback_request_is_marked_as_the_fallback(self, tmp_path):
        from istota.brain._types import BrainRequest

        fb = _FakeBrain("native", BrainResult(True, "answer", stop_reason="completed"))
        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", fallback="native")
        task = _make_task(source_type="cli")
        req = BrainRequest(
            prompt="p", allowed_tools=[], cwd=tmp_path, env={}, timeout_seconds=60,
        )
        assert req.is_fallback is False

        with contextmanager_chain([
            patch("istota.executor.make_brain", return_value=fb),
            patch("istota.executor._native_with_user_key", side_effect=lambda nc, *a, **k: nc),
        ]):
            _run_fallback(config, config.brain, "native", task, req)

        assert fb.received_reqs[0].is_fallback is True
        # The caller's own request is not mutated — `_dc.replace` copies, and
        # the primary's result is persisted from that same object afterwards.
        assert req.is_fallback is False

    def test_a_rerouted_attempt_marks_only_the_second_run(self, tmp_path):
        """Through `execute_task`, so the primary's request is covered too."""
        _results, primary, fb, _alerts = _run(
            tmp_path,
            primary_result=BrainResult(
                False, "usage limit reached", stop_reason="usage_limit"
            ),
        )
        assert [r.is_fallback for r in primary.received_reqs] == [False]
        assert [r.is_fallback for r in fb.received_reqs] == [True]

    def test_the_breaker_path_marks_the_run_it_substitutes(self, tmp_path):
        """The cooldown path runs the fallback with no primary call at all, so
        a flag derived from "a primary result was held" would read false for
        every task in the window — the same trap `_ran_fallback` documents."""
        _results, primary, fb, _alerts = _run(
            tmp_path,
            primary_result=BrainResult(
                False, "usage limit reached", stop_reason="usage_limit"
            ),
            n_runs=2,
        )
        # Task 1 probes the primary and reroutes; task 2 skips it entirely.
        assert primary.calls == 1
        assert [r.is_fallback for r in fb.received_reqs] == [True, True]


class TestFallbackNoticeText:
    """The one place the notice's wording lives. Every stream surface renders
    `payload["text"]` rather than composing its own sentence, so a surface can't
    drift from the others."""

    def test_names_both_brains_and_the_reason(self):
        text = fallback_notice_text("claude_code", "usage_limit", "native", "", None)
        assert text == (
            "My `claude_code` brain overheated when it hit its usage limit. "
            "I'm using my `native` backup. I might say weird stuff, but I'm doing my best."
        )

    def test_names_the_model_when_it_is_known(self):
        text = fallback_notice_text("claude_code", "usage_limit", "native", "vendor/model-x", None)
        assert "vendor/model-x" in text

    def test_names_the_dropped_pin_instead_of_a_model(self):
        text = fallback_notice_text(
            "claude_code", "transient_api_error", "native", "", "claude-opus-5",
        )
        assert "claude-opus-5" in text
        # The resolved model is unknown at this point — don't imply otherwise.
        assert "default" in text

    def test_cooldown_reads_as_cooling_down_not_as_a_fresh_failure(self):
        text = fallback_notice_text("claude_code", "cooldown", "native", "", None)
        assert text.startswith("My `claude_code` brain is still cooling down")
        assert "overheated" not in text

    @pytest.mark.parametrize(
        ("reason", "plain_language_reason"),
        [
            ("not_found", "I can't find its CLI"),
            ("transient_api_error", "its provider returned an error"),
            ("fallback", "it couldn't start"),
        ],
    )
    def test_non_quota_failures_are_straightforward(self, reason, plain_language_reason):
        text = fallback_notice_text("claude_code", reason, "native", "", None)
        assert plain_language_reason in text
        assert "overheated" not in text

    def test_unknown_reason_passes_through(self):
        text = fallback_notice_text("claude_code", "weird_new_reason", "native", "", None)
        assert "weird_new_reason" in text


class TestOneNoticePerTurn:
    """ISSUE-361: one notice per user message, not one per failover attempt.

    A failing task is retried by the scheduler under the *same* task id, and the
    event log spans every attempt (nothing wipes it between retries — that is
    what `EventWriter` resumes its seq from). Each attempt emitted its own
    notice, so one user message showed the banner stacked three deep: the first
    naming the real cause, the two behind it repeating "cooling down" with
    nothing new in them.

    The reroute itself is unchanged — every attempt still runs on the fallback.
    Only the sentence about it is said once.
    """

    def _run_attempts(self, tmp_path, task_ids, *, recorder=None):
        """Run one execution per entry in `task_ids`, in order, as the retry
        ladder does: the same task id repeated is a retry, a fresh EventWriter
        each time, and DB writes ON so the log persists across attempts the way
        it does in the daemon. One shared database throughout, so a dedup that
        forgot to scope by task would be caught here rather than pass.

        Returns ``({task_id: [brain_fallback rows]}, primary, fallback)``.
        """
        from istota import db as _db
        from istota.events import EventWriter

        config = _make_config(tmp_path)
        _db.init_db(config.db_path)
        config.brain = BrainConfig(kind="claude_code", fallback="native",
                                   fallback_cooldown_seconds=900)
        config.security.sandbox_enabled = False
        primary = _FakeBrain(
            "claude_code",
            BrainResult(False, "usage limit reached", stop_reason="usage_limit"),
        )
        fb = _FakeBrain(
            "native",
            BrainResult(True, "fallback answer", stop_reason="completed",
                        model_used="fb-model"),
        )
        patches = _patch_executor() + [
            patch(
                "istota.executor.make_brain",
                side_effect=lambda bc: (
                    primary if getattr(bc, "kind", "") == "claude_code" else fb
                ),
            ),
            patch("istota.executor._native_with_user_key",
                  side_effect=lambda nc, *a, **k: nc),
            patch("istota.notifications.send_notification",
                  side_effect=lambda *a, **k: None),
        ]
        seen = {}
        with contextmanager_chain(patches):
            for task_id in task_ids:
                task = _make_task(id=task_id, source_type="cli",
                                  attempt_count=seen.get(task_id, 0))
                seen[task_id] = seen.get(task_id, 0) + 1
                writer = EventWriter(task.id, str(config.db_path))
                if recorder is not None:
                    writer.subscribe(recorder)
                execute_task(task, config, [], event_writer=writer)
        notices = {}
        with _db.get_db(config.db_path) as conn:
            for task_id in set(task_ids):
                notices[task_id] = [
                    r for r in _db.get_task_events(conn, task_id)
                    if r["kind"] == "brain_fallback"
                ]
        return notices, primary, fb

    def test_three_attempts_of_one_task_log_one_notice(self, tmp_path):
        notices, _primary, fb = self._run_attempts(tmp_path, [1, 1, 1])
        assert fb.calls == 3, "every attempt still reroutes"
        assert len(notices[1]) == 1

    def test_the_notice_kept_is_the_one_naming_the_real_cause(self, tmp_path):
        """First-notice-wins, not last: attempt 1 knows *why* the primary went
        away, and the attempts behind it only know that it is cooling down."""
        notices, _primary, _fb = self._run_attempts(tmp_path, [1, 1, 1])
        assert [n["payload"]["reason"] for n in notices[1]] == ["usage_limit"]

    def test_subscribers_are_told_once_too(self, tmp_path):
        """The suppression is at the emit, so an in-process surface (the REPL)
        sees one event as well — not a persisted row that hides three."""
        rec = _RecordingSubscriber()
        self._run_attempts(tmp_path, [1, 1, 1], recorder=rec)
        assert len([e for e in rec.events if e.kind == "brain_fallback"]) == 1

    def test_a_different_task_gets_its_own_notice(self, tmp_path):
        """The dedup is per turn. The next user message is a different task and
        is owed its own explanation, cooldown window or not — even though both
        tasks' events share one table."""
        notices, _primary, _fb = self._run_attempts(tmp_path, [1, 1, 2])
        assert len(notices[1]) == 1
        assert len(notices[2]) == 1
        assert notices[2][0]["payload"]["reason"] == "cooldown"
