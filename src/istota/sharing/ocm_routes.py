"""The public Open Cloud Mesh routes: discovery and ``POST /ocm/shares``.

Mounted at the root, outside ``/istota/api/``: no session, no origin check,
no API body guard. These are the web app's first fully unauthenticated
public routes; everything they do is in `sharing.ocm`.

- ``GET /ocm-provider`` and ``/ocm-provider/`` (the path Nextcloud reads,
  both spellings answered, no redirect) and ``GET /.well-known/ocm`` (for
  other OCM servers) return the discovery document.
- ``POST /ocm/shares`` takes an offer and ``POST /ocm/notifications`` a
  remote notification. Each body is read through the bounded reader and
  handled in a worker thread, since both make outbound requests.

**Every route answers 404 while ``[sharing.ocm] enabled`` is off**, read per
request, so a configuration reload that turns OCM off closes them at once.
nginx proxies the four locations on every install; with OCM off this is what
answers there.

`current_config` and `client_address` are stubs `webui/app.py` overrides, so
the router mounts on a bare app in tests.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from istota.sharing import ocm

logger = logging.getLogger("istota.sharing.ocm")

router = APIRouter()

_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def current_config():
    """The loaded config; `webui/app.py` overrides this."""
    return None


def client_address(request: Request) -> str | None:
    """The client's address when a trusted proxy names it; `webui/app.py` overrides this."""
    return None


def _enabled(config) -> bool:
    return config is not None and bool(config.db_path) and bool(config.sharing.ocm.enabled)


def _not_found() -> JSONResponse:
    # The framework's own 404 for a path with no route, so OCM off reads as absent.
    return JSONResponse({"detail": "Not Found"}, status_code=404, headers=_HEADERS)


def _discovery(config) -> JSONResponse:
    if not _enabled(config):
        return _not_found()
    try:
        document = ocm.discovery_document(config)
    except ValueError:
        # No [site] hostname: there is no endpoint to publish.
        return _not_found()
    return JSONResponse(document, headers=_HEADERS)


@router.get("/ocm-provider", include_in_schema=False)
def ocm_provider(config=Depends(current_config)) -> JSONResponse:
    return _discovery(config)


@router.get("/ocm-provider/", include_in_schema=False)
def ocm_provider_slash(config=Depends(current_config)) -> JSONResponse:
    return _discovery(config)


@router.get("/.well-known/ocm", include_in_schema=False)
def well_known_ocm(config=Depends(current_config)) -> JSONResponse:
    return _discovery(config)


def _receive(handler, config, body: bytes, headers: dict, address: str | None) -> ocm.OcmAnswer:
    from istota import db

    with db.get_db(config.db_path) as conn:
        return handler(conn, config, body, headers, client_address=address)


async def _take(request: Request, config, address: str | None, handler, max_bytes: int,
                invalid: str, what: str) -> JSONResponse:
    from istota.webui.request_guards import BodyError, read_bounded_body

    if not _enabled(config):
        return _not_found()
    try:
        body = await read_bounded_body(request, max_bytes, require_length=False)
    except BodyError as exc:
        return JSONResponse(
            {"message": invalid, "validationErrors": []},
            status_code=400 if exc.status != 413 else 413, headers=_HEADERS,
        )
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        answer = await asyncio.to_thread(_receive, handler, config, body, headers, address)
    except Exception as exc:  # noqa: BLE001 — a sender gets a fixed 500, the log the type
        logger.error("ocm %s failed: %s", what, type(exc).__name__)
        return JSONResponse({"message": "server_error", "validationErrors": []},
                            status_code=500, headers=_HEADERS)
    return JSONResponse(answer.body, status_code=answer.status, headers=_HEADERS)


@router.post("/ocm/shares", include_in_schema=False)
async def ocm_shares(
    request: Request,
    config=Depends(current_config),
    address: str | None = Depends(client_address),
) -> JSONResponse:
    return await _take(request, config, address, ocm.receive_share, ocm.OFFER_MAX_BYTES,
                       "invalid_offer", "offer")


@router.post("/ocm/notifications", include_in_schema=False)
async def ocm_notifications(
    request: Request,
    config=Depends(current_config),
    address: str | None = Depends(client_address),
) -> JSONResponse:
    return await _take(request, config, address, ocm.receive_notification,
                       ocm.NOTIFICATION_MAX_BYTES, "invalid_notification", "notification")
