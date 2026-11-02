"""What the per-user settings routers share: the refusal shape, the id parser, the config.

A refusal carries fixed text and never echoes a body, because a validation error echoes
its input and what a task wrote can carry data.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

NO_STORE = {"Cache-Control": "no-store"}
_MAX_ID_DIGITS = 18


def refusal(
    status: int, error: str, detail: str, field: str | None = None, *, state: str | None = None,
) -> JSONResponse:
    body = {"error": error, "detail": detail, "field": field}
    if state is not None:
        body["state"] = state
    return JSONResponse(body, status_code=status, headers=NO_STORE)


def not_found() -> JSONResponse:
    return refusal(404, "not_found", "There is no such item.")


def request_config(request: Request):
    config = getattr(request.app.state, "istota_config", None)
    if config is None or not getattr(config, "db_path", None):
        raise HTTPException(503, "Config not loaded")
    return config


def parse_id(text: str) -> int | None:
    """A row id from a path, or None. Taken as text, since a typed path parameter's
    validation error echoes what it was sent. Bounded, or SQLite cannot bind it."""
    if text.isascii() and text.isdigit() and len(text) <= _MAX_ID_DIGITS:
        return int(text)
    return None


def usable_external_skills(config, user_id: str) -> dict:
    """The `external` skills this user could use at all, name to metadata."""
    from istota.skills._loader import usable_external_skills as usable

    try:
        return usable(config, user_id)
    except Exception as exc:  # noqa: BLE001 — a page with no skills to offer still works
        logger.warning("egress_skills_unavailable: %s", type(exc).__name__)
        return {}
