"""App-level message types the agent loop carries but doesn't interpret.

These satisfy the ``CustomMessage`` protocol (``role`` + ``timestamp``) so they
ride along in the context list. The session layer's ``convert_to_llm`` decides
how to render each for the provider — a ``CompactionSummaryMessage`` becomes a
user-role text message; a loop that never compacts never sees one.

Prior art: Pi's CompactionDetails / CompactionSummaryMessage (compaction.ts).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CompactionDetails:
    """File operations tracked across compaction cycles.

    Persisted on each ``CompactionSummaryMessage`` and carried forward into the
    next summarization pass so the model keeps awareness of every file touched,
    even after several compactions. Without this, the second compaction forgets
    files the first one summarized away.
    """

    read_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


@dataclass
class CompactionSummaryMessage:
    """A structured summary that replaces a prefix of compacted messages."""

    role: str = "compaction_summary"
    summary: str = ""
    tokens_before: int = 0
    details: CompactionDetails = field(default_factory=CompactionDetails)
    timestamp: float = 0.0


@dataclass
class BashExecutionMessage:
    """A recorded bash invocation (reserved for future bash-history rendering)."""

    role: str = "bash_execution"
    command: str = ""
    output: str = ""
    timestamp: float = 0.0
