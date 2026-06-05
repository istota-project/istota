"""Phase 2 — pure (non-async) helpers in the agent loop."""

from istota.agent.loop import _drain_queue, _has_path_overlap, _missing_required
from istota.llm.types import (
    TextContent,
    ToolCallContent,
    ToolParameter,
    ToolSchema,
    UserMessage,
)


class TestDrainQueue:
    def test_one_at_a_time_takes_first(self):
        msgs = [
            UserMessage(content=[TextContent(text="s1")]),
            UserMessage(content=[TextContent(text="s2")]),
        ]
        drained = _drain_queue(msgs, "one_at_a_time")
        assert len(drained) == 1
        assert drained[0].content[0].text == "s1"

    def test_all_takes_everything(self):
        msgs = [
            UserMessage(content=[TextContent(text="s1")]),
            UserMessage(content=[TextContent(text="s2")]),
        ]
        assert len(_drain_queue(msgs, "all")) == 2

    def test_empty(self):
        assert _drain_queue([], "all") == []
        assert _drain_queue([], "one_at_a_time") == []


class TestPathOverlap:
    def test_no_overlap(self):
        calls = [
            ToolCallContent(id="1", name="Read", arguments={"file_path": "/a"}),
            ToolCallContent(id="2", name="Read", arguments={"file_path": "/b"}),
        ]
        assert _has_path_overlap(calls) is False

    def test_overlap_file_path(self):
        calls = [
            ToolCallContent(id="1", name="Write", arguments={"file_path": "/a"}),
            ToolCallContent(id="2", name="Edit", arguments={"file_path": "/a"}),
        ]
        assert _has_path_overlap(calls) is True

    def test_overlap_path_key(self):
        calls = [
            ToolCallContent(id="1", name="Glob", arguments={"path": "/x"}),
            ToolCallContent(id="2", name="Grep", arguments={"path": "/x"}),
        ]
        assert _has_path_overlap(calls) is True

    def test_no_paths(self):
        calls = [ToolCallContent(id="1", name="Bash", arguments={"command": "ls"})]
        assert _has_path_overlap(calls) is False


class TestMissingRequired:
    def test_all_present(self):
        schema = ToolSchema(
            name="t",
            description="",
            parameters=[ToolParameter(name="x", type="string", required=True)],
        )
        assert _missing_required({"x": "v"}, schema) == []

    def test_missing_one(self):
        schema = ToolSchema(
            name="t",
            description="",
            parameters=[
                ToolParameter(name="x", type="string", required=True),
                ToolParameter(name="y", type="string", required=False),
            ],
        )
        assert _missing_required({"y": "v"}, schema) == ["x"]

    def test_null_counts_as_missing(self):
        schema = ToolSchema(
            name="t",
            description="",
            parameters=[ToolParameter(name="x", type="string", required=True)],
        )
        assert _missing_required({"x": None}, schema) == ["x"]
