"""Brain protocol — the boundary between executor orchestration and model invocation.

The executor builds a fully composed prompt + env + sandbox configuration
and hands a BrainRequest to the brain. The brain owns everything from
"we have a prompt and an env" through "we have a result + trace": building
the model call, running it, parsing streaming events, and retrying on
transient API errors. Result post-processing (malformed-output detection,
CM-aware composition) and deferred file processing stay in the executor.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ._events import StreamEvent


@dataclass
class BrainRequest:
    """Inputs the brain needs to execute one task attempt."""

    prompt: str
    allowed_tools: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_seconds: int

    # Model selection (empty string = brain default)
    model: str = ""
    effort: str = ""

    # Optional: override system prompt with a file's contents
    custom_system_prompt_path: Path | None = None

    # Whether the brain should stream events for progress callbacks. When
    # False, the brain may pick a faster non-streaming path if it has one.
    streaming: bool = True

    # Stream events forwarded to the caller (per ToolUseEvent, TextEvent,
    # and ContextManagementEvent emitted by the brain). The executor wraps
    # this to filter for tool-use vs text and rate-limit Talk progress.
    on_progress: Callable[[StreamEvent], None] | None = None

    # Returns True if the task has been cancelled and execution should stop.
    # Polled between events; the brain kills its subprocess and returns
    # stop_reason="cancelled".
    cancel_check: Callable[[], bool] | None = None

    # Called once with the subprocess PID after spawn (for !stop support).
    on_pid: Callable[[int], None] | None = None

    # Wraps a command list (e.g. for bubblewrap sandboxing). The brain
    # builds its raw command, then calls sandbox_wrap(cmd) before exec
    # if provided. Returning the cmd unchanged is the no-op default.
    sandbox_wrap: Callable[[list[str]], list[str]] | None = None

    # ClaudeCodeBrain-specific: optional fallback file the model writes its
    # final result to when stream parsing fails. Other brains may ignore.
    result_file: Path | None = None


@dataclass
class BrainResult:
    """Outputs of one brain.execute() call.

    actions_taken and execution_trace are JSON-encoded strings (or None
    when not applicable) — same shape as the original executor returns,
    so downstream DB writes and post-processing stay unchanged.

    stop_reason is a short tag describing how execution ended; the
    executor uses it to decide retry vs cancel vs fail.
    """

    success: bool
    result_text: str
    actions_taken: str | None = None
    execution_trace: str | None = None
    stop_reason: str = "completed"  # completed/cancelled/timeout/oom/transient_api_error/error/not_found


class Brain(Protocol):
    """The single boundary every brain implementation satisfies."""

    def execute(self, req: BrainRequest) -> BrainResult: ...


@dataclass
class BrainConfig:
    """Selects which brain to use and shared brain-level knobs.

    Per-brain settings (e.g. OpenRouter API key) live in their own nested
    config blocks added in later phases.
    """

    kind: str = "claude_code"  # "claude_code" (only option in phase 1)
