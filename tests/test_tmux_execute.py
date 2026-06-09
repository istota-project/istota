"""Stage 2 tests for TmuxClaudeBrain.execute() — the full tmux drive flow.

The tmux/claude subprocess boundary is mocked: these tests exercise the
orchestration (session lifecycle, readiness gate, prompt injection, sentinel
polling, transcript→BrainResult mapping, cancel/timeout/teardown) without
launching a real interactive `claude`. The live end-to-end run is a separate,
permission-gated step. See ``Specs/Active/tmux-subscription-brain-feasibility.md``.
"""

import json
from pathlib import Path


from istota.brain._types import BrainRequest
from istota.brain.tmux_claude import TmuxClaudeBrain


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
    _wait_sentinel writes a sentinel payload pointing at a prepared transcript."""

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

        def fake_wait_sentinel(sentinel, deadline, cancel_check):
            self.calls.append(("_wait_sentinel", (sentinel,), {}))
            if self._sentinel_status == "done":
                payload = {
                    "transcript_path": str(self.transcript),
                    "last_assistant_message": self.last_msg,
                }
                Path(sentinel).write_text(json.dumps(payload))
            return self._sentinel_status

        monkeypatch.setattr(self.brain, "_wait_sentinel", fake_wait_sentinel)

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
        assert names.index("_inject_prompt") < names.index("_wait_sentinel")
        assert "_kill" in names
        assert names.index("_wait_sentinel") < names.index("_kill")

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

    def test_ready_failure_kills_and_errors(self, monkeypatch, tmp_path):
        tr = _write_transcript(tmp_path, [_assistant([{"type": "text", "text": "x"}])])
        h = _Harness(monkeypatch, tmp_path, transcript=tr, ready=False)
        res = h.brain.execute(_req(tmp_path))
        assert res.success is False
        assert res.stop_reason in ("timeout", "error")
        assert "_inject_prompt" not in h.names()  # never got to inject
        assert "_kill" in h.names()


class TestPreconditions:
    def test_tmux_missing_returns_not_found(self, monkeypatch, tmp_path):
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        res = TmuxClaudeBrain().execute(_req(tmp_path))
        assert res.success is False
        assert res.stop_reason == "not_found"


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
