"""The public route serving a user's Istota calendar feed: ``GET /istota/calendar/<token>.ics``.

No session and no origin check: a calendar app polls it with nothing but the
URL, and the token in the path is the whole credential (`sharing.events`).
It is outside ``/istota/api/``, so the API body guard does not apply either.

- An unknown, reset or malformed token is a 404, the same answer each way.
- Two in-memory limits, checked before the lookup: ``TOKEN_MAX_HITS`` an hour
  per token (keyed on its digest, so the limiter holds no token) and
  ``ADDRESS_MAX_HITS`` an hour per client address, which is known only when
  ``[web] trusted_proxy_hops`` is set (`webui.app._client_ip`). Over either,
  429. Not the login throttle, whose keys and release step fit a login.
- ``Cache-Control: private, max-age=300`` and an ETag over the bytes, with
  ``If-None-Match`` answered 304.

nginx serves the path from its own location with the access log off, since
the request line carries the token. The handler is a sync ``def``, so the
database read runs in the thread pool.

`current_config` and `client_address` are stubs `webui/app.py` overrides, so
the router mounts on a bare app in tests.
"""

from __future__ import annotations

import hashlib
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from istota.lib import tokens
from istota.lib.rate_limit import SlidingWindow
from istota.sharing import events

logger = logging.getLogger("istota.sharing.feed")

router = APIRouter()

WINDOW_SECONDS = 3600
TOKEN_MAX_HITS = 120
# Higher than the per-token limit: one address can be a provider polling many
# users' feeds (a hosted calendar app fetches from a shared pool).
ADDRESS_MAX_HITS = 600
_SUFFIX = ".ics"

_token_window = SlidingWindow(WINDOW_SECONDS, TOKEN_MAX_HITS)
_address_window = SlidingWindow(WINDOW_SECONDS, ADDRESS_MAX_HITS)

_PLAIN = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def reset_limits() -> None:
    """Forget every recorded hit; for tests."""
    global _token_window, _address_window
    _token_window = SlidingWindow(WINDOW_SECONDS, TOKEN_MAX_HITS)
    _address_window = SlidingWindow(WINDOW_SECONDS, ADDRESS_MAX_HITS)


def current_config():
    """The loaded config; `webui/app.py` overrides this."""
    return None


def client_address(request: Request) -> str | None:
    """The client's address when a trusted proxy names it; `webui/app.py` overrides this."""
    return None


def _not_found() -> Response:
    return Response("Not found", status_code=404, media_type="text/plain", headers=_PLAIN)


def _too_many() -> Response:
    return Response("Too many requests", status_code=429, media_type="text/plain",
                    headers={**_PLAIN, "Retry-After": "300"})


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = {part.strip().removeprefix("W/") for part in header.split(",")}
    return etag in candidates or "*" in candidates


@router.get("/istota/calendar/{name}", include_in_schema=False)
def calendar_feed(
    name: str,
    request: Request,
    config=Depends(current_config),
    address: str | None = Depends(client_address),
) -> Response:
    from istota import db

    if config is None or not config.db_path or not name.endswith(_SUFFIX):
        return _not_found()
    token = name[: -len(_SUFFIX)]
    if not events.well_formed_token(token):
        return _not_found()
    now = time.monotonic()
    if not _token_window.admit(tokens.hash_token(token), now):
        return _too_many()
    if address is not None and not _address_window.admit(address, now):
        return _too_many()
    try:
        with db.get_db(config.db_path) as conn:
            user_id = events.user_for_token(conn, token, touch=True)
            if user_id is None:
                return _not_found()
            body = events.render_feed(conn, config, user_id)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — a reader gets a plain 503, the log the type
        logger.error("calendar feed failed: %s", type(exc).__name__)
        return Response("Unavailable", status_code=503, media_type="text/plain", headers=_PLAIN)
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {
        "Cache-Control": "private, max-age=300",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="text/calendar; charset=utf-8", headers=headers)
