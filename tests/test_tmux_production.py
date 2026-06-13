"""Production-readiness tests for TmuxClaudeBrain.

Covers the hardening from ``Specs/Active/claude-tmux-production-readiness.md``:
the shared CLI-flag helper (§1/§8), fail-fast multi-signal completion + transient
retry (§3), the headless fallback + process-global circuit breaker (§4), the
``[brain.tmux]`` config (Data & config), observability (§7), session-label wiring
(§9), and the live transcript tailer (§10).

The tmux/claude subprocess boundary is mocked at ``_tmux`` / the session
primitives; no live tmux runs here.
"""

import json
import logging
from pathlib import Path

import pytest

from istota.brain._types import BrainRequest, BrainResult
from istota.brain import claude_code
from istota.brain.claude_code import build_claude_cli_flags
from istota.brain.tmux_claude import (
    TmuxClaudeBrain,
    _CircuitBreaker,
    _TranscriptTailer,
    consume_circuit_open_alert,
    reset_circuit_breaker,
)
from istota.config import BrainConfig, TmuxBrainConfig


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    # Skip the one-shot `claude --version` probe so tests don't shell out.
    import istota.brain.tmux_claude as mod
    monkeypatch.setattr(mod, "_VERSION_CHECKED", True)
    reset_circuit_breaker()
    yield
    reset_circuit_breaker()


def _req(tmp_path, **kw):
    base = dict(
        prompt="hi", allowed_tools=[], cwd=tmp_path,
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}, timeout_seconds=60,
    )
    base.update(kw)
    return BrainRequest(**base)


def _assistant(content, stop_reason="end_turn", msg_id="m1"):
    return {
        "type": "assistant", "uuid": f"u-{msg_id}",
        "message": {
            "id": msg_id, "role": "assistant", "model": "claude-opus-4-8",
            "stop_reason": stop_reason, "content": content,
        },
    }


def _write_transcript(tmp_path, records, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


# --------------------------------------------------------------------------
# Stage 1/8 — shared CLI flag helper
# --------------------------------------------------------------------------


class TestFlagHelper:
    def test_empty_unsupported_matches_headless_argv(self, tmp_path):
        # Golden: the headless argv ClaudeCodeBrain builds is exactly
        # ["claude","-p","-"] + build_claude_cli_flags(req) +
        # --dangerously-skip-permissions + stream flags. No --allowedTools
        # allowlist — Agent stays denied and the run skips per-tool prompts.
        sp = tmp_path / "sp.md"
        sp.write_text("system")
        req = _req(tmp_path, allowed_tools=["Bash", "Read"],
                   model="claude-opus-4-8", effort="high",
                   custom_system_prompt_path=sp, streaming=False)
        flags = build_claude_cli_flags(req)
        assert flags == [
            "--disallowedTools", "Agent", "Workflow",
            "--model", "claude-opus-4-8", "--effort", "high",
            "--system-prompt-file", str(sp),
        ]
        # And the brain's _build_command wraps it identically (plus the
        # non-interactive skip-permissions flag).
        cmd = claude_code.ClaudeCodeBrain._build_command(req)
        assert cmd == ["claude", "-p", "-"] + flags + ["--dangerously-skip-permissions"]

    def test_unsupported_flag_dropped(self, tmp_path, caplog):
        claude_code._WARNED_UNSUPPORTED_FLAGS.clear()
        req = _req(tmp_path, allowed_tools=["Bash"], model="claude-opus-4-8",
                   effort="high")
        with caplog.at_level(logging.WARNING):
            flags = build_claude_cli_flags(req, unsupported=frozenset({"--effort"}))
        assert "--effort" not in flags
        assert "--model" in flags and "--disallowedTools" in flags
        assert any("unsupported_flag" in r.message for r in caplog.records)

    def test_unsupported_warning_is_once_per_process(self, tmp_path, caplog):
        claude_code._WARNED_UNSUPPORTED_FLAGS.clear()
        req = _req(tmp_path, model="claude-opus-4-8")
        with caplog.at_level(logging.WARNING):
            build_claude_cli_flags(req, unsupported=frozenset({"--model"}))
            build_claude_cli_flags(req, unsupported=frozenset({"--model"}))
        warns = [r for r in caplog.records if "unsupported_flag" in r.message]
        assert len(warns) == 1

    def test_empty_allowed_tools_skips_tool_flags(self, tmp_path):
        flags = build_claude_cli_flags(_req(tmp_path, allowed_tools=[]))
        assert "--allowedTools" not in flags
        assert "--disallowedTools" not in flags


# --------------------------------------------------------------------------
# Stage 3 — fail-fast multi-signal completion + transient retry
# --------------------------------------------------------------------------


class _WaitHarness:
    """Drive _wait_for_completion with a scripted pane sequence + sentinel."""

    def __init__(self, monkeypatch, *, panes, sentinel, alive=True):
        self.brain = TmuxClaudeBrain()
        self._panes = iter(panes)
        self._last = panes[-1] if panes else ""
        self._alive = alive
        monkeypatch.setattr(self.brain, "_capture", self._capture)
        monkeypatch.setattr(self.brain, "_pane_alive", lambda name: self._alive)
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        self.sentinel = sentinel

    def _capture(self, name):
        try:
            self._last = next(self._panes)
        except StopIteration:
            pass
        return self._last


class TestWaitForCompletion:
    def test_sentinel_done(self, monkeypatch, tmp_path):
        s = tmp_path / "stop.json"
        s.write_text("{}")
        h = _WaitHarness(monkeypatch, panes=["working"], sentinel=s)
        import time as _t
        status, pane = h.brain._wait_for_completion("s", s, _t.monotonic() + 100, None)
        assert status == "done"

    def test_cancel(self, monkeypatch, tmp_path):
        s = tmp_path / "stop.json"
        h = _WaitHarness(monkeypatch, panes=["working"], sentinel=s)
        import time as _t
        status, _ = h.brain._wait_for_completion(
            "s", s, _t.monotonic() + 100, lambda: True
        )
        assert status == "cancelled"

    def test_error_marker_fails_fast(self, monkeypatch, tmp_path):
        s = tmp_path / "stop.json"
        h = _WaitHarness(monkeypatch, panes=["API Error: 529 {}"], sentinel=s)
        import time as _t
        # Deadline far in the future — must return *before* it on the marker.
        status, pane = h.brain._wait_for_completion("s", s, _t.monotonic() + 9999, None)
        assert status == "error"
        assert "API Error" in pane

    def test_dead_pane_errors(self, monkeypatch, tmp_path):
        s = tmp_path / "stop.json"
        h = _WaitHarness(monkeypatch, panes=["working"], sentinel=s, alive=False)
        import time as _t
        status, _ = h.brain._wait_for_completion("s", s, _t.monotonic() + 9999, None)
        assert status == "error"

    def test_timeout_with_stall_warning(self, monkeypatch, tmp_path, caplog):
        s = tmp_path / "stop.json"
        h = _WaitHarness(monkeypatch, panes=["thinking…"], sentinel=s)
        import time as _t
        with caplog.at_level(logging.WARNING):
            status, _ = h.brain._wait_for_completion("s", s, _t.monotonic() - 1, None)
        # Past deadline immediately → timeout. (Stall-warn is exercised live; the
        # halfway math needs a positive window which an already-elapsed deadline
        # doesn't give — covered by test_stall_warns_at_halfway.)
        assert status == "timeout"

    def test_stall_warns_at_halfway(self, monkeypatch, tmp_path, caplog):
        s = tmp_path / "stop.json"
        # Provide a real monotonic clock that advances so the halfway mark trips.
        import istota.brain.tmux_claude as mod
        ticks = iter([0.0, 0.0, 6.0, 11.0])  # start, loop1, loop2(halfway), loop3(deadline)

        def fake_mono():
            try:
                return next(ticks)
            except StopIteration:
                return 999.0

        monkeypatch.setattr(mod.time, "monotonic", fake_mono)
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        brain = TmuxClaudeBrain()
        monkeypatch.setattr(brain, "_capture", lambda name: "thinking")
        monkeypatch.setattr(brain, "_pane_alive", lambda name: True)
        with caplog.at_level(logging.WARNING):
            status, _ = brain._wait_for_completion("s", s, 10.0, None)
        assert status == "timeout"
        assert any("tmux_stall" in r.message for r in caplog.records)


class _RetryHarness:
    """Mock the session lifecycle for execute(); _run_session returns scripted
    outcomes per attempt so the transient-retry loop can be exercised."""

    def __init__(self, monkeypatch, outcomes):
        self.brain = TmuxClaudeBrain()
        self._outcomes = list(outcomes)
        self.attempts = 0
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        monkeypatch.setattr(self.brain, "_cleanup_legacy_hook", lambda req: None)
        monkeypatch.setattr(self.brain, "_run_session", self._run_session)

    def _run_session(self, req, attempt):
        self.attempts += 1
        return self._outcomes[attempt]


class TestTransientRetry:
    def test_transient_then_success(self, monkeypatch, tmp_path):
        err = (BrainResult(success=False, result_text="API Error: 529 {}",
                           stop_reason="error"), True)
        ok = (BrainResult(success=True, result_text="done", stop_reason="completed"), False)
        h = _RetryHarness(monkeypatch, [err, ok])
        res = h.brain.execute(_req(tmp_path))
        assert res.success is True
        assert h.attempts == 2

    def test_non_transient_no_retry(self, monkeypatch, tmp_path):
        err = (BrainResult(success=False, result_text="API Error: 400 {}",
                           stop_reason="error"), False)
        h = _RetryHarness(monkeypatch, [err, err, err])
        res = h.brain.execute(_req(tmp_path))
        assert res.success is False
        assert res.stop_reason == "error"
        assert h.attempts == 1

    def test_transient_exhausts_retries(self, monkeypatch, tmp_path):
        err = (BrainResult(success=False, result_text="API Error: 503 {}",
                           stop_reason="error"), True)
        h = _RetryHarness(monkeypatch, [err, err, err])
        res = h.brain.execute(_req(tmp_path))
        assert res.success is False
        assert h.attempts == 3  # API_RETRY_MAX_ATTEMPTS

    def test_retry_delay_honored(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        err = (BrainResult(success=False, result_text="API Error: 529 {}",
                           stop_reason="error"), True)
        ok = (BrainResult(success=True, result_text="ok", stop_reason="completed"), False)
        h = _RetryHarness(monkeypatch, [err, ok])
        # Override the harness's no-op sleep with a spy (after construction).
        slept = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
        h.brain.execute(_req(tmp_path))
        assert mod.API_RETRY_DELAY_SECONDS in slept


# --------------------------------------------------------------------------
# Stage 4 — fallback + circuit breaker + config + observability
# --------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        b = _CircuitBreaker()
        assert b.record_launch_failure(3) is False
        assert b.record_launch_failure(3) is False
        assert b.record_launch_failure(3) is True  # threshold crossed
        assert b.should_skip(300) is True
        assert b.pop_alert() is True
        assert b.pop_alert() is False  # cleared

    def test_success_resets(self):
        b = _CircuitBreaker()
        b.record_launch_failure(2)
        b.record_launch_failure(2)
        assert b.should_skip(300) is True
        assert b.record_success() is True  # was open
        assert b.should_skip(300) is False
        assert b.consecutive == 0

    def test_cooldown_elapses(self, monkeypatch):
        import istota.brain.tmux_claude as mod
        clock = [0.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
        b = _CircuitBreaker()
        b.record_launch_failure(1)  # opens immediately
        assert b.should_skip(300) is True
        clock[0] = 301.0
        assert b.should_skip(300) is False  # cooldown elapsed → probe allowed


class _ExecHarness:
    """execute() with _run_session mocked to a fixed (result, retryable)."""

    def __init__(self, monkeypatch, result, retryable=False):
        self.brain = TmuxClaudeBrain(TmuxBrainConfig(fallback_trip_threshold=3))
        self.calls = 0
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        monkeypatch.setattr(self.brain, "_cleanup_legacy_hook", lambda req: None)

        def run(req, attempt):
            self.calls += 1
            return (result, retryable)

        monkeypatch.setattr(self.brain, "_run_session", run)


class TestFallbackAndBreaker:
    def test_launch_failure_returns_fallback(self, monkeypatch, tmp_path):
        h = _ExecHarness(monkeypatch, BrainResult(
            success=False, result_text="not ready", stop_reason="fallback"))
        res = h.brain.execute(_req(tmp_path))
        assert res.stop_reason == "fallback"

    def test_breaker_opens_and_short_circuits(self, monkeypatch, tmp_path):
        h = _ExecHarness(monkeypatch, BrainResult(
            success=False, result_text="x", stop_reason="fallback"))
        # 3 launch failures (threshold) → circuit opens.
        for _ in range(3):
            h.brain.execute(_req(tmp_path))
        calls_before = h.calls
        # Next execute short-circuits: _run_session not called.
        res = h.brain.execute(_req(tmp_path))
        assert res.stop_reason == "fallback"
        assert h.calls == calls_before  # tmux not tried
        # The open transition armed exactly one alert.
        assert consume_circuit_open_alert() is True
        assert consume_circuit_open_alert() is False

    def test_tmux_missing_records_failure_and_returns_not_found(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        brain = TmuxClaudeBrain(TmuxBrainConfig(fallback_trip_threshold=1))
        res = brain.execute(_req(tmp_path))
        assert res.stop_reason == "not_found"
        # fed the breaker (threshold 1 → opens), so the next call short-circuits.
        assert mod._BREAKER.should_skip(300) is True

    def test_success_resets_breaker(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        # Two failures, then a success.
        fail = _ExecHarness(monkeypatch, BrainResult(
            success=False, result_text="x", stop_reason="fallback"))
        fail.brain.execute(_req(tmp_path))
        fail.brain.execute(_req(tmp_path))
        ok = _ExecHarness(monkeypatch, BrainResult(
            success=True, result_text="done", stop_reason="completed"))
        ok.brain.execute(_req(tmp_path))
        assert mod._BREAKER.consecutive == 0


class TestObservability:
    def test_structured_log_line_emitted(self, monkeypatch, tmp_path, caplog):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "Hi"}])])
        brain = TmuxClaudeBrain()
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")
        for m in ("_new_session", "_launch_claude", "_inject_prompt", "_kill"):
            monkeypatch.setattr(brain, m, lambda *a, **k: None)
        monkeypatch.setattr(brain, "_wait_ready", lambda *a, **k: True)
        monkeypatch.setattr(brain, "_pane_pid", lambda *a, **k: 1)
        monkeypatch.setattr(brain, "_learn_transcript_path", lambda *a, **k: None)

        def wfc(name, sentinel, deadline, cancel_check):
            Path(sentinel).write_text(json.dumps(
                {"transcript_path": str(tr), "last_assistant_message": "Hi"}))
            return ("done", "")

        monkeypatch.setattr(brain, "_wait_for_completion", wfc)
        with caplog.at_level(logging.INFO, logger="istota.brain.tmux_claude"):
            brain.execute(_req(tmp_path, session_label="istota-7-0"))
        lines = [r.message for r in caplog.records if r.message.startswith("tmux_brain session=")]
        assert lines, "expected one tmux_brain structured line"
        line = lines[-1]
        for key in ("session=istota-7-0", "outcome=done", "ready_ms=", "wait_ms=", "tools="):
            assert key in line


class TestConfig:
    def test_empty_config_matches_defaults(self):
        from istota.brain.tmux_claude import (
            _FALLBACK_TRIP_THRESHOLD, _READY_TIMEOUT_S, _READY_MARKERS,
        )
        b = TmuxClaudeBrain()  # no config
        assert b._p.fallback_trip_threshold == _FALLBACK_TRIP_THRESHOLD
        assert b._p.ready_timeout_seconds == _READY_TIMEOUT_S
        assert b._p.ready_markers == _READY_MARKERS

    def test_config_overrides_honored(self):
        cfg = TmuxBrainConfig(
            fallback_trip_threshold=2, ready_timeout_seconds=5.0,
            ready_markers=["custom ready"], error_markers=["boom"],
        )
        b = TmuxClaudeBrain(cfg)
        assert b._p.fallback_trip_threshold == 2
        assert b._p.ready_timeout_seconds == 5.0
        assert b._p.ready_markers == ("custom ready",)
        assert b._p.error_markers == ("boom",)

    def test_load_config_parses_tmux_block(self, tmp_path):
        from istota.config import load_config
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[brain]\nkind = "tmux_claude"\n\n'
            '[brain.tmux]\nfallback_trip_threshold = 9\n'
            'ready_markers = ["foo", "bar"]\ncli_version_pin = "2.2.0"\n'
        )
        c = load_config(cfg_file)
        assert c.brain.kind == "tmux_claude"
        assert c.brain.tmux.fallback_trip_threshold == 9
        assert c.brain.tmux.ready_markers == ["foo", "bar"]
        assert c.brain.tmux.cli_version_pin == "2.2.0"
        # unspecified fields keep defaults
        assert c.brain.tmux.fallback_cooldown_seconds == 300.0

    def test_cli_version_mismatch_warns_once(self, monkeypatch, caplog):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod, "_VERSION_CHECKED", False)
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/claude")

        class _CP:
            stdout = "9.9.9 (Claude Code)"

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _CP())
        with caplog.at_level(logging.WARNING, logger="istota.brain.tmux_claude"):
            mod._warn_cli_version_once("2.1.168")
            mod._warn_cli_version_once("2.1.168")  # one-shot
        warns = [r for r in caplog.records if "cli_version_mismatch" in r.message]
        assert len(warns) == 1

    def test_make_brain_passes_tmux_config(self):
        from istota.brain import make_brain
        cfg = BrainConfig(kind="tmux_claude",
                          tmux=TmuxBrainConfig(fallback_trip_threshold=4))
        brain = make_brain(cfg)
        assert isinstance(brain, TmuxClaudeBrain)
        assert brain._p.fallback_trip_threshold == 4


class TestOnboardingSeed:
    """A fresh per-session CLAUDE_CONFIG_DIR makes the interactive TUI re-run
    first-run onboarding (theme picker, trust, bypass). _seed_onboarding writes
    a .claude.json that marks the install + project already-onboarded."""

    def test_seed_writes_onboarding_keys(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        TmuxClaudeBrain._seed_onboarding(cfg_dir, Path("/data/tmp/stefan"))
        seeded = json.loads((cfg_dir / ".claude.json").read_text())
        assert seeded["theme"] == "dark"
        assert seeded["hasCompletedOnboarding"] is True
        assert seeded["bypassPermissionsModeAccepted"] is True
        proj = seeded["projects"]["/data/tmp/stefan"]
        assert proj["hasTrustDialogAccepted"] is True
        assert proj["hasCompletedProjectOnboarding"] is True

    def test_execute_seeds_before_launch(self, monkeypatch, tmp_path):
        # The whole-execute path writes the seed into the session config dir.
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        tr = tmp_path / "t.jsonl"
        tr.write_text(json.dumps(_assistant([{"type": "text", "text": "x"}])) + "\n")
        brain = TmuxClaudeBrain()
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")
        seen = {}
        orig_seed = brain._seed_onboarding

        def spy_seed(config_dir, launch_cwd):
            orig_seed(config_dir, launch_cwd)  # write the real seed
            seen["json"] = json.loads((config_dir / ".claude.json").read_text())

        monkeypatch.setattr(brain, "_seed_onboarding", spy_seed)
        for m in ("_new_session", "_launch_claude", "_inject_prompt", "_kill"):
            monkeypatch.setattr(brain, m, lambda *a, **k: None)
        monkeypatch.setattr(brain, "_wait_ready", lambda *a: True)
        monkeypatch.setattr(brain, "_pane_pid", lambda *a: 1)
        monkeypatch.setattr(brain, "_learn_transcript_path", lambda *a: None)

        def wfc(name, sentinel, deadline, cancel_check):
            Path(sentinel).write_text(json.dumps(
                {"transcript_path": str(tr), "last_assistant_message": "x"}))
            return ("done", "")

        monkeypatch.setattr(brain, "_wait_for_completion", wfc)
        brain.execute(_req(tmp_path, env={"ISTOTA_DEFERRED_DIR": str(deferred)}))
        assert seen["json"]["hasCompletedOnboarding"] is True


class TestThemeDialog:
    """Safety net: if the seed misses (CLI renamed a key), _wait_ready scripts
    past the first-run theme picker with a bare Enter (dark is pre-selected)."""

    def test_theme_then_ready(self, monkeypatch):
        keys = []
        panes = [
            "Choose the text style that looks best\n ❯ 2. Dark mode ✔\n To change this later, run /theme",
            "bypass permissions on (shift+tab to cycle)",
        ]
        brain = TmuxClaudeBrain()
        seq = iter(panes)
        last = {"p": panes[-1]}

        def capture(name):
            try:
                last["p"] = next(seq)
            except StopIteration:
                pass
            return last["p"]

        monkeypatch.setattr(brain, "_capture", capture)
        monkeypatch.setattr(brain, "_tmux", lambda *a: keys.append(a) or _CP())
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        import time as _t
        assert brain._wait_ready("s", _t.monotonic() + 100) is True
        payloads = [a[3] for a in keys if a[:3] == ("send-keys", "-t", "s")]
        assert payloads[0] == "Enter"  # accept pre-selected dark theme
        assert "theme" in brain._last_dialogs


class _CP:
    stdout = ""
    returncode = 0


class TestPromptSubmission:
    """The bracketed-paste race: _inject_prompt must confirm a turn started and
    resend Enter only if it didn't (never blindly — no stray empty Enter)."""

    def test_turn_started_via_userpromptsubmit(self, tmp_path):
        s = tmp_path / "started.json"
        s.write_text(json.dumps({"hook_event_name": "UserPromptSubmit"}))
        assert TmuxClaudeBrain._turn_started(s) is True

    def test_turn_started_via_transcript_exists(self, tmp_path):
        tr = tmp_path / "t.jsonl"
        tr.write_text("{}")
        s = tmp_path / "started.json"
        s.write_text(json.dumps({"hook_event_name": "SessionStart",
                                 "transcript_path": str(tr)}))
        assert TmuxClaudeBrain._turn_started(s) is True

    def test_turn_not_started_sessionstart_no_transcript(self, tmp_path):
        s = tmp_path / "started.json"
        s.write_text(json.dumps({"hook_event_name": "SessionStart",
                                 "transcript_path": str(tmp_path / "missing.jsonl")}))
        assert TmuxClaudeBrain._turn_started(s) is False
        # missing sentinel → not started
        assert TmuxClaudeBrain._turn_started(tmp_path / "nope.json") is False

    def test_inject_submits_once_when_confirmed(self, monkeypatch, tmp_path):
        brain = TmuxClaudeBrain()
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        keys = []
        monkeypatch.setattr(brain, "_tmux", lambda *a: keys.append(a) or _CP())
        # Turn starts immediately after the first Enter.
        monkeypatch.setattr(brain, "_turn_started", lambda s: True)
        brain._inject_prompt("s", tmp_path / "p.txt", tmp_path / "started.json")
        enters = [a for a in keys if a[:1] == ("send-keys",) and a[-1] == "Enter"]
        assert len(enters) == 1  # exactly one Enter — no stray resend

    def test_inject_resends_until_turn_starts(self, monkeypatch, tmp_path):
        brain = TmuxClaudeBrain()
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        # monotonic advances so the per-attempt confirm window expires quickly.
        ticks = iter([float(i) for i in range(0, 200)])
        monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks, 999.0))
        keys = []
        monkeypatch.setattr(brain, "_tmux", lambda *a: keys.append(a) or _CP())
        # Not started for the first two attempts, then started.
        calls = {"n": 0}

        def started(s):
            calls["n"] += 1
            return calls["n"] > 6  # a few confirm-polls fail, then succeeds

        monkeypatch.setattr(brain, "_turn_started", started)
        brain._inject_prompt("s", tmp_path / "p.txt", tmp_path / "started.json")
        enters = [a for a in keys if a[:1] == ("send-keys",) and a[-1] == "Enter"]
        assert len(enters) >= 2  # resent at least once


class TestRootSandboxEnv:
    """Under root the interactive TUI refuses --dangerously-skip-permissions
    unless IS_SANDBOX=1 (the container-is-the-sandbox escape hatch)."""

    def _run_to_session_env(self, monkeypatch, tmp_path, *, root):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod, "_is_root", lambda: root)
        brain = TmuxClaudeBrain()
        captured = {}
        monkeypatch.setattr(brain, "_new_session",
                            lambda name, env: captured.update(env))
        monkeypatch.setattr(brain, "_launch_claude", lambda *a: None)
        monkeypatch.setattr(brain, "_wait_ready", lambda *a: False)  # bail after launch
        monkeypatch.setattr(brain, "_kill", lambda *a: None)
        monkeypatch.setattr(brain, "_capture", lambda *a: "")
        brain._run_session(_req(tmp_path), attempt=0)
        return captured

    def test_root_sets_is_sandbox(self, monkeypatch, tmp_path):
        env = self._run_to_session_env(monkeypatch, tmp_path, root=True)
        assert env.get("IS_SANDBOX") == "1"

    def test_non_root_leaves_is_sandbox_unset(self, monkeypatch, tmp_path):
        env = self._run_to_session_env(monkeypatch, tmp_path, root=False)
        assert "IS_SANDBOX" not in env

    def test_existing_is_sandbox_preserved(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod, "_is_root", lambda: True)
        brain = TmuxClaudeBrain()
        captured = {}
        monkeypatch.setattr(brain, "_new_session", lambda name, env: captured.update(env))
        monkeypatch.setattr(brain, "_launch_claude", lambda *a: None)
        monkeypatch.setattr(brain, "_wait_ready", lambda *a: False)
        monkeypatch.setattr(brain, "_kill", lambda *a: None)
        monkeypatch.setattr(brain, "_capture", lambda *a: "")
        brain._run_session(_req(tmp_path, env={"IS_SANDBOX": "custom"}), attempt=0)
        assert captured.get("IS_SANDBOX") == "custom"


class TestSessionLabel:
    def test_label_with_retry_suffix(self, monkeypatch, tmp_path):
        # On a retry attempt the session name gets an -rN suffix to stay unique.
        brain = TmuxClaudeBrain()
        names = []
        monkeypatch.setattr(brain, "_new_session", lambda name, env: names.append(name))
        monkeypatch.setattr(brain, "_launch_claude", lambda *a: None)
        monkeypatch.setattr(brain, "_wait_ready", lambda *a: False)  # bail at ready
        monkeypatch.setattr(brain, "_kill", lambda *a: None)
        monkeypatch.setattr(brain, "_capture", lambda *a: "")
        brain._run_session(_req(tmp_path, session_label="istota-5-0"), attempt=1)
        assert names[0] == "istota-5-0-r1"


# --------------------------------------------------------------------------
# Stage 5 — live transcript tailer (§10)
# --------------------------------------------------------------------------


class TestTranscriptTailer:
    def test_forwards_new_blocks_once_in_order(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text("")
        events = []
        tailer = _TranscriptTailer(path, events.append)

        # Round 1: one assistant record with thinking+text, then a tool.
        path.write_text(json.dumps(_assistant(
            [{"type": "thinking", "thinking": "hmm"},
             {"type": "text", "text": "checking"}],
            stop_reason="tool_use")) + "\n")
        tailer._drain_once()
        # Round 2: the same record plus a new tool-use record appended.
        recs = [
            _assistant([{"type": "thinking", "thinking": "hmm"},
                        {"type": "text", "text": "checking"}], stop_reason="tool_use"),
            _assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                         "input": {"command": "ls"}}], stop_reason="tool_use", msg_id="m2"),
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in recs))
        tailer._drain_once()
        # Round 3: final text block.
        recs.append(_assistant([{"type": "text", "text": "done"}], msg_id="m3"))
        path.write_text("".join(json.dumps(r) + "\n" for r in recs))
        tailer._drain_once()

        kinds = [type(e).__name__ for e in events]
        assert kinds == ["ThinkingEvent", "TextEvent", "ToolUseEvent", "TextEvent"]
        # No duplicates despite re-reading the whole file each drain.
        texts = [e.text for e in events if type(e).__name__ == "TextEvent"]
        assert texts == ["checking", "done"]

    def test_tool_dedup_by_id(self, tmp_path):
        path = tmp_path / "t.jsonl"
        events = []
        tailer = _TranscriptTailer(path, events.append)
        rec = _assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                           "input": {"command": "ls"}}], stop_reason="tool_use")
        path.write_text(json.dumps(rec) + "\n")
        tailer._drain_once()
        tailer._drain_once()  # second drain must not re-emit
        assert [type(e).__name__ for e in events] == ["ToolUseEvent"]

    def test_start_stop_join_does_not_raise(self, tmp_path):
        # Regression: Thread has a private _stop() method that join() calls;
        # the tailer's Event must NOT be named self._stop or join() raises
        # "'Event' object is not callable" (the docker repro that aborted the
        # _run_session finally and triggered a retry loop).
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps(_assistant([{"type": "text", "text": "x"}])) + "\n")
        tailer = _TranscriptTailer(path, lambda e: None)
        tailer.start()
        tailer.stop()
        tailer.join(timeout=2.0)  # must not raise
        assert not tailer.is_alive()

    def test_exception_in_callback_isolated(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps(_assistant([{"type": "text", "text": "x"}])) + "\n")

        def boom(_):
            raise RuntimeError("downstream blew up")

        tailer = _TranscriptTailer(path, boom)
        tailer._drain_once()  # must not raise


class TestExecuteStreamingPaths:
    def _streaming_brain(self, monkeypatch, tr, *, on_progress, streaming=True):
        brain = TmuxClaudeBrain()
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")
        for m in ("_new_session", "_launch_claude", "_inject_prompt", "_kill"):
            monkeypatch.setattr(brain, m, lambda *a, **k: None)
        monkeypatch.setattr(brain, "_wait_ready", lambda *a, **k: True)
        monkeypatch.setattr(brain, "_pane_pid", lambda *a, **k: 1)

        def wfc(name, sentinel, deadline, cancel_check):
            Path(sentinel).write_text(json.dumps(
                {"transcript_path": str(tr), "last_assistant_message": "done"}))
            return ("done", "")

        monkeypatch.setattr(brain, "_wait_for_completion", wfc)
        return brain

    def test_no_tailer_when_not_streaming(self, monkeypatch, tmp_path):
        # streaming=False → no tailer; _build_result forwards events at Stop.
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "done"}])])
        started_calls = []
        brain = self._streaming_brain(monkeypatch, tr, on_progress=None)
        monkeypatch.setattr(brain, "_learn_transcript_path",
                            lambda *a: started_calls.append(1))
        events = []
        brain.execute(_req(tmp_path, streaming=False, on_progress=events.append))
        # learn_transcript_path never called (tailer gated off)
        assert started_calls == []
        # whole-turn forward still happened
        assert [type(e).__name__ for e in events] == ["TextEvent"]

    def test_tailer_started_when_streaming_and_path_known(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "done"}])])
        brain = self._streaming_brain(monkeypatch, tr, on_progress=lambda e: None)
        monkeypatch.setattr(brain, "_learn_transcript_path", lambda *a: tr)
        started = {}

        import istota.brain.tmux_claude as mod
        real_tailer = mod._TranscriptTailer

        class _SpyTailer(real_tailer):
            def start(self):
                started["yes"] = True
                # don't actually run a thread in the test

            def stop(self):
                pass

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(mod, "_TranscriptTailer", _SpyTailer)
        events = []
        brain.execute(_req(tmp_path, streaming=True, on_progress=events.append))
        assert started.get("yes") is True
        # _build_result must NOT re-forward (tailer owns progress) → no events
        # from the Stop path here (the spy tailer emitted nothing).
        assert events == []

    def test_learn_transcript_path_reads_started_sentinel(self, tmp_path, monkeypatch):
        brain = TmuxClaudeBrain()
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        started = tmp_path / "started.json"
        started.write_text(json.dumps({"transcript_path": "/x/y.jsonl"}))
        got = brain._learn_transcript_path(started, tmp_path)
        assert str(got) == "/x/y.jsonl"

    def test_build_result_trace_independent_of_tailer(self, monkeypatch, tmp_path):
        # The persisted execution_trace comes from the Stop-time parse and is
        # identical whether or not a tailer ran (reconcile guarantee).
        tr = _write_transcript(tmp_path, [
            _assistant([{"type": "text", "text": "checking"},
                        {"type": "tool_use", "id": "t1", "name": "Bash",
                         "input": {"command": "ls"}}], stop_reason="tool_use"),
            _assistant([{"type": "text", "text": "done"}], msg_id="m2"),
        ])
        brain = TmuxClaudeBrain()
        sentinel = tmp_path / "stop.json"
        sentinel.write_text(json.dumps(
            {"transcript_path": str(tr), "last_assistant_message": "done"}))
        r1 = brain._build_result(sentinel, _req(tmp_path), forward_progress=True)
        r2 = brain._build_result(sentinel, _req(tmp_path), forward_progress=False)
        assert r1.execution_trace == r2.execution_trace
        assert r1.result_text == r2.result_text == "done"
