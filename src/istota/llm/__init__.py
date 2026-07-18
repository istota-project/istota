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

# OpenRouter app-attribution defaults (https://openrouter.ai/docs/app-attribution).
# Sent as HTTP-Referer / X-Title so requests are attributed to Istota on the
# OpenRouter dashboard and public rankings. Other providers ignore unknown
# headers, so these are only injected when the endpoint is OpenRouter. An
# operator can override either via [brain.native.extra_headers].
_OPENROUTER_APP_URL = "https://istota.cynium.com"
_OPENROUTER_APP_TITLE = "Istota"

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


def make_provider(config):
    """Construct an ``LLMProvider`` from a native-brain config object.

    ``config`` is duck-typed: it needs a ``provider`` attribute selecting the
    backend plus the fields that backend reads. This keeps Layer 1 decoupled
    from ``config.NativeBrainConfig`` (added in Phase 3) — tests pass a simple
    namespace and the real config slots in unchanged.

    Supported ``provider`` values:
    - ``"openai_compat"`` → ``OpenAICompatibleProvider`` (api_key, base_url,
      extra_headers)
    """
    from .openai_compat import OpenAICompatibleProvider

    kind = getattr(config, "provider", "openai_compat")
    if kind == "openai_compat":
        base_url = getattr(config, "base_url", "https://api.anthropic.com/v1")
        # Caching is tri-state. ``None`` (no explicit setting) defaults on for
        # Anthropic (the default deployment) and off everywhere else, so a
        # plain-OpenAI / local endpoint that rejects ``cache_control`` never
        # sees it. An explicit ``True``/``False`` always wins.
        pc = getattr(config, "prompt_caching", None)
        if pc is None:
            caching = "api.anthropic.com" in base_url
        else:
            caching = bool(pc)
        extra_headers = dict(getattr(config, "extra_headers", None) or {})
        if "openrouter.ai" in (base_url or ""):
            # Attribute this app to Istota on OpenRouter. Operator-set headers
            # win (case-insensitively), so a deployment can rebrand or clear them.
            present = {k.lower() for k in extra_headers}
            if "http-referer" not in present:
                extra_headers["HTTP-Referer"] = _OPENROUTER_APP_URL
            if "x-title" not in present:
                extra_headers["X-Title"] = _OPENROUTER_APP_TITLE
        return OpenAICompatibleProvider(
            api_key=getattr(config, "api_key", ""),
            base_url=base_url,
            extra_headers=extra_headers,
            prompt_caching=caching,
        )
    raise ValueError(f"Unknown provider: {kind!r}")
