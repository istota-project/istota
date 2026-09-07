"""Unwrap a Nextcloud OCS envelope, or say what came back instead.

Stdlib-only leaf (``httpx`` is typed against but never imported), so a skill
subprocess or a low-level caller can use it without pulling in the package. No
config, no DB, no brain.

Every Nextcloud read answers ``{"ocs": {"meta": …, "data": …}}``. Fifteen call
sites reached into that by hand as ``response.json().get("ocs", {}).get("data",
{})``, which turns three different faults — an unreadable body, a JSON body
that is not an OCS envelope, and a genuinely empty result — into one silent
empty default. :func:`ocs_data` is the version that names which one happened.

``istota.nextcloud._http`` keeps its own richer reader: it also checks the
envelope's ``meta.statuscode``, maps Nextcloud's 99x range to text and carries
the endpoint, and its :class:`~istota.nextcloud.OcsError` subclasses the one
here so the two are one exception family rather than two classes with one name.
"""

from __future__ import annotations

from typing import Any

_SNIPPET_CHARS = 200


class OcsError(Exception):
    """Nextcloud answered with something that is not an OCS envelope.

    A bare ``json.JSONDecodeError`` says only "Expecting value: line 1 column 1
    (char 0)", which is what an empty body, an HTML error page and an XML
    envelope all decode to — three different faults with one message, and the
    Talk poll handler logs the message alone, so a production deployment ran a
    hundred of these a day for weeks with nothing in the log to tell them apart
    (ISSUE-399). Carry the status, the declared type and a bounded prefix of
    what actually came back, so the next occurrence names its own cause.
    """


def is_ocs_envelope(body: Any) -> bool:
    """True when *body* is a decoded response carrying an ``ocs`` envelope."""
    return isinstance(body, dict) and "ocs" in body


def ocs_body_data(
    body: Any, what: str, *, status: int | None = None, default: Any = None,
) -> Any:
    """Pull ``ocs.data`` out of an already-decoded body, or raise OcsError.

    ``status`` is the HTTP status where the caller has one; it is named in the
    error because "no envelope" from a 200 and from a 502 are different faults.
    """
    if not is_ocs_envelope(body):
        where = f" (HTTP {status})" if status is not None else ""
        raise OcsError(
            f"{what}: JSON without an ocs envelope{where}: "
            f"{str(body)[:_SNIPPET_CHARS]!r}"
        )
    data = (body.get("ocs") or {}).get("data")
    return default if data is None else data


def ocs_data(response, what: str, default: Any = None) -> Any:
    """Pull ``ocs.data`` out of an OCS response, or raise OcsError.

    The body prefix is capped and flattened to one line. It is only ever read
    on the failure path, where by construction the payload is not the answer
    that was asked for but an error page, an empty body or a proxy's own reply.
    """
    try:
        body = response.json()
    except Exception as e:
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {}) or {}
        ctype = headers.get("content-type", "?")
        # `.text` raises on a response whose body was never read. These calls
        # are all non-streaming, so it is available — but a diagnostic must not
        # be the thing that raises.
        try:
            text = getattr(response, "text", "") or ""
        except Exception:
            text = ""
        snippet = " ".join(text.split())[:_SNIPPET_CHARS]
        raise OcsError(
            f"{what}: non-JSON response (HTTP {status}, content-type {ctype}, "
            f"{len(text)} chars): {snippet!r} [{type(e).__name__}: {e}]"
        ) from e

    return ocs_body_data(
        body, what, status=getattr(response, "status_code", None), default=default,
    )
