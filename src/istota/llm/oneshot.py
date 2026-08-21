"""One-shot text completion — lightweight inference without the agent loop.

Some callers need a single prompt->text completion from a provider
(conversation-context triage, short structured extractions) rather than a full
tool-using agent run. ``complete_text`` drives one provider ``stream`` to
completion, collects the assistant text, and applies an optional wall-clock
timeout. It never raises for model/transport failures — a ``StreamError`` or a
timeout returns ``None`` so callers can fall back cleanly.

Two return shapes, one implementation. ``complete_text`` / ``make_completer``
hand back the text and nothing else; ``complete_message`` /
``make_message_completer`` hand back the whole ``AssistantMessage``, which also
carries ``usage``, ``model`` and ``stop_reason``. The text-only pair was the
only one for a while, and a caller that wanted to record what a one-shot call
spent had no way to (ISSUE-272) — the tokens were on the object being discarded
one line before the return.
"""

import asyncio
import logging
from collections.abc import Callable

from .provider import StreamDone, StreamError
from .types import AssistantMessage, TextContent, UserMessage

logger = logging.getLogger(__name__)


async def acomplete_message(
    provider,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "",
    max_tokens: int = 1024,
) -> AssistantMessage | None:
    """Async one-shot completion. Returns the final assistant message, or None.

    The message carries ``usage``, ``model`` and ``stop_reason`` alongside the
    text, so a caller can record what the call spent.

    A ``StreamError`` is returned rather than swallowed: the turn reached the
    provider and may have spent tokens, and the error message is on the same
    object. ``stop_reason == "error"`` is how a caller tells the two apart, and
    ``complete_text`` still collapses that case to None for the callers that
    only ever wanted the answer.
    """
    messages = [UserMessage(content=[TextContent(text=user_prompt)])]
    final = None
    async for event in provider.stream(
        system_prompt, messages, [], model=model, max_tokens=max_tokens
    ):
        if isinstance(event, StreamError):
            logger.warning(
                "oneshot completion error: %s", event.message.error_message
            )
            # Stamp the reason here rather than trusting five construction
            # sites to set it. `StreamError.message` defaults to a bare
            # `AssistantMessage`, whose `stop_reason` is `end_turn` — so a site
            # that yields `StreamError()` or forgets the field would make
            # `complete_text` return `""` instead of `None`, and a triage
            # completer hand `""` to a JSON parser as though it were an answer.
            # Collapsing the union is the one place that invariant can be held.
            error_message = event.message
            error_message.stop_reason = "error"
            return error_message
        if isinstance(event, StreamDone):
            final = event.message
    return final


async def acomplete_text(
    provider,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "",
    max_tokens: int = 1024,
) -> str | None:
    """Async one-shot completion. Returns the assistant text, or None on error."""
    final = await acomplete_message(
        provider, system_prompt, user_prompt, model=model, max_tokens=max_tokens
    )
    if final is None or final.stop_reason == "error":
        return None
    return final.text


def complete_message(
    provider,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "",
    max_tokens: int = 1024,
    timeout: float | None = None,
) -> AssistantMessage | None:
    """Sync wrapper over ``acomplete_message`` with an optional wall-clock timeout.

    Returns None on timeout or transport error — nothing was parsed by then, so
    there is no usage to report either. Safe to call from synchronous code
    (context triage, the executor); runs its own event loop.
    """

    async def _run():
        coro = acomplete_message(
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


def complete_text(
    provider,
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "",
    max_tokens: int = 1024,
    timeout: float | None = None,
) -> str | None:
    """Sync one-shot completion returning just the text, or None on any failure.

    "Any failure" now includes one case the pre-``complete_message`` version let
    through: a turn whose ``stop_reason`` is ``error`` but which arrived as an
    ordinary ``StreamDone`` rather than a ``StreamError``. OpenRouter signals an
    upstream generation failure that way (``_FINISH_REASON_MAP`` maps
    ``error -> error`` in ``openai_compat``), and the old code returned that
    message's text — usually empty, sometimes a partial. Returning None instead
    is the safer reading and matches what every caller already does with an
    unusable answer. Pinned by a test, since it is a real behaviour change
    rather than a refactor artefact.
    """
    final = complete_message(
        provider, system_prompt, user_prompt,
        model=model, max_tokens=max_tokens, timeout=timeout,
    )
    if final is None or final.stop_reason == "error":
        return None
    return final.text


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


def make_message_completer(
    provider, model: str, *, max_tokens: int = 1024
) -> Callable[..., AssistantMessage | None]:
    """Bind a provider + model into a ``prompt -> AssistantMessage|None`` callable.

    Same call shape as ``make_completer``, but the caller gets the whole message
    — text, ``usage``, ``model``, ``stop_reason`` — so a one-shot call can be
    accounted for rather than only answered.
    """

    def _complete(
        prompt: str, *, timeout: float | None = None
    ) -> AssistantMessage | None:
        return complete_message(
            provider, "", prompt, model=model, max_tokens=max_tokens, timeout=timeout
        )

    return _complete
