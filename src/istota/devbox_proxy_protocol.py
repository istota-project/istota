"""Wire protocol for the devbox credential proxy.

Pure data + serialization. No sockets, no asyncio, no httpx — importable
from both the daemon (`istota.devbox_proxy`) and tests without pulling in
any I/O machinery.

Framing: one JSON object per line, terminated by ``\\n``. Requests are
client-to-daemon; responses are daemon-to-client. Each connection carries
exactly one request and one response, then closes — same shape as the
existing skill proxy.

Action and error-code names are stable: the audit log keys on them and
the container-side helper scripts switch on them as plain strings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ---- Action names ----------------------------------------------------------

ACTION_PING = "ping"
ACTION_GIT_CREDENTIAL = "git_credential"
ACTION_GL_API = "gitlab_api"
ACTION_GH_API = "github_api"

ALL_ACTIONS: frozenset[str] = frozenset({
    ACTION_PING,
    ACTION_GIT_CREDENTIAL,
    ACTION_GL_API,
    ACTION_GH_API,
})

# ---- Error codes -----------------------------------------------------------

ERR_NO_TOKEN = "no_token"
ERR_UNKNOWN_ACTION = "unknown_action"
ERR_BAD_REQUEST = "bad_request"
ERR_NOT_ALLOWED = "not_allowed"
ERR_UPSTREAM = "upstream_error"
ERR_INTERNAL = "internal"

# ---- Size cap --------------------------------------------------------------

MAX_REQUEST_BYTES: int = 16 * 1024 * 1024  # 16 MiB, matches skill_proxy


# ---- Exceptions ------------------------------------------------------------


@dataclass
class ProtocolError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


# ---- Encoders --------------------------------------------------------------


def encode_request(action: str, **fields: Any) -> str:
    """Serialize a request envelope as a single newline-terminated JSON line.

    The daemon never calls this — clients do. The helpers test against it
    directly, and the container-side wrappers do the equivalent inline.
    """
    payload = {"action": action, **fields}
    return json.dumps(payload, separators=(",", ":")) + "\n"


def encode_response(*, ok: bool, **fields: Any) -> str:
    """Serialize a successful response envelope."""
    payload = {"ok": ok, **fields}
    return json.dumps(payload, separators=(",", ":")) + "\n"


def encode_error(code: str, message: str, **extra: Any) -> str:
    """Serialize a structured error envelope.

    ``code`` is one of the stable ``ERR_*`` constants; ``message`` is a
    human-readable explanation; ``extra`` becomes additional response
    fields (e.g. ``status``, ``body`` for upstream errors).
    """
    payload: dict[str, Any] = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":")) + "\n"


# ---- Decoders --------------------------------------------------------------


def decode_request(line: str) -> dict[str, Any]:
    """Parse a request line into a dict.

    Raises ``ProtocolError(ERR_BAD_REQUEST, ...)`` on any framing,
    JSON-shape, or size problem so the daemon can return the canonical
    structured error envelope.
    """
    if len(line.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ProtocolError(
            ERR_BAD_REQUEST,
            f"request exceeds {MAX_REQUEST_BYTES // (1024 * 1024)} MiB",
        )

    stripped = line.strip()
    if not stripped:
        raise ProtocolError(ERR_BAD_REQUEST, "empty request")

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ProtocolError(ERR_BAD_REQUEST, f"invalid JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise ProtocolError(ERR_BAD_REQUEST, "request must be a JSON object")

    if "action" not in parsed or not isinstance(parsed["action"], str):
        raise ProtocolError(ERR_BAD_REQUEST, "missing 'action' field")

    return parsed


def decode_response(line: str) -> dict[str, Any]:
    """Parse a response line into a dict.

    Used by the test helpers and by any in-tree client that talks to a
    test daemon. Raises ``ProtocolError`` on malformed input.
    """
    stripped = line.strip()
    if not stripped:
        raise ProtocolError(ERR_BAD_REQUEST, "empty response")

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ProtocolError(ERR_BAD_REQUEST, f"invalid JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise ProtocolError(ERR_BAD_REQUEST, "response must be a JSON object")

    return parsed
