"""Shared client used by the in-container devbox proxy shims.

Lives at /usr/local/lib/istota_devbox/ in the image; each shim script
adds that directory to ``sys.path`` and imports ``call`` /
``request_line``. Keeping the I/O code in one place avoids drift between
the credential helper, the api wrappers, and the gh/glab dispatch
shims.

The protocol mirrors what the host-side daemon expects: one JSON object
per line, newline-terminated, one request and one response per
connection.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

_DEFAULT_SOCK = "/run/istota-cred.sock"
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
        raise ProxyUnreachable(
            f"istota credential proxy unreachable at {path} — "
            "is the host-side service running?"
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


class RepoUnknown(RuntimeError):
    """Raised when we can't resolve the current repo slug from git."""
    pass


def get_repo_slug(remote: str = "origin") -> str:
    """Return ``owner/repo`` (or ``namespace/path`` for GitLab) from the current
    repo's remote URL.

    Tests set ``ISTOTA_DEVBOX_REPO_SLUG`` to bypass the git invocation —
    not a production knob, but the cheapest way to make subprocess tests
    of the gh/glab shims independent of a real working tree.
    """
    import re
    import subprocess

    override = os.environ.get("ISTOTA_DEVBOX_REPO_SLUG", "").strip()
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        raise RepoUnknown(f"could not run git remote get-url: {e}") from e
    if result.returncode != 0:
        raise RepoUnknown(
            f"could not resolve remote {remote!r}: {result.stderr.strip() or 'unknown'}"
        )
    url = result.stdout.strip()
    match = re.match(
        r"(?:https?://[^/]+/|git@[^:]+:|ssh://(?:[^@]+@)?[^/:]+(?::\d+)?/)([^/]+/[^/]+?)(?:\.git)?$",
        url,
    )
    if not match:
        raise RepoUnknown(f"could not parse repo from URL: {url!r}")
    return match.group(1)


def die(message: str, exit_code: int = 1) -> None:
    """Print a one-line error to stderr and exit."""
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)


# ---- Shared API-wrapper plumbing -------------------------------------------
#
# The gitlab-api and github-api shims share the same argparse surface and
# response handling — only the action name and the human-readable bin name
# differ. Keeping the logic here means the two shim scripts are five-liners
# and we don't drift between them.


def api_wrapper_parser(bin_name: str):
    """Build the argparse.ArgumentParser used by gitlab-api / github-api."""
    import argparse

    p = argparse.ArgumentParser(
        prog=bin_name,
        description=f"REST API call routed through the istota devbox proxy ({bin_name}).",
    )
    p.add_argument("--method", default="GET",
                   help="HTTP method (GET, POST, PUT, PATCH, DELETE).")
    p.add_argument("--endpoint", required=True,
                   help="Endpoint path, e.g. /projects/42 or /repos/foo/bar/pulls.")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body", help="Inline request body string.")
    body.add_argument("--body-file", help="Read request body from this file.")
    body.add_argument("--body-stdin", action="store_true",
                      help="Read request body from stdin.")
    p.add_argument(
        "--header", action="append", default=[],
        metavar="KEY=VALUE",
        help="Extra request header. Repeatable.",
    )
    return p


def _resolve_body(args) -> str | None:
    if args.body is not None:
        return args.body
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as fh:
            return fh.read()
    if args.body_stdin:
        return sys.stdin.read()
    return None


def _parse_headers(pairs: list[str], bin_name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            die(f"{bin_name}: --header must be KEY=VALUE, got {raw!r}", exit_code=2)
        key, _, value = raw.partition("=")
        out[key.strip()] = value
    return out


def emit_response(response: dict, bin_name: str) -> int:
    """Common stdout/stderr/exit-code logic for shim API responses.

    ``ok=true`` → body to stdout, exit 0.
    ``ok=false`` → body still to stdout (callers may want to inspect
    error JSON), human message to stderr, exit 1.
    """
    body_text = response.get("body", "")
    if body_text:
        sys.stdout.write(body_text)
        if not body_text.endswith("\n"):
            sys.stdout.write("\n")
    if response.get("ok"):
        return 0
    print(f"{bin_name}: {response.get('message', 'proxy error')}", file=sys.stderr)
    return 1


def api_wrapper_main(argv: list[str], *, action: str, bin_name: str) -> int:
    """Implementation shared by gitlab-api and github-api."""
    parser = api_wrapper_parser(bin_name)
    args = parser.parse_args(argv)

    body = _resolve_body(args)
    headers = _parse_headers(args.header, bin_name)

    try:
        response = call(
            action,
            method=args.method.upper(),
            endpoint=args.endpoint,
            body=body,
            headers=headers,
        )
    except (ProxyUnreachable, ProxyProtocolError) as e:
        die(f"{bin_name}: {e}", exit_code=1)

    return emit_response(response, bin_name)
