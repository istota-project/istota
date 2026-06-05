#!/usr/bin/env python3
"""Smoke-test the native brain's openai_compat provider against live settings.

Reads the ISTOTA_BRAIN_NATIVE_* values the same way a deployment supplies them
(process environment, optionally overlaid from an env file such as docker/.env),
builds the real ``OpenAICompatibleProvider`` via ``istota.llm.make_provider``,
and sends one tiny completion. It reports whether the endpoint + key + model
actually work, plus token usage — and never prints the API key.

Usage:
    uv run python scripts/test_native_brain.py                # overlay docker/.env
    uv run python scripts/test_native_brain.py path/to/.env   # overlay a different file
    # inside the container (env already set, no file needed):
    uv run python scripts/test_native_brain.py --no-env-file
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

# Run from the repo root so "src" layout imports resolve under `uv run`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from istota.llm import (
    StreamDone,
    StreamError,
    TextContent,
    TextDelta,
    UserMessage,
    make_provider,
)


def parse_env_file(path: str) -> dict[str, str]:
    """Minimal KEY=VALUE parser (strips surrounding quotes, ignores comments)."""
    out: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[key] = val
    return out


def load_settings(argv: list[str]) -> dict[str, str]:
    """Process env first; overlay an env file unless --no-env-file is given."""
    env_path = "docker/.env"
    use_file = True
    for arg in argv:
        if arg == "--no-env-file":
            use_file = False
        elif not arg.startswith("-"):
            env_path = arg

    values = dict(os.environ)
    if use_file:
        if os.path.exists(env_path):
            print(f"  overlaying values from {env_path}")
            values.update(parse_env_file(env_path))
        else:
            print(f"  (no env file at {env_path}; using process environment only)")
    return values


async def run() -> int:
    s = load_settings(sys.argv[1:])

    kind = s.get("ISTOTA_BRAIN_KIND", "claude_code")
    provider_name = s.get("ISTOTA_BRAIN_NATIVE_PROVIDER", "openai_compat")
    base_url = s.get("ISTOTA_BRAIN_NATIVE_BASE_URL", "https://api.anthropic.com/v1")
    model = s.get("ISTOTA_BRAIN_NATIVE_MODEL", "")
    api_key = s.get("ISTOTA_BRAIN_NATIVE_API_KEY", "")
    prompt_caching = s.get("ISTOTA_BRAIN_NATIVE_PROMPT_CACHING", "false").lower() == "true"
    max_tokens = int(s.get("ISTOTA_BRAIN_NATIVE_MAX_TOKENS", "16384") or "16384")

    print("\nNative brain settings under test:")
    print(f"  ISTOTA_BRAIN_KIND           = {kind}")
    print(f"  ISTOTA_BRAIN_NATIVE_PROVIDER = {provider_name}")
    print(f"  base_url                     = {base_url}")
    print(f"  model                        = {model or '(unset)'}")
    print(f"  prompt_caching               = {prompt_caching}")
    # Never print the key — only its presence and length.
    print(f"  api_key                      = {'set (%d chars)' % len(api_key) if api_key else 'NOT SET'}")

    if kind != "native":
        print(f"\n  note: ISTOTA_BRAIN_KIND is '{kind}', not 'native'. Testing the")
        print("        native settings anyway so you can validate them before switching.")

    problems = []
    if not api_key:
        problems.append("ISTOTA_BRAIN_NATIVE_API_KEY is empty")
    if not model:
        problems.append("ISTOTA_BRAIN_NATIVE_MODEL is empty (openai_compat needs an explicit model id)")
    if problems:
        print("\nFAIL — cannot test:")
        for p in problems:
            print(f"  • {p}")
        return 2

    cfg = SimpleNamespace(
        provider=provider_name,
        api_key=api_key,
        base_url=base_url,
        extra_headers=None,
        prompt_caching=prompt_caching,
    )
    provider = make_provider(cfg)

    print(f"\n  → sending a 1-line completion to {base_url}/chat/completions ...")
    text_parts: list[str] = []
    error_msg = None
    usage = None
    stop_reason = None

    async for event in provider.stream(
        system_prompt="You are a connectivity probe. Reply with exactly: pong",
        messages=[UserMessage(content=[TextContent(text="ping")])],
        tools=[],
        model=model,
        max_tokens=min(max_tokens, 64),
    ):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, StreamError):
            error_msg = event.message.error_message
        elif isinstance(event, StreamDone):
            usage = event.message.usage
            stop_reason = event.message.stop_reason

    if error_msg:
        print("\nFAIL — provider returned an error:")
        print(f"  {error_msg}")
        print("\n  Common causes:")
        print("  • 401 / authentication → wrong or revoked ISTOTA_BRAIN_NATIVE_API_KEY")
        print("  • 404 / model_not_found → model id not served at this base_url")
        print("  • Connection error → base_url unreachable / wrong host or scheme")
        return 1

    reply = "".join(text_parts).strip()
    print("\nPASS — endpoint, key, and model all work.")
    print(f"  model reply : {reply!r}")
    print(f"  stop_reason : {stop_reason}")
    if usage:
        print(
            f"  tokens      : in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_read={usage.cache_read_tokens}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
