"""A scripted model endpoint, for driving the daemon offline.

It speaks the OpenAI chat-completions format, which the native brain uses, and
Anthropic's Messages format at `/v1/messages`, which the `claude` CLI uses.
Both replay the same script: one turn per request in call order, or, for an
item carrying `when`, the turn a route picks from the request itself.

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
import re
from contextlib import contextmanager
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

# The credential a deployment tier binds this endpoint with. It authenticates
# nothing — the daemon sends whatever the compose file hardcodes as
# `ISTOTA_BRAIN_NATIVE_API_KEY`, and this endpoint answers regardless — but
# `HttpStub.start` requires one for a non-loopback bind so the tier knows the
# name of every value it has published on a shared network. Here rather than in
# a fixture for the same reason `FORGE_TOKEN` is on the forge: a service's own
# credential belongs to the service.
ENDPOINT_CREDENTIAL = "unused-by-the-scripted-endpoint"

# What a request refused by the barrier is answered with, and it is chosen
# against the *daemon's* classifier rather than for HTTP correctness.
#
# 409 Conflict is the semantically apt code and it is the wrong one. A refused
# turn fails the task, and the scheduler then decides whether to retry:
# `is_permanent` is `is_api_error_banner(result) and is_permanent_api_error(result)`,
# and 409 appears in neither `TRANSIENT_STATUS_CODES` nor
# `PERMANENT_STATUS_CODES` (`brain/claude_code.py`). Not-permanent means retry,
# which writes the row back as `status = 'pending'` with `scheduled_for` one,
# four, then sixteen minutes out — and a harness that counted that as in-flight
# would wedge every remaining test in the profile.
#
# 403 is in `PERMANENT_STATUS_CODES`, so the daemon fails the task outright,
# and it is in no HTTP client's retry set either. `Stack.in_flight` and
# `Stack.reset_framework_state` close the retry hole independently, for the
# tasks that fail for reasons this endpoint did not choose; this is the half
# that stops the barrier's own remedy from creating one.
BARRIER_STATUS = 403

#: What a request that matched no route is answered with, when the script has
#: routes and no positional item is left. A daemon poller's task lands here, so
#: it has to be an answer that does nothing and is not an error: a failed task
#: is retried, and the retry row is what wedges the next reset's quiesce.
UNSCRIPTED_TURN = {"text": "NO_ACTION: unscripted"}

#: How a scenario marks the requests it is responsible for (`[e2e:<nonce>]`).
#: `unmatched` records the markers each unmatched request carried, so a reader
#: can tell a stray poller task (none) from a scenario's own task that found no
#: route (one or more).
MARKER_RE = re.compile(r"\[e2e:[^\]\s]+\]")

#: How much of an unmatched request's first user message `unmatched` keeps.
UNMATCHED_EXCERPT = 200


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
        #: Requests turned away while the barrier was up. Read by `Stack.reset`
        #: to tell "nothing arrived during the swap" from "something did".
        self.refused: int = 0
        #: `claude` CLI session-title requests, answered without a turn and
        #: not recorded in `requests` (see `_is_title_request`).
        self.titles_served: int = 0
        #: Requests the script had no answer for, once it has routes: one that
        #: matched no route with no positional item left, or one that matched a
        #: route and ran past its turns. A dict each: `reason` (`no_route` or
        #: `exhausted`), `when` (the matched route's marker, or None),
        #: `markers` (every `[e2e:...]` in the request's first user message)
        #: and `excerpt` (its first `UNMATCHED_EXCERPT` characters).
        self.unmatched: list[dict] = []
        #: The next positional item. Counted apart from `served` because a
        #: routed request takes none; with no route in the script every request
        #: is positional and the two are equal.
        self.positional_served: int = 0
        self._barred: bool = False

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
        with self._lock:
            self.refused = 0

    def describe(self) -> str:
        """Counts, not content, for `Stack.diagnostics`.

        The bodies are the whole conversation — system prompt, memory, tool
        results — and dumping them into every failure report would bury the
        three lines that say what went wrong.

        Three views, narrowest first. `tool_results()` is what the commands
        printed, which is where a failure names itself and is what
        `Stack.diagnostics` renders alongside these counts. `transcript()` is
        every message ever sent, for a scenario asserting on something the model
        was told rather than on something a command said. This stays counts
        only: what it answers is whether the endpoint served the turns it was
        scripted for, and no amount of content answers that.
        """
        with self._lock:
            served, scripted, seen = self.served, len(self.turns), len(self.requests)
            refused, unmatched = self.refused, len(self.unmatched)
            routes = sum(1 for item in self.turns if "when" in item)
            positional = self.positional_served
        if routes:
            return (
                f"  {served} request(s) answered: {routes} route(s) and "
                f"{scripted - routes} positional item(s) scripted, "
                f"{positional} positional served, "
                f"{seen} request(s) recorded, {refused} refused at the barrier, "
                f"{unmatched} unmatched"
            )
        return (
            f"  {served} turn(s) served of {scripted} scripted, "
            f"{seen} request(s) recorded, {refused} refused at the barrier, "
            f"{unmatched} unmatched"
        )

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

    @property
    def anthropic_container_url(self) -> str:
        """`ANTHROPIC_BASE_URL` for a `claude` CLI inside a container.

        No `/v1`: the CLI appends `/v1/messages` itself.
        """
        return f"http://{FROM_CONTAINER}:{self.port}"

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
            self.positional_served = 0
            self.requests.clear()
            self.unmatched.clear()

    @contextmanager
    def barrier(self):
        """Refuse every request for the duration, and count what was refused.

        The barrier between quiescing and rescripting. The daemon runs its
        pollers on their own threads for the whole session — Talk every 10
        seconds, the tasks file every 30 — so one of them can create a task in
        the window between "the task table reads quiescent" and "the next
        test's script is installed", and that task then consumes turn 0. The
        symptom is an assertion about work done on behalf of a different task,
        or an exhausted-script error frame on a run that scripted plenty, and
        both read as subsystem faults.

        Re-checking quiescence after the swap catches the row that appeared;
        this catches the request that arrives *during* it, which the re-check
        structurally cannot see. Turning it into a loud refusal is the point:
        a stolen turn is silent, and a task that failed saying "the harness was
        swapping scripts" names itself.

        `refused` is a counter rather than a flag so a caller can compare
        across the block and tell one refusal from three.
        """
        with self._lock:
            self._barred = True
        try:
            yield
        finally:
            with self._lock:
                self._barred = False

    def marked_unmatched(self) -> list[dict]:
        """The `unmatched` entries a scenario is responsible for.

        Those whose request carried an `[e2e:...]` marker. A request with none
        is a daemon poller's own task, which a routed script answers with
        `UNSCRIPTED_TURN` on purpose and which no scenario asked for.
        """
        with self._lock:
            return [dict(entry) for entry in self.unmatched if entry["markers"]]

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

    def messages_by_role(self, role: str) -> list[str]:
        """Every message of one role, across every request, in request order.

        `transcript()` flattens the roles away, and some claims are *about* the
        role a string arrived on — a system prompt that reached the model as a
        user message is a different thing from one that did not, and the two
        are indistinguishable in the flattened view.

        Takes the lock and snapshots, for the reason `transcript()` does: the
        daemon's own pollers keep creating tasks after a scenario's
        `wait_for_task` returns, so handler threads may still be appending.
        Never raises on a malformed body, for the reason `tool_results` does.
        """
        with self._lock:
            bodies = list(self.requests)
        out: list[str] = []
        for body in bodies:
            messages = body.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if message.get("role") == role:
                    out.append(str(message.get("content")))
        return out

    def tool_results(self) -> list[str]:
        """What each Bash call printed, one entry per call, in call order.

        `transcript()` is the whole conversation and is dominated by the system
        prompt — 60KB of it per request, repeated once per turn — so a scenario
        that dumps it on failure buries the twenty lines that say what went
        wrong. This is just the tool-role messages, which is where a command's
        stdout, its stderr and the Bash tool's `[exit code: N]` suffix all land.

        **Keyed on `tool_call_id`, not on the content**, and that is the whole
        difference between "one entry per call" and "one entry per distinct
        output". Every request carries the whole history, so some deduplication
        is needed or the first turn's output appears once per later turn — but
        deduplicating by content silently merges two calls that printed the same
        bytes, and `bash.py` returns the literal `(no output)` for every command
        that printed nothing, which is not an exotic case. The caller labels
        these by index, so a merge there does not lose a block, it *renumbers*
        every block after it — a diagnostic that misattributes which command
        produced a line, in the one function that exists to attribute it.

        `_message_to_wire` sets `tool_call_id` on every tool-role message it
        emits (`src/istota/llm/openai_compat.py`), so the key is always present
        on this path; content stands in only if some other producer omits it.

        Never raises on a malformed body. `do_POST` guards the body itself but
        not the shape of `messages`, and the callers here are assertion helpers
        and a failure-report renderer — an `AttributeError` from either reads as
        a harness crash mid-scenario rather than as the thing that went wrong.
        """
        return list(self.tool_results_by_id().values())

    def tool_results_by_id(self) -> dict[str, str]:
        """`tool_results`, keyed by the call id, in either wire format.

        OpenAI carries a result as a `tool`-role message with `tool_call_id`;
        Anthropic as a `tool_result` block inside a `user` message, keyed by
        `tool_use_id`, with `is_error` beside it. An Anthropic error result is
        returned with `ERROR_PREFIX` in front, since the flag is the one place
        that wire format says a tool refused and a caller asserting on a
        refusal needs it. Never raises on a malformed body.
        """
        with self._lock:
            bodies = list(self.requests)
        results: dict[str, str] = {}
        for body in bodies:
            messages = body.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if message.get("role") == "tool":
                    content = str(message.get("content"))
                    key = str(message.get("tool_call_id") or content)
                    results.setdefault(key, content)
                    continue
                blocks = message.get("content")
                if message.get("role") != "user" or not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    content = _block_text(block.get("content"))
                    if block.get("is_error"):
                        content = ERROR_PREFIX + content
                    key = str(block.get("tool_use_id") or content)
                    results.setdefault(key, content)
        # Insertion-ordered since 3.7, so this is first-seen order, which is the
        # order the calls were made.
        return results


#: What `tool_results_by_id` puts in front of an Anthropic `is_error` result.
ERROR_PREFIX = "[tool error] "


def _block_text(content: object) -> str:
    """A `tool_result` block's content as text: a string, or text blocks."""
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def first_user_text(body: dict) -> str:
    """The request's first user message, as text, in either wire format.

    OpenAI carries the system prompt as a `system`-role message ahead of it and
    Anthropic as a top-level field, so the first `user`-role message is the
    same thing in both: the user half of the task prompt (`prompts.md`), which
    is where a scenario's marker travels. Never raises on a malformed body.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            return _block_text(message.get("content"))
    return ""


def assistant_turns(body: dict) -> int:
    """How many `assistant` messages the request already carries.

    A task's position in its own conversation, read off the request alone: a
    tool round trip adds one, so a task's second request takes its route's
    second turn, and a retried attempt, which starts from a fresh message list,
    takes the first again.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    return sum(
        1 for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )


def choose_route(routes: list[dict], text: str) -> dict | None:
    """The route whose `when` occurs furthest right in `text`, or None.

    Furthest right because the user half puts conversation history before the
    request: on a thread's second mail both markers are present and the current
    one is the later. A tie (one `when` ending where another does) goes to the
    longer `when`, the more specific match.
    """
    best: dict | None = None
    best_key: tuple[int, int] | None = None
    for route in routes:
        when = str(route.get("when") or "")
        if not when:
            continue
        start = text.rfind(when)
        if start < 0:
            continue
        key = (start + len(when), len(when))
        if best_key is None or key > best_key:
            best, best_key = route, key
    return best


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


def _is_title_request(body: dict) -> bool:
    """Whether this is the `claude` CLI's session-title call.

    The CLI sends it beside the first real turn, concurrently, as a
    structured-output request whose schema has one required `title`. Served
    from the script it would take a turn at a race-dependent position, so it
    gets a fixed answer and no turn. Matched on the schema rather than on the
    prompt text, which is the CLI's to change.
    """
    config = body.get("output_config")
    if not isinstance(config, dict):
        return False
    fmt = config.get("format")
    if not isinstance(fmt, dict):
        return False
    schema = fmt.get("schema")
    return isinstance(schema, dict) and schema.get("required") == ["title"]


def _event(name: str, payload: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


def _anthropic_turn_frames(turn: dict, model: str) -> list[bytes]:
    """One scripted turn as Anthropic Messages streaming events.

    The same turn shape `_turn_frames` takes, so one script serves either
    wire format. This half exists for the `claude` CLI, which is the only
    client that speaks it here; it is driven for real by
    `tests/smoke/test_code_review_in_stack.py`'s claude class.
    """
    frames = [_event("message_start", {
        "type": "message_start",
        "message": {
            "id": "msg_scripted", "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    })]
    index = 0
    text = turn.get("text", "")
    if text:
        frames.append(_event("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "text", "text": ""},
        }))
        for piece in _chunks(text, TEXT_CHUNK):
            frames.append(_event("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": piece},
            }))
        frames.append(_event("content_block_stop", {
            "type": "content_block_stop", "index": index,
        }))
        index += 1
    for call in turn.get("tool_calls") or []:
        frames.append(_event("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {
                "type": "tool_use", "id": call["id"], "name": call["name"],
                "input": {},
            },
        }))
        arguments = call.get("arguments", {})
        encoded = arguments if isinstance(arguments, str) else json.dumps(arguments)
        for piece in _chunks(encoded, ARGS_CHUNK):
            frames.append(_event("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": piece},
            }))
        frames.append(_event("content_block_stop", {
            "type": "content_block_stop", "index": index,
        }))
        index += 1
    stop_reason = "tool_use" if turn.get("tool_calls") else "end_turn"
    frames.append(_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": 1},
    }))
    frames.append(_event("message_stop", {"type": "message_stop"}))
    return frames


#: The status an exhausted Anthropic script is answered with. Not an in-stream
#: `error` event, which the `claude` CLI reports as "an empty or malformed
#: response" and so loses the message (measured on 2.1.197); a 400 is quoted
#: and not retried.
ANTHROPIC_EXHAUSTED_STATUS = 400


def _anthropic_exhausted_body(served: int, scripted: int) -> bytes:
    """`_exhausted_frame`'s message, as an Anthropic JSON error body."""
    return json.dumps({
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": (
                f"scripted endpoint exhausted: request {served + 1} arrived "
                f"but only {scripted} turn(s) were scripted"
            ),
        },
    }).encode()


def _record_unmatched(
    endpoint: ScriptedEndpoint, text: str, *, reason: str, when: str | None,
) -> None:
    endpoint.unmatched.append({
        "reason": reason,
        "when": when,
        "markers": MARKER_RE.findall(text),
        "excerpt": text[:UNMATCHED_EXCERPT],
    })


def _pick_turn(
    endpoint: ScriptedEndpoint, body: dict,
) -> tuple[dict | None, int, int]:
    """`(turn, index, count)`: the turn to serve, or None for the exhausted
    frame, plus the index and count that frame names.

    Called under the endpoint's lock, after `served` is counted. With no route
    in the script every request takes the positional branch, so
    `positional_served` advances exactly as `served` used to and the frames are
    the ones this endpoint has always sent.
    """
    routes = [item for item in endpoint.turns if "when" in item]
    positional = [item for item in endpoint.turns if "when" not in item]
    if not routes:
        index = endpoint.positional_served
        endpoint.positional_served += 1
        turn = positional[index] if index < len(positional) else None
        return turn, index, len(positional)

    text = first_user_text(body)
    route = choose_route(routes, text)
    if route is not None:
        route_turns = list(route.get("turns") or [])
        index = assistant_turns(body)
        if index < len(route_turns):
            return route_turns[index], index, len(route_turns)
        _record_unmatched(endpoint, text, reason="exhausted", when=route["when"])
        return None, index, len(route_turns)

    index = endpoint.positional_served
    if index < len(positional):
        endpoint.positional_served += 1
        return positional[index], index, len(positional)
    _record_unmatched(endpoint, text, reason="no_route", when=None)
    return UNSCRIPTED_TURN, index, len(positional)


def serve_script(
    turns: list[dict],
    *,
    port: int = 0,
    host: str = LOOPBACK,
    credential: str | None = None,
) -> ScriptedEndpoint:
    """Start an endpoint replaying `turns`, one per request, in order.

    A turn is ``{"text": str}`` or ``{"tool_calls": [{"id", "name",
    "arguments"}]}``, optionally with ``finish_reason`` and ``usage``.

    An item with a ``when`` key is a **route** instead:
    ``{"when": "<marker>", "turns": [turn, ...]}``. A request whose first user
    message contains the marker is answered from that route, at the index the
    assistant messages it already carries give (`choose_route`,
    `assistant_turns`), so the answer depends on the request alone and a poller
    task cannot take a scenario's turn. Items without ``when`` keep their
    positional meaning, and a script with no route behaves as it always has.
    With routes present, a request matching none takes the next positional
    item, or `UNSCRIPTED_TURN` when none is left; that and a route run past its
    end are recorded in `unmatched`. Port 0
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

        def do_HEAD(self) -> None:
            """The `claude` CLI's connectivity check, answered without a turn.

            It sends `HEAD /` before its first `/v1/messages` POST; counting
            that as a request would shift every scripted turn by one.
            """
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path.endswith("/chat/completions"):
                anthropic = False
            elif path.endswith("/v1/messages"):
                anthropic = True
            else:
                self.send_error(
                    404, "only /chat/completions and /v1/messages are scripted"
                )
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

            if anthropic and _is_title_request(body):
                with endpoint._lock:
                    endpoint.titles_served += 1
                payload = b"".join(_anthropic_turn_frames(
                    {"text": json.dumps({"title": "Scripted session"})},
                    body.get("model", ""),
                ))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            with endpoint._lock:
                if endpoint._barred:
                    endpoint.refused += 1
                    barred = True
                else:
                    barred = False
                    endpoint.served += 1
                    endpoint.requests.append(body)
                    turn, index, scripted_count = _pick_turn(endpoint, body)

            if barred:
                # Not recorded in `requests`: nothing was served, and a body in
                # the transcript that never got a turn would make `served` and
                # `len(requests)` disagree for every later reader.
                self.send_error(
                    BARRIER_STATUS,
                    "the harness was swapping scripts",
                    "A turn was requested while `Stack.reset` held the barrier "
                    "between quiescing and rescripting. The task that made this "
                    "request was created by one of the daemon's own pollers "
                    "after the task table read quiescent; refusing it is what "
                    "stops it consuming turn 0 of the next scenario.",
                )
                return

            model = body.get("model", "")
            if anthropic and turn is None:
                body_bytes = _anthropic_exhausted_body(index, scripted_count)
                self.send_response(ANTHROPIC_EXHAUSTED_STATUS)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
            if anthropic:
                frames = _anthropic_turn_frames(turn, model)
            elif turn is not None:
                frames = _turn_frames(turn, model)
            else:
                frames = [_exhausted_frame(index, scripted_count)]

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
