"""Docker-API allowlist proxy daemon.

Per-user asyncio reverse proxy that sits in front of the host Docker
socket and is *safe to bind into the bwrap sandbox unconditionally*. The
raw Docker socket is root-equivalent — anything that can write to it can
launch a privileged, host-mounting container — so the executor never
binds the raw socket into the sandbox again. This proxy is bound at the
conventional in-sandbox path ``/var/run/docker.sock`` instead, and only
forwards a tightly-scoped allowlist of operations against the user's own
``devbox-<user_id>`` container.

The Docker daemon speaks HTTP/1.1 over its unix socket, and HTTP/1.1 is a
keep-alive protocol: one connection carries as many requests as the client
cares to send. So this is a real HTTP/1.1 intermediary, not a gate standing
in front of a tunnel — it reads, classifies and answers *every* request on
a connection, and where it cannot do that it ends the connection instead:

* parses each request's method + path (and, for exec-create, the body),
* decides allow/deny with a pure :func:`classify_request`,
* for the ordinary request/response ops — ping, version, container list,
  inspect, restart, exec inspect, exec create — fully mediates: reads the
  whole request, forwards it, reads the whole response, writes it back,
  then loops and classifies the next head on the same connection,
* for ``archive``, which streams an opaque tar body, relays the bytes and
  then closes the connection: it copies no more from the client than the
  request declared, so nothing can be pipelined behind the tar,
* for ``exec start``, waits to find out. An exec start usually hands the
  connection over to a raw stdio stream, and the proxy does not interpret
  that. But it does not always: moby hijacks only when the start body does
  not say ``Detach: true``, and an error answers with ordinary framed HTTP
  too. So the head and its declared body go up, the *response* head comes
  back, and only a hijack gets the full-duplex pump — otherwise it is a
  normal response, relayed, with the client-to-daemon direction never
  opened. Either way the connection ends there.

The head is forwarded to the daemon verbatim, so the proxy's parse of it and
the daemon's have to agree, or classification decides nothing:
:func:`head_structure_error` refuses anything the two could read
differently.

Every deny is terminal for the same reason: a refused request may have an
unread body still in the stream, and a leftover body would be parsed as the
next request head — request smuggling against ourselves.

Forbidden everything else: container create/run/build/pull, volumes,
networks, swarm, daemon reconfiguration, delete, update. Those → ``403``
with a docker-client-compatible JSON error and an audit line.

Security argument: the devbox container is provisioned (Ansible/compose)
**unprivileged with no host bind mounts**, so ``exec``/``cp`` into it —
even as root-in-container — cannot reach host root. Forbidding container
creation outright is the clean boundary: root-in-an-unprivileged-no-host-
mount container is not host root.

The protocol shape mirrors ``devbox_proxy``'s audit style but the wire is
transparent HTTP, not the line-JSON credential protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("istota.docker_proxy")
audit_logger = logging.getLogger("istota.docker_proxy.audit")
audit_logger.propagate = True
audit_logger.setLevel(logging.INFO)

# Every refusal this proxy writes carries this prefix in its JSON ``message``,
# and the docker CLI reprints it verbatim: ``Error response from daemon:
# istota-docker-proxy: untracked_exec``. Named rather than spelled inline
# because a caller has to be able to tell a refusal apart from an answer, and
# nothing else in the exchange does. A ``docker exec`` refused at the
# exec-inspect step has already run the command and streamed its output; only
# the fetch of the command's own status is denied, so the CLI exits 1 — which
# is exactly what a command that failed looks like. Read as an answer, that
# satisfies any assertion expecting a failure, whatever the container did
# (ISSUE-313). One literal in one place, so a reader's match cannot drift from
# what the writer emits.
PROXY_ERROR_PREFIX = "istota-docker-proxy: "


# ---- Allowlist classification (pure, unit-testable core) -------------------

# Optional Docker API version prefix, e.g. ``/v1.43/containers/...``.
_VERSION_PREFIX_RE = re.compile(r"^/v1\.\d+(/.*)$")

_PING_RE = re.compile(r"^/_ping$")
_VERSION_RE = re.compile(r"^/version$")
_CONTAINERS_LIST_RE = re.compile(r"^/containers/json$")
_CONTAINER_INSPECT_RE = re.compile(r"^/containers/([^/]+)/json$")
_EXEC_CREATE_RE = re.compile(r"^/containers/([^/]+)/exec$")
_EXEC_START_RE = re.compile(r"^/exec/([^/]+)/start$")
_EXEC_INSPECT_RE = re.compile(r"^/exec/([^/]+)/json$")
_ARCHIVE_RE = re.compile(r"^/containers/([^/]+)/archive$")
_RESTART_RE = re.compile(r"^/containers/([^/]+)/restart$")


def _normalize_path(path: str) -> str:
    """Strip an optional ``/v1.NN`` API-version prefix and any query string."""
    m = _VERSION_PREFIX_RE.match(path)
    if m:
        path = m.group(1)
    return path.split("?", 1)[0]


def is_exec_create(method: str, path: str) -> bool:
    """True if this is a ``POST /containers/{name}/exec`` (the one mediated op)."""
    return method.upper() == "POST" and _EXEC_CREATE_RE.match(_normalize_path(path)) is not None


def _exec_create_body_ok(body: bytes | None) -> tuple[bool, str]:
    """Validate an exec-create request body.

    The body is small non-streaming JSON. We require its presence (so a
    Content-Length-less request can't slip the privilege check) and reject
    any privilege-bearing field — the devbox CLI never sets ``Privileged``
    or a ``HostConfig`` on exec-create, so their presence is a hand-crafted
    request from sandboxed Bash.
    """
    if body is None:
        return False, "no_content_length"
    try:
        data = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except (ValueError, UnicodeDecodeError):
        return False, "bad_body"
    if not isinstance(data, dict):
        return False, "bad_body"
    if data.get("Privileged") is True:
        return False, "privileged"
    # exec-create has no HostConfig in its schema; its presence is a probe.
    if "HostConfig" in data:
        return False, "hostconfig"
    return True, "exec_create"


def classify_request(
    method: str,
    path: str,
    body: bytes | None,
    *,
    container_name: str,
    tracked_exec_ids: set[str],
) -> tuple[bool, str]:
    """Pure allow/deny decision for one Docker-API request.

    Returns ``(allowed, reason)``. No I/O. ``container_name`` is the user's
    owned container (``devbox-<user_id>``); every container-scoped op must
    target it exactly or it is ``not_owned``. ``tracked_exec_ids`` is the
    set of exec ids this proxy issued for the owned container — exec
    start/inspect are allowed only for tracked ids.

    For exec-create, ``body`` is the parsed request body (bytes) used for
    the no-``Privileged`` check; for every other op ``body`` is ignored.
    """
    method = method.upper()
    p = _normalize_path(path)

    # Daemon handshake — no ownership scope needed.
    if p == "/_ping" and method in ("GET", "HEAD"):
        return True, "ping"
    if _VERSION_RE.match(p) and method == "GET":
        return True, "version"

    # Container list. Allowed per the allowlist; the response is relayed
    # unfiltered, so it names every container on the host and not just this
    # user's. Container names are not secrets and the dangerous ops
    # (create/run/privileged) are blocked regardless, so this is an
    # accepted, documented limitation.
    if _CONTAINERS_LIST_RE.match(p) and method == "GET":
        return True, "containers_list"

    def _owned(name: str) -> bool:
        return name == container_name

    m = _CONTAINER_INSPECT_RE.match(p)
    if m and method == "GET":
        return (True, "inspect") if _owned(m.group(1)) else (False, "not_owned")

    m = _ARCHIVE_RE.match(p)
    if m and method in ("HEAD", "GET", "PUT"):
        return (True, "archive") if _owned(m.group(1)) else (False, "not_owned")

    m = _RESTART_RE.match(p)
    if m and method == "POST":
        return (True, "restart") if _owned(m.group(1)) else (False, "not_owned")

    m = _EXEC_CREATE_RE.match(p)
    if m and method == "POST":
        if not _owned(m.group(1)):
            return False, "not_owned"
        return _exec_create_body_ok(body)

    m = _EXEC_START_RE.match(p)
    if m and method == "POST":
        return (True, "exec_start") if m.group(1) in tracked_exec_ids else (False, "untracked_exec")

    m = _EXEC_INSPECT_RE.match(p)
    if m and method == "GET":
        return (True, "exec_inspect") if m.group(1) in tracked_exec_ids else (False, "untracked_exec")

    return False, "forbidden"


# ---- Audit logging ---------------------------------------------------------


def configure_audit_log(path: str | None) -> logging.Handler | None:
    """Attach a FileHandler to the audit logger when ``path`` is set."""
    if not path:
        return None
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    handler.setLevel(logging.INFO)
    audit_logger.addHandler(handler)
    return handler


def _audit(*, user_id: str, method: str, path: str, result: str, reason: str, dur_ms: int) -> None:
    """Emit one key=value audit line, mirroring devbox_proxy's style."""
    # Strip query string from the logged path — it can carry exec stdio
    # framing args, never anything we need for the audit trail.
    clean_path = path.split("?", 1)[0]
    audit_logger.info(
        "docker_proxy user=%s method=%s path=%s result=%s reason=%s dur_ms=%d",
        user_id, method, clean_path, result, reason, dur_ms,
    )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


# ---- HTTP wire helpers -----------------------------------------------------


_CRLF = b"\r\n"
_HEADER_END = b"\r\n\r\n"
# The request head (request line + headers) is bounded by the StreamReader's
# default 64 KiB limit; readuntil raises LimitOverrunError past it. A legit
# docker request head is a few hundred bytes.
# Cap on a buffered request body on the mediated path. None of those ops has
# a streaming body — the largest is exec-create's JSON, which is tiny — so
# 1 MiB is far above any real payload and bounds the read.
_MAX_MEDIATED_BODY = 1024 * 1024
# Cap on a buffered upstream response. The mediated ops all return small
# JSON — the largest is a container list, a few KB per container — so this
# is far above any real payload and bounds the read. Past it the proxy
# answers 502 rather than relaying a truncated response.
_MAX_MEDIATED_RESPONSE = 8 * 1024 * 1024
# Largest single chunk this proxy will relay. A chunk-size line is written by
# whoever is sending the body, so an uncapped one is an allocation the client
# gets to choose — and this daemon runs on the host, outside any task cgroup.
_MAX_CHUNK_BYTES = 8 * 1024 * 1024
# Drop a connection that has been sitting idle between requests. Keep-alive
# means a client can hold a connection open indefinitely, and connections
# are a capped resource (see MAX_CONCURRENT_CONNECTIONS).
_HEAD_IDLE_TIMEOUT_SECONDS = 60
# The same idea for a body: a client that sends a valid head declaring a body
# and then stalls would otherwise hold a connection slot forever. Applied per
# read rather than as a total deadline, because a legitimate ``docker cp`` of
# a large tar is slow but never idle.
_BODY_IDLE_TIMEOUT_SECONDS = 60
MAX_CONCURRENT_CONNECTIONS = 64

# The two ops the proxy relays without mediating what follows: ``exec start``
# may hand the connection over to a raw stdio stream, and ``archive`` streams
# a tar body whose framing is deliberately not parsed. Either way the
# connection cannot carry a further classified request, so both end it.
_TERMINAL_REASONS = frozenset({"exec_start", "archive"})


def is_terminal_op(reason: str) -> bool:
    """True if an allowed op ends the connection instead of looping."""
    return reason in _TERMINAL_REASONS


# How moby answers an exec start it has hijacked: 101 when the client asked
# to upgrade, otherwise a 200 carrying one of these content types.
_HIJACK_CONTENT_TYPES = (
    "application/vnd.docker.raw-stream",
    "application/vnd.docker.multiplexed-stream",
)


def is_hijack_response(status: int, headers: dict[str, str]) -> bool:
    """True if the daemon has switched this connection to a raw stdio stream.

    An exec start does *not* always hijack. moby calls ``HijackConnection``
    only when the start body does not say ``Detach: true``, and an error — a
    stopped container, an exec id that already ran — returns an ordinary
    framed response as well. On those paths the daemon is still speaking
    HTTP, so anything copied to it is parsed as the next request.
    """
    if status == 101:
        return True
    content_type = headers.get("content-type", "").lower()
    return any(content_type.startswith(t) for t in _HIJACK_CONTENT_TYPES)


def _http_response(status_code: int, reason_phrase: str, message: str) -> bytes:
    body = json.dumps({"message": f"{PROXY_ERROR_PREFIX}{message}"}).encode("utf-8")
    head = (
        f"HTTP/1.1 {status_code} {reason_phrase}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("utf-8")
    return head + body


def _parse_request_head(raw: bytes) -> tuple[str, str, dict[str, str]]:
    """Parse a buffered request head into ``(method, path, headers)``.

    Headers are lower-cased keys. Raises ``ValueError`` on a malformed head.
    """
    text_lines = raw.split(_CRLF)
    request_line = text_lines[0].decode("latin-1")
    parts = request_line.split(" ")
    if len(parts) < 2:
        raise ValueError("malformed request line")
    method, path = parts[0], parts[1]
    headers: dict[str, str] = {}
    for line in text_lines[1:]:
        if not line:
            continue
        decoded = line.decode("latin-1")
        if ":" not in decoded:
            continue
        key, _, value = decoded.partition(":")
        headers[key.strip().lower()] = value.strip()
    return method, path, headers


# Header names are RFC 7230 tokens. Anything outside this set either makes
# the daemon reject the request or — worse — makes it read the head
# differently from the way this proxy read it.
_TOKEN_BYTES = frozenset(
    b"!#$%&'*+-.^_`|~0123456789"
    b"abcdefghijklmnopqrstuvwxyz"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
# Bound the trailer section of a chunked body: individual lines are capped by
# the stream reader, the number of them is not.
_MAX_TRAILER_BYTES = 8192


def head_structure_error(raw_head: bytes) -> str | None:
    """Reason to reject a head this proxy and the daemon could read differently.

    The proxy classifies the head it parsed and then forwards the original
    bytes, so the two parses have to agree or the allowlist decides nothing.
    The Docker daemon is Go: ``net/textproto`` ends a line at a bare ``\n``,
    and folds a line starting with space or tab into the previous header's
    value. This parser does neither, and either difference is enough to carry
    a whole second request past classification — a blob holding exactly one
    CRLFCRLF is one request here and three to the daemon, and the smuggled
    one can be ``POST /containers/create`` with the host root bind-mounted.

    So anything the two could read differently is a 400 rather than a guess.
    Header names are held to tokens for the same reason: the daemon rejects
    the rest today, and relying on that is relying on someone else's parser
    to stay strict.
    """
    if not raw_head.endswith(_HEADER_END):
        return "malformed_head"
    for index, byte in enumerate(raw_head):
        if byte == 0x0A and (index == 0 or raw_head[index - 1] != 0x0D):
            return "bare_lf_in_head"
        if byte == 0x0D and raw_head[index + 1:index + 2] != b"\n":
            return "bare_cr_in_head"

    for line in raw_head[:-len(_HEADER_END)].split(_CRLF)[1:]:
        if not line:
            return "empty_header_line"
        if line[0:1] in (b" ", b"\t"):
            return "obs_fold_header"
        name, sep, _ = line.partition(b":")
        if not sep or not name:
            return "malformed_header_line"
        if not all(byte in _TOKEN_BYTES for byte in name):
            return "bad_header_name"
    return None


def request_framing_error(raw_head: bytes) -> str | None:
    """Reason to reject a request head whose body framing is ambiguous.

    A head that declares its body length two ways — ``Content-Length``
    together with ``Transfer-Encoding``, or two disagreeing
    ``Content-Length`` values — lets the proxy and the daemon disagree about
    where the body ends. That disagreement *is* request smuggling: bytes one
    of them treats as body, the other treats as the next request. No real
    docker client sends either shape, so reject rather than pick a winner.

    A lone ``Transfer-Encoding`` is fine here and handled per-op: the
    mediated path refuses it (it has to find the body's end to read the next
    head), the archive path copies it chunk by chunk.
    """
    content_lengths: list[str] = []
    transfer_encodings: list[str] = []
    for line in raw_head.split(_CRLF)[1:]:
        if not line:
            continue
        key, sep, value = line.decode("latin-1").partition(":")
        if not sep:
            continue
        name = key.strip().lower()
        if name == "content-length":
            content_lengths.append(value.strip())
        elif name == "transfer-encoding":
            transfer_encodings.append(value.strip())

    if content_lengths and transfer_encodings:
        return "smuggling_cl_and_te"
    # Identical duplicates are fine and deliberate: Go's fixLength dedups
    # equal values and errors only on distinct ones, so the two agree.
    if len(set(content_lengths)) > 1:
        return "smuggling_duplicate_cl"
    # RFC 7230 requires the final transfer coding to be exactly ``chunked``,
    # and Go enforces that. A substring test would accept ``xchunked`` and
    # ``chunked, gzip`` — both of which this proxy would chunk-frame and the
    # daemon would refuse, which is the parser disagreement the head checks
    # exist to prevent.
    if transfer_encodings:
        if len(transfer_encodings) > 1:
            return "unsupported_transfer_encoding"
        if transfer_encodings[0].split(",")[-1].strip().lower() != "chunked":
            return "unsupported_transfer_encoding"
    for value in content_lengths:
        if not (value.isascii() and value.isdigit()):
            return "bad_content_length"
    return None


def _with_connection_close(raw_head: bytes) -> bytes:
    """Rewrite a request head so the upstream connection is not persistent.

    Only for ops relayed opaquely, where the proxy has already decided the
    connection ends with this request. Saying so upstream stops the daemon
    holding the socket open for a second request that will never arrive.

    Not used for ``exec start``: that request carries ``Connection: Upgrade``
    and rewriting it would break the hijack. Terminality there is structural
    — the handler returns and the connection closes — not a header.
    """
    lines = raw_head.split(_CRLF)
    kept = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name in (b"connection", b"keep-alive", b"proxy-connection"):
            continue
        kept.append(line)
    kept.append(b"Connection: close")
    return _CRLF.join(kept) + _HEADER_END


def _client_wants_keep_alive(raw_head: bytes, headers: dict[str, str]) -> bool:
    """Whether to keep the connection after answering this request.

    HTTP/1.1 persists unless the client says ``close``; HTTP/1.0 is the other
    way round. Getting this backwards for a 1.0 client means it waits for an
    end-of-response that only a close will signal.
    """
    connection = headers.get("connection", "").lower()
    if raw_head.split(_CRLF, 1)[0].upper().endswith(b"HTTP/1.0"):
        return "keep-alive" in connection
    return "close" not in connection


def _parse_response_head(raw_head: bytes) -> tuple[int, dict[str, str]]:
    """Parse a response head into ``(status_code, lower-cased headers)``."""
    lines = raw_head.split(_CRLF)
    parts = lines[0].decode("latin-1").split(" ")
    try:
        status = int(parts[1])
    except (IndexError, ValueError) as exc:
        raise ValueError("malformed status line") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, sep, value = line.decode("latin-1").partition(":")
        if not sep:
            continue
        headers[key.strip().lower()] = value.strip()
    return status, headers


def _response_has_body(status: int, request_method: str) -> bool:
    """RFC 7230 s3.3.3: these responses carry no body, whatever they declare."""
    if request_method.upper() == "HEAD":
        return False
    return not (100 <= status < 200 or status in (204, 304))


def _chunk_size(size_line: bytes) -> int:
    """Parse a chunk-size line (hex, optional ``;ext``) into an int."""
    field = size_line.split(b";", 1)[0].strip()
    if not field or not all(c in b"0123456789abcdefABCDEF" for c in field):
        raise ValueError("malformed chunk size")
    return int(field, 16)


def _decode_chunked(raw: bytes) -> bytes:
    """Concatenate the payloads of a chunked body; empty on malformed input."""
    out = bytearray()
    pos = 0
    while True:
        line_end = raw.find(_CRLF, pos)
        if line_end == -1:
            return b""
        try:
            size = _chunk_size(raw[pos:line_end + len(_CRLF)])
        except ValueError:
            return b""
        pos = line_end + len(_CRLF)
        if size == 0:
            return bytes(out)
        out.extend(raw[pos:pos + size])
        pos += size + len(_CRLF)


def _parse_response_body_id(raw_response: bytes) -> str | None:
    """Extract the exec ``Id`` from a buffered exec-create response.

    Decodes a chunked body first. The daemon frames a small JSON response
    either way depending on how it wrote it, and chunk framing parsed as
    JSON yields no id — which leaves the exec untracked and makes every
    following exec start a 403, presenting as a broken ``docker exec``
    rather than as a parsing bug.
    """
    sep = raw_response.find(_HEADER_END)
    if sep == -1:
        return None
    head = raw_response[:sep + len(_HEADER_END)]
    body = raw_response[sep + len(_HEADER_END):]
    try:
        _, headers = _parse_response_head(head)
    except ValueError:
        return None
    if "transfer-encoding" in headers:
        body = _decode_chunked(body)
    try:
        data = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return None
    if isinstance(data, dict):
        ident = data.get("Id")
        if isinstance(ident, str) and ident:
            return ident
    return None


# ---- The proxy server ------------------------------------------------------


@dataclass
class DockerApiProxy:
    """Per-user Docker-API allowlist proxy.

    One process per user (systemd ``@``-instance), so the in-process
    ``_exec_ids`` map has no cross-worker split.
    """

    user_id: str
    container_name: str
    upstream_socket: str
    listen_socket: str
    exec_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        # issued exec id -> monotonic created-at
        self._exec_ids: dict[str, float] = {}
        self._connection_sem = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)

    # -- exec-id tracking --

    def _track_exec(self, exec_id: str) -> None:
        self._exec_ids[exec_id] = time.monotonic()

    def _sweep_exec_ids(self, *, now: float | None = None) -> None:
        """Drop created-but-never-started exec ids older than the TTL."""
        if not self._exec_ids:
            return
        cutoff = (now if now is not None else time.monotonic()) - self.exec_ttl_seconds
        stale = [eid for eid, created in self._exec_ids.items() if created < cutoff]
        for eid in stale:
            self._exec_ids.pop(eid, None)

    # -- connection handling --

    async def _read_head(self, reader: asyncio.StreamReader) -> bytes | None:
        """Read bytes up to and including the blank line ending the head.

        Returns the raw head bytes, or ``None`` on a clean EOF before any
        data (idle close) or on an idle timeout. Raises ``ValueError`` if the
        head exceeds the cap or the connection closes mid-head.
        """
        try:
            return await asyncio.wait_for(
                reader.readuntil(_HEADER_END), _HEAD_IDLE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None  # idle keep-alive connection; give the slot back
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return None  # clean idle close, nothing buffered
            raise ValueError("connection closed mid-head") from exc
        except asyncio.LimitOverrunError as exc:
            raise ValueError("request head too large") from exc

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one client connection, one classified request at a time.

        The loop is what makes the allowlist a filter on *requests* rather
        than on the first request of each connection (ISSUE-294). Nothing is
        ever copied through unclassified: :meth:`_handle_one` either answers
        the request it read or returns False, and False always means close.
        """
        try:
            while await self._handle_one(reader, writer):
                pass
        except Exception:
            logger.exception("docker_proxy connection error")
            try:
                await self._deny(writer, 500, "Internal Server Error", "internal proxy error")
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_one(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> bool:
        """Read, classify and serve one request. True to keep the connection.

        Returning False tells :meth:`_handle` to close. Every path that does
        not fully consume the request body returns False, because a body
        left in the stream would be read as the next request head.
        """
        start = time.monotonic()
        method = path = "?"

        try:
            raw_head = await self._read_head(reader)
        except ValueError as exc:
            await self._deny(writer, 400, "Bad Request", str(exc))
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason="bad_head", dur_ms=_elapsed_ms(start))
            return False
        if raw_head is None:
            return False  # idle close, nothing to do

        # Validate the head's shape before parsing it: everything below —
        # the classification, the body length, the decision to keep the
        # connection — assumes this proxy and the daemon read these bytes
        # the same way.
        structure_error = head_structure_error(raw_head)
        if structure_error:
            await self._deny(writer, 400, "Bad Request", structure_error)
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason=structure_error, dur_ms=_elapsed_ms(start))
            return False

        try:
            method, path, headers = _parse_request_head(raw_head)
        except ValueError:
            await self._deny(writer, 400, "Bad Request", "malformed request")
            _audit(user_id=self.user_id, method="?", path="?",
                   result="deny", reason="malformed", dur_ms=_elapsed_ms(start))
            return False

        framing_error = request_framing_error(raw_head)
        if framing_error:
            await self._deny(writer, 400, "Bad Request", framing_error)
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason=framing_error, dur_ms=_elapsed_ms(start))
            return False

        if "expect" in headers:
            # Both body-reading paths below read the whole request body
            # before opening an upstream connection, so neither can run a
            # 100-continue handshake: the client would wait for an interim
            # response only the daemon can send, and the proxy would wait for
            # the body, forever, holding a connection slot. No docker client
            # sends Expect, and these bodies are small.
            await self._deny(writer, 417, "Expectation Failed", "expect_unsupported")
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason="expect_unsupported", dur_ms=_elapsed_ms(start))
            return False

        self._sweep_exec_ids()

        if is_exec_create(method, path):
            return await self._handle_exec_create(raw_head, headers, reader, writer, start)

        allowed, reason = classify_request(
            method, path, None,
            container_name=self.container_name,
            tracked_exec_ids=set(self._exec_ids),
        )
        if not allowed:
            await self._deny(writer, 403, "Forbidden", reason)
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason=reason, dur_ms=_elapsed_ms(start))
            return False

        if not is_terminal_op(reason):
            return await self._mediate(raw_head, headers, method, path, reason, reader, writer, start)

        if reason == "exec_start":
            # Single-use: evict the id before relaying, so a replay is denied.
            m = _EXEC_START_RE.match(_normalize_path(path))
            if m:
                self._exec_ids.pop(m.group(1), None)
            relayed = await self._relay_exec_start(raw_head, headers, reader, writer)
        else:
            relayed = await self._stream_archive(raw_head, headers, reader, writer)

        _audit(user_id=self.user_id, method=method, path=path,
               result="allow" if relayed else "deny",
               reason=reason if relayed else "upstream_unavailable",
               dur_ms=_elapsed_ms(start))
        return False  # relayed opaquely: nothing may follow on this connection

    async def _mediate(
        self,
        raw_head: bytes,
        headers: dict[str, str],
        method: str,
        path: str,
        reason: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        start: float,
    ) -> bool:
        """Forward one ordinary request/response pair, keeping the connection.

        Ping, version, container list, inspect, restart and exec inspect are
        plain HTTP with no streaming and no hijack, so the proxy can be a
        real intermediary for them: read the whole request, read the whole
        response, write it back, and let the caller classify the next head.
        """
        if "transfer-encoding" in headers:
            # Finding the next head means knowing where this body ends, and
            # this proxy does not decode chunked request bodies on the
            # mediated path. None of these ops has a streaming body anyway.
            await self._deny(writer, 400, "Bad Request", "unsupported_transfer_encoding")
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason="unsupported_transfer_encoding",
                   dur_ms=_elapsed_ms(start))
            return False

        body = b""
        content_length = headers.get("content-length")
        if content_length is not None:
            # request_framing_error already proved this is all digits.
            length = int(content_length)
            if length > _MAX_MEDIATED_BODY:
                await self._deny(writer, 403, "Forbidden", "request body too large")
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="body_too_large", dur_ms=_elapsed_ms(start))
                return False
            try:
                body = await _read_exactly(reader, length)
            except ValueError as exc:
                await self._deny(writer, 408, "Request Timeout", str(exc))
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="body_read_failed", dur_ms=_elapsed_ms(start))
                return False

        try:
            up_reader, up_writer = await asyncio.open_unix_connection(self.upstream_socket)
        except OSError:
            await self._deny(writer, 502, "Bad Gateway", "upstream_unavailable")
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason="upstream_unavailable", dur_ms=_elapsed_ms(start))
            return False

        try:
            up_writer.write(raw_head)
            if body:
                up_writer.write(body)
            await up_writer.drain()
            try:
                interim, response, reusable = await self._read_full_response(
                    up_reader, request_method=method,
                )
            except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                await self._deny(writer, 502, "Bad Gateway", "bad upstream response")
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="bad_upstream_response",
                       dur_ms=_elapsed_ms(start))
                return False
            writer.write(interim + response)
            await writer.drain()
        finally:
            try:
                up_writer.close()
                await up_writer.wait_closed()
            except Exception:
                pass

        _audit(user_id=self.user_id, method=method, path=path,
               result="allow", reason=reason, dur_ms=_elapsed_ms(start))
        return reusable and _client_wants_keep_alive(raw_head, headers)

    async def _stream_archive(
        self,
        raw_head: bytes,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Relay a ``docker cp`` tar stream, then let the connection end.

        Returns True if the stream was relayed, False if upstream could not
        be reached (the caller audits accordingly — a 502 is not an allow).

        The tar body is opaque by design, so the proxy cannot mediate what
        comes after it and does not try. What it does instead is read from
        the client only as far as the request declared — exactly
        Content-Length bytes, or exactly one chunked body — and not one byte
        further. A pipelined follow-up request therefore never reaches the
        daemon, which is the property the old splice gave away.

        The copy runs concurrently with the response, because the daemon may
        answer before the body is finished (a ``PUT`` to a path that does not
        exist is a 404 straight away) and then stop reading. Serializing the
        two would push the rest of the body into a socket nobody drains and
        lose the daemon's answer behind the write error.
        """
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(self.upstream_socket)
        except OSError:
            await self._deny(writer, 502, "Bad Gateway", "upstream_unavailable")
            return False

        try:
            up_writer.write(_with_connection_close(raw_head))
            await up_writer.drain()

            response = asyncio.create_task(_pump(up_reader, writer))
            try:
                await self._copy_archive_body(headers, reader, up_writer)
            except Exception:
                # The daemon has very likely answered and stopped reading.
                # Its response is already on its way to the client through
                # ``response``; don't replace it with a 500. The bare except
                # is deliberate: whatever went wrong on the client side, the
                # response task must still be awaited below or it is dropped
                # while writing into a socket about to be closed.
                logger.debug("docker_proxy archive body ended early", exc_info=True)
            finally:
                await response
        finally:
            try:
                up_writer.close()
                await up_writer.wait_closed()
            except Exception:
                pass
        return True

    async def _copy_archive_body(
        self,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        """Copy exactly the request body the archive head declared, and stop."""
        content_length = headers.get("content-length")
        if content_length is not None:
            await _copy_exact(reader, up_writer, int(content_length))
        elif "transfer-encoding" in headers:
            # request_framing_error already proved this is exactly "chunked".
            await _copy_chunked(reader, up_writer)
        # Otherwise the request has no body (GET/HEAD): read nothing at all
        # from the client, so nothing at all can be forwarded.

    async def _handle_exec_create(
        self,
        raw_head: bytes,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        start: float,
    ) -> bool:
        """Mediate an exec-create. True to keep the client connection open.

        The one op whose body the proxy parses in both directions: the
        request body carries the no-``Privileged`` check, and the response
        body carries the exec ``Id`` that authorizes the matching exec start.
        """
        method, path = "POST", "?"
        try:
            _, path, _ = _parse_request_head(raw_head)
        except ValueError:
            path = "?"

        cl_raw = headers.get("content-length")
        body: bytes | None = None
        if cl_raw is not None:
            # request_framing_error already proved this is all ASCII digits.
            length = int(cl_raw)
            if length > _MAX_MEDIATED_BODY:
                await self._deny(writer, 403, "Forbidden", "exec body too large")
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="body_too_large", dur_ms=_elapsed_ms(start))
                return False
            try:
                body = await _read_exactly(reader, length)
            except ValueError as exc:
                await self._deny(writer, 408, "Request Timeout", str(exc))
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="body_read_failed", dur_ms=_elapsed_ms(start))
                return False

        allowed, reason = classify_request(
            method, path, body,
            container_name=self.container_name,
            tracked_exec_ids=set(self._exec_ids),
        )
        if not allowed:
            await self._deny(writer, 403, "Forbidden", reason)
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason=reason, dur_ms=_elapsed_ms(start))
            return False

        # Fully mediate: forward head+body upstream, read the whole response,
        # capture the issued exec Id, write the response through unchanged.
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(self.upstream_socket)
        except OSError:
            await self._deny(writer, 502, "Bad Gateway", "upstream_unavailable")
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason="upstream_unavailable", dur_ms=_elapsed_ms(start))
            return False

        try:
            up_writer.write(raw_head)
            if body is not None:
                up_writer.write(body)
            await up_writer.drain()

            try:
                interim, response, reusable = await self._read_full_response(
                    up_reader, request_method=method,
                )
            except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                await self._deny(writer, 502, "Bad Gateway", "bad upstream response")
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="bad_upstream_response",
                       dur_ms=_elapsed_ms(start))
                return False
            exec_id = _parse_response_body_id(response)
            if exec_id:
                self._track_exec(exec_id)

            writer.write(interim + response)
            await writer.drain()
            _audit(user_id=self.user_id, method=method, path=path,
                   result="allow", reason="exec_create", dur_ms=_elapsed_ms(start))
        finally:
            try:
                up_writer.close()
                await up_writer.wait_closed()
            except Exception:
                pass

        return reusable and _client_wants_keep_alive(raw_head, headers)

    async def _read_full_response(
        self, reader: asyncio.StreamReader, *, request_method: str,
    ) -> tuple[bytes, bytes, bool]:
        """Read one complete HTTP response.

        Returns ``(interim_heads, response, reusable)``. Interim 1xx heads
        are handed back separately from the response they precede: both get
        relayed to the client, but only the response is a reply, and folding
        them together would make :func:`_parse_response_body_id` read an
        interim head's "body" and lose the exec id.

        ``reusable`` is False when the response's own framing ends at
        connection close rather than at a Content-Length or a terminal
        chunk. A client cannot find the end of such a body either, so the
        proxy must not invite another request onto the connection.

        Raises ``ValueError`` on anything it cannot frame; the caller turns
        that into a 502 rather than relaying a response of unknown length.
        """
        # A 1xx is interim, not an answer: relay it and keep reading for the
        # real response. curl adds ``Expect: 100-continue`` on its own for a
        # request body over 1 KiB, so treating the interim head as the whole
        # response would strand the client waiting for a reply already
        # discarded — and, on exec-create, lose the issued exec Id with it.
        # A 101 never reaches here; an upgrade only follows exec start, which
        # is spliced rather than mediated.
        interim = bytearray()
        while True:
            try:
                head = await reader.readuntil(_HEADER_END)
            except asyncio.IncompleteReadError as exc:
                raise ValueError("upstream closed mid-response-head") from exc
            except asyncio.LimitOverrunError as exc:
                raise ValueError("upstream response head too large") from exc

            status, headers = _parse_response_head(head)
            if not 100 <= status < 200:
                break
            interim.extend(head)
            if len(interim) > _MAX_MEDIATED_RESPONSE:
                raise ValueError("too many interim responses")

        response, reusable = await _read_response_body(
            reader, head, status, headers, request_method=request_method,
        )
        return bytes(interim), response, reusable

    async def _relay_exec_start(
        self,
        raw_head: bytes,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Relay an exec start, going full-duplex only once the daemon hijacks.

        Returns True if the exchange reached the daemon, False on a 502.

        An exec start is *usually* a hijack: the connection stops being HTTP
        and becomes a raw bidirectional stdio stream, which is why the proxy
        does not interpret it. But it is not always. moby hijacks only when
        the start body does not say ``Detach: true``, and an error — a
        stopped container, an exec id that already ran — is an ordinary
        framed response too. On those paths the daemon is still parsing HTTP,
        so copying client bytes to it on the assumption of a hijack hands it
        a request nobody classified. That is ISSUE-294 again, and a
        privileged host-mounting ``POST /containers/create`` rides through it.

        So the order matters: send the head and exactly the body it declared,
        read the *response* head, and only then decide. A hijack gets the
        full-duplex pump. Anything else is a normal response — relay it and
        close, having never copied a client byte past the declared body.
        """
        body = b""
        content_length = headers.get("content-length")
        if content_length is not None:
            # request_framing_error already proved this is all ASCII digits.
            length = int(content_length)
            if length > _MAX_MEDIATED_BODY:
                await self._deny(writer, 403, "Forbidden", "exec start body too large")
                return False
            body = await _read_exactly(reader, length)

        try:
            up_reader, up_writer = await asyncio.open_unix_connection(self.upstream_socket)
        except OSError:
            await self._deny(writer, 502, "Bad Gateway", "upstream_unavailable")
            return False

        try:
            up_writer.write(raw_head)
            if body:
                up_writer.write(body)
            await up_writer.drain()

            try:
                head = await up_reader.readuntil(_HEADER_END)
                status, response_headers = _parse_response_head(head)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
                await self._deny(writer, 502, "Bad Gateway", "bad upstream response")
                return False

            if not is_hijack_response(status, response_headers):
                # Still HTTP. Relay the response and end the connection —
                # never open the client-to-daemon direction.
                try:
                    response, _ = await _read_response_body(
                        up_reader, head, status, response_headers, request_method="POST",
                    )
                except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    await self._deny(writer, 502, "Bad Gateway", "bad upstream response")
                    return False
                writer.write(response)
                await writer.drain()
                return True

            # Hijacked: from here the bytes are stdio, not HTTP, and neither
            # side parses them as such. Full duplex until the stream dies.
            writer.write(head)
            await writer.drain()
            await asyncio.gather(
                _pump(reader, up_writer),
                _pump(up_reader, writer),
            )
            return True
        finally:
            try:
                up_writer.close()
                await up_writer.wait_closed()
            except Exception:
                pass

    async def _deny(self, writer: asyncio.StreamWriter, status: int, phrase: str, message: str) -> None:
        try:
            writer.write(_http_response(status, phrase, message))
            await writer.drain()
        except Exception:
            pass

    async def serve_forever(self) -> None:
        sock_path = Path(self.listen_socket)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            sock_path.unlink()

        async def _client_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if self._connection_sem.locked():
                await self._deny(writer, 503, "Service Unavailable", "proxy at connection cap")
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                return
            async with self._connection_sem:
                await self._handle(reader, writer)

        previous_umask = os.umask(0o117)
        try:
            server = await asyncio.start_unix_server(_client_cb, path=str(sock_path))
        finally:
            os.umask(previous_umask)
        try:
            os.chmod(str(sock_path), 0o660)
        except OSError:
            pass

        logger.info(
            "docker_proxy listening user_id=%s socket=%s container=%s upstream=%s",
            self.user_id, sock_path, self.container_name, self.upstream_socket,
        )
        try:
            async with server:
                await server.serve_forever()
        finally:
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass


async def _read_response_body(
    reader: asyncio.StreamReader,
    head: bytes,
    status: int,
    headers: dict[str, str],
    *,
    request_method: str,
) -> tuple[bytes, bool]:
    """Read the body belonging to an already-read response head.

    Returns ``(head + body, reusable)``. ``reusable`` is False when the body
    was framed by end-of-stream rather than by Content-Length or a terminal
    chunk — a client cannot find the end of such a body either, so the
    connection must not carry another request.
    """
    reusable = "close" not in headers.get("connection", "").lower()

    if not _response_has_body(status, request_method):
        return head, reusable

    if "transfer-encoding" in headers:
        body = await _read_chunked_body(reader, limit=_MAX_MEDIATED_RESPONSE)
        return head + body, reusable

    content_length = headers.get("content-length")
    if content_length is not None and content_length.isascii() and content_length.isdigit():
        length = int(content_length)
        if length > _MAX_MEDIATED_RESPONSE:
            raise ValueError("upstream response too large")
        try:
            return head + await reader.readexactly(length), reusable
        except asyncio.IncompleteReadError as exc:
            raise ValueError("upstream closed mid-response-body") from exc

    # Neither framed nor chunked: the body runs to end of stream, which a
    # persistent connection cannot express. A daemon doing that has to have
    # said "Connection: close"; if it did not, the response is unframed and
    # reading on would hang for a close that never comes.
    if reusable:
        raise ValueError("unframed upstream response")
    body = bytearray()
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > _MAX_MEDIATED_RESPONSE:
            raise ValueError("upstream response too large")
    return head + bytes(body), False


async def _read_exactly(reader: asyncio.StreamReader, count: int) -> bytes:
    """Read exactly ``count`` bytes, refusing to wait forever for them."""
    try:
        return await asyncio.wait_for(
            reader.readexactly(count), _BODY_IDLE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ValueError("timed out reading request body") from exc
    except asyncio.IncompleteReadError as exc:
        raise ValueError("client closed mid-body") from exc


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    """Read one CRLF-terminated line, refusing to wait forever for it."""
    try:
        return await asyncio.wait_for(
            reader.readuntil(_CRLF), _BODY_IDLE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ValueError("timed out reading chunk header") from exc
    except asyncio.IncompleteReadError as exc:
        raise ValueError("closed mid-chunk-header") from exc
    except asyncio.LimitOverrunError as exc:
        raise ValueError("chunk header too large") from exc


async def _copy_exact(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count: int,
) -> None:
    """Copy exactly ``count`` bytes from the client upstream, then stop.

    Stopping at the declared length is the whole point. A byte read past it
    is the start of a pipelined follow-up request, and copying that upstream
    is the tunnel this proxy exists to prevent.
    """
    remaining = count
    while remaining > 0:
        try:
            chunk = await asyncio.wait_for(
                reader.read(min(65536, remaining)), _BODY_IDLE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise ValueError("timed out reading request body") from exc
        if not chunk:
            raise ValueError("client closed mid-body")
        writer.write(chunk)
        await writer.drain()
        remaining -= len(chunk)


async def _copy_chunked(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
) -> None:
    """Copy one chunked request body verbatim, stopping after its last chunk.

    Same contract as :func:`_copy_exact`: the body's own framing says where
    it ends and nothing past that is read from the client. Each size line is
    validated *before* any of it is relayed, so a malformed or oversized
    chunk is refused rather than half-forwarded, and the chunk data itself
    moves in bounded pieces — a size line is client-chosen, and buffering a
    whole chunk before writing would let one request name the allocation.
    """
    while True:
        size_line = await _read_line(reader)
        size = _chunk_size(size_line)
        if size > _MAX_CHUNK_BYTES:
            raise ValueError("chunk too large")
        writer.write(size_line)
        if size == 0:
            break
        await _copy_exact(reader, writer, size)
        terminator = await _read_exactly(reader, len(_CRLF))
        if terminator != _CRLF:
            raise ValueError("malformed chunk terminator")
        writer.write(terminator)
        await writer.drain()

    # Trailers, terminated by a blank line. Each line is bounded by the
    # stream reader; the number of them is not, so bound that here.
    trailer_bytes = 0
    while True:
        line = await _read_line(reader)
        trailer_bytes += len(line)
        if trailer_bytes > _MAX_TRAILER_BYTES:
            raise ValueError("chunked trailers too large")
        writer.write(line)
        if line == _CRLF:
            break
    await writer.drain()


async def _read_chunked_body(reader: asyncio.StreamReader, *, limit: int) -> bytes:
    """Read a chunked response body verbatim, through trailers.

    Returns the raw chunked bytes rather than the decoded payload, so the
    caller can relay them under the response's own ``Transfer-Encoding``
    header without re-framing anything. Every size is checked against the
    limit *before* the read that would honour it — a cap tested after the
    allocation is not a cap.
    """
    out = bytearray()
    while True:
        size_line = await reader.readuntil(_CRLF)
        size = _chunk_size(size_line)
        if size > _MAX_CHUNK_BYTES or len(out) + len(size_line) + size > limit:
            raise ValueError("upstream response too large")
        out.extend(size_line)
        if size == 0:
            break
        out.extend(await reader.readexactly(size))
        terminator = await reader.readexactly(len(_CRLF))
        if terminator != _CRLF:
            raise ValueError("malformed chunk terminator")
        out.extend(terminator)

    trailer_bytes = 0
    while True:
        line = await reader.readuntil(_CRLF)
        trailer_bytes += len(line)
        if trailer_bytes > _MAX_TRAILER_BYTES:
            raise ValueError("chunked trailers too large")
        out.extend(line)
        if line == _CRLF:
            break
    return bytes(out)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until EOF; half-close the writer on the way out."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception:
        logger.debug("docker_proxy pump error", exc_info=True)
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass


# ---- Daemon entry point ----------------------------------------------------


def _default_socket_path(user_id: str, config) -> Path:
    sock_dir = getattr(config.devbox, "api_proxy_socket_dir", "/var/run/istota-docker")
    return Path(sock_dir) / f"{user_id}.sock"


async def serve(user_id: str, config, *, socket_path: Path | None = None) -> None:
    """Run the docker-API proxy daemon for one user until cancelled."""
    container_name = f"{config.devbox.container_prefix}{user_id}"
    upstream = config.devbox.docker_socket
    listen = socket_path or _default_socket_path(user_id, config)

    proxy = DockerApiProxy(
        user_id=user_id,
        container_name=container_name,
        upstream_socket=upstream,
        listen_socket=str(listen),
        exec_ttl_seconds=getattr(config.devbox, "api_proxy_exec_ttl_seconds", 300),
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    serve_task = asyncio.create_task(proxy.serve_forever())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, pending = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        serve_task.cancel()


def main(argv: list[str] | None = None) -> int:
    """``python -m istota.docker_proxy --user <id>`` entry point."""
    import argparse

    from istota.config import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="user_id this proxy serves")
    parser.add_argument("--config", default=None, help="optional config.toml path override")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = load_config(Path(args.config) if args.config else None)
    audit_log_path = getattr(config.devbox, "api_proxy_audit_log", "") or ""
    configure_audit_log(audit_log_path or None)
    try:
        asyncio.run(serve(args.user, config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
