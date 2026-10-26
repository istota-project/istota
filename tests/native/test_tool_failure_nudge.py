"""The tool-health notice: a run piling up failed tool calls is told so once.

A failure is an error result, or a Bash command whose status suffix reports a
failure (`bash_result_failed`); Bash leaves `is_error` unset on a non-zero
exit. The notice is injected through `prepare_next_turn` like the turn-budget
nudge, and these tests reuse that file's request and brain fixtures.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

from istota.brain import BrainRequest
from istota.brain.native import NativeBrain, _failure_breakdown
from istota.config import NativeBrainConfig, SessionLogConfig
from istota.llm.provider import StreamDone, StreamError, StreamStart, ToolCallDelta, TextDelta
from istota.llm.types import AssistantMessage, TextContent, ToolCallContent, Usage

from ._mock_provider import MockProvider
from .test_turn_budget_nudge import _brain, _req

_NOTICE = "tool-health notice"


def _bash(i: int, command: str) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCallContent(
                id=f"b{i}",
                name="Bash",
                arguments={"command": command, "description": f"step {i}"},
            )
        ],
        usage=Usage(input_tokens=10, output_tokens=1),
        stop_reason="tool_use",
    )


def _read(i: int, path: str) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCallContent(id=f"r{i}", name="Read", arguments={"file_path": path})],
        usage=Usage(input_tokens=10, output_tokens=1),
        stop_reason="tool_use",
    )


def _final(text: str = "done") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        usage=Usage(input_tokens=10, output_tokens=1),
        stop_reason="end_turn",
    )


def _notices(messages) -> list[str]:
    out = []
    for msg in messages:
        for block in getattr(msg, "content", []):
            text = getattr(block, "text", "")
            if _NOTICE in text:
                out.append(text)
    return out


def _wire_notices(provider) -> list[list[str]]:
    """The notices present in each request, in call order."""
    return [_notices(call["messages"]) for call in provider.calls]


def _logged_brain(provider, root: Path, **cfg) -> NativeBrain:
    config = NativeBrainConfig(
        model="claude-sonnet-4-6",
        session_log=SessionLogConfig(enabled=True, dir=str(root)),
        **cfg,
    )
    return NativeBrain(config, provider=provider)


def _logged_req(prompt: str, cwd: Path, **kw) -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=["Bash", "Read"],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model="claude-sonnet-4-6",
        task_id=7101,
        attempt=1,
        user_id="alice",
        source_type="talk",
        **kw,
    )


def _records(root: Path) -> list[dict]:
    files = sorted((root / "alice").glob("*.jsonl"))
    assert len(files) == 1, files
    return [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]


class TestFiring:
    def test_fires_once_at_the_threshold_across_mixed_tools(self, tmp_path):
        turns = [
            _bash(0, "ls ./missing-a"),
            _read(1, str(tmp_path / "missing-b.txt")),
            _bash(2, "cat ./missing-c"),
            _read(3, str(tmp_path / "missing-d.txt")),
            _bash(4, "ls ./missing-e"),
            _final(),
        ]
        provider = MockProvider(turns)
        result = _brain(provider, tool_failure_nudge_after=4).execute(
            _req("go", tmp_path, tools=["Bash", "Read"])
        )
        assert result.success is True, result.result_text
        per_call = _wire_notices(provider)
        # Calls 0-3 precede the fourth failure; the fifth request carries it.
        assert per_call[:4] == [[], [], [], []]
        assert len(per_call[4]) == 1
        # Once: later requests carry the same single notice, never a second.
        assert all(len(n) == 1 for n in per_call[4:])
        text = per_call[4][0]
        assert "Your last 4 tool calls failed" in text
        assert "Bash ×2 (ls, cat)" in text
        assert "Read ×2" in text
        assert "No tool call in this run has worked yet" in text
        assert "not from the user" in text

    def test_a_success_resets_the_count(self, tmp_path):
        (tmp_path / "here.txt").write_text("hello\n")
        turns = [
            _bash(0, "ls ./missing-a"),
            _bash(1, "ls ./missing-b"),
            _bash(2, "ls ./missing-c"),
            _read(3, str(tmp_path / "here.txt")),
            _bash(4, "cat ./missing-d"),
            _bash(5, "cat ./missing-e"),
            _bash(6, "cat ./missing-f"),
            _final(),
        ]
        provider = MockProvider(turns)
        _brain(provider, tool_failure_nudge_after=4).execute(
            _req("go", tmp_path, tools=["Bash", "Read"])
        )
        assert _wire_notices(provider) == [[]] * len(provider.calls)

    def test_a_successful_bash_counts_as_a_success(self, tmp_path):
        # Exit 0 is the other half of the Bash rule: three failures, a working
        # command, then one more failure is one in a row, not four.
        turns = [
            _bash(0, "ls ./missing-a"),
            _bash(1, "ls ./missing-b"),
            _bash(2, "ls ./missing-c"),
            _bash(3, "true"),
            _bash(4, "ls ./missing-d"),
            _final(),
        ]
        provider = MockProvider(turns)
        _brain(provider, tool_failure_nudge_after=4).execute(
            _req("go", tmp_path, tools=["Bash"])
        )
        assert _wire_notices(provider) == [[]] * len(provider.calls)

    def test_turns_since_the_last_success(self, tmp_path):
        turns = [
            _bash(0, "true"),
            _bash(1, "ls ./missing-a"),
            _bash(2, "ls ./missing-b"),
            _final(),
        ]
        provider = MockProvider(turns)
        _brain(provider, tool_failure_nudge_after=2).execute(
            _req("go", tmp_path, tools=["Bash"])
        )
        text = _wire_notices(provider)[3][0]
        assert "Your last 2 tool calls failed" in text
        assert "The last one that worked was 2 turns ago" in text

    def test_breakdown_names_at_most_three_bash_commands(self, tmp_path):
        commands = ["ls ./x", "cat ./x", "ls ./y", "head ./x", "wc ./x"]
        turns = [_bash(i, c) for i, c in enumerate(commands)] + [_final()]
        provider = MockProvider(turns)
        _brain(provider, tool_failure_nudge_after=5).execute(
            _req("go", tmp_path, tools=["Bash"])
        )
        text = _wire_notices(provider)[5][0]
        assert "Bash ×5 (ls, cat, head)" in text
        assert "wc" not in text

    def test_zero_disables(self, tmp_path):
        turns = [_bash(i, f"ls ./missing-{i}") for i in range(6)] + [_final()]
        provider = MockProvider(turns)
        _brain(provider, tool_failure_nudge_after=0).execute(
            _req("go", tmp_path, tools=["Bash"])
        )
        assert _wire_notices(provider) == [[]] * len(provider.calls)

    def test_text_only_run_never_sees_it(self, tmp_path):
        provider = MockProvider([_final()])
        _brain(provider, tool_failure_nudge_after=1).execute(_req("summarize", tmp_path))
        assert _wire_notices(provider) == [[]]


class TestBreakdown:
    def test_most_frequent_tool_first(self):
        from collections import Counter

        assert (
            _failure_breakdown(Counter({"WebFetch": 1, "Bash": 4}), ["curl"])
            == "Bash ×4 (curl), WebFetch ×1"
        )


class TestInteraction:
    def test_both_nudges_on_one_boundary_budget_first(self, tmp_path):
        # max_turns=4 with the early reminder at 50% puts the budget nudge on
        # the boundary after turn 2, where two failures also meet the threshold.
        turns = [_bash(0, "ls ./a"), _bash(1, "ls ./b"), _final()]
        provider = MockProvider(turns)
        _brain(
            provider,
            max_turns=4,
            turn_budget_nudge_early_percent=50,
            turn_budget_nudge_remaining=[],
            tool_failure_nudge_after=2,
        ).execute(_req("go", tmp_path, tools=["Bash"]))
        last = provider.calls[2]["messages"]
        tail = [getattr(m.content[0], "text", "") for m in last[-2:]]
        assert "halfway" in tail[0], tail
        assert _NOTICE in tail[1], tail

    def test_not_appended_to_new_messages_and_recorded_as_a_nudge(self, tmp_path):
        root = tmp_path / "logs"
        work = tmp_path / "work"
        work.mkdir()
        turns = [_bash(0, "ls ./a"), _bash(1, "cat ./b"), _final()]
        provider = MockProvider(turns)
        result = _logged_brain(provider, root, tool_failure_nudge_after=2).execute(
            _logged_req("go", work)
        )
        assert result.success is True, result.result_text
        assert len(_wire_notices(provider)[2]) == 1
        records = _records(root)
        nudges = [r for r in records if r["type"] == "nudge"]
        assert len(nudges) == 1, nudges
        assert nudges[0]["phase"] == "tool_failure"
        assert nudges[0]["consecutive_failures"] == 2
        assert nudges[0]["breakdown"] == "Bash ×2 (ls, cat)"
        # Every `message` record is something the loop appended; the notice
        # reaches the model through the returned list only.
        messages = json.dumps([r for r in records if r["type"] == "message"])
        assert _NOTICE not in messages
        assert not any(_NOTICE in str(entry) for entry in result.execution_trace)

    def test_declines_while_aborted(self, tmp_path):
        # No request follows an abort, so the wire cannot show a notice that
        # was built; the `nudge` record is written when one is.
        root = tmp_path / "logs"
        work = tmp_path / "work"
        work.mkdir()
        flag = work / "cancel-now"
        turns = [
            _bash(0, "ls ./a"),
            _bash(1, "ls ./b"),
            _bash(2, "ls ./c"),
            # The fourth failure is the cancel itself: the poller sees the
            # flag and aborts the command, and the boundary after it is where
            # the notice would otherwise fire.
            _bash(3, f"touch {flag} && sleep 10"),
            _final(),
        ]
        provider = MockProvider(turns)
        req = _logged_req("go", work, cancel_check=flag.exists)
        result = _logged_brain(provider, root, tool_failure_nudge_after=4).execute(req)
        assert result.stop_reason == "cancelled"
        records = _records(root)
        # The fourth result is in the log, so the count did reach four.
        tool_results = [
            r for r in records
            if r["type"] == "message" and r["message"]["role"] == "tool_result"
        ]
        assert len(tool_results) == 4
        assert [r for r in records if r["type"] == "nudge"] == []
        assert all(n == [] for n in _wire_notices(provider))


class _OverflowingProvider:
    """Scripted turns, with `"overflow"` as a context-length error.

    A summary request (sent with no tools) is answered without consuming a
    turn, so the script reads as the loop's turns alone.
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def stream(self, system_prompt, messages, tools, **kw) -> AsyncIterator:
        yield StreamStart()
        if not tools:
            yield TextDelta(text="## Goal\nsummary")
            yield StreamDone(
                message=AssistantMessage(
                    content=[TextContent(text="## Goal\nsummary")],
                    usage=Usage(input_tokens=5, output_tokens=5),
                    stop_reason="end_turn",
                )
            )
            return
        self.calls.append({"messages": list(messages)})
        turn = self.turns.pop(0)
        if turn == "overflow":
            yield StreamError(
                message=AssistantMessage(
                    stop_reason="error",
                    error_message="HTTP 400: context length exceeded",
                )
            )
            return
        for block in turn.content:
            if isinstance(block, TextContent):
                yield TextDelta(text=block.text)
            elif isinstance(block, ToolCallContent):
                yield ToolCallDelta(
                    id=block.id, name=block.name, arguments_delta=json.dumps(block.arguments)
                )
        yield StreamDone(message=turn)


class TestOverflowRecovery:
    def test_the_count_survives_an_overflow_recovery_continue(self, tmp_path):
        # Two failures before the overflow and two after: four in a row only
        # if the accounting is carried across the continue.
        provider = _OverflowingProvider(
            [
                _bash(0, "ls ./a"),
                _bash(1, "ls ./b"),
                "overflow",
                _bash(2, "cat ./c"),
                _bash(3, "cat ./d"),
                _final("recovered"),
            ]
        )
        result = _brain(provider, tool_failure_nudge_after=4).execute(
            _req("go", tmp_path, tools=["Bash"])
        )
        assert result.success is True, result.result_text
        assert result.result_text == "recovered"
        per_call = [_notices(c["messages"]) for c in provider.calls]
        assert per_call[:5] == [[], [], [], [], []]
        assert len(per_call[5]) == 1
        assert "Bash ×4 (ls, cat)" in per_call[5][0]
