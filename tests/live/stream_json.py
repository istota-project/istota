"""Reading raw `--output-format stream-json` frames, for the `live` witness.

Separate from the witness itself so the scanning can be tested where the
witness cannot: `tests/test_live_witness_scan.py` runs these functions in the
default suite against hand-written transcripts, including one that must come
back negative. The live test is the only thing that can produce a real
transcript, and it costs money to run — so the half that can be wrong for free
is held by a test that runs everywhere.

**Not `brain._events.parse_stream_line`, deliberately.** That parser's own
docstring records that it returns `None` for a user frame, which is where a
tool result lives — so a witness written against istota's parser cannot see the
one thing it exists to observe, and would fall back to asserting on the path
string it put in the prompt. That assertion is satisfied by a model that never
opened anything.

Nothing here raises on malformed input: a line that is not JSON is not a frame,
and a frame missing a key it should have carries no result. A transcript the
CLI shaped differently must read as "no image-bearing tool result", never as an
error that reads like a broken test.
"""

import json
from typing import Any


def iter_frames(stdout: str) -> list[dict]:
    """Every JSON object on its own line, in order. Non-JSON lines are dropped."""
    frames: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            frames.append(parsed)
    return frames


def _content_blocks(frame: dict) -> list[dict]:
    """The content blocks of an `assistant` / `user` frame, or `[]`."""
    message = frame.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def read_calls(frames: list[dict]) -> list[tuple[str, str]]:
    """`(tool_use_id, file_path)` for every `Read` the model actually called.

    A recorded tool call, not a claim in prose: the frame is emitted by the CLI
    at the moment it dispatches the tool.
    """
    calls: list[tuple[str, str]] = []
    for frame in frames:
        if frame.get("type") != "assistant":
            continue
        for block in _content_blocks(frame):
            if block.get("type") != "tool_use" or block.get("name") != "Read":
                continue
            args = block.get("input")
            path = args.get("file_path") if isinstance(args, dict) else None
            calls.append((str(block.get("id") or ""), str(path or "")))
    return calls


def tool_result_content(frames: list[dict], tool_use_id: str) -> Any:
    """The `content` of the tool result answering `tool_use_id`, or `None`.

    An empty id matches nothing rather than everything: `read_calls` returns
    `""` for a `tool_use` block with no `id`, and a result block missing its
    `tool_use_id` would otherwise be handed back as that call's answer.
    """
    if not tool_use_id:
        return None
    for frame in frames:
        if frame.get("type") != "user":
            continue
        for block in _content_blocks(frame):
            if block.get("type") != "tool_result":
                continue
            if str(block.get("tool_use_id") or "") == tool_use_id:
                return block.get("content")
    return None


def carries_image(content: Any) -> bool:
    """Whether a tool result's content holds an image block with pixels in it.

    The whole point of the witness: Claude Code documents that reading an image
    returns visual content rather than the file's bytes, and this is what
    distinguishes that from a text result saying the file is binary. Scoped to
    the tool result's own content, so an image block anywhere else in the
    transcript cannot answer for it.

    An `image`-typed block **with no payload** does not count. The nesting is
    walked loosely because nothing pins the CLI's tool-result shape for us, and
    that looseness is exactly what would let a type marker on a file descriptor
    — `{"type": "text", ..., "file": {"type": "image"}}` — read as sight. A
    payload is the part a text result cannot have.
    """
    if isinstance(content, dict):
        if content.get("type") == "image" and _has_payload(content):
            return True
        return any(carries_image(value) for value in content.values())
    if isinstance(content, list):
        return any(carries_image(item) for item in content)
    return False


def _has_payload(block: dict) -> bool:
    """Whether an image block carries bytes rather than only a label."""
    source = block.get("source")
    if isinstance(source, dict):
        return bool(source.get("data") or source.get("url") or source.get("file_id"))
    return bool(source or block.get("data"))


def transcript_summary(frames: list[dict]) -> str:
    """A one-line census of what did arrive, for a failure message.

    A live assertion that fails against an unfamiliar transcript is expensive to
    diagnose — the run cost money and cannot be replayed — so the failure says
    what the frames were rather than only that a match was missing.
    """
    census: dict[str, int] = {}
    for frame in frames:
        kind = str(frame.get("type") or "?")
        subtype = frame.get("subtype")
        if isinstance(subtype, str) and subtype:
            kind = f"{kind}/{subtype}"
        census[kind] = census.get(kind, 0) + 1
    tools = sorted({
        str(block.get("name") or "?")
        for frame in frames
        if frame.get("type") == "assistant"
        for block in _content_blocks(frame)
        if block.get("type") == "tool_use"
    })
    return (
        f"{len(frames)} frames "
        f"({', '.join(f'{k}={v}' for k, v in sorted(census.items())) or 'none'}); "
        f"tools called: {', '.join(tools) or 'none'}"
    )
