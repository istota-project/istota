"""FastAPI application for Moneyman REST API."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from istota.money.api.auth import verify_api_key
from istota.money.cli import load_context


def create_app(config_path: str | None = None) -> FastAPI:
    """Create a FastAPI app with the given config."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = load_context(config_path)
        app.state.ctx = ctx
        yield

    app = FastAPI(
        title="Moneyman",
        lifespan=lifespan,
        dependencies=[Depends(verify_api_key)],
    )

    from istota.money.api import health, ledger, transactions, invoices, work

    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(ledger.router, prefix="/api", tags=["ledger"])
    app.include_router(transactions.router, prefix="/api", tags=["transactions"])
    app.include_router(invoices.router, prefix="/api", tags=["invoices"])
    app.include_router(work.router, prefix="/api/work", tags=["work"])

    return app


app = create_app()
