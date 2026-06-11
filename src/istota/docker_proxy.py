"""Docker-API allowlist proxy daemon.

Per-user asyncio reverse proxy that sits in front of the host Docker
socket and is *safe to bind into the bwrap sandbox unconditionally*. The
raw Docker socket is root-equivalent — anything that can write to it can
launch a privileged, host-mounting container — so the executor never
binds the raw socket into the sandbox again. This proxy is bound at the
conventional in-sandbox path ``/var/run/docker.sock`` instead, and only
forwards a tightly-scoped allowlist of operations against the user's own
``devbox-<user_id>`` container.

The Docker daemon speaks HTTP/1.1 over its unix socket. The proxy:

* parses each request's method + path (and, for exec-create, the body),
* decides allow/deny with a pure :func:`classify_request`,
* for every allowed op **except exec-create**, splices the client socket
  full-duplex to the real docker socket and never interprets the stream
  (``cp`` streams tar archives; ``exec start`` hijacks the connection for
  bidirectional stdio),
* for exec-create — the one fully-mediated op — buffers and parses the
  request body (to enforce the no-``Privileged`` check) and the response
  body (to capture the issued exec ``Id`` for exec-id tracking), then
  writes the response through unchanged.

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

    # Container list. Allowed per the allowlist; note the spliced response
    # is not filtered (we never interpret a spliced stream). Container names
    # are not secrets and the dangerous ops (create/run/privileged) are
    # blocked regardless, so this is an accepted, documented limitation.
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
# Cap on a fully-mediated body (exec-create request + response). Exec-create
# JSON is tiny; 1 MiB is far above any real payload and bounds the buffered
# read.
_MAX_MEDIATED_BODY = 1024 * 1024
MAX_CONCURRENT_CONNECTIONS = 64


def _http_response(status_code: int, reason_phrase: str, message: str) -> bytes:
    body = json.dumps({"message": f"istota-docker-proxy: {message}"}).encode("utf-8")
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


def _parse_response_body_id(raw_response: bytes) -> str | None:
    """Extract the exec ``Id`` from a buffered exec-create response."""
    sep = raw_response.find(_HEADER_END)
    if sep == -1:
        return None
    body = raw_response[sep + len(_HEADER_END):]
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
        data (idle close). Raises ``ValueError`` if the head exceeds the cap
        or the connection closes mid-head.
        """
        try:
            return await reader.readuntil(_HEADER_END)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return None  # clean idle close, nothing buffered
            raise ValueError("connection closed mid-head") from exc
        except asyncio.LimitOverrunError as exc:
            raise ValueError("request head too large") from exc

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        start = time.monotonic()
        method = path = "?"
        try:
            try:
                raw_head = await self._read_head(reader)
            except ValueError as exc:
                await self._deny(writer, 400, "Bad Request", str(exc))
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="bad_head", dur_ms=_elapsed_ms(start))
                return
            if raw_head is None:
                return  # idle close, nothing to do

            try:
                method, path, headers = _parse_request_head(raw_head)
            except ValueError:
                await self._deny(writer, 400, "Bad Request", "malformed request")
                _audit(user_id=self.user_id, method="?", path="?",
                       result="deny", reason="malformed", dur_ms=_elapsed_ms(start))
                return

            self._sweep_exec_ids()
            tracked = set(self._exec_ids)

            if is_exec_create(method, path):
                await self._handle_exec_create(raw_head, headers, reader, writer, start)
                return

            allowed, reason = classify_request(
                method, path, None,
                container_name=self.container_name,
                tracked_exec_ids=tracked,
            )
            if not allowed:
                await self._deny(writer, 403, "Forbidden", reason)
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason=reason, dur_ms=_elapsed_ms(start))
                return

            # exec start is single-use: evict the id now so a replay on the
            # same connection-batch is denied.
            if reason == "exec_start":
                m = _EXEC_START_RE.match(_normalize_path(path))
                if m:
                    self._exec_ids.pop(m.group(1), None)

            await self._splice(raw_head, reader, writer)
            _audit(user_id=self.user_id, method=method, path=path,
                   result="allow", reason=reason, dur_ms=_elapsed_ms(start))
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

    async def _handle_exec_create(
        self,
        raw_head: bytes,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        start: float,
    ) -> None:
        method, path = "POST", "?"
        try:
            _, path, _ = _parse_request_head(raw_head)
        except ValueError:
            path = "?"

        cl_raw = headers.get("content-length")
        body: bytes | None = None
        if cl_raw is not None:
            try:
                length = int(cl_raw)
            except ValueError:
                await self._deny(writer, 400, "Bad Request", "bad content-length")
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="bad_content_length", dur_ms=_elapsed_ms(start))
                return
            if length > _MAX_MEDIATED_BODY:
                await self._deny(writer, 403, "Forbidden", "exec body too large")
                _audit(user_id=self.user_id, method=method, path=path,
                       result="deny", reason="body_too_large", dur_ms=_elapsed_ms(start))
                return
            body = await reader.readexactly(length)

        allowed, reason = classify_request(
            method, path, body,
            container_name=self.container_name,
            tracked_exec_ids=set(self._exec_ids),
        )
        if not allowed:
            await self._deny(writer, 403, "Forbidden", reason)
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason=reason, dur_ms=_elapsed_ms(start))
            return

        # Fully mediate: forward head+body upstream, read the whole response,
        # capture the issued exec Id, write the response through unchanged.
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(self.upstream_socket)
        except OSError:
            await self._deny(writer, 502, "Bad Gateway", "upstream_unavailable")
            _audit(user_id=self.user_id, method=method, path=path,
                   result="deny", reason="upstream_unavailable", dur_ms=_elapsed_ms(start))
            return

        try:
            up_writer.write(raw_head)
            if body is not None:
                up_writer.write(body)
            await up_writer.drain()

            response = await self._read_full_response(up_reader)
            exec_id = _parse_response_body_id(response)
            if exec_id:
                self._track_exec(exec_id)

            writer.write(response)
            await writer.drain()
            _audit(user_id=self.user_id, method=method, path=path,
                   result="allow", reason="exec_create", dur_ms=_elapsed_ms(start))
        finally:
            try:
                up_writer.close()
                await up_writer.wait_closed()
            except Exception:
                pass

    async def _read_full_response(self, reader: asyncio.StreamReader) -> bytes:
        """Read a complete HTTP response head + (Content-Length) body."""
        buf = bytearray()
        while _HEADER_END not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                return bytes(buf)
            buf.extend(chunk)
            if len(buf) > _MAX_MEDIATED_BODY:
                break
        sep = bytes(buf).find(_HEADER_END)
        if sep == -1:
            return bytes(buf)
        head = bytes(buf[:sep])
        already = len(buf) - (sep + len(_HEADER_END))
        content_length = None
        for line in head.split(_CRLF):
            decoded = line.decode("latin-1")
            if decoded.lower().startswith("content-length:"):
                try:
                    content_length = int(decoded.split(":", 1)[1].strip())
                except ValueError:
                    content_length = None
                break
        if content_length is None:
            return bytes(buf)
        remaining = content_length - already
        while remaining > 0:
            chunk = await reader.read(min(4096, remaining))
            if not chunk:
                break
            buf.extend(chunk)
            remaining -= len(chunk)
        return bytes(buf)

    async def _splice(
        self,
        raw_head: bytes,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Open upstream, replay the buffered head, then full-duplex copy."""
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(self.upstream_socket)
        except OSError:
            await self._deny(client_writer, 502, "Bad Gateway", "upstream_unavailable")
            return

        try:
            up_writer.write(raw_head)
            await up_writer.drain()
            await asyncio.gather(
                _pump(client_reader, up_writer),
                _pump(up_reader, client_writer),
            )
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
