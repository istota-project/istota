"""Istota LLM provider abstraction (Layer 1 of the native brain).

Provider-agnostic inference. The interface is the OpenAI-compatible chat
completions API (``OpenAICompatibleProvider``), which works against Anthropic,
OpenRouter, and any local OpenAI-compatible endpoint.

This layer knows nothing about tools dispatch or agent loops — it converts
istota's ``Message`` types to/from a provider's wire format and yields
``StreamEvent``s. Providers never raise for model/API failures; they encode
failures as ``StreamError`` events (Pi's contract) so the loop handles errors
uniformly.
"""

from .provider import (
    LLMProvider,
    StreamDone,
    StreamError,
    StreamEvent,
    StreamStart,
    TextDelta,
    ToolCallDelta,
)
from .types import (
    AssistantMessage,
    Content,
    ImageContent,
    Message,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolParameter,
    ToolResultMessage,
    ToolSchema,
    Usage,
    UserMessage,
)

__all__ = [
    "AssistantMessage",
    "Content",
    "ImageContent",
    "LLMProvider",
    "Message",
    "StreamDone",
    "StreamError",
    "StreamEvent",
    "StreamStart",
    "TextContent",
    "TextDelta",
    "ThinkingContent",
    "ToolCallContent",
    "ToolCallDelta",
    "ToolParameter",
    "ToolResultMessage",
    "ToolSchema",
    "Usage",
    "UserMessage",
    "make_provider",
]


def make_provider(spec):
    """Construct an ``LLMProvider`` for one provider record.

    ``spec`` is a ``llm.providers.ProviderSpec``: the endpoint, its key (an
    ``InferenceKey``, the only key type this accepts) and the resolved compat.
    Every provider kind is reached over the OpenAI-compatible chat API; the kind
    decides the path rule and the wire defaults, never a base-URL substring.
    """
    from .openai_compat import OpenAICompatibleProvider
    from .providers import (
        OPENROUTER_APP_TITLE,
        OPENROUTER_APP_URL,
        ProviderSpec,
        reveal_inference_key,
    )

    if not isinstance(spec, ProviderSpec):
        raise TypeError(f"make_provider takes a ProviderSpec, got {type(spec).__name__}")
    compat = spec.compat
    extra_headers = dict(spec.extra_headers or {})
    if compat.attribution_headers:
        # Operator-set headers win (case-insensitively), so a deployment can
        # rebrand or clear them.
        present = {k.lower() for k in extra_headers}
        if "http-referer" not in present:
            extra_headers["HTTP-Referer"] = OPENROUTER_APP_URL
        if "x-title" not in present:
            extra_headers["X-Title"] = OPENROUTER_APP_TITLE
    return OpenAICompatibleProvider(
        api_key=reveal_inference_key(spec.inference_key),
        base_url=spec.openai_base_url,
        extra_headers=extra_headers,
        prompt_caching=compat.prompt_caching,
        # An unknown value warns in the provider and runs as "auto".
        reasoning_replay=compat.reasoning_replay,
        prompt_cache_key=compat.prompt_cache_key,
        # An unknown value warns in the provider and runs as "5m".
        prompt_cache_ttl=compat.prompt_cache_ttl,
        # An unknown value warns in the provider and runs as "off".
        strict_tool_schemas=compat.strict_tool_schemas,
        cost_accounting=compat.cost_accounting,
        max_completion_tokens_models=compat.max_completion_tokens_models,
        reasoning_effort=compat.reasoning_effort,
    )
