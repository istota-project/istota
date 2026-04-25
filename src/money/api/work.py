"""Work entry API routes."""

from __future__ import annotations

from datetime import datetime

import click
from fastapi import APIRouter, Depends, HTTPException, Query

from money.api.deps import get_ctx
from money.api.models import WorkEntryRequest, WorkEntryUpdateRequest
from money.cli import Context, _require_data_dir

router = APIRouter()


@router.get("/")
def list_entries(
    client: str | None = Query(None),
    period: str | None = Query(None),
    uninvoiced: bool = Query(False),
    invoiced: bool = Query(False),
    ctx: Context = Depends(get_ctx),
):
    from money.work import list_work_entries

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    invoiced_filter = None
    if uninvoiced:
        invoiced_filter = False
    elif invoiced:
        invoiced_filter = True

    entries = list_work_entries(data_dir, client=client, invoiced=invoiced_filter, period=period)

    return {
        "status": "ok",
        "count": len(entries),
        "entries": [
            {
                "id": e.id,
                "date": e.date.isoformat(),
                "client": e.client,
                "service": e.service,
                "qty": e.qty,
                "amount": e.amount,
                "discount": e.discount,
                "description": e.description,
                "entity": e.entity or None,
                "invoice": e.invoice or None,
                "paid_date": e.paid_date.isoformat() if e.paid_date else None,
            }
            for e in entries
        ],
    }


@router.post("/")
def add_entry(
    req: WorkEntryRequest,
    ctx: Context = Depends(get_ctx),
):
    from money.work import add_work_entry

    try:
        datetime.strptime(req.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    entry_id = add_work_entry(
        data_dir, req.date, req.client.lower(), req.service,
        qty=req.qty, amount=req.amount, discount=req.discount,
        description=req.description, entity=req.entity,
    )
    return {"status": "ok", "id": entry_id, "message": f"Added work entry #{entry_id}"}


@router.put("/{entry_id}")
def update_entry(
    entry_id: int,
    req: WorkEntryUpdateRequest,
    ctx: Context = Depends(get_ctx),
):
    from money.work import update_work_entry

    fields = {}
    if req.date is not None:
        fields["date"] = req.date
    if req.client is not None:
        fields["client"] = req.client.lower()
    if req.service is not None:
        fields["service"] = req.service
    if req.qty is not None:
        fields["qty"] = req.qty
    if req.amount is not None:
        fields["amount"] = req.amount
    if req.discount is not None:
        fields["discount"] = req.discount
    if req.description is not None:
        fields["description"] = req.description
    if req.entity is not None:
        fields["entity"] = req.entity
    if req.invoice is not None:
        fields["invoice"] = req.invoice

    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    if update_work_entry(data_dir, entry_id, **fields):
        return {"status": "ok", "message": f"Updated work entry #{entry_id}"}
    raise HTTPException(status_code=404, detail=f"Entry #{entry_id} not found or already invoiced")


@router.delete("/{entry_id}")
def delete_entry(
    entry_id: int,
    ctx: Context = Depends(get_ctx),
):
    from money.work import remove_work_entry

    try:
        data_dir = _require_data_dir(ctx)
    except click.ClickException as e:
        raise HTTPException(status_code=400, detail=str(e))

    if remove_work_entry(data_dir, entry_id):
        return {"status": "ok", "message": f"Removed work entry #{entry_id}"}
    raise HTTPException(status_code=404, detail=f"Entry #{entry_id} not found or already invoiced")
