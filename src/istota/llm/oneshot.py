"""One-shot text completion — lightweight inference without the agent loop.

Some callers need a single prompt->text completion from a provider (Pass-2 skill
classification, short structured extractions) rather than a full tool-using
agent run. ``complete_text`` drives one provider ``stream`` to completion,
collects the assistant text, and applies an optional wall-clock timeout. It
never raises for model/transport failures — a ``StreamError`` or a timeout
returns ``None`` so callers can fall back cleanly.
"""

import asyncio
import logging
from collections.abc import Callable

from .provider import StreamDone, StreamError
from .types import TextContent, UserMessage

logger = logging.getLogger(__name__)


async def acomplete_text(
    provider,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "",
    max_tokens: int = 1024,
) -> str | None:
    """Async one-shot completion. Returns the assistant text, or None on error."""
    messages = [UserMessage(content=[TextContent(text=user_prompt)])]
    final = None
    async for event in provider.stream(
        system_prompt, messages, [], model=model, max_tokens=max_tokens
    ):
        if isinstance(event, StreamError):
            logger.warning(
                "oneshot completion error: %s", event.message.error_message
            )
            return None
        if isinstance(event, StreamDone):
            final = event.message
    return final.text if final is not None else None


def complete_text(
    provider,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "",
    max_tokens: int = 1024,
    timeout: float | None = None,
) -> str | None:
    """Sync wrapper over ``acomplete_text`` with an optional wall-clock timeout.

    Returns None on timeout or transport error. Safe to call from synchronous
    code (skill selection, the executor); runs its own event loop.
    """

    async def _run():
        coro = acomplete_text(
            provider, system_prompt, user_prompt, model=model, max_tokens=max_tokens
        )
        if timeout is not None:
            return await asyncio.wait_for(coro, timeout=timeout)
        return await coro

    try:
        return asyncio.run(_run())
    except asyncio.TimeoutError:
        logger.warning("oneshot completion timed out after %.1fs", timeout)
        return None
    except Exception as e:  # never raise for inference failures
        logger.warning("oneshot completion failed: %s", e)
        return None


def make_completer(
    provider, model: str, *, max_tokens: int = 1024
) -> Callable[[str], str | None]:
    """Bind a provider + model into a ``prompt -> text|None`` callable.

    ``max_tokens`` defaults higher than a chat reply needs on purpose: reasoning
    models emit ``reasoning_content`` before any answer, so a tight budget can
    leave the actual content empty. Callers that classify against such models
    should give generous headroom.
    """

    def _complete(prompt: str, *, timeout: float | None = None) -> str | None:
        return complete_text(
            provider, "", prompt, model=model, max_tokens=max_tokens, timeout=timeout
        )

    return _complete
