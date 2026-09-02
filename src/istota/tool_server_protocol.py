"""Wire protocol between the daemon and one native-brain tool server.

Pure data + serialization. No sockets, no asyncio, no subprocesses — the same
split ``devbox_exec_protocol`` already uses, and for the same reason: the wire
format is then testable with no I/O at all, and the one module both ends agree
through has nothing in it that can fail for an environmental reason.

Framing
-------

A 4-byte big-endian length, then that many bytes of UTF-8 JSON holding one
object. Nothing else is on the wire — no line orientation, no multiplexed
streams — because every message here is a small JSON object and the one large
payload (an image ``Read``) is already base64 inside one.

``MAX_FRAME_BYTES`` is enforced on **both** ends, encode and decode. A cap only
on the reader lets a writer build a frame it can never deliver and then block;
a cap only on the writer lets a garbled or hostile 4-byte length ask the reader
for a 4 GiB buffer. Over-cap, truncated-at-EOF, non-UTF-8, non-JSON, not an
object, or an unknown ``type`` are all ``ProtocolError`` — the single exception
this module raises, and only out of its decoders and ``encode``.

The messages
------------

===========  =========  ==================================================
type         direction  carries
===========  =========  ==================================================
``hello``    d -> s     the ToolEnv the server is to build: cwd, the
                        subprocess env, the three root lists, the deferred
                        dir, the timeouts and the caps. Once, first.
``call``     d -> s     ``{id, tool, args}`` — one tool invocation
``abort``    d -> s     ``{id}`` — the loop's abort event, for that call
``shutdown`` d -> s     ``{}`` — stop accepting work and exit 0
``ready``    s -> d     the ToolEnv is built and the tools are bound
``update``   s -> d     ``{id, text}`` — one ``on_update`` chunk
``result``   s -> d     ``{id, content, is_error, terminate}``
``fatal``    s -> d     ``{message}``, sent before the socket closes
===========  =========  ==================================================

``ToolResult.details`` does not cross. ``agent/loop.py`` is its only reader and
only carries it through the after-hook override, so a proxy tool returns
``details=None`` and there is nothing to serialize.

Content blocks travel as plain dicts (``{"type": "text", "text": …}`` /
``{"type": "image", "media_type": …, "data": …, "display_name": …}``) rather
than through a dataclass serializer, because that is already what they are:
tool arguments are a ``dict`` off the model's JSON, and tool content is text or
base64. Mapping those dicts to ``llm.types`` is
``session/tools/remote.py``'s job on one side and ``tool_server.py``'s on the
other; keeping it out of here is what lets this module stay stdlib-only.
"""

from __future__ import annotations

import json
import struct
from typing import Any

# ---- Version ---------------------------------------------------------------

# Both ends ship from the same tree — the server is `python -m
# istota.tool_server` out of the same checkout the daemon is running — so a
# mismatch means something is badly wrong rather than a rolling upgrade. It is
# still checked, because "badly wrong" is exactly when a clear error is worth
# most, and because the sandbox binds a *venv* and a *source tree* whose
# contents are decided by the deployment.
PROTOCOL_VERSION = 1

SUPPORTED_PROTOCOLS: frozenset[int] = frozenset({1})

# ---- Message types ---------------------------------------------------------

MSG_HELLO = "hello"
MSG_CALL = "call"
MSG_ABORT = "abort"
MSG_SHUTDOWN = "shutdown"

MSG_READY = "ready"
MSG_UPDATE = "update"
MSG_RESULT = "result"
MSG_FATAL = "fatal"

DAEMON_MESSAGES: frozenset[str] = frozenset({
    MSG_HELLO,
    MSG_CALL,
    MSG_ABORT,
    MSG_SHUTDOWN,
})

SERVER_MESSAGES: frozenset[str] = frozenset({
    MSG_READY,
    MSG_UPDATE,
    MSG_RESULT,
    MSG_FATAL,
})

ALL_MESSAGES: frozenset[str] = DAEMON_MESSAGES | SERVER_MESSAGES

# ---- Content block types ---------------------------------------------------

CONTENT_TEXT = "text"
CONTENT_IMAGE = "image"

# ---- Caps and timings ------------------------------------------------------

# One frame's payload. Well above anything a tool produces — `ToolEnv`'s own
# `max_output_bytes` is 30 KB and `max_read_bytes` 25 MB, and a base64 image
# `Read` is capped by the latter — so the cap is a bound on damage rather than
# a limit anything legitimate meets.
MAX_FRAME_BYTES: int = 32 * 1024 * 1024

_LENGTH = struct.Struct(">I")
FRAME_HEADER_BYTES: int = _LENGTH.size  # 4

# How long the daemon waits for `ready` after the spawn. Generous, because the
# server has to start a Python interpreter inside a fresh bwrap namespace on a
# possibly-loaded host; short enough that a server which will never answer
# fails the attempt rather than holding a worker for the task's whole budget.
STARTUP_TIMEOUT_SECONDS: float = 30.0

# After `shutdown`, how long the daemon waits for the process to exit before
# killing its group. `--die-with-parent` is the backstop under that.
SHUTDOWN_GRACE_SECONDS: float = 5.0

# Nothing above is deployment-shaped, so none of it is a config key.


class ProtocolError(Exception):
    """A frame or message this side refuses to go on from.

    Raised only from ``encode`` and from the decoders. Both ends treat it as
    fatal for the connection: a codec disagreement is not something either side
    can resynchronise from, since the stream is length-prefixed and a bad
    length means the next boundary is unknown.
    """


def encode(message: dict[str, Any]) -> bytes:
    """One message → one length-prefixed frame.

    Raises ``ProtocolError`` for a non-object, an unknown or missing ``type``,
    something JSON cannot represent, or a frame over the cap.
    """
    if not isinstance(message, dict):
        raise ProtocolError(f"a message must be an object, got {type(message).__name__}")
    kind = message.get("type")
    if kind not in ALL_MESSAGES:
        raise ProtocolError(f"unknown message type {kind!r}")
    try:
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"message is not JSON-serializable: {exc}") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame of {len(payload)} bytes exceeds the {MAX_FRAME_BYTES}-byte cap"
        )
    return _LENGTH.pack(len(payload)) + payload


def decode_payload(payload: bytes) -> dict[str, Any]:
    """One frame body → one message. Raises ``ProtocolError`` on anything else."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"frame is not valid UTF-8: {exc}") from exc
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"a message must be an object, got {type(obj).__name__}")
    kind = obj.get("type")
    if kind not in ALL_MESSAGES:
        raise ProtocolError(f"unknown message type {kind!r}")
    return obj


class FrameDecoder:
    """Incremental reader: feed it bytes, take whole messages out.

    A class rather than a read-one-frame helper because neither end reads from
    a socket in frame-sized pieces, and because it is what makes the truncation
    rule expressible: bytes left over at EOF are a *fault*, not a quiet end,
    and only something holding the buffer can say so. ``close()`` is where that
    is raised.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending_bytes(self) -> int:
        """Bytes held that are not yet a whole frame. Zero at a clean boundary."""
        return len(self._buf)

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Add bytes, return every whole message they completed, in order."""
        self._buf.extend(data)
        out: list[dict[str, Any]] = []
        while True:
            if len(self._buf) < FRAME_HEADER_BYTES:
                return out
            (length,) = _LENGTH.unpack_from(self._buf, 0)
            if length > MAX_FRAME_BYTES:
                # Checked before waiting for the body: a hostile or garbled
                # length must never be a 4 GiB allocation, and there is nothing
                # to resynchronise to afterwards.
                raise ProtocolError(
                    f"frame of {length} bytes exceeds the {MAX_FRAME_BYTES}-byte cap"
                )
            end = FRAME_HEADER_BYTES + length
            if len(self._buf) < end:
                return out
            payload = bytes(self._buf[FRAME_HEADER_BYTES:end])
            del self._buf[:end]
            out.append(decode_payload(payload))

    def close(self) -> None:
        """Assert the stream ended on a frame boundary.

        A partial frame at EOF means the peer died mid-write. Silence there
        would present a truncated conversation as a complete one, which is the
        failure this whole seam must not have: a tool call whose result never
        arrived has to read as a broken server, not as a call nobody made.
        """
        if self._buf:
            raise ProtocolError(
                f"stream ended mid-frame with {len(self._buf)} bytes buffered"
            )
