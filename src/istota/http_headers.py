"""One case-insensitive HTTP header lookup, for the webhook adapters.

HTTP header names are case-insensitive and no sender promises a casing, so
every webhook adapter grows its own copy of this — and each copy is reading the
header that carries a signature. It was written twice inside the SMS providers
before being consolidated into their shared `_types`, and the WhatsApp webhook
made it three.

A module of its own rather than a home in `transport/_types.py`, because the
SMS providers' `_types` is deliberately stdlib-only and importing anything
under `transport` would pull `transport/__init__` and with it email, talk, web,
ingest and routing. Same reasoning, and the same shape, as `ntfy_headers.py`.

stdlib-only leaf: imports nothing, raises nothing.
"""

from __future__ import annotations

from collections.abc import Mapping


def header_value(headers: Mapping[str, str], name: str) -> str:
    """The first header matching ``name``, case-insensitively, or ``""``.

    A missing header and an empty one are the same answer, deliberately: every
    caller treats both as "the sender did not supply this", and a signature
    check must refuse an empty string exactly as it refuses an absent one.
    """
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


__all__ = ["header_value"]
