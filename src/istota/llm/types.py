"""Message and tool types for the LLM provider layer.

Content blocks are frozen dataclasses — immutability prevents accidental
mutation during parallel tool execution. Message types are mutable because the
agent loop appends to ``content`` during streaming.
"""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class TextContent:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass(frozen=True)
class ImageContent:
    type: Literal["image"] = "image"
    media_type: str = ""
    data: str = ""  # base64


@dataclass(frozen=True)
class ToolCallContent:
    type: Literal["tool_call"] = "tool_call"
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ThinkingContent:
    type: Literal["thinking"] = "thinking"
    thinking: str = ""


Content = TextContent | ImageContent | ToolCallContent | ThinkingContent


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Provider-reported cost in USD for this turn, when the endpoint returns it
    # (OpenRouter's ``usage.cost``). ``None`` means the provider reported no
    # cost, so telemetry falls back to catalog-price computation. Distinct from
    # ``0.0`` (a genuine free turn) — see ``session.usage.TaskUsage.add``.
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass
class UserMessage:
    role: Literal["user"] = "user"
    content: list[TextContent | ImageContent] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass
class AssistantMessage:
    role: Literal["assistant"] = "assistant"
    content: list[Content] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"  # end_turn | tool_use | error | aborted | max_tokens
    error_message: str | None = None
    model: str = ""
    timestamp: float = 0.0

    @property
    def tool_calls(self) -> list[ToolCallContent]:
        return [c for c in self.content if isinstance(c, ToolCallContent)]

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in self.content if isinstance(c, TextContent))


@dataclass
class ToolResultMessage:
    role: Literal["tool_result"] = "tool_result"
    tool_call_id: str = ""
    tool_name: str = ""
    content: list[TextContent | ImageContent] = field(default_factory=list)
    is_error: bool = False
    timestamp: float = 0.0


# The LLM layer's message type — what providers understand.
Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: str  # "string", "integer", "boolean", "object", "array"
    description: str = ""
    required: bool = True
    enum: list[str] | None = None
    properties: "dict[str, ToolParameter] | None" = None


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: list[ToolParameter] = field(default_factory=list)
