"""Shared dependencies for API routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from istota.money.cli import Context, resolve_ledger


def get_ctx(request: Request) -> Context:
    ctx = request.app.state.ctx

    # auth_user is set by verify_api_key:
    #   None  = master key (admin), X-User header honored
    #   str   = derived key, scoped to that user
    auth_user = getattr(request.state, "auth_user", None)
    x_user = request.headers.get("X-User")

    if auth_user is not None:
        # Derived key auth — user is fixed, ignore X-User if it conflicts
        if x_user and x_user != auth_user:
            raise HTTPException(403, "API key is scoped to a different user")
        if auth_user not in ctx.users:
            raise HTTPException(400, f"Unknown user: {auth_user}")
        return ctx.for_user(auth_user)

    # Master key or no auth — honor X-User header
    if x_user:
        if x_user not in ctx.users:
            raise HTTPException(400, f"Unknown user: {x_user}")
        return ctx.for_user(x_user)

    if ctx.has_single_user:
        return ctx.for_default_user()
    raise HTTPException(400, "X-User header required (multi-user config)")


def get_ledger_path(ledger: str | None, ctx: Context) -> Path:
    return resolve_ledger(ledger, ctx.ledgers)
