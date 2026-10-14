"""Why a host could not be reached, as a fixed plain message.

Moved out of ``webui/setup_checks.py`` so the doctor check can give the same
reasons without importing from ``webui/``. The failure's types and text only
choose among fixed messages; none of the text is returned. stdlib-only leaf.
"""

from __future__ import annotations

import math
import socket
import ssl
from urllib.parse import urlsplit


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def _chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        parts.append(f"{type(node).__name__} {node}")
        node = node.__cause__ or node.__context__
    return " | ".join(parts)


def _chain_types(exc: BaseException | None) -> list[type]:
    types: list[type] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        types.append(type(exc))
        exc = exc.__cause__ or exc.__context__
    return types


def chain_class_names(exc: BaseException | None) -> list[str]:
    """The class names along ``exc``'s cause chain, for a log line with no text."""
    return [t.__name__ for t in _chain_types(exc)]


_DNS_MARKERS = (
    "name or service not known", "nodename nor servname", "temporary failure in name resolution",
    "no address associated", "failed to resolve", "getaddrinfo failed", "errno -2]", "errno -3]",
    "errno -5]", "errno 8]", "errno 11001]", "nameresolutionerror", "gaierror",
)
_REFUSED_MARKERS = ("connection refused", "errno 61]", "errno 111]", "errno 10061]")
_TIMEOUT_MARKERS = ("timed out", "timeout")
_TLS_MARKERS = ("certificate", "ssl", "tls", "wrong_version_number")


def unreachable_message(host: str, *, exc: BaseException | None = None, text: str = "",
                        seconds: float) -> str:
    """Why ``host`` could not be reached, from the failure's type and text.

    The text only chooses among fixed messages; none of it is returned.
    """
    types = _chain_types(exc)
    haystack = (text + " " + (_chain_text(exc) if exc else "")).lower()
    if socket.gaierror in types or any(marker in haystack for marker in _DNS_MARKERS):
        return f"Couldn't reach {host}: the name doesn't resolve."
    if ConnectionRefusedError in types or any(marker in haystack for marker in _REFUSED_MARKERS):
        return f"Couldn't reach {host}: the connection was refused."
    if any(issubclass(t, ssl.SSLError) for t in types) or any(m in haystack for m in _TLS_MARKERS):
        return f"Couldn't make a secure connection to {host}."
    if any(issubclass(t, TimeoutError) for t in types) or any(m in haystack for m in _TIMEOUT_MARKERS):
        return timed_out_message(host, seconds)
    return f"Couldn't reach {host}."


def timed_out_message(host: str, seconds: float) -> str:
    return f"{host} didn't answer within {_seconds(seconds)}."


def _seconds(seconds: float) -> str:
    whole = math.ceil(seconds) if seconds >= 1 else seconds
    return f"{whole:g} seconds" if whole != 1 else "1 second"


def is_read_timeout(exc: BaseException | None) -> bool:
    """The connection was made and the server did not answer in time.

    Matched on httpx's class name so this leaf takes no httpx import. A
    Nextcloud brute-force throttle looks like this: it delays its answer past
    the client's timeout, which ``_http`` otherwise reports like a refusal.
    """
    return any(t.__name__ == "ReadTimeout" for t in _chain_types(exc))
