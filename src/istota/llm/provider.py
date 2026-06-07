"""Provider protocol and stream-event types.

A provider streams a single assistant completion as a sequence of
``StreamEvent``s. The contract (Pi's key insight): **providers never raise for
model/API/runtime failures.** They encode failures as a ``StreamError`` event
carrying an ``AssistantMessage`` with ``stop_reason="error"`` and
``error_message`` set. The agent loop then checks ``stop_reason`` uniformly
instead of wrapping every provider call in try/except.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .types import AssistantMessage, Message, ToolSchema


@dataclass
class StreamStart:
    type: Literal["start"] = "start"


@dataclass
class TextDelta:
    type: Literal["text_delta"] = "text_delta"
    text: str = ""


@dataclass
class ToolCallDelta:
    type: Literal["tool_call_delta"] = "tool_call_delta"
    id: str = ""
    name: str = ""
    arguments_delta: str = ""


@dataclass
class StreamDone:
    type: Literal["done"] = "done"
    message: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class StreamError:
    type: Literal["error"] = "error"
    message: AssistantMessage = field(default_factory=AssistantMessage)


# Discriminated union via the ``type`` field.
StreamEvent = StreamStart | TextDelta | ToolCallDelta | StreamDone | StreamError


class LLMProvider(Protocol):
    """Contract: never raise for model/API failures.

    Encode failures as ``StreamError`` events with ``stop_reason="error"`` and
    ``error_message`` set. This lets the loop handle errors uniformly instead
    of catching exceptions from N different providers.
    """

    def stream(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        model: str = "",
        max_tokens: int = 16384,
        reasoning_effort: str | None = None,
        render_tool_images: bool = False,
    ) -> AsyncIterator[StreamEvent]: ...
