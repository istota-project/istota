"""Stage 2 tests for TmuxClaudeBrain.execute() — the full tmux drive flow.

The tmux/claude subprocess boundary is mocked: these tests exercise the
orchestration (session lifecycle, readiness gate, prompt injection, sentinel
polling, transcript→BrainResult mapping, cancel/timeout/teardown) without
launching a real interactive `claude`. The live end-to-end run is a separate,
permission-gated step. See ``Specs/Active/tmux-subscription-brain-feasibility.md``.
"""

import json
from pathlib import Path

import pytest

from istota.brain._types import BrainRequest
from istota.brain.tmux_claude import TmuxClaudeBrain, reset_circuit_breaker


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    """Circuit-breaker state is process-global; reset around every test so the
    suite stays order-independent under xdist. Also skip the one-shot
    `claude --version` probe."""
    import istota.brain.tmux_claude as mod
    monkeypatch.setattr(mod, "_VERSION_CHECKED", True)
    reset_circuit_breaker()
    yield
    reset_circuit_breaker()


class _CP:
    """Stand-in for subprocess.CompletedProcess from a mocked _tmux."""
    stdout = ""
    returncode = 0


def _req(tmp_path, **kw):
    base = dict(
        prompt="say hello",
        allowed_tools=[],
        cwd=tmp_path,
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
        timeout_seconds=60,
    )
    base.update(kw)
    return BrainRequest(**base)


def _write_transcript(tmp_path, records, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


def _assistant(content, stop_reason="end_turn", msg_id="m1"):
    return {
        "type": "assistant",
        "uuid": f"u-{msg_id}",
        "message": {
            "id": msg_id, "role": "assistant", "model": "claude-opus-4-8",
            "stop_reason": stop_reason, "content": content,
        },
    }


class _Harness:
    """Wires a TmuxClaudeBrain with the tmux boundary mocked. The fake
    _wait_for_completion writes a sentinel payload pointing at a prepared
    transcript (and the live tailer is disabled via _learn_transcript_path)."""

    def __init__(self, monkeypatch, tmp_path, *, transcript, last_msg="Hello",
                 ready=True, sentinel_status="done"):
        self.brain = TmuxClaudeBrain()
        self.calls = []
        self.transcript = transcript
        self.last_msg = last_msg
        self._ready = ready
        self._sentinel_status = sentinel_status

        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/tmux")

        def rec(name):
            def f(*a, **k):
                self.calls.append((name, a, k))
                if name == "_pane_pid":
                    return 4242
                if name == "_wait_ready":
                    return self._ready
                return None
            return f

        for m in ("_new_session", "_launch_claude", "_inject_prompt", "_kill"):
            monkeypatch.setattr(self.brain, m, rec(m))
        monkeypatch.setattr(self.brain, "_pane_pid", rec("_pane_pid"))
        monkeypatch.setattr(self.brain, "_wait_ready", rec("_wait_ready"))
        # These orchestration tests exercise the whole-turn (non-tailer) path:
        # no live transcript path learned, so _build_result forwards events.
        monkeypatch.setattr(self.brain, "_learn_transcript_path", lambda *a, **k: None)

        def fake_wait_for_completion(name, sentinel, deadline, cancel_check):
            self.calls.append(("_wait_for_completion", (sentinel,), {}))
            if self._sentinel_status == "done":
                payload = {
                    "transcript_path": str(self.transcript),
                    "last_assistant_message": self.last_msg,
                }
                Path(sentinel).write_text(json.dumps(payload))
            return (self._sentinel_status, "")

        monkeypatch.setattr(self.brain, "_wait_for_completion", fake_wait_for_completion)

    def names(self):
        return [c[0] for c in self.calls]


class TestHappyPath:
    def test_text_only_success(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "Hello"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, last_msg="Hello")
        res = h.brain.execute(_req(tmp_path))
        assert res.success is True
        assert res.result_text == "Hello"
        assert res.stop_reason == "completed"

    def test_result_prefers_last_assistant_message(self, monkeypatch, tmp_path):
        # last_assistant_message from the Stop payload wins over a re-derived
        # transcript answer (they should agree, but the payload is canonical).
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "from transcript"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, last_msg="from payload")
        res = h.brain.execute(_req(tmp_path))
        assert res.result_text == "from payload"

    def test_result_falls_back_to_transcript_when_no_payload_message(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "fallback answer"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, last_msg=None)
        res = h.brain.execute(_req(tmp_path))
        assert res.result_text == "fallback answer"

    def test_lifecycle_order(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "Hi"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr)
        h.brain.execute(_req(tmp_path))
        names = h.names()
        # session created → ready gate → prompt injected → sentinel polled → killed
        assert names.index("_new_session") < names.index("_wait_ready")
        assert names.index("_wait_ready") < names.index("_inject_prompt")
        assert names.index("_inject_prompt") < names.index("_wait_for_completion")
        assert "_kill" in names
        assert names.index("_wait_for_completion") < names.index("_kill")

    def test_on_pid_reported(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "Hi"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr)
        seen = []
        h.brain.execute(_req(tmp_path, on_pid=seen.append))
        assert seen == [4242]

    def test_on_progress_emits_whole_turn_events(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [
            _assistant(
                [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
                stop_reason="tool_use",
            ),
            _assistant([{"type": "text", "text": "done"}], msg_id="m2"),
        ])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, last_msg="done")
        events = []
        h.brain.execute(_req(tmp_path, on_progress=events.append))
        kinds = [type(e).__name__ for e in events]
        # ResultEvent is NOT forwarded to on_progress (it's the return value)
        assert "ResultEvent" not in kinds
        assert kinds == ["ThinkingEvent", "TextEvent", "ToolUseEvent", "TextEvent"]

    def test_actions_and_trace_shapes(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [
            _assistant(
                [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
                stop_reason="tool_use",
            ),
            _assistant([{"type": "text", "text": "done"}], msg_id="m2"),
        ])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, last_msg="done")
        res = h.brain.execute(_req(tmp_path))
        actions = json.loads(res.actions_taken)
        trace = json.loads(res.execution_trace)
        assert any("ls" in a for a in actions)
        assert {"type": "tool"} == {k: trace[1][k] for k in ("type",)}
        assert [t["type"] for t in trace] == ["text", "tool", "text"]


class TestCancelTimeout:
    def test_cancel_returns_cancelled_and_kills(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, sentinel_status="cancelled")
        res = h.brain.execute(_req(tmp_path))
        assert res.success is False
        assert res.stop_reason == "cancelled"
        assert "_kill" in h.names()

    def test_timeout_returns_timeout_and_kills(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, sentinel_status="timeout")
        res = h.brain.execute(_req(tmp_path))
        assert res.success is False
        assert res.stop_reason == "timeout"
        assert "_kill" in h.names()

    def test_ready_failure_falls_back_and_kills(self, monkeypatch, tmp_path):
        # A REPL that never becomes ready is a launch-level failure → fallback
        # (the executor reruns headless), not a 30-min timeout (§3/§4).
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, ready=False)
        res = h.brain.execute(_req(tmp_path))
        assert res.success is False
        assert res.stop_reason == "fallback"
        assert "_inject_prompt" not in h.names()  # never got to inject
        assert "_kill" in h.names()


class TestPreconditions:
    def test_tmux_missing_returns_not_found(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        res = TmuxClaudeBrain().execute(_req(tmp_path))
        assert res.success is False
        assert res.stop_reason == "not_found"


class TestSandboxInteraction:
    """Production-viability: under bwrap the claude pane process must be
    sandbox-wrapped, and the sentinel/settings must live in the shared RW region
    (ISTOTA_DEFERRED_DIR) — not a private /tmp dir that becomes the sandbox's
    own tmpfs. See spec §4."""

    def test_launch_applies_sandbox_wrap(self, monkeypatch, tmp_path):
        brain = TmuxClaudeBrain()
        sent = []
        monkeypatch.setattr(brain, "_tmux",
                            lambda *a: sent.append(a) or _CP())
        def wrap(cmd):
            return ["bwrap", "--die-with-parent", "--", *cmd]

        req = _req(tmp_path, allowed_tools=["Bash"], model="claude-opus-4-8",
                   sandbox_wrap=wrap)
        brain._launch_claude("s1", req, tmp_path)
        # the literal send-keys command is the 5th element of the send-keys -l call
        literal = next(a for a in sent if a[:4] == ("send-keys", "-t", "s1", "-l"))
        cmd = literal[4]
        assert "bwrap --die-with-parent --" in cmd
        assert "claude" in cmd
        assert "--model claude-opus-4-8" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert str(tmp_path) in cmd  # cd <launch_cwd>

    def test_no_sandbox_wrap_launches_bare_claude(self, monkeypatch, tmp_path):
        brain = TmuxClaudeBrain()
        sent = []
        monkeypatch.setattr(brain, "_tmux", lambda *a: sent.append(a) or _CP())
        req = _req(tmp_path, sandbox_wrap=None)
        brain._launch_claude("s1", req, tmp_path)
        literal = next(a for a in sent if a[:4] == ("send-keys", "-t", "s1", "-l"))
        assert "bwrap" not in literal[4]
        assert "claude" in literal[4]

    def test_sentinel_under_deferred_dir(self, monkeypatch, tmp_path):
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr)
        captured = {}
        orig = h.brain._wait_for_completion

        def spy(name, sentinel, deadline, cancel_check):
            captured["sentinel"] = sentinel
            return orig(name, sentinel, deadline, cancel_check)

        monkeypatch.setattr(h.brain, "_wait_for_completion", spy)
        req = _req(tmp_path, env={"ISTOTA_DEFERRED_DIR": str(deferred)})
        h.brain.execute(req)
        # sentinel lives under the deferred dir (the sandbox-shared RW bind)
        assert str(captured["sentinel"]).startswith(str(deferred))
        # the per-session config dir (CLAUDE_CONFIG_DIR) holds the hook
        # settings.json — under the workdir, which is under the deferred base.
        assert str(captured["sentinel"]).endswith("stop.json")

    def test_no_shared_claude_dir_under_base(self, monkeypatch, tmp_path):
        # The clobber fix: hooks live in a per-session config dir, NOT a shared
        # base_dir/.claude/. The workdir (config dir included) is rmtree'd, so
        # nothing persists under the deferred base.
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr)
        h.brain.execute(_req(tmp_path, env={"ISTOTA_DEFERRED_DIR": str(deferred)}))
        assert not (deferred / ".claude").exists()
        # workdir cleaned up entirely
        assert not list(deferred.glob(".tmux-*"))

    def test_hooks_target_sentinels_in_config_dir(self, tmp_path):
        brain = TmuxClaudeBrain()
        config_dir = tmp_path / "deferred" / ".tmux-s" / "config"
        sentinel = tmp_path / "deferred" / ".tmux-s" / "stop.json"
        started = tmp_path / "deferred" / ".tmux-s" / "started.json"
        sentinel.parent.mkdir(parents=True)
        brain._write_hooks(config_dir, sentinel, started)
        cfg = json.loads((config_dir / "settings.json").read_text())
        stop_cmd = cfg["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert stop_cmd.startswith("cat > ")
        assert str(sentinel) in stop_cmd
        # early hooks for transcript-path learning (§10)
        start_cmd = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        assert str(started) in start_cmd
        assert str(started) in cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    def test_concurrent_sessions_isolated_config_dirs(self, monkeypatch, tmp_path):
        # Two interleaved execute() runs with distinct labels must resolve their
        # own sentinels — no shared hook file to cross-fire.
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        seen_sentinels = []
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        for label in ("istota-task-1-0", "istota-task-2-0"):
            h = _Harness(monkeypatch, tmp_path, transcript=tr)
            orig = h.brain._wait_for_completion

            def spy(name, sentinel, deadline, cancel_check, _o=orig):
                seen_sentinels.append(str(sentinel))
                return _o(name, sentinel, deadline, cancel_check)

            monkeypatch.setattr(h.brain, "_wait_for_completion", spy)
            h.brain.execute(_req(tmp_path, env={"ISTOTA_DEFERRED_DIR": str(deferred)},
                                 session_label=label))
        assert len(set(seen_sentinels)) == 2  # distinct per-session sentinels
        assert "istota-task-1-0" in seen_sentinels[0]
        assert "istota-task-2-0" in seen_sentinels[1]


class TestWaitReadyDialogs:
    """_wait_ready scripts past the launch dialogs. Under bwrap (Stage 3′
    finding) the 'Bypass Permissions mode' warning appears after trust because
    ~/.claude is tmpfs'd; its accept option is NOT pre-selected, so a bare Enter
    would exit claude."""

    def _brain_with_panes(self, monkeypatch, panes, keys):
        brain = TmuxClaudeBrain()
        seq = iter(panes)
        state = {"last": panes[-1]}

        def capture(name):
            try:
                state["last"] = next(seq)
            except StopIteration:
                pass
            return state["last"]

        monkeypatch.setattr(brain, "_capture", capture)
        monkeypatch.setattr(brain, "_tmux",
                            lambda *a: keys.append(a) or _CP())
        # no real sleeping
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        return brain

    def test_handles_trust_then_bypass_then_ready(self, monkeypatch):
        keys = []
        panes = [
            "Quick safety check: Is this a project you trust?\n1. Yes, I trust this folder",
            "WARNING: Claude Code running in Bypass Permissions mode\n1. No, exit\n2. Yes, I accept",
            "Try \"fix typecheck errors\"\nbypass permissions on (shift+tab to cycle)",
        ]
        import time as _t
        brain = self._brain_with_panes(monkeypatch, panes, keys)
        assert brain._wait_ready("s", _t.monotonic() + 100) is True
        sent = [a for a in keys if a[:3] == ("send-keys", "-t", "s")]
        # trust → Enter; bypass → "2" then Enter
        payloads = [a[3] for a in sent]
        assert "Enter" in payloads
        assert "2" in payloads
        # the "2" must come before its following Enter (accept, not exit)
        assert payloads.index("2") < len(payloads) - 1

    def test_bypass_selects_option_2_not_bare_enter(self, monkeypatch):
        keys = []
        panes = [
            "WARNING: Claude Code running in Bypass Permissions mode\n1. No, exit\n2. Yes, I accept",
            "bypass permissions on (shift+tab to cycle)",
        ]
        import time as _t
        brain = self._brain_with_panes(monkeypatch, panes, keys)
        assert brain._wait_ready("s", _t.monotonic() + 100) is True
        payloads = [a[3] for a in keys if a[:3] == ("send-keys", "-t", "s")]
        # first action on the bypass dialog is "2" (select accept), then Enter
        assert payloads[0] == "2"
        assert payloads[1] == "Enter"

    def test_timeout_returns_false(self, monkeypatch):
        keys = []
        panes = ["still loading…"]
        import time as _t
        brain = self._brain_with_panes(monkeypatch, panes, keys)
        assert brain._wait_ready("s", _t.monotonic() - 1) is False


class TestSessionNaming:
    def test_session_label_used_when_provided(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr)
        h.brain.execute(_req(tmp_path, session_label="istota-task-99-1"))
        new = next(c for c in h.calls if c[0] == "_new_session")
        # session name is the first positional arg to _new_session
        assert new[1][0] == "istota-task-99-1"

    def test_session_name_derived_when_absent(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr)
        h.brain.execute(_req(tmp_path))
        new = next(c for c in h.calls if c[0] == "_new_session")
        assert new[1][0].startswith("istota-tmux-")
