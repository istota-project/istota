"""Output-aware loop detection (Crush refinement item 2)."""

from istota.llm.types import AssistantMessage, TextContent, ToolCallContent, ToolResultMessage
from istota.session.loop_detection import detect_repeated_tool_calls


def _call(call_id: str, name: str, args: dict) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCallContent(id=call_id, name=name, arguments=args)],
        stop_reason="tool_use",
    )


def _result(call_id: str, name: str, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(text=text)],
    )


def _repeat(name: str, args: dict, result_text: str, n: int) -> list:
    msgs = []
    for i in range(n):
        cid = f"{name}-{i}"
        msgs.append(_call(cid, name, args))
        msgs.append(_result(cid, name, result_text))
    return msgs


class TestDetectRepeatedToolCalls:
    def test_none_when_no_repeats(self):
        msgs = [
            *_repeat("Read", {"file_path": "a"}, "alpha", 1),
            *_repeat("Read", {"file_path": "b"}, "beta", 1),
        ]
        assert detect_repeated_tool_calls(msgs) is None

    def test_fires_on_identical_call_and_result(self):
        msgs = _repeat("Bash", {"command": "ls"}, "same output", 6)
        sig = detect_repeated_tool_calls(msgs)
        assert sig is not None

    def test_below_threshold_does_not_fire(self):
        msgs = _repeat("Bash", {"command": "ls"}, "same output", 5)
        assert detect_repeated_tool_calls(msgs, max_repeats=5) is None

    def test_distinct_results_same_call_does_not_fire(self):
        # Same call+args but different output each time — making progress.
        msgs = []
        for i in range(8):
            cid = f"c{i}"
            msgs.append(_call(cid, "Bash", {"command": "tail log"}))
            msgs.append(_result(cid, "Bash", f"line {i}"))
        assert detect_repeated_tool_calls(msgs) is None

    def test_window_limits_lookback(self):
        # 6 identical at the start, then 10 distinct calls push them out of window.
        old = _repeat("Bash", {"command": "old"}, "x", 6)
        recent = []
        for i in range(10):
            cid = f"r{i}"
            recent.append(_call(cid, "Read", {"file_path": f"f{i}"}))
            recent.append(_result(cid, "Read", f"content {i}"))
        assert detect_repeated_tool_calls(old + recent, window=10) is None

    def test_ignores_unpaired_calls(self):
        # A dangling call with no result is skipped, not counted.
        msgs = [_call("x", "Bash", {"command": "ls"})]
        assert detect_repeated_tool_calls(msgs) is None

    def test_non_unique_ids_with_distinct_results_does_not_fire(self):
        # NB-5: endpoints that emit deterministic per-response ids (call_0,
        # llama.cpp / vLLM style) reuse the same id every turn. Six progressing
        # `tail log` calls each get a DIFFERENT result — must NOT trip. The old
        # global last-write-wins result map paired every call with the newest
        # result, hashing them identically.
        msgs = []
        for i in range(8):
            msgs.append(_call("call_0", "Bash", {"command": "tail log"}))
            msgs.append(_result("call_0", "Bash", f"line {i}"))
        assert detect_repeated_tool_calls(msgs) is None

    def test_non_unique_ids_with_identical_results_fires(self):
        # Same reused id AND identical result each turn = genuinely stuck.
        msgs = []
        for _ in range(6):
            msgs.append(_call("call_0", "Bash", {"command": "ls"}))
            msgs.append(_result("call_0", "Bash", "same output"))
        assert detect_repeated_tool_calls(msgs) is not None

    def test_parallel_calls_in_one_message_pair_by_local_id(self):
        # An assistant message with two parallel calls, followed by their two
        # results, must pair each call with its own result (unique ids within
        # the message), not cross-pair.
        msgs = [
            AssistantMessage(
                content=[
                    ToolCallContent(id="call_0", name="Read", arguments={"file_path": "a"}),
                    ToolCallContent(id="call_1", name="Read", arguments={"file_path": "b"}),
                ],
                stop_reason="tool_use",
            ),
            _result("call_0", "Read", "alpha"),
            _result("call_1", "Read", "beta"),
        ]
        assert detect_repeated_tool_calls(msgs) is None
