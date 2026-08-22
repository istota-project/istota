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
from http.server import BaseHTTPRequestHandler

from ..httpstub import FROM_CONTAINER, LOOPBACK, HttpStub

# Deliberately small, so a multi-character payload always arrives as more than
# one delta. Streaming reassembly is most of what `_parse_sse_lines` does, and a
# server that sent each turn whole would leave that path unexercised by
# everything built on top of this module.
TEXT_CHUNK = 4
ARGS_CHUNK = 8

# What `config_env` renders as the model name and the turn ceiling. Named
# because a scenario asserts on the first (`test_lean_stack.py` checks the
# request carried it) and because the second is a bound the agent loop is
# supposed to hit loudly rather than grind past.
SCRIPTED_MODEL = "scripted-test-model"
MAX_TURNS = 4


class ScriptedEndpoint(HttpStub):
    """A running endpoint and the record of what it was asked.

    `requests` stays its own list rather than becoming `HttpStub.calls`: a chat
    completion is a JSON body a scenario reads whole, not a method-and-path
    tuple, and forcing it into `ServiceCall` would lose the shape every
    assertion here uses. The protocol admits that — call recording is on
    `HttpStub`, not on `Service`.
    """

    name = "model"

    def __init__(self, turns: list[dict] | None = None) -> None:
        super().__init__()
        self.requests: list[dict] = []
        self.turns: list[dict] = list(turns or [])
        self.served: int = 0

    # -- the `Service` members --------------------------------------------

    def config_env(self) -> dict[str, str]:
        """Point the daemon's native brain at this endpoint.

        All four are read by `docker/istota/render-config.sh` and passed
        through by `docker/docker-compose.yml`, which is the rule every service
        is held to. They were hardcoded in the smoke fixture's render
        environment; on the service is where they belong, and moving them
        leaves that environment with nothing subsystem-specific in it.
        """
        return {
            "ISTOTA_BRAIN_KIND": "native",
            "ISTOTA_BRAIN_NATIVE_BASE_URL": self.container_url,
            "ISTOTA_BRAIN_NATIVE_MODEL": SCRIPTED_MODEL,
            # A handful of turns is all a scripted scenario has; a loop that
            # asked for more should fail loudly rather than grind through a
            # hundred attempts.
            "ISTOTA_BRAIN_NATIVE_MAX_TURNS": str(MAX_TURNS),
        }

    def reset(self) -> None:
        """Empty the script and forget what was asked.

        Deliberately not a *useful* script: the stack's own reset installs the
        real turns immediately afterwards, and leaving the previous test's
        script in place between the two would let a poller's task consume it.
        """
        super().reset()
        self.rescript([])

    def describe(self) -> str:
        """Counts, not content, for `Stack.diagnostics`.

        The bodies are the whole conversation — system prompt, memory, tool
        results — and dumping them into every failure report would bury the
        three lines that say what went wrong. A scenario that needs the content
        has `transcript()`.
        """
        with self._lock:
            served, scripted, seen = self.served, len(self.turns), len(self.requests)
        return f"  {served} turn(s) served of {scripted} scripted, {seen} request(s) recorded"

    # -- addresses --------------------------------------------------------
    #
    # `/v1` on the end, because the provider appends `/chat/completions` to
    # whatever `base_url` it is given.

    @property
    def url(self) -> str:
        """For a caller in this process."""
        return f"http://{LOOPBACK}:{self.port}/v1"

    @property
    def container_url(self) -> str:
        """For a caller inside a container on this host."""
        return f"http://{FROM_CONTAINER}:{self.port}/v1"

    # -- scripting --------------------------------------------------------

    def rescript(self, turns: list[dict]) -> None:
        """Replace the script, and rewind.

        A caller that only learns what to script *after* the endpoint is
        listening needs this: a scripted command may have to name a port that
        did not exist until something bound it. Starting a second endpoint
        instead would mean re-rendering the config that carries the first one's
        `base_url`, which is a stack restart.

        Rewinding is part of it. Leaving the index where it was would make the
        new script's first turn answer as though it were the Nth, and the
        symptom is an exhausted-script error frame on a run that scripted
        plenty.
        """
        with self._lock:
            self.turns = list(turns)
            self.served = 0
            self.requests.clear()

    def transcript(self) -> str:
        """Every message the endpoint was ever sent, as one string.

        The place tool *results* show up. A scenario asserting on what a
        command printed has no other view of it: the output goes into the
        conversation the daemon sends back, never into the task row.
        """
        with self._lock:
            bodies = list(self.requests)
        parts = []
        for body in bodies:
            for message in body.get("messages") or []:
                parts.append(str(message.get("content")))
        return "\n".join(parts)


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

    The message says where *not* to look, because this frame is a common second
    cause. `tasks.max_attempts` defaults to 3, so a first attempt that failed
    for an unrelated reason consumes the script and the retry lands here — and
    the error the row ends up carrying then names the harness rather than the
    original fault, on exactly the path a maintainer would be debugging.
    """
    return _frame(
        {
            "error": {
                "code": 500,
                "message": (
                    f"scripted endpoint exhausted: request {served + 1} arrived "
                    f"but only {scripted} turn(s) were scripted. If this is a "
                    "retry, the first attempt failed for another reason and "
                    "this message has replaced it — read the daemon log."
                ),
            }
        }
    )


def serve_script(
    turns: list[dict],
    *,
    port: int = 0,
    host: str = LOOPBACK,
    credential: str | None = None,
) -> ScriptedEndpoint:
    """Start an endpoint replaying `turns`, one per request, in order.

    A turn is ``{"text": str}`` or ``{"tool_calls": [{"id", "name",
    "arguments"}]}``, optionally with ``finish_reason`` and ``usage``. Port 0
    lets the OS choose, which is what keeps concurrent test sessions from
    colliding — the chosen port is on the returned object.

    `host` defaults to loopback and only the deployment tiers override it.
    Binding all interfaces unconditionally would publish an unauthenticated POST
    listener on every `uv run pytest`, which the ten default-suite tests here
    have no use for — they connect over `url`, which is loopback. It also raises
    the macOS incoming-connections prompt, where the run appears to hang on a
    dialog nobody is looking at.

    `credential` is therefore required whenever `host` is not loopback, per
    `HttpStub.start`. This endpoint does not *check* it: the daemon sends
    whatever `ISTOTA_BRAIN_NATIVE_API_KEY` the compose file hardcodes, and a 401
    from here would surface as a task that failed for an unrelated-looking
    reason — which is the failure mode this whole module exists to avoid. What
    the value buys is that the tier knows the name of every secret it has
    published, which is what the secret-isolation scenario scans a transcript
    for.
    """
    # The script lives on the endpoint rather than in this closure, so
    # `rescript` can replace it after the server is listening.
    endpoint = ScriptedEndpoint(turns=list(turns))

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Bounds a parked keep-alive connection. `server_close` now joins
        # handler threads (see `daemon_threads` below), and HTTP/1.1 keeps the
        # socket open between requests — so a client that connected and went
        # quiet would block `handle_one_request`, and the join behind it, for as
        # long as it liked. Five seconds is far longer than any scripted turn.
        timeout = 5

        # Stdlib hook names, so they are not ours to rename. No `noqa` codes:
        # the project pins ruff to E4/E7/E9/F (AGENTS.md), so a suppression for
        # A003 or N802 would name a rule that is not enabled and read as though
        # it were doing something.
        def log_message(self, *args) -> None:
            """Silence. The server logs one line per request to stderr
            otherwise, and pytest attaches all of it to unrelated failures."""

        def handle_error(self, request, client_address) -> None:
            """Silence too, and for the same reason.

            `log_message` alone is not enough: an unhandled exception in a
            handler thread — a malformed body, a client that hung up mid-write —
            goes to `handle_error`, which prints a full traceback to stderr that
            pytest then attaches to whichever test happens to be running.
            """

        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                self.send_error(404, "only /chat/completions is scripted")
                return

            length = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    # A body of `[]` or `"x"` parses fine and then raises
                    # `AttributeError` on `.get` below — in a handler thread
                    # whose `handle_error` is deliberately silent, so the client
                    # sees a dropped connection. `transcript()` would raise the
                    # same way later. `ServiceCall.payload` carries the same
                    # guard for the stubs that record a body rather than
                    # replaying one, which is what it bites on when someone
                    # points a different client at either.
                    raise ValueError("body was not a JSON object")
            except (ValueError, OSError):
                # A 400 the caller can see beats a traceback in someone else's
                # test output.
                self.send_error(400, "expected a JSON body")
                return

            with endpoint._lock:
                index = endpoint.served
                endpoint.served += 1
                endpoint.requests.append(body)
                scripted = list(endpoint.turns)

            model = body.get("model", "")
            if index < len(scripted):
                frames = _turn_frames(scripted[index], model)
            else:
                frames = [_exhausted_frame(index, len(scripted))]

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

    endpoint.start(_Handler, host=host, port=port, credential=credential)
    return endpoint
