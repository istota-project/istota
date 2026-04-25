"""Ledger API routes."""

from __future__ import annotations

import click
from fastapi import APIRouter, Depends, HTTPException, Query

from money.api.deps import get_ctx, get_ledger_path
from money.api.models import QueryRequest
from money.cli import Context

router = APIRouter()


@router.get("/ledgers")
def list_ledgers(ctx: Context = Depends(get_ctx)):
    if not ctx.ledgers:
        return {"status": "error", "error": "No ledgers configured"}
    return {
        "status": "ok",
        "ledger_count": len(ctx.ledgers),
        "ledgers": [{"name": e["name"]} for e in ctx.ledgers],
    }


@router.get("/check")
def check_ledger(
    ledger: str | None = Query(None),
    ctx: Context = Depends(get_ctx),
):
    from money.core.ledger import check

    try:
        path = get_ledger_path(ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return check(path)


@router.get("/balances")
def balances(
    account: str | None = Query(None),
    ledger: str | None = Query(None),
    ctx: Context = Depends(get_ctx),
):
    from money.core.ledger import balances as ledger_balances

    try:
        path = get_ledger_path(ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ledger_balances(path, account)


@router.post("/query")
def query(
    req: QueryRequest,
    ctx: Context = Depends(get_ctx),
):
    from money.core.ledger import query as ledger_query

    try:
        path = get_ledger_path(req.ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ledger_query(path, req.bql)


@router.get("/reports/{report_type}")
def report(
    report_type: str,
    year: int | None = Query(None),
    ledger: str | None = Query(None),
    ctx: Context = Depends(get_ctx),
):
    from money.core.ledger import report as ledger_report

    if report_type not in ("income-statement", "balance-sheet", "cash-flow"):
        raise HTTPException(status_code=400, detail=f"Unknown report type: {report_type}")

    try:
        path = get_ledger_path(ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ledger_report(path, report_type, year)


@router.get("/lots/{symbol}")
def lots(
    symbol: str,
    ledger: str | None = Query(None),
    ctx: Context = Depends(get_ctx),
):
    from money.core.ledger import lots as ledger_lots

    try:
        path = get_ledger_path(ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ledger_lots(path, symbol)


@router.get("/wash-sales")
def wash_sales(
    year: int | None = Query(None),
    ledger: str | None = Query(None),
    ctx: Context = Depends(get_ctx),
):
    from money.core.ledger import wash_sales as ledger_wash_sales

    try:
        path = get_ledger_path(ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ledger_wash_sales(path, year)
