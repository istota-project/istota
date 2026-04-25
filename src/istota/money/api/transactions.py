"""Transaction API routes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import click
from fastapi import APIRouter, Depends, HTTPException

from istota.money.api.deps import get_ctx, get_ledger_path
from istota.money.api.models import ImportCsvRequest, SyncMonarchRequest, TransactionRequest
from istota.money.cli import Context

router = APIRouter()


def _get_db_conn(ctx: Context):
    if not ctx.db_path:
        return None
    import sqlite3
    from istota.money.db import init_db
    ctx.db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(ctx.db_path)
    conn = sqlite3.connect(str(ctx.db_path))
    conn.row_factory = sqlite3.Row
    return conn


@router.post("/transactions")
def add_transaction(
    req: TransactionRequest,
    ctx: Context = Depends(get_ctx),
):
    from istota.money.core.transactions import add_transaction as core_add

    try:
        parsed_date = datetime.strptime(req.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        path = get_ledger_path(req.ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    return core_add(
        path, parsed_date, req.payee, req.narration,
        req.debit, req.credit, req.amount, req.currency,
    )


@router.post("/transactions/sync-monarch")
def sync_monarch(
    req: SyncMonarchRequest,
    ctx: Context = Depends(get_ctx),
):
    from istota.money.core.transactions import (
        parse_monarch_config,
        sync_all_profiles,
        sync_monarch as core_sync,
    )

    if not ctx.monarch_config_path:
        raise HTTPException(status_code=400, detail="No monarch_config set in config")
    if not ctx.monarch_config_path.exists():
        raise HTTPException(status_code=400, detail=f"Config not found: {ctx.monarch_config_path}")

    config = parse_monarch_config(ctx.monarch_config_path, secrets=ctx.secrets)
    db_conn = _get_db_conn(ctx)

    try:
        if req.ledger:
            try:
                path = get_ledger_path(req.ledger, ctx)
            except click.ClickException as e:
                raise HTTPException(status_code=400, detail=str(e))
            # Find profiles targeting this ledger
            matching = [p for p in config.profiles if p.ledger.lower() == req.ledger.lower()]
            if matching:
                import asyncio
                from istota.money.core.models import MonarchConfig as MC
                from istota.money.core.transactions import fetch_monarch_transactions
                lookback = max(p.sync.lookback_days for p in matching)
                txns = asyncio.run(fetch_monarch_transactions(config, lookback))
                results = []
                for profile in matching:
                    profile_config = MC(
                        credentials=config.credentials,
                        sync=profile.sync,
                        accounts=profile.accounts,
                        categories=profile.categories,
                        tags=profile.tags,
                    )
                    r = core_sync(
                        path, profile_config, db_conn=db_conn,
                        dry_run=req.dry_run, transactions=txns, profile=profile.name,
                    )
                    r["name"] = profile.name
                    r["ledger"] = profile.ledger
                    results.append(r)
                return {"status": "ok", "profiles": results}
            else:
                return core_sync(path, config, db_conn=db_conn, dry_run=req.dry_run)
        else:
            return sync_all_profiles(
                config, ctx.ledgers, db_conn=db_conn, dry_run=req.dry_run,
            )
    finally:
        if db_conn:
            db_conn.commit()
            db_conn.close()


@router.post("/transactions/import-csv")
def import_csv(
    req: ImportCsvRequest,
    ctx: Context = Depends(get_ctx),
):
    from istota.money.core.transactions import import_csv as core_import

    file_path = Path(req.file).resolve()
    if ctx.data_dir:
        allowed_dir = ctx.data_dir.resolve()
        if not file_path.is_relative_to(allowed_dir):
            raise HTTPException(
                status_code=400,
                detail="File path must be within the user's data directory",
            )
    if not file_path.exists():
        raise HTTPException(status_code=400, detail="File not found")

    try:
        path = get_ledger_path(req.ledger, ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    db_conn = _get_db_conn(ctx)

    try:
        return core_import(
            ledger_path=path, file_path=file_path, account=req.account,
            db_conn=db_conn, include_tags=req.include_tags,
            exclude_tags=req.exclude_tags,
        )
    finally:
        if db_conn:
            db_conn.commit()
            db_conn.close()
