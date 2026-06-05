"""Session-layer custom message types carried through the agent loop."""

from istota.agent.types import CustomMessage
from istota.session.messages import (
    CompactionDetails,
    CompactionSummaryMessage,
)


class TestCompactionSummaryMessage:
    def test_satisfies_custom_message_protocol(self):
        msg = CompactionSummaryMessage(summary="s")
        assert isinstance(msg, CustomMessage)
        assert msg.role == "compaction_summary"

    def test_defaults(self):
        msg = CompactionSummaryMessage()
        assert msg.summary == ""
        assert msg.tokens_before == 0
        assert isinstance(msg.details, CompactionDetails)
        assert msg.details.read_files == []
        assert msg.details.modified_files == []


class TestCompactionDetails:
    def test_holds_file_lists(self):
        d = CompactionDetails(read_files=["a"], modified_files=["b"])
        assert d.read_files == ["a"]
        assert d.modified_files == ["b"]
