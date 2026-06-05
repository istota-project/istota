#!/usr/bin/env python3
"""Tier-2 standalone native-brain loop runner.

Runs one prompt through a ``NativeBrain`` with no executor, scheduler, Talk,
email, or deferred-DB machinery. Prints the live event stream, the final
``BrainResult``, and the accumulated ``TaskUsage`` so cost is visible during dev.
Tools operate in a throwaway temp dir — nothing touches any user's Nextcloud/DB.

Examples
--------
    # Offline, deterministic — a scripted mock provider drives the loop.
    uv run python scripts/native_repl.py --provider mock \
        --script tests/native/fixtures/two_tool_turn.json "list files and read README"

    # Replay a recorded SSE session through the real parser (no credits).
    uv run python scripts/native_repl.py --provider replay \
        --fixture tests/native/fixtures/single_tool.jsonl "..."

    # Live, against whatever the dev config points at (needs a key).
    uv run python scripts/native_repl.py -c config/config.dev.toml --provider live \
        "summarize this repo"

The mock ``--script`` is a JSON array of assistant turns:
    [
      {"tool_calls": [{"id": "c1", "name": "Read", "arguments": {"file_path": "README"}}]},
      {"text": "Done — README is a project readme."}
    ]
Each tool_call turn drives one loop iteration; the first text-only turn ends it.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

# Make ``src/`` importable when run directly (uv run handles this too).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from istota.brain import TextEvent, ToolUseEvent  # noqa: E402
from istota.brain._types import BrainRequest  # noqa: E402
from istota.brain.native import NativeBrain  # noqa: E402
from istota.config import NativeBrainConfig  # noqa: E402

DEFAULT_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]


class _ScriptedMockProvider:
    """Yields canned assistant turns from a JSON script. Never touches network."""

    def __init__(self, script_path: Path):
        from istota.llm.provider import StreamDone
        from istota.llm.types import AssistantMessage, TextContent, ToolCallContent

        raw = json.loads(Path(script_path).read_text())
        self._StreamDone = StreamDone
        self._turns = []
        for turn in raw:
            content = []
            if turn.get("text"):
                content.append(TextContent(text=turn["text"]))
            for tc in turn.get("tool_calls", []):
                content.append(
                    ToolCallContent(
                        id=tc.get("id", ""),
                        name=tc["name"],
                        arguments=tc.get("arguments", {}),
                    )
                )
            stop = "tool_use" if turn.get("tool_calls") else "end_turn"
            self._turns.append(AssistantMessage(content=content, stop_reason=stop))

    async def stream(self, system_prompt, messages, tools, *, model="", max_tokens=16384):
        msg = self._turns.pop(0) if self._turns else None
        if msg is None:
            from istota.llm.types import AssistantMessage, TextContent

            msg = AssistantMessage(content=[TextContent(text="(script exhausted)")])
        yield self._StreamDone(message=msg)


def _build_native_config(args) -> NativeBrainConfig:
    if args.config:
        from istota.config import load_config

        cfg = load_config(Path(args.config))
        native = cfg.brain.native
    else:
        native = NativeBrainConfig()
    if args.model:
        native.model = args.model
    if args.max_turns:
        native.max_turns = args.max_turns
    return native


def _build_provider(args, native: NativeBrainConfig):
    if args.provider == "mock":
        if not args.script:
            sys.exit("--provider mock requires --script PATH")
        return _ScriptedMockProvider(Path(args.script))
    if args.provider == "replay":
        if not args.fixture:
            sys.exit("--provider replay requires --fixture PATH")
        from istota.llm.replay import ReplayProvider

        return ReplayProvider(Path(args.fixture), model=native.model)
    if args.provider == "live":
        from istota.llm import make_provider

        if native.provider == "openai_compat" and not native.api_key:
            print(
                "warning: no api_key on [brain.native] — set ISTOTA_BRAIN_NATIVE_API_KEY",
                file=sys.stderr,
            )
        return make_provider(native)
    sys.exit(f"unknown provider {args.provider!r}")


def _on_event(event) -> None:
    if isinstance(event, ToolUseEvent):
        print(f"  \033[36m▸ {event.description}\033[0m")
    elif isinstance(event, TextEvent):
        text = event.text.strip()
        if text:
            print(f"  \033[90m{text}\033[0m")


def main() -> int:
    p = argparse.ArgumentParser(description="Standalone native-brain loop runner")
    p.add_argument("prompt", help="the user prompt to run")
    p.add_argument("-c", "--config", help="path to a config TOML (for live provider)")
    p.add_argument(
        "--provider", choices=["mock", "replay", "live"], default="mock",
        help="inference backend (default: mock)",
    )
    p.add_argument("--model", default="", help="override the model id")
    p.add_argument("--fixture", help="SSE fixture path (replay provider)")
    p.add_argument("--script", help="JSON turn script (mock provider)")
    p.add_argument(
        "--tools", default=",".join(DEFAULT_TOOLS),
        help="comma-separated tool subset (default: all six)",
    )
    p.add_argument("--max-turns", type=int, default=0, help="override max assistant turns")
    p.add_argument("--show-events", action="store_true", help="dump the full execution trace")
    args = p.parse_args()

    native = _build_native_config(args)
    provider = _build_provider(args, native)
    brain = NativeBrain(native, provider=provider)

    allowed = [t.strip() for t in args.tools.split(",") if t.strip()]
    workdir = Path(tempfile.mkdtemp(prefix="native-repl-"))

    req = BrainRequest(
        prompt=args.prompt,
        allowed_tools=allowed,
        cwd=workdir,
        env={},
        timeout_seconds=120,
        model=native.model,
        streaming=True,
        on_progress=_on_event,
    )

    print(f"\033[1mprompt:\033[0m {args.prompt}")
    print(f"\033[1mprovider:\033[0m {args.provider}  model={native.model or '(default)'}  cwd={workdir}")
    print("\033[1m--- stream ---\033[0m")
    result = brain.execute(req)

    print("\033[1m--- result ---\033[0m")
    print(result.result_text)
    print(f"\033[1mstop_reason:\033[0m {result.stop_reason}  success={result.success}")
    if result.usage is not None:
        u = result.usage
        print(
            f"\033[1musage:\033[0m in={u.input_tokens} out={u.output_tokens} "
            f"cache_read={u.cache_read_tokens} ${u.cost_usd:.4f}"
        )
    if args.show_events and result.execution_trace:
        print("\033[1m--- trace ---\033[0m")
        for entry in json.loads(result.execution_trace):
            print(f"  [{entry.get('type')}] {entry.get('text', '')[:200]}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
