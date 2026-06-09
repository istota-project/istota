"""Tests for parse_transcript() — reconstructing StreamEvents from the
interactive `claude` TUI's transcript JSONL.

The interactive transcript differs from `-p`'s stream-json: it is an
append-only session log (top-level ``type`` per line: assistant/user/system/…)
with no terminal ``result`` record. The final answer is synthesized from the
last ``end_turn`` assistant turn's text blocks. Schema pinned against real
transcript files under ~/.claude/projects/ during the Stage 1 spike. See
``Specs/Active/tmux-subscription-brain-feasibility.md``.
"""

import glob
import json
import os
from pathlib import Path

import pytest

from istota.brain._events import (
    ResultEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from istota.brain.tmux_claude import (
    TmuxClaudeBrain,
    _transcript_has_final_turn,
    parse_transcript,
)


def _write(tmp_path, records) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


def _assistant(content, stop_reason="end_turn", msg_id="msg_1"):
    return {
        "type": "assistant",
        "uuid": f"u-{msg_id}",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-4-8",
            "stop_reason": stop_reason,
            "content": content,
        },
    }


class TestParseTranscriptBasic:
    def test_text_only_turn(self, tmp_path):
        p = _write(tmp_path, [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            _assistant([{"type": "text", "text": "Hello there."}]),
        ])
        events = parse_transcript(p)
        assert isinstance(events[-1], ResultEvent)
        assert events[-1].success is True
        assert events[-1].text == "Hello there."
        # exactly one TextEvent before the terminal result
        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert [e.text for e in text_events] == ["Hello there."]

    def test_blocks_emitted_in_document_order(self, tmp_path):
        p = _write(tmp_path, [
            _assistant(
                [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "working on it"},
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "ls"}},
                ],
                stop_reason="tool_use",
            ),
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"},
            ]}},
            _assistant([{"type": "text", "text": "Found one file."}], msg_id="msg_2"),
        ])
        events = parse_transcript(p)
        kinds = [type(e).__name__ for e in events]
        assert kinds == [
            "ThinkingEvent", "TextEvent", "ToolUseEvent", "TextEvent", "ResultEvent",
        ]
        tool = next(e for e in events if isinstance(e, ToolUseEvent))
        assert tool.tool_name == "Bash"
        assert tool.tool_call_id == "t1"
        assert "ls" in tool.description
        assert events[-1].text == "Found one file."

    def test_thinking_reconstructed(self, tmp_path):
        p = _write(tmp_path, [
            _assistant([
                {"type": "thinking", "thinking": "reasoning here"},
                {"type": "text", "text": "answer"},
            ]),
        ])
        events = parse_transcript(p)
        thinking = [e for e in events if isinstance(e, ThinkingEvent)]
        assert [e.text for e in thinking] == ["reasoning here"]

    def test_final_answer_is_last_end_turn_text(self, tmp_path):
        # An earlier end_turn turn must NOT win over the final one.
        p = _write(tmp_path, [
            _assistant([{"type": "text", "text": "first answer"}], msg_id="m1"),
            {"type": "user", "message": {"role": "user", "content": "more"}},
            _assistant([{"type": "text", "text": "final answer"}], msg_id="m2"),
        ])
        events = parse_transcript(p)
        assert events[-1].text == "final answer"

    def test_multiple_text_blocks_joined(self, tmp_path):
        p = _write(tmp_path, [
            _assistant([
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "part two"},
            ]),
        ])
        events = parse_transcript(p)
        assert events[-1].text == "part one\npart two"


class TestParseTranscriptEdges:
    def test_empty_transcript_yields_failed_result(self, tmp_path):
        p = _write(tmp_path, [])
        events = parse_transcript(p)
        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].success is False

    def test_blank_lines_skipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            json.dumps(_assistant([{"type": "text", "text": "ok"}])) + "\n\n\n"
        )
        events = parse_transcript(p)
        assert events[-1].text == "ok"

    def test_malformed_line_skipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            "{not json\n"
            + json.dumps(_assistant([{"type": "text", "text": "ok"}])) + "\n"
        )
        events = parse_transcript(p)
        assert events[-1].text == "ok"

    def test_no_text_only_tool_use_still_results(self, tmp_path):
        # Degenerate: session ended with a tool_use turn and no final text.
        p = _write(tmp_path, [
            _assistant(
                [{"type": "tool_use", "id": "t1", "name": "Bash",
                  "input": {"command": "ls"}}],
                stop_reason="tool_use",
            ),
        ])
        events = parse_transcript(p)
        assert isinstance(events[-1], ResultEvent)
        # No final answer text → empty result, but a ToolUseEvent was emitted.
        assert any(isinstance(e, ToolUseEvent) for e in events)


class TestFinalTurnSignal:
    def test_end_turn_present(self, tmp_path):
        p = _write(tmp_path, [_assistant([{"type": "text", "text": "done"}])])
        assert _transcript_has_final_turn(p) is True

    def test_only_tool_use_turn_not_final(self, tmp_path):
        p = _write(tmp_path, [
            _assistant([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                       stop_reason="tool_use"),
        ])
        assert _transcript_has_final_turn(p) is False

    def test_missing_file_not_final(self, tmp_path):
        assert _transcript_has_final_turn(tmp_path / "nope.jsonl") is False

    def test_empty_file_not_final(self, tmp_path):
        p = tmp_path / "e.jsonl"
        p.write_text("")
        assert _transcript_has_final_turn(p) is False


class TestParseSettled:
    """The post-Stop flush-race guard: poll for the final turn before parsing."""

    def test_returns_immediately_when_already_settled(self, tmp_path, monkeypatch):
        p = _write(tmp_path, [_assistant([{"type": "text", "text": "hi"}])])
        import istota.brain.tmux_claude as mod
        slept = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
        events = TmuxClaudeBrain()._parse_transcript_settled(p)
        assert slept == []  # no polling needed
        assert events[-1].text == "hi"

    def test_polls_until_final_turn_appears(self, tmp_path, monkeypatch):
        p = _write(tmp_path, [
            _assistant([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                       stop_reason="tool_use"),
        ])
        import istota.brain.tmux_claude as mod
        calls = {"n": 0}
        real = mod._transcript_has_final_turn

        def flaky(path):
            calls["n"] += 1
            if calls["n"] < 3:
                return False
            # third check: the final turn has now "flushed"
            p.write_text(p.read_text() + json.dumps(
                _assistant([{"type": "text", "text": "final"}], msg_id="m2")) + "\n")
            return real(p)

        monkeypatch.setattr(mod, "_transcript_has_final_turn", flaky)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        events = TmuxClaudeBrain()._parse_transcript_settled(p)
        assert calls["n"] >= 3
        assert events[-1].text == "final"
        assert any(isinstance(e, ToolUseEvent) for e in events)

    def test_best_effort_after_budget(self, tmp_path, monkeypatch):
        # Final turn never appears (session ended on tool_use); must not hang.
        p = _write(tmp_path, [
            _assistant([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                       stop_reason="tool_use"),
        ])
        import istota.brain.tmux_claude as mod
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        # collapse the budget so the loop exits after one check
        monkeypatch.setattr(mod, "_TRANSCRIPT_SETTLE_S", 0.0)
        events = TmuxClaudeBrain()._parse_transcript_settled(p)
        assert isinstance(events[-1], ResultEvent)  # best-effort parse returned


@pytest.mark.skipif(
    not glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")),
    reason="no real transcripts on this host",
)
class TestAgainstRealTranscript:
    """Validate the parser tolerates a real, full transcript without raising
    and produces a sane terminal ResultEvent."""

    def _pick(self):
        files = sorted(
            glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")),
            key=os.path.getsize,
            reverse=True,
        )
        return Path(files[0])

    def test_parses_without_error(self):
        events = parse_transcript(self._pick())
        assert isinstance(events[-1], ResultEvent)
        # A large real session must surface tool calls and text.
        assert any(isinstance(e, ToolUseEvent) for e in events)
        assert any(isinstance(e, TextEvent) for e in events)
