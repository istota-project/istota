"""A scripted OpenAI-compatible endpoint, for driving the daemon offline.

Why an HTTP server and not `llm/replay.py`'s `ReplayProvider`: the lean compose
stack runs the daemon inside a container and the test on the host, so the
injection point has to be one that survives a process boundary. `base_url` is
already a plain config value (`config.py:2450`) that already reaches the
rendered `config.toml`, so pointing it here changes no product code. Injecting a
provider object would mean adding an env-var construction path to
`make_provider` — production wiring changed in order to test it, and a seam no
operator ever exercises. The full reasoning is recorded in the spec's Stage 6
decision.

The cost is that this file re-implements the wire format, which is exactly the
thing `ReplayProvider` exists to avoid. That cost is paid down in
`tests/test_model_endpoint.py`, which drives this module through the real
`OpenAICompatibleProvider` over a real socket, in the default suite. Change the
framing here and that file goes red — the smoke tier would only report a task
that failed for an unrelated-looking reason.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Deliberately small, so a multi-character payload always arrives as more than
# one delta. Streaming reassembly is most of what `_parse_sse_lines` does, and a
# server that sent each turn whole would leave that path unexercised by
# everything built on top of this module.
TEXT_CHUNK = 4
ARGS_CHUNK = 8

# The host side connects over loopback; a container reaches the same listener by
# the Docker Desktop / Docker Engine alias. Both names are offered rather than
# guessed at the call site, because the two are needed at once: the smoke test
# asserts against `requests` in-process while the daemon it is driving talks to
# `container_base_url`.
LOOPBACK = "127.0.0.1"
FROM_CONTAINER = "host.docker.internal"


@dataclass
class ScriptedEndpoint:
    """A running endpoint and the record of what it was asked."""

    port: int
    host_bound: str
    requests: list[dict] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """For a caller in this process."""
        return f"http://{LOOPBACK}:{self.port}/v1"

    @property
    def container_base_url(self) -> str:
        """For a caller inside a container on this host."""
        return f"http://{FROM_CONTAINER}:{self.port}/v1"

    def close(self) -> None:
        if self._server is not None:
            # `shutdown` before `server_close`: the former stops the serve loop
            # and blocks until it has, the latter releases the socket. Reversed,
            # the loop can be mid-`accept` on a closed fd.
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> ScriptedEndpoint:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _frame(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _chunk_frame(model: str, delta: dict, finish_reason=None) -> bytes:
    return _frame(
        {
            "id": "chatcmpl-scripted",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )


def _turn_frames(turn: dict, model: str) -> list[bytes]:
    """One scripted turn as the SSE frames a real endpoint would emit.

    Terminating properly is not cosmetic: `_parse_sse_lines` treats EOF with
    neither a `finish_reason` nor `[DONE]` as a truncated response and yields a
    `StreamError` rather than the content. Both are sent, as real endpoints do.
    """
    frames: list[bytes] = []

    for piece in _chunks(turn.get("text", ""), TEXT_CHUNK):
        if piece:
            frames.append(_chunk_frame(model, {"content": piece}))

    for index, call in enumerate(turn.get("tool_calls") or []):
        # The opening frame carries id and name; the argument JSON follows in
        # fragments, which is the shape that makes the accumulator necessary.
        frames.append(
            _chunk_frame(
                model,
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": ""},
                        }
                    ]
                },
            )
        )
        arguments = call.get("arguments", {})
        encoded = arguments if isinstance(arguments, str) else json.dumps(arguments)
        for piece in _chunks(encoded, ARGS_CHUNK):
            frames.append(
                _chunk_frame(
                    model,
                    {"tool_calls": [{"index": index, "function": {"arguments": piece}}]},
                )
            )

    default_reason = "tool_calls" if turn.get("tool_calls") else "stop"
    frames.append(_chunk_frame(model, {}, turn.get("finish_reason", default_reason)))
    if turn.get("usage"):
        frames.append(_frame({"choices": [], "usage": turn["usage"]}))
    frames.append(b"data: [DONE]\n\n")
    return frames


def _exhausted_frame(served: int, scripted: int) -> bytes:
    """The response to a turn the script does not have.

    An error frame rather than a replay of the last turn. Replaying is the
    tempting default and it hides the thing worth knowing: the agent loop made a
    call the test did not describe, and answering it with a stale response turns
    an unplanned control flow into a pass. The parser surfaces this as a
    `StreamError`, which the daemon records as a failed task.
    """
    return _frame(
        {
            "error": {
                "code": 500,
                "message": (
                    f"scripted endpoint exhausted: request {served + 1} arrived "
                    f"but only {scripted} turn(s) were scripted"
                ),
            }
        }
    )


def serve_script(turns: list[dict], *, port: int = 0) -> ScriptedEndpoint:
    """Start an endpoint replaying `turns`, one per request, in order.

    A turn is ``{"text": str}`` or ``{"tool_calls": [{"id", "name",
    "arguments"}]}``, optionally with ``finish_reason`` and ``usage``. Port 0
    lets the OS choose, which is what keeps concurrent test sessions from
    colliding — the chosen port is on the returned object.
    """
    endpoint = ScriptedEndpoint(port=0, host_bound="0.0.0.0")
    state = {"index": 0}
    lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # noqa: A003 - stdlib hook name
            """Silence. The server logs one line per request to stderr
            otherwise, and pytest attaches all of it to unrelated failures."""

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
            if not self.path.endswith("/chat/completions"):
                self.send_error(404, "only /chat/completions is scripted")
                return

            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")

            with lock:
                index = state["index"]
                state["index"] += 1
                endpoint.requests.append(body)

            model = body.get("model", "")
            if index < len(turns):
                frames = _turn_frames(turns[index], model)
            else:
                frames = [_exhausted_frame(index, len(turns))]

            payload = b"".join(frames)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            # Explicit length rather than chunked: the whole script is known up
            # front, and a fixed length removes any question about whether the
            # client saw a clean end of body versus a dropped connection —
            # which the parser reports very differently.
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer((endpoint.host_bound, port), _Handler)
    server.daemon_threads = True
    endpoint.port = server.server_address[1]
    endpoint._server = server
    endpoint._thread = threading.Thread(target=server.serve_forever, daemon=True)
    endpoint._thread.start()
    return endpoint
