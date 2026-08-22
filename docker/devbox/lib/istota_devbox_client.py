"""Shared client used by the in-container devbox proxy shims.

Lives at /usr/local/lib/istota_devbox/ in the image. ``git-credential-istota``
adds that directory to ``sys.path`` and imports ``call`` / ``die``.

The protocol mirrors what the host-side daemon expects: one JSON object
per line, newline-terminated, one request and one response per
connection.

Only the credential helper uses this now. The curated ``gh`` / ``glab``
shims and the ``github-api`` / ``gitlab-api`` REST wrappers are gone: the
container runs the real binaries behind ``istota_forge_cli.py``, which is
a stdlib-only leaf and deliberately imports nothing from here.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

_DEFAULT_SOCK = "/run/istota-cred/sock"
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024  # 2x request cap; plenty of room


def socket_path() -> str:
    return os.environ.get("ISTOTA_CRED_SOCK", _DEFAULT_SOCK)


def request_line(action: str, **fields: Any) -> bytes:
    """Build the JSON request line the daemon expects.

    Mirrors ``istota.devbox_proxy_protocol.encode_request`` but lives
    here so the shim image doesn't need to vendor the istota package.
    """
    payload = {"action": action, **fields}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def call(action: str, *, timeout: float = 35.0, **fields: Any) -> dict:
    """Open the socket, send one request line, read one response line.

    Returns the parsed response dict. Raises ``ProxyUnreachable`` if the
    socket can't be reached, and ``ProxyProtocolError`` on a malformed
    response. The daemon's structured error envelope (``ok=false``) is
    *not* a Python exception — callers should inspect ``response["ok"]``
    and handle the ``error`` code themselves.
    """
    path = socket_path()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
    except (OSError, FileNotFoundError) as e:
        # Name the deployment shape, not just the path. `git push` reaches
        # this rather than the forge wrapper's exit 4, so a message that only
        # named the socket sent the reader looking for a service that, on one
        # of the two shapes, is not supposed to exist (ISSUE-282).
        raise ProxyUnreachable(
            f"istota credential proxy unreachable at {path}. The Ansible "
            "deployment runs a per-user credential proxy for the devbox and "
            "this means it is down; the docker-compose deployment "
            "deliberately runs none, so git cannot obtain a forge credential "
            "inside the container there at all. Report this rather than "
            "retrying."
        ) from e

    try:
        sock.sendall(request_line(action, **fields))
        buf = bytearray()
        while b"\n" not in buf and len(buf) < _MAX_RESPONSE_BYTES:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    line = bytes(buf).decode("utf-8", errors="replace").strip()
    if not line:
        raise ProxyProtocolError("proxy closed connection without a response")
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProxyProtocolError(f"malformed proxy response: {e}") from e
    if not isinstance(parsed, dict):
        raise ProxyProtocolError("proxy response was not a JSON object")
    return parsed


class ProxyUnreachable(RuntimeError):
    pass


class ProxyProtocolError(RuntimeError):
    pass


def die(message: str, exit_code: int = 1) -> None:
    """Print a one-line error to stderr and exit."""
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)
