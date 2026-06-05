#!/usr/bin/env python3
"""Tier-4 shadow compare — run one prompt through both brains, diff the output.

Confidence-building before flipping any task type to native. Runs the same
prompt through ``ClaudeCodeBrain`` and ``NativeBrain`` (each in its own throwaway
cwd so parallel file tools can't collide), then diffs result text, tool-call
sequence, and ``TaskUsage``.

This is the local, manual form of the Phase 4 shadow mode. It needs the live
``claude`` CLI for the claude_code side and a real API key (via
``ISTOTA_BRAIN_NATIVE_API_KEY`` or ``[brain.native] api_key``) for the native
side, so it is not run in CI — only the pure ``compare_brain_results`` /
``format_comparison`` logic is unit-tested.

    uv run python scripts/brain_shadow.py -c config/config.dev.toml \
        "read README and summarize it in one sentence"

Exact parity is not expected — the two brains manage context differently and
expose different tool schemas. Outcomes should be *equivalent*; large text
divergence or wildly different tool sequences are the signal to investigate.
"""

import argparse
import difflib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DEFAULT_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]


def _actions(result) -> list:
    try:
        return json.loads(result.actions_taken or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def _usage_dict(result):
    u = getattr(result, "usage", None)
    if u is None:
        return None
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cost_usd": u.cost_usd,
    }


def compare_brain_results(a, b) -> dict:
    """Diff two BrainResults. ``a`` is claude_code, ``b`` is native, by convention.

    Pure — no I/O. Returns a structured comparison dict consumed by
    ``format_comparison`` and any caller that wants to assert on divergence.
    """
    text_a = a.result_text or ""
    text_b = b.result_text or ""
    actions_a = _actions(a)
    actions_b = _actions(b)
    return {
        "text_identical": text_a == text_b,
        "text_similarity": difflib.SequenceMatcher(None, text_a, text_b).ratio(),
        "text_a": text_a,
        "text_b": text_b,
        "actions_match": actions_a == actions_b,
        "actions_a": actions_a,
        "actions_b": actions_b,
        "stop_a": a.stop_reason,
        "stop_b": b.stop_reason,
        "success_a": a.success,
        "success_b": b.success,
        "usage_a": _usage_dict(a),
        "usage_b": _usage_dict(b),
    }


def format_comparison(cmp: dict) -> str:
    lines = []
    lines.append("=== shadow compare (a=claude_code, b=native) ===")
    lines.append(f"success:    a={cmp['success_a']}  b={cmp['success_b']}")
    lines.append(f"stop:       a={cmp['stop_a']}  b={cmp['stop_b']}")
    lines.append(
        f"text:       identical={cmp['text_identical']} "
        f"similarity={cmp['text_similarity']:.3f}"
    )
    if not cmp["text_identical"]:
        diff = difflib.unified_diff(
            cmp["text_a"].splitlines(),
            cmp["text_b"].splitlines(),
            fromfile="claude_code",
            tofile="native",
            lineterm="",
        )
        lines.append("  --- text diff ---")
        lines.extend("  " + ln for ln in diff)
    lines.append(f"tool calls: match={cmp['actions_match']}")
    if not cmp["actions_match"]:
        lines.append(f"  a ({len(cmp['actions_a'])}): {cmp['actions_a']}")
        lines.append(f"  b ({len(cmp['actions_b'])}): {cmp['actions_b']}")
    if cmp["usage_b"] is not None:
        u = cmp["usage_b"]
        lines.append(
            f"native usage: in={u['input_tokens']} out={u['output_tokens']} "
            f"cache_read={u['cache_read_tokens']} ${u['cost_usd']:.4f}"
        )
    return "\n".join(lines)


def _run_brain(brain, prompt, model, tools):
    from istota.brain._types import BrainRequest

    workdir = Path(tempfile.mkdtemp(prefix="shadow-"))
    req = BrainRequest(
        prompt=prompt,
        allowed_tools=tools,
        cwd=workdir,
        env={},
        timeout_seconds=300,
        model=model,
        streaming=False,
    )
    return brain.execute(req)


def main() -> int:
    p = argparse.ArgumentParser(description="Shadow-compare claude_code vs native")
    p.add_argument("prompt")
    p.add_argument("-c", "--config", required=True, help="config TOML (selects brains)")
    p.add_argument("--tools", default=",".join(DEFAULT_TOOLS))
    args = p.parse_args()

    from istota.brain import make_brain
    from istota.config import BrainConfig, load_config

    config = load_config(Path(args.config))
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]

    cc_brain = make_brain(BrainConfig(kind="claude_code"))
    native_brain = make_brain(BrainConfig(kind="native", native=config.brain.native))

    print("running claude_code ...", file=sys.stderr)
    cc = _run_brain(cc_brain, args.prompt, config.model, tools)
    print("running native ...", file=sys.stderr)
    nat = _run_brain(native_brain, args.prompt, config.brain.native.model, tools)

    print(format_comparison(compare_brain_results(cc, nat)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
