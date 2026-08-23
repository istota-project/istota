"""Wire protocol for the devbox exec transport.

Pure data + serialization. No sockets, no asyncio, no subprocesses — so the
wire format is testable with no I/O, the same split ``devbox_proxy_protocol``
already uses for the credential proxy.

**This file is vendored.** ``docker/devbox/lib/istota_devbox_exec_protocol.py``
is a byte-identical copy kept in step by ``scripts/sync-devbox-lib.sh``, because
the in-container server imports it and Docker cannot COPY from outside its build
context. So: stdlib only, no imports from ``istota``, and nothing here may
assume the package is installed.

Framing
-------

One JSON request line terminated by ``\\n``, one JSON acknowledgement line, then
binary frames in both directions until close. The frame header is 8 bytes,
``>BxxxI`` — the same shape as Docker's stdcopy header.

===  =================  ===============================================
 id  direction          carries
===  =================  ===============================================
  0  client -> server   stdin bytes; the ``write_file`` body
  1  server -> client   stdout bytes; the ``read_file`` body
  2  server -> client   stderr bytes
  3  both               a JSON control object; the ``stat``/``ping`` replies
===  =================  ===============================================

Every action's payload has a stream. Leaving one unassigned is exactly the
ambiguity a vendored copy and a host copy drift apart on.

The acknowledgement
-------------------

``{"status":"ok","protocol":1}`` or ``{"status":"error","code":…,"message":…}``.
An error acknowledgement closes the connection and nothing is streamed, which
means the server sends the ack **after** its path checks and after a successful
spawn. That ordering is what the guarantee rests on: an ``ok`` means the command
is running.

``protocol`` is checked by the client, which exits 121 on a value it does not
know. The server ships in a separately built image and the client is copied out
of the daemon's tree at task setup, so the two are independently upgradable by
construction; bump ``PROTOCOL_VERSION`` whenever a frame or an action changes.

Termination
-----------

The last frame is always a control frame carrying ``exit_code``. It reports what
``waitpid`` said and never an inference: ``signal`` is set only for a child the
kernel actually signalled (``WIFSIGNALED``), never derived from ``128+N``. A
``bash -o pipefail -c 'yes | head -1'`` *exits* 141, so reporting
``signal: "SIGPIPE"`` there would fabricate a fact — and a program may
legitimately exit 141. The hint travels as ``note`` instead, which is how
``shell_exec.py`` already handles the same status. This subsystem has produced
three wrong-exit-status bugs; the fix for that is fewer inferences.

There is no ``env`` field, deliberately, and ``tests/test_devbox_exec_protocol``
pins that deletion. The child's environment is the container's own: forwarding a
filtered copy of the model's environment is hygiene rather than a boundary (a
hand-written client sends whatever it likes), and it would point the caches at
sandbox paths the container does not have.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

# ---- Version ---------------------------------------------------------------

PROTOCOL_VERSION = 1

# Every version this build can speak. A client compares the server's number
# against this set; anything outside it is a hard stop rather than a guess.
SUPPORTED_PROTOCOLS: frozenset[int] = frozenset({1})

# ---- Streams ---------------------------------------------------------------

STREAM_STDIN = 0
STREAM_STDOUT = 1
STREAM_STDERR = 2
STREAM_CONTROL = 3

ALL_STREAMS: frozenset[int] = frozenset({
    STREAM_STDIN,
    STREAM_STDOUT,
    STREAM_STDERR,
    STREAM_CONTROL,
})

# ---- Actions ---------------------------------------------------------------

ACTION_EXEC = "exec"
ACTION_WRITE_FILE = "write_file"
ACTION_READ_FILE = "read_file"
ACTION_STAT = "stat"
ACTION_PING = "ping"

ALL_ACTIONS: frozenset[str] = frozenset({
    ACTION_EXEC,
    ACTION_WRITE_FILE,
    ACTION_READ_FILE,
    ACTION_STAT,
    ACTION_PING,
})

# ---- Error codes -----------------------------------------------------------

ERR_BAD_REQUEST = "bad_request"
ERR_UNKNOWN_ACTION = "unknown_action"
ERR_PATH_REFUSED = "path_refused"
ERR_NO_SUCH_CWD = "no_such_cwd"
ERR_SPAWN_FAILED = "spawn_failed"
ERR_TOO_LARGE = "too_large"
ERR_INTERNAL = "internal"

ALL_ERROR_CODES: frozenset[str] = frozenset({
    ERR_BAD_REQUEST,
    ERR_UNKNOWN_ACTION,
    ERR_PATH_REFUSED,
    ERR_NO_SUCH_CWD,
    ERR_SPAWN_FAILED,
    ERR_TOO_LARGE,
    ERR_INTERNAL,
})

# ---- Caps ------------------------------------------------------------------

# The request line. Everything past this is refused before it is parsed.
MAX_REQUEST_BYTES: int = 1024 * 1024

# One frame's payload. There is no cap on an exec's total output — the whole
# point of leaving `docker exec` behind — but a single frame is bounded so a
# garbled or hostile 4-byte length cannot ask either side for a 4 GiB buffer.
MAX_FRAME_BYTES: int = 1024 * 1024

# A `write_file` body, streamed to disk rather than buffered, and a `read_file`
# reply, which is refused past the cap rather than truncated.
MAX_WRITE_FILE_BYTES: int = 64 * 1024 * 1024
MAX_READ_FILE_BYTES: int = 64 * 1024 * 1024

# What both sides read and write in one go. Below MAX_FRAME_BYTES on purpose.
CHUNK_BYTES: int = 64 * 1024

# ---- The one client-to-server control object -------------------------------

STDIN_EOF_KEY = "stdin_eof"

# ---- Exit-status hints -----------------------------------------------------

# 141 is the one status with a fixed code, so it is the one that can be
# annotated. Same rule as `shell_exec.SIGPIPE_EXIT` / `SIGPIPE_NOTE`, restated
# rather than imported because this file also runs in a container that has no
# istota package.
SIGPIPE_EXIT = 141

SIGPIPE_NOTE = (
    "exit 141 usually means SIGPIPE: a command in the pipeline was killed "
    "because the next one closed the pipe (`| head`, `| grep -q`), and with "
    "pipefail on that becomes the pipeline's status. The server reports what "
    "waitpid said and never infers a signal from 128+N, so `signal` is null "
    "here even though a signal was involved somewhere in the pipeline."
)

_HEADER = struct.Struct(">BxxxI")
FRAME_HEADER_BYTES: int = _HEADER.size  # 8


# ---- Exceptions ------------------------------------------------------------


@dataclass
class ProtocolError(Exception):
    """A request, frame or reply this side refuses to go on from.

    ``code`` is one of the stable ``ERR_*`` constants, so a server can turn any
    of these straight into an error acknowledgement without a second mapping.
    """

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


# ---- Frames ----------------------------------------------------------------


def pack_header(stream: int, length: int) -> bytes:
    """Serialize an 8-byte frame header."""
    if stream not in ALL_STREAMS:
        raise ProtocolError(ERR_BAD_REQUEST, f"unknown stream id {stream}")
    if length < 0 or length > MAX_FRAME_BYTES:
        raise ProtocolError(ERR_TOO_LARGE, f"frame length {length} out of range")
    return _HEADER.pack(stream, length)


def pack_frame(stream: int, payload: bytes) -> bytes:
    """Serialize one frame. A zero-length payload is a legal frame."""
    return pack_header(stream, len(payload)) + payload


def unpack_header(header: bytes) -> tuple[int, int]:
    """Parse an 8-byte frame header into ``(stream, length)``."""
    if len(header) != FRAME_HEADER_BYTES:
        raise ProtocolError(
            ERR_BAD_REQUEST,
            f"frame header must be {FRAME_HEADER_BYTES} bytes, got {len(header)}",
        )
    stream, length = _HEADER.unpack(header)
    if stream not in ALL_STREAMS:
        raise ProtocolError(ERR_BAD_REQUEST, f"unknown stream id {stream}")
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(ERR_TOO_LARGE, f"frame length {length} over cap")
    return stream, length


class FrameDecoder:
    """Incremental frame parser: feed it whatever a read returned.

    A socket read hands back an arbitrary slice — half a header, three frames
    and a fragment, a single byte. Both sides need the same reassembly, and a
    pure buffer is the only version of it that can be tested without a socket.

    ``max_payload`` can only lower the module's own cap, never raise it:
    ``unpack_header`` refuses anything over ``MAX_FRAME_BYTES`` before this
    class gets a look at it.
    """

    def __init__(self, max_payload: int = MAX_FRAME_BYTES) -> None:
        self._buf = bytearray()
        self._max_payload = max_payload

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Buffer ``data`` and return every complete frame it finished."""
        self._buf.extend(data)
        frames: list[tuple[int, bytes]] = []
        while True:
            if len(self._buf) < FRAME_HEADER_BYTES:
                return frames
            stream, length = unpack_header(bytes(self._buf[:FRAME_HEADER_BYTES]))
            if length > self._max_payload:
                raise ProtocolError(ERR_TOO_LARGE, f"frame length {length} over cap")
            end = FRAME_HEADER_BYTES + length
            if len(self._buf) < end:
                return frames
            frames.append((stream, bytes(self._buf[FRAME_HEADER_BYTES:end])))
            del self._buf[:end]

    @property
    def pending(self) -> int:
        """Bytes buffered but not yet part of a complete frame."""
        return len(self._buf)


# ---- Lines -----------------------------------------------------------------


def encode_line(payload: dict[str, Any]) -> bytes:
    """Serialize one newline-terminated JSON line."""
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def decode_line(line: bytes | str) -> dict[str, Any]:
    """Parse one JSON line into a dict, with the cap applied first."""
    raw = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError(
            ERR_TOO_LARGE,
            f"line exceeds {MAX_REQUEST_BYTES // (1024 * 1024)} MiB",
        )
    stripped = raw.strip()
    if not stripped:
        raise ProtocolError(ERR_BAD_REQUEST, "empty line")
    try:
        parsed = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(ERR_BAD_REQUEST, f"invalid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ProtocolError(ERR_BAD_REQUEST, "must be a JSON object")
    return parsed


# ---- Requests --------------------------------------------------------------


def encode_exec_request(
    *,
    argv: list[str] | None = None,
    shell: str | None = None,
    cwd: str,
    stdin: bool = False,
    timeout: float = 0,
) -> bytes:
    """Serialize an ``exec`` request.

    ``argv`` and ``shell`` are mutually exclusive. The shims always send
    ``argv`` — no shell, so no quoting bug can be introduced between the model's
    shell and the container; ``shell`` exists for the devbox skill's own ``exec``
    verb, which the server runs under ``bash -o pipefail -c``.

    There is deliberately **no** ``env`` parameter. See the module docstring.
    """
    payload: dict[str, Any] = {"action": ACTION_EXEC}
    if argv is not None:
        payload["argv"] = list(argv)
    if shell is not None:
        payload["shell"] = shell
    payload["cwd"] = cwd
    payload["stdin"] = bool(stdin)
    payload["timeout"] = timeout
    validate_request(payload)
    return encode_line(payload)


def encode_write_file_request(*, path: str, size: int, mode: int = 0o644) -> bytes:
    """Serialize a ``write_file`` request; the body follows on stream 0."""
    payload = {
        "action": ACTION_WRITE_FILE,
        "path": path,
        "mode": mode,
        "size": size,
    }
    validate_request(payload)
    return encode_line(payload)


def encode_read_file_request(*, path: str) -> bytes:
    """Serialize a ``read_file`` request; the body comes back on stream 1."""
    payload = {"action": ACTION_READ_FILE, "path": path}
    validate_request(payload)
    return encode_line(payload)


def encode_stat_request() -> bytes:
    """Serialize a ``stat`` request; the reply is one control frame."""
    return encode_line({"action": ACTION_STAT})


def encode_ping_request() -> bytes:
    """Serialize a ``ping`` request; the reply is one control frame."""
    return encode_line({"action": ACTION_PING})


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Check a decoded request and return it with defaults filled in.

    Raises ``ProtocolError`` with the code the server should acknowledge. The
    server calls this rather than hand-checking fields, so the encoder and the
    decoder cannot disagree about what a legal request is.
    """
    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise ProtocolError(ERR_BAD_REQUEST, "missing 'action' field")
    if action not in ALL_ACTIONS:
        raise ProtocolError(ERR_UNKNOWN_ACTION, f"unknown action '{action}'")

    if action == ACTION_EXEC:
        argv = payload.get("argv")
        shell = payload.get("shell")
        if (argv is None) == (shell is None):
            raise ProtocolError(
                ERR_BAD_REQUEST, "exec takes exactly one of 'argv' or 'shell'"
            )
        if argv is not None:
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(a, str) for a in argv)
            ):
                raise ProtocolError(
                    ERR_BAD_REQUEST, "'argv' must be a non-empty list of strings"
                )
        if shell is not None and (not isinstance(shell, str) or not shell.strip()):
            raise ProtocolError(ERR_BAD_REQUEST, "'shell' must be a non-empty string")
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise ProtocolError(ERR_BAD_REQUEST, "exec requires a 'cwd'")
        stdin = payload.get("stdin", False)
        if not isinstance(stdin, bool):
            raise ProtocolError(ERR_BAD_REQUEST, "'stdin' must be a boolean")
        timeout = payload.get("timeout", 0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ProtocolError(ERR_BAD_REQUEST, "'timeout' must be a number")
        if timeout < 0:
            raise ProtocolError(ERR_BAD_REQUEST, "'timeout' must not be negative")
        payload["stdin"] = stdin
        payload["timeout"] = timeout

    elif action == ACTION_WRITE_FILE:
        _require_path(payload)
        size = payload.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ProtocolError(
                ERR_BAD_REQUEST, "'size' must be a non-negative integer"
            )
        if size > MAX_WRITE_FILE_BYTES:
            raise ProtocolError(
                ERR_TOO_LARGE,
                f"body of {size} bytes exceeds the "
                f"{MAX_WRITE_FILE_BYTES // (1024 * 1024)} MiB cap",
            )
        mode = payload.get("mode", 0o644)
        # Permission bits only. The setuid, setgid and sticky bits are outside
        # what a file-transfer verb needs, and the server applies this mode with
        # an explicit chmod that defeats the umask, so `04755` would otherwise
        # arrive under the repos root exactly as asked for.
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise ProtocolError(
                ERR_BAD_REQUEST, "'mode' must be permission bits in the range 0-0o777"
            )
        payload["mode"] = mode

    elif action == ACTION_READ_FILE:
        _require_path(payload)

    return payload


def _require_path(payload: dict[str, Any]) -> None:
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        raise ProtocolError(ERR_BAD_REQUEST, "missing 'path' field")


def decode_request(line: bytes | str) -> dict[str, Any]:
    """Parse and validate one request line."""
    return validate_request(decode_line(line))


# ---- Acknowledgements ------------------------------------------------------


def encode_ack_ok(protocol: int = PROTOCOL_VERSION) -> bytes:
    """Serialize the acknowledgement that means the command is running."""
    return encode_line({"status": "ok", "protocol": protocol})


def encode_ack_error(code: str, message: str, **extra: Any) -> bytes:
    """Serialize a refusal. Nothing is streamed after one of these."""
    payload: dict[str, Any] = {"status": "error", "code": code, "message": message}
    payload.update(extra)
    return encode_line(payload)


def decode_ack(line: bytes | str) -> dict[str, Any]:
    """Parse an acknowledgement line."""
    ack = decode_line(line)
    status = ack.get("status")
    if status not in ("ok", "error"):
        raise ProtocolError(ERR_BAD_REQUEST, "acknowledgement has no 'status'")
    return ack


def supported_protocol(value: Any) -> bool:
    """True when this build speaks the protocol version ``value`` names.

    A bool is not a version: ``True == 1`` in Python, and an acknowledgement
    carrying ``"protocol": true`` is a malformed one rather than protocol 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value in SUPPORTED_PROTOCOLS


# ---- Control frames --------------------------------------------------------


def encode_control(obj: dict[str, Any]) -> bytes:
    """Serialize a control object as a stream-3 frame."""
    return pack_frame(STREAM_CONTROL, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def decode_control(payload: bytes) -> dict[str, Any]:
    """Parse a stream-3 frame's payload."""
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(ERR_BAD_REQUEST, f"invalid control frame: {e}") from e
    if not isinstance(parsed, dict):
        raise ProtocolError(ERR_BAD_REQUEST, "control frame must be a JSON object")
    return parsed


def encode_stdin_eof() -> bytes:
    """The one control object that travels client to server."""
    return encode_control({STDIN_EOF_KEY: True})


def is_stdin_eof(obj: dict[str, Any]) -> bool:
    """True for the client's ``{"stdin_eof": true}``."""
    return obj.get(STDIN_EOF_KEY) is True


def is_terminal(obj: dict[str, Any]) -> bool:
    """True for the last frame of a connection.

    Every action ends with a control frame carrying ``exit_code``, including
    ``ping`` and ``stat``, so a client has one rule rather than one per action.
    """
    return "exit_code" in obj


def sigpipe_note(exit_code: int | None, signal_name: str | None) -> str | None:
    """The note that goes with an unsignalled exit 141, or ``None``.

    Only for a child that was *not* signalled: a child the kernel killed with
    SIGPIPE reports ``signal`` and needs no guess about what 141 meant.
    """
    if signal_name is None and exit_code == SIGPIPE_EXIT:
        return SIGPIPE_NOTE
    return None
