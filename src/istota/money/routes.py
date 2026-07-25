"""FastAPI router for the money web API.

The host application (istota) mounts ``router`` at its chosen prefix and
overrides ``require_auth`` via ``app.dependency_overrides`` so that the
session/cookie/OIDC concerns stay with the host. Per-user data config is
resolved per request through :func:`istota.money.resolve_for_user`,
fed by the istota config attached to ``request.app.state.istota_config``.
"""

from __future__ import annotations

import logging
import math
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from istota.money._loader import UserNotFoundError, resolve_for_user
from istota.money.cli import UserContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth dependency — host app overrides via app.dependency_overrides
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> dict:
    """Return ``{"username": ..., "display_name": ...}`` or raise 401.

    Default reads ``request.session["user"]`` (Starlette SessionMiddleware).
    Istota overrides this with its own ``_require_api_auth``.
    """
    user = None
    try:
        user = request.session.get("user")
    except (AssertionError, AttributeError):
        # No SessionMiddleware installed.
        pass
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def verify_origin(request: Request) -> None:
    """CSRF check stub for mutating routes — host overrides via dependency_overrides.

    Default is a no-op so the router stays usable in isolation (tests). The host
    app installs a real Origin/Referer check. Same shape as ``require_auth``.
    """
    return None


def get_user_config(
    request: Request,
    user: dict = Depends(require_auth),
) -> UserContext:
    istota_config = getattr(request.app.state, "istota_config", None)
    try:
        return resolve_for_user(user["username"], istota_config)
    except UserNotFoundError:
        raise HTTPException(404, "user not configured")


def _resolve_today(request: Request, username: str):
    """Return today's date in the user's configured timezone.

    Estimated-tax payment quarters hinge on the date (e.g. the Q2 payment is due
    June 15). Resolving the quarter from the server's UTC clock pushed a
    Pacific-time user past the boundary a day early — on June 15 evening Pacific
    the server already saw June 16 and jumped to Q3. Use the user's own tz.
    """
    from datetime import date, datetime

    istota_config = getattr(request.app.state, "istota_config", None)
    tz_name = "UTC"
    if istota_config is not None:
        try:
            uc = istota_config.get_user(username)
            if uc is not None and getattr(uc, "timezone", None):
                tz_name = uc.timezone
        except Exception:
            pass
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return date.today()


def _load_invoicing_config(user_ctx: UserContext):
    """Load invoicing config, preferring DB over the legacy TOML path."""
    from istota.money import config_store
    from istota.money.core.invoicing import parse_invoicing_config

    db_path = getattr(user_ctx, "db_path", None)
    if db_path is not None and config_store.has_invoicing_data(db_path):
        return config_store.load_invoicing(db_path)
    if user_ctx.invoicing_config_path and user_ctx.invoicing_config_path.exists():
        return parse_invoicing_config(user_ctx.invoicing_config_path)
    return None


def _load_tax_config(user_ctx: UserContext):
    """Load tax config, preferring DB over the legacy TOML path."""
    from istota.money import config_store
    from istota.money.core.tax import parse_tax_config

    db_path = getattr(user_ctx, "db_path", None)
    if db_path is not None and config_store.has_tax_data(db_path):
        return config_store.load_tax(db_path)
    if user_ctx.tax_config_path and user_ctx.tax_config_path.exists():
        return parse_tax_config(user_ctx.tax_config_path)
    return None


def _resolve_user_ledger(user_ctx: UserContext, ledger_name: str | None):
    if not user_ctx.ledgers:
        return None
    if ledger_name:
        for entry in user_ctx.ledgers:
            if entry["name"].lower() == ledger_name.lower():
                return entry["path"]
        return None
    return user_ctx.ledgers[0]["path"]


# ---------------------------------------------------------------------------
# Router — caller chooses the prefix
# ---------------------------------------------------------------------------


router = APIRouter()


@router.get("/me")
async def api_me(user: dict = Depends(require_auth)):
    return {
        "username": user["username"],
        "display_name": user.get("display_name", user["username"]),
    }


@router.get("/accounts")
async def api_accounts(
    ledger: str | None = None,
    year: int | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Return account tree with balances for the authenticated user."""
    from istota.money.core.ledger import list_open_accounts, run_bean_query

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    where = f" WHERE year = {int(year)}" if year else ""
    bql = f"SELECT account, sum(position){where} GROUP BY account ORDER BY account"

    try:
        rows = run_bean_query(ledger_path, bql)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    # On the all-time view, surface accounts that have been opened but
    # not yet posted to. Without this, a freshly-seeded ledger renders
    # as "No accounts found" because BQL aggregates over postings only.
    if year is None:
        seen = {r["account"] for r in rows}
        try:
            extra = list_open_accounts(ledger_path)
        except Exception:
            extra = []
        for acct in extra:
            if acct not in seen:
                rows.append({"account": acct, "sum(position)": ""})
        rows.sort(key=lambda r: r["account"])
    return {"status": "ok", "accounts": rows}


@router.get("/transactions")
async def api_transactions(
    ledger: str | None = None,
    account: str | None = None,
    year: int | None = None,
    filter: str | None = None,
    page: int = 1,
    per_page: int = 100,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import run_bean_query, _sanitize_bql_string

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    conditions = []
    if account:
        safe = _sanitize_bql_string(account)
        conditions.append(f"account ~ '{safe}'")
    else:
        conditions.append("account ~ '^(Income|Expenses):'")
    if year:
        conditions.append(f"year = {int(year)}")
    if filter:
        safe = _sanitize_bql_string(filter)
        if safe.startswith("#"):
            tag = safe[1:]
            conditions.append(f"'{tag}' IN tags")
        else:
            conditions.append(f"(payee ~ '{safe}' OR narration ~ '{safe}')")

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    bql = (
        f"SELECT date, flag, payee, narration, account, position, tags,"
        f" entry_meta('id') as id"
        f"{where} ORDER BY date DESC"
    )

    try:
        rows = run_bean_query(ledger_path, bql)
        total = len(rows)
        start = (page - 1) * per_page
        end = start + per_page
        return {
            "status": "ok",
            "transactions": rows[start:end],
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/postings")
async def api_postings(
    date: str,
    payee: str = "",
    narration: str = "",
    account: str = "",
    position: str = "",
    ledger: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import run_bean_query, _sanitize_bql_string

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    conditions = [f"date = {date}"]
    if payee:
        safe = _sanitize_bql_string(payee)
        conditions.append(f"payee = '{safe}'")
    if narration:
        safe = _sanitize_bql_string(narration)
        conditions.append(f"narration = '{safe}'")

    where = " WHERE " + " AND ".join(conditions)
    need_grouping = bool(account and position)
    select_cols = (
        "account, position, filename, entry_meta('lineno') as txn_line"
        if need_grouping else "account, position"
    )
    bql = f"SELECT {select_cols}{where} ORDER BY account"

    try:
        rows = run_bean_query(ledger_path, bql)
        if need_grouping:
            from collections import defaultdict
            groups: dict[tuple, list] = defaultdict(list)
            for row in rows:
                key = (row.get("filename", ""), row.get("txn_line", ""))
                groups[key].append({"account": row["account"], "position": row["position"]})
            for postings in groups.values():
                if any(
                    p["account"].strip() == account.strip()
                    and p["position"].strip() == position.strip()
                    for p in postings
                ):
                    return {"status": "ok", "postings": postings}
        return {"status": "ok", "postings": rows}
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.post("/transactions/update")
async def api_transaction_update(
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Edit a transaction in place, located by its stable ``id:`` metadata.

    Body: ``{id, old_account, old_position, new_date?, new_payee?,
    new_narration?, new_account?, new_position?, ledger?}``. Re-validated with
    ``bean-check``; an edit that produces an invalid ledger is rolled back and
    surfaced as a 422.
    """
    from istota.money.core.edit import edit_transaction

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    txn_id = (body.get("id") or "").strip()
    if not txn_id:
        return JSONResponse(
            {"status": "error", "error": "missing transaction id"}, status_code=400
        )

    ledger_path = _resolve_user_ledger(user_ctx, body.get("ledger"))
    if not ledger_path:
        return JSONResponse({"status": "error", "error": "ledger not found"}, status_code=404)

    result = edit_transaction(
        ledger_path,
        txn_id,
        old_account=body.get("old_account"),
        old_position=body.get("old_position"),
        new_date=body.get("new_date"),
        new_payee=body.get("new_payee"),
        new_narration=body.get("new_narration"),
        new_account=body.get("new_account"),
        new_position=body.get("new_position"),
    )

    if result.get("status") == "ok":
        return result
    error = (result.get("error") or "").lower()
    if "not found" in error:
        return JSONResponse(result, status_code=404)
    # Validation failure (rolled back) or bad posting selector.
    return JSONResponse(result, status_code=422)


@router.get("/report/{report_type}")
async def api_report(
    report_type: str,
    ledger: str | None = None,
    year: int | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import report

    if report_type not in ("income-statement", "balance-sheet", "cash-flow"):
        return JSONResponse({"error": "unknown report type"}, status_code=400)

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    try:
        return report(ledger_path, report_type, year)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/check")
async def api_check(
    ledger: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.ledger import check

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    try:
        return check(ledger_path)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@router.get("/ledgers")
async def api_ledgers(user_ctx: UserContext = Depends(get_user_config)):
    return {"ledgers": [e["name"] for e in user_ctx.ledgers]}


@router.get("/clients")
async def api_clients(user_ctx: UserContext = Depends(get_user_config)):
    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return {"status": "ok", "clients": []}

    clients = []
    for key, c in config.clients.items():
        entity = config.companies.get(c.entity or config.default_entity, config.company)
        clients.append({
            "key": key,
            "name": c.name,
            "email": c.email,
            "address": c.address,
            "terms": c.terms,
            "entity": c.entity or config.default_entity,
            "entity_name": entity.name,
            "schedule": c.schedule,
            "schedule_day": c.schedule_day,
            "ar_account": c.ar_account or config.default_ar_account,
        })
    return {"status": "ok", "clients": clients}


@router.get("/invoices")
async def api_invoices(
    client: str | None = None,
    show_all: bool = False,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.invoicing import build_line_items
    from istota.money.work import get_invoice_numbers, get_entries_for_invoice

    data_dir = user_ctx.data_dir
    if not data_dir:
        return {"status": "ok", "invoices": [], "invoice_count": 0, "outstanding_count": 0}

    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return {"status": "ok", "invoices": [], "invoice_count": 0, "outstanding_count": 0}

    invoice_numbers = get_invoice_numbers(data_dir)
    invoices = []
    for inv_num in invoice_numbers:
        inv_entries = get_entries_for_invoice(data_dir, inv_num)
        if not inv_entries:
            continue
        if client and not any(e.client == client for e in inv_entries):
            continue

        items = build_line_items(inv_entries, config.services)
        total = sum(item.amount for item in items)
        is_paid = all(e.paid_date is not None for e in inv_entries)
        paid_date_val = inv_entries[0].paid_date

        if is_paid and not show_all:
            continue

        client_key = inv_entries[0].client
        client_config = config.clients.get(client_key)
        client_name = client_config.name if client_config else client_key
        inv_date = min(e.date for e in inv_entries)

        invoice_info = {
            "invoice_number": inv_num,
            "client": client_name,
            "client_key": client_key,
            "date": inv_date.isoformat(),
            "total": round(total, 2),
            "status": "paid" if is_paid else "outstanding",
        }
        if is_paid and paid_date_val:
            invoice_info["paid_date"] = paid_date_val.isoformat()
        invoices.append(invoice_info)

    outstanding = [i for i in invoices if i["status"] == "outstanding"]
    return {
        "status": "ok",
        "invoice_count": len(invoices),
        "outstanding_count": len(outstanding),
        "invoices": invoices,
    }


@router.get("/business-settings")
async def api_business_settings(user_ctx: UserContext = Depends(get_user_config)):
    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return {"status": "ok", "entities": [], "services": [], "defaults": {}}

    entities = [{
        "key": key,
        "name": c.name,
        "address": c.address,
        "email": c.email,
        "payment_instructions": c.payment_instructions,
        "logo": c.logo,
        "ar_account": c.ar_account,
        "bank_account": c.bank_account,
        "currency": c.currency,
    } for key, c in config.companies.items()]

    services = [{
        "key": key,
        "display_name": s.display_name,
        "rate": s.rate,
        "type": s.type,
        "income_account": s.income_account,
    } for key, s in config.services.items()]

    defaults = {
        "currency": config.currency,
        "default_entity": config.default_entity,
        "default_ar_account": config.default_ar_account,
        "default_bank_account": config.default_bank_account,
        "invoice_output": config.invoice_output,
        "next_invoice_number": config.next_invoice_number,
        "notifications": config.notifications,
        "days_until_overdue": config.days_until_overdue,
    }
    return {"status": "ok", "entities": entities, "services": services, "defaults": defaults}


@router.get("/invoice-details")
async def api_invoice_details(
    invoice_number: str,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.invoicing import build_line_items
    from istota.money.work import get_entries_for_invoice

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"error": "no data dir"}, status_code=404)

    try:
        config = _load_invoicing_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return JSONResponse({"error": "no invoicing config"}, status_code=404)

    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        return JSONResponse({"error": "invoice not found"}, status_code=404)

    service_entries = [e for e in entries if e.service != "_manual"]
    items = build_line_items(service_entries, config.services)

    for e in entries:
        if e.service == "_manual":
            from istota.money.core.models import InvoiceLineItem
            items.append(InvoiceLineItem(
                display_name=e.description or "Manual item",
                description="",
                quantity=1,
                rate=e.amount or 0,
                discount=0,
                amount=e.amount or 0,
            ))

    return {
        "status": "ok",
        "invoice_number": invoice_number,
        "items": [{
            "description": item.display_name,
            "detail": item.description,
            "quantity": item.quantity,
            "rate": round(item.rate, 2),
            "discount": round(item.discount, 2),
            "amount": round(item.amount, 2),
        } for item in items],
    }


@router.post("/invoices/{invoice_number}/mark-paid")
async def api_invoice_mark_paid(
    invoice_number: str,
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Record payment for an invoice (sets paid_date on its work entries).

    Web-level toggle only — sets paid_date, does not post a bank payment
    transaction to the ledger (the CLI's ``invoice paid --bank`` path is
    separate and unchanged).
    """
    from datetime import date

    from istota.money.work import (
        get_entries_for_invoice,
        record_invoice_payment,
    )

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    if not get_entries_for_invoice(data_dir, invoice_number):
        return JSONResponse({"status": "error", "error": "invoice not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    paid_date = (body or {}).get("paid_date") or date.today().isoformat()

    count = record_invoice_payment(data_dir, invoice_number, paid_date)
    return {
        "status": "ok",
        "invoice_number": invoice_number,
        "paid_date": paid_date,
        "count": count,
    }


@router.post("/invoices/{invoice_number}/mark-pending")
async def api_invoice_mark_pending(
    invoice_number: str,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Un-pay an invoice (clears paid_date, keeps the invoice number)."""
    from istota.money.work import clear_invoice_payment, get_entries_for_invoice

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    if not get_entries_for_invoice(data_dir, invoice_number):
        return JSONResponse({"status": "error", "error": "invoice not found"}, status_code=404)

    count = clear_invoice_payment(data_dir, invoice_number)
    return {"status": "ok", "invoice_number": invoice_number, "count": count}


@router.get("/invoices/{invoice_number}/pdf")
async def api_invoice_pdf(
    invoice_number: str,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Stream the generated PDF for an invoice, or 404 if not on disk."""
    from istota.money.core.invoicing import find_invoice_pdf

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    # Prefer the invoicing config's output dir; fall back to the conventional
    # location. config.invoice_output defaults to "invoices/generated".
    invoice_output_dir = data_dir / "invoices" / "generated"
    try:
        config = _load_invoicing_config(user_ctx)
        if config is not None and getattr(config, "invoice_output", None):
            invoice_output_dir = data_dir / config.invoice_output
    except Exception:
        # Fall back to the default location, but make the miss visible — a
        # custom invoice_output we couldn't read would otherwise 404 silently.
        logger.warning(
            "invoice pdf: could not load invoicing config for %s, using default output dir",
            invoice_number,
            exc_info=True,
        )

    pdf = find_invoice_pdf(invoice_output_dir, invoice_number)
    if pdf is None:
        return JSONResponse({"status": "error", "error": "pdf not found"}, status_code=404)

    return FileResponse(
        path=str(pdf),
        media_type="application/pdf",
        filename=pdf.name,
    )


# =============================================================================
# Work entries
# =============================================================================

# Fields a client may set on create/update. Identity (``uid``) and the
# invoicing lifecycle (``invoice`` / ``paid_date``) are owned by the store and
# the invoicing path respectively — an edit form must not be able to stamp an
# invoice number or mark work paid.
_WORK_WRITABLE_FIELDS = (
    "date", "client", "service", "qty", "amount", "discount", "description", "entity",
)

_WORK_STATUSES = ("uninvoiced", "invoiced", "paid", "all")

# Free-text fields, and the subset that must carry an actual value. The store
# writes these into a TOML basic string, so a non-string here is a traceback
# rather than the documented error envelope.
_WORK_STRING_FIELDS = ("client", "service", "description", "entity")
_WORK_REQUIRED_STRING_FIELDS = ("client", "service")

# Control characters other than newline. The serializer escapes these now, but
# there's no legitimate reason for one to arrive in a work entry, and refusing
# at the boundary keeps the corruption class dead even if the escaping regresses.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def _work_row(entry, config) -> dict:
    """Enrich a stored work entry into the web row shape.

    ``computed_amount`` runs the same rate resolution invoice generation does
    (``entry_line_item``), so for an uninvoiced entry it is the number that
    will actually appear on the invoice. For an *invoiced* one it is a
    reconstruction at today's rate: nothing stores what an entry was billed
    for (the invoice list re-derives its totals the same way), so a rate change
    retroactively reprices historical rows. The UI marks those as computed at
    the current rate rather than presenting them as the billed figure.
    """
    from istota.money.core.invoicing import entry_line_item
    from istota.money.work import entry_etag

    warnings: list[str] = []
    if not entry.uid:
        warnings.append("no_uid")

    svc = config.services.get(entry.service) if config else None
    client_config = config.clients.get(entry.client) if config else None
    if config is not None:
        if svc is None:
            warnings.append("unknown_service")
        if client_config is None:
            warnings.append("unknown_client")

    computed = None
    if svc is not None:
        computed = round(entry_line_item(entry, svc).amount, 2)

    return {
        "uid": entry.uid,
        "index": entry.id,
        "etag": entry_etag(entry),
        "date": entry.date.isoformat(),
        "client": entry.client,
        "client_name": client_config.name if client_config else entry.client,
        "service": entry.service,
        "service_name": svc.display_name if svc else entry.service,
        "service_type": svc.type if svc else "",
        "qty": entry.qty,
        "amount": entry.amount,
        "discount": entry.discount,
        "description": entry.description,
        "entity": entry.entity,
        "invoice": entry.invoice,
        "paid_date": entry.paid_date.isoformat() if entry.paid_date else None,
        "computed_amount": computed,
        "editable": bool(entry.uid) and not entry.invoice,
        "warnings": warnings,
    }


def _work_totals(rows: list[dict]) -> dict:
    """Bucket counts for the toolbar summary.

    Computed over the client/period-filtered set but *before* the status
    filter, so "4 uninvoiced · $1,800" stays true while you're looking at the
    paid bucket.
    """
    uninvoiced = [r for r in rows if not r["invoice"]]
    return {
        "uninvoiced_count": len(uninvoiced),
        "uninvoiced_amount": round(sum(r["computed_amount"] or 0 for r in uninvoiced), 2),
        "invoiced_count": len([r for r in rows if r["invoice"]]),
        "paid_count": len([r for r in rows if r["paid_date"]]),
    }


def _work_config_or_none(user_ctx: UserContext):
    """Invoicing config for service/client resolution, or None if unreadable.

    A broken or absent config must not make work entries invisible — the
    entries are the record of work done. It only costs display names and
    computed amounts.
    """
    try:
        return _load_invoicing_config(user_ctx)
    except Exception as e:
        logger.warning("work_invoicing_config_unreadable error=%s", e)
        return None


def _work_entry_response(data_dir, uid: str, config) -> dict | None:
    """Re-read an entry by uid and render its row (fresh display index)."""
    from istota.money.work import load_work_entries

    for entry in load_work_entries(data_dir):
        if entry.uid and entry.uid == uid:
            return _work_row(entry, config)
    return None


def _work_mutation_error(result, config) -> JSONResponse:
    """Map a store mutation result onto the right status code."""
    if result.status == "not_found":
        return JSONResponse({"status": "error", "error": "entry not found"}, status_code=404)
    if result.status == "invoiced":
        return JSONResponse(
            {"status": "error", "error": "entry is invoiced"}, status_code=409,
        )
    if result.status == "conflict":
        return JSONResponse(
            {
                "status": "error",
                "error": "entry changed",
                "entry": _work_row(result.entry, config) if result.entry else None,
            },
            status_code=409,
        )
    return JSONResponse(
        {"status": "error", "error": "no fields to update"}, status_code=400,
    )


def _coerce_work_fields(body: dict) -> tuple[dict, str | None]:
    """Pull the writable subset out of a request body, or return an error."""
    from datetime import datetime

    fields: dict = {}
    for key in _WORK_WRITABLE_FIELDS:
        if key not in body:
            continue
        fields[key] = body[key]

    if "date" in fields:
        raw = fields["date"]
        if not isinstance(raw, str):
            return {}, "invalid date"
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return {}, "invalid date format — use YYYY-MM-DD"

    for key in _WORK_STRING_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if not isinstance(value, str):
            return {}, f"invalid {key}"
        if _CONTROL_CHARS_RE.search(value):
            return {}, f"invalid {key} — control characters are not allowed"
        if key in _WORK_REQUIRED_STRING_FIELDS:
            # Normalize here so create and update agree: a blank client was
            # refused on create and silently stored on update.
            value = value.strip()
            if not value:
                return {}, f"{key} is required"
        fields[key] = value

    for key in ("qty", "amount", "discount"):
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            # qty/amount are genuinely nullable ("this service doesn't use
            # one"); discount is not — a null there would break every amount
            # computation downstream, so read it as "no discount".
            if key == "discount":
                fields[key] = 0
            continue
        if isinstance(value, bool):
            return {}, f"invalid {key}"
        if not isinstance(value, (int, float)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return {}, f"invalid {key}"
        # NaN / ±inf reach here from a JSON literal, from float("inf"), and
        # from an overflowing "1e400" — all of which blow up in the
        # serializer's int() conversion rather than storing anything sane.
        if isinstance(value, float) and not math.isfinite(value):
            return {}, f"invalid {key}"
        fields[key] = value

    return fields, None


@router.get("/work")
async def api_work_list(
    client: str | None = None,
    period: str | None = None,
    status: str = "all",
    user_ctx: UserContext = Depends(get_user_config),
):
    """List work entries, enriched with display names and computed amounts."""
    from istota.money.work import load_work_entries

    if status not in _WORK_STATUSES:
        return JSONResponse(
            {"status": "error", "error": f"unknown status: {status}"}, status_code=400,
        )

    data_dir = user_ctx.data_dir
    if not data_dir:
        return {"status": "ok", "entries": [], "totals": _work_totals([])}

    config = _work_config_or_none(user_ctx)
    entries = load_work_entries(data_dir)
    if client:
        wanted = client.lower()
        entries = [e for e in entries if e.client.lower() == wanted]
    if period:
        entries = [e for e in entries if e.date.isoformat().startswith(period)]

    rows = [_work_row(e, config) for e in entries]
    totals = _work_totals(rows)

    if status == "uninvoiced":
        rows = [r for r in rows if not r["invoice"]]
    elif status == "invoiced":
        rows = [r for r in rows if r["invoice"] and not r["paid_date"]]
    elif status == "paid":
        rows = [r for r in rows if r["paid_date"]]

    return {"status": "ok", "entries": rows, "totals": totals}


@router.post("/work")
async def api_work_create(
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Create a work entry."""
    from istota.money.core.ids import new_txn_id
    from istota.money.work import add_work_entry

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    fields, err = _coerce_work_fields(body)
    if err:
        return JSONResponse({"status": "error", "error": err}, status_code=400)

    # _coerce_work_fields has already stripped and non-empty-checked these;
    # what's left is the "absent from the body entirely" case.
    entry_date = fields.get("date")
    client_key = fields.get("client", "")
    service_key = fields.get("service", "")
    if not entry_date or not client_key or not service_key:
        return JSONResponse(
            {"status": "error", "error": "date, client and service are required"},
            status_code=400,
        )

    config = _work_config_or_none(user_ctx)
    # An entry whose service isn't configured is silently dropped at invoice
    # time — the work is recorded and never billed. Refuse it at the door.
    if config is not None and service_key not in config.services:
        return JSONResponse(
            {"status": "error", "error": f"unknown service: {service_key}"},
            status_code=400,
        )

    uid = new_txn_id()
    add_work_entry(
        data_dir,
        entry_date,
        client_key,
        service_key,
        qty=fields.get("qty"),
        amount=fields.get("amount"),
        discount=fields.get("discount") or 0,
        description=fields.get("description") or "",
        entity=fields.get("entity") or "",
        uid=uid,
    )

    row = _work_entry_response(data_dir, uid, config)
    if row is None:
        return JSONResponse(
            {"status": "error", "error": "entry not readable after write"}, status_code=500,
        )
    return {"status": "ok", "entry": row}


@router.patch("/work/{uid}")
async def api_work_update(
    uid: str,
    request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Update an uninvoiced work entry, addressed by its stable uid."""
    from istota.money.work import update_work_entry_by_uid

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    fields, err = _coerce_work_fields(body)
    if err:
        return JSONResponse({"status": "error", "error": err}, status_code=400)
    if not fields:
        return JSONResponse(
            {"status": "error", "error": "no fields to update"}, status_code=400,
        )

    config = _work_config_or_none(user_ctx)
    service_key = fields.get("service")
    if service_key is not None and config is not None and service_key not in config.services:
        return JSONResponse(
            {"status": "error", "error": f"unknown service: {service_key}"},
            status_code=400,
        )

    result = update_work_entry_by_uid(
        data_dir, uid, expect_etag=body.get("etag") or None, **fields,
    )
    if not result.ok:
        return _work_mutation_error(result, config)

    # The mutation result carries the entry re-read *inside* the write lock,
    # with a display index that already accounts for a date move. Re-reading
    # here instead would return None if the entry vanished in between, and the
    # declared client type has no null case.
    return {"status": "ok", "entry": _work_row(result.entry, config)}


@router.delete("/work/{uid}")
async def api_work_delete(
    uid: str,
    etag: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Delete an uninvoiced work entry, addressed by its stable uid."""
    from istota.money.work import remove_work_entry_by_uid

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"status": "error", "error": "no data dir"}, status_code=404)

    result = remove_work_entry_by_uid(data_dir, uid, expect_etag=etag or None)
    if not result.ok:
        return _work_mutation_error(result, _work_config_or_none(user_ctx))
    return {"status": "ok", "uid": uid}


@router.get("/tax/estimate")
async def api_tax_estimate(
    request: Request,
    ledger: str | None = None,
    method: str = "annualized",
    quarter: int | None = None,
    year: int | None = None,
    user: dict = Depends(require_auth),
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money.core.models import TaxConfig
    from istota.money.core.tax import (
        annualization_months,
        estimate_quarterly_tax,
        load_tax_inputs,
        payment_quarter_from_date,
        query_se_income,
    )

    try:
        config = _load_tax_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return JSONResponse({"error": "no tax config"}, status_code=404)

    saved = load_tax_inputs(user_ctx.db_path)

    tax_year = year or config.tax_year
    today = _resolve_today(request, user["username"])
    current_quarter = quarter or payment_quarter_from_date(today, tax_year)
    months = annualization_months(current_quarter, tax_year, today)
    use_method = method if method != "annualized" else saved.get("method", method)

    se_income_ytd = 0.0
    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if ledger_path:
        try:
            config_for_query = TaxConfig(
                **{**config.__dict__, "tax_year": tax_year}
            ) if tax_year != config.tax_year else config
            se_income_ytd = query_se_income(ledger_path, config_for_query, months)
        except Exception:
            pass

    def _val(key, fallback):
        v = saved.get(key)
        return v if v is not None else fallback

    result = estimate_quarterly_tax(
        se_income_ytd=se_income_ytd,
        w2_income=_val("w2_income", config.w2_income),
        w2_federal_withholding=_val("w2_federal_withholding", config.w2_federal_withholding),
        w2_state_withholding=_val("w2_state_withholding", config.w2_state_withholding),
        federal_estimated_paid=_val("federal_estimated_paid", config.federal_estimated_paid),
        state_estimated_paid=_val("state_estimated_paid", config.state_estimated_paid),
        filing_status=config.filing_status,
        tax_year=tax_year,
        method=use_method,
        prior_year_federal_tax=config.prior_year_federal_tax,
        prior_year_state_tax=config.prior_year_state_tax,
        enable_qbi=config.enable_qbi_deduction,
        current_quarter=current_quarter,
        w2_months=saved.get("w2_months", 12),
        income_months=months,
        config=config,
    )
    return {"status": "ok", **result.__dict__}


@router.post("/tax/estimate")
async def api_tax_estimate_recalculate(
    request: Request,
    ledger: str | None = None,
    user: dict = Depends(require_auth),
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money.core.models import TaxConfig
    from istota.money.core.tax import (
        annualization_months,
        estimate_quarterly_tax,
        payment_quarter_from_date,
        query_se_income,
        save_tax_inputs,
    )

    try:
        config = _load_tax_config(user_ctx)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
    if config is None:
        return JSONResponse({"error": "no tax config"}, status_code=404)

    body = await request.json()
    tax_year = body.get("year", config.tax_year)
    method = body.get("method", "annualized")
    today = _resolve_today(request, user["username"])
    current_quarter = body.get("quarter") or payment_quarter_from_date(today, tax_year)
    months = annualization_months(current_quarter, tax_year, today)

    def _bval(key, fallback):
        v = body.get(key)
        return v if v is not None else fallback

    w2_income = _bval("w2_income", config.w2_income)
    w2_fed_wh = _bval("w2_federal_withholding", config.w2_federal_withholding)
    w2_state_wh = _bval("w2_state_withholding", config.w2_state_withholding)
    fed_est_paid = _bval("federal_estimated_paid", config.federal_estimated_paid)
    state_est_paid = _bval("state_estimated_paid", config.state_estimated_paid)
    w2_months = _bval("w2_months", 12)

    save_tax_inputs(user_ctx.db_path, {
        "method": method,
        "w2_income": w2_income,
        "w2_federal_withholding": w2_fed_wh,
        "w2_state_withholding": w2_state_wh,
        "federal_estimated_paid": fed_est_paid,
        "state_estimated_paid": state_est_paid,
        "w2_months": w2_months,
    })

    se_income_ytd = 0.0
    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if ledger_path:
        try:
            config_for_query = TaxConfig(
                **{**config.__dict__, "tax_year": tax_year}
            ) if tax_year != config.tax_year else config
            se_income_ytd = query_se_income(ledger_path, config_for_query, months)
        except Exception:
            pass

    result = estimate_quarterly_tax(
        se_income_ytd=se_income_ytd,
        w2_income=w2_income,
        w2_federal_withholding=w2_fed_wh,
        w2_state_withholding=w2_state_wh,
        federal_estimated_paid=fed_est_paid,
        state_estimated_paid=state_est_paid,
        filing_status=config.filing_status,
        tax_year=tax_year,
        method=method,
        prior_year_federal_tax=config.prior_year_federal_tax,
        prior_year_state_tax=config.prior_year_state_tax,
        enable_qbi=config.enable_qbi_deduction,
        current_quarter=current_quarter,
        w2_months=w2_months,
        income_months=months,
        config=config,
    )
    return {"status": "ok", **result.__dict__}


# =============================================================================
# Config CRUD routes (DB-backed money config — invoicing / tax / monarch)
# =============================================================================


def _client_to_dict(c) -> dict:
    return {
        "key": c.key, "name": c.name, "address": c.address, "email": c.email,
        "terms": c.terms, "ar_account": c.ar_account, "entity": c.entity,
        "schedule": c.schedule, "schedule_day": c.schedule_day,
        "reminder_days": c.reminder_days, "notifications": c.notifications,
        "days_until_overdue": c.days_until_overdue,
        "ledger_posting": c.ledger_posting,
        "bundles": c.bundles, "separate": c.separate,
    }


def _company_to_dict(c) -> dict:
    return {
        "key": c.key, "name": c.name, "address": c.address, "email": c.email,
        "payment_instructions": c.payment_instructions, "logo": c.logo,
        "ar_account": c.ar_account, "bank_account": c.bank_account,
        "currency": c.currency,
    }


def _service_to_dict(s) -> dict:
    return {
        "key": s.key, "display_name": s.display_name, "rate": s.rate,
        "type": s.type, "income_account": s.income_account,
    }


@router.get("/config/invoicing")
async def api_config_invoicing(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "settings": {
            "accounting_path": cfg.accounting_path,
            "invoice_output": cfg.invoice_output,
            "next_invoice_number": cfg.next_invoice_number,
            "default_entity": cfg.default_entity,
            "currency": cfg.currency,
            "default_ar_account": cfg.default_ar_account,
            "default_bank_account": cfg.default_bank_account,
            "notifications": cfg.notifications,
            "days_until_overdue": cfg.days_until_overdue,
        },
    }


@router.put("/config/invoicing")
async def api_config_invoicing_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Update scalar invoicing settings.

    Body: a JSON object with any of the scalar setting keys. Unknown keys
    are rejected. Collection edits go through the per-entity routes below.
    """
    from istota.money import config_store
    body = await request.json()
    cfg = config_store.load_invoicing(user_ctx.db_path)
    allowed = {
        "accounting_path", "invoice_output", "next_invoice_number",
        "default_entity", "currency", "default_ar_account",
        "default_bank_account", "notifications", "days_until_overdue",
    }
    bad = set(body) - allowed
    if bad:
        return JSONResponse(
            {"status": "error", "error": f"unknown keys: {sorted(bad)}"},
            status_code=400,
        )
    for k, v in body.items():
        setattr(cfg, k, v)
    config_store.save_invoicing(user_ctx.db_path, cfg, replace_collections=False)
    return {"status": "ok"}


@router.get("/config/companies")
async def api_config_companies(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "companies": [_company_to_dict(c) for c in cfg.companies.values()],
    }


@router.post("/config/companies")
async def api_config_companies_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    key = body.get("key")
    if not key:
        return JSONResponse({"status": "error", "error": "key required"}, 400)
    fields = {k: v for k, v in body.items() if k != "key"}
    comp, state = config_store.upsert_company(user_ctx.db_path, key, **fields)
    return {"status": "ok", "state": state, "company": _company_to_dict(comp)}


@router.put("/config/companies/{key}")
async def api_config_companies_put(
    key: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    comp, state = config_store.upsert_company(user_ctx.db_path, key, **body)
    return {"status": "ok", "state": state, "company": _company_to_dict(comp)}


@router.delete("/config/companies/{key}")
async def api_config_companies_delete(
    key: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    ok = config_store.delete_company(user_ctx.db_path, key)
    return {"status": "ok", "removed": ok}


@router.get("/config/clients")
async def api_config_clients(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "clients": [_client_to_dict(c) for c in cfg.clients.values()],
    }


@router.post("/config/clients")
async def api_config_clients_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    key = body.get("key")
    if not key:
        return JSONResponse({"status": "error", "error": "key required"}, 400)
    fields = {k: v for k, v in body.items() if k != "key"}
    client, state = config_store.upsert_client(user_ctx.db_path, key, **fields)
    return {"status": "ok", "state": state, "client": _client_to_dict(client)}


@router.put("/config/clients/{key}")
async def api_config_clients_put(
    key: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    client, state = config_store.upsert_client(user_ctx.db_path, key, **body)
    return {"status": "ok", "state": state, "client": _client_to_dict(client)}


@router.delete("/config/clients/{key}")
async def api_config_clients_delete(
    key: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    ok = config_store.delete_client(user_ctx.db_path, key)
    return {"status": "ok", "removed": ok}


@router.get("/config/services")
async def api_config_services(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_invoicing(user_ctx.db_path)
    return {
        "status": "ok",
        "services": [_service_to_dict(s) for s in cfg.services.values()],
    }


@router.post("/config/services")
async def api_config_services_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    key = body.get("key")
    if not key:
        return JSONResponse({"status": "error", "error": "key required"}, 400)
    fields = {k: v for k, v in body.items() if k != "key"}
    svc, state = config_store.upsert_service(user_ctx.db_path, key, **fields)
    return {"status": "ok", "state": state, "service": _service_to_dict(svc)}


@router.put("/config/services/{key}")
async def api_config_services_put(
    key: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    svc, state = config_store.upsert_service(user_ctx.db_path, key, **body)
    return {"status": "ok", "state": state, "service": _service_to_dict(svc)}


@router.delete("/config/services/{key}")
async def api_config_services_delete(
    key: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    ok = config_store.delete_service(user_ctx.db_path, key)
    return {"status": "ok", "removed": ok}


@router.get("/config/tax")
async def api_config_tax(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_tax(user_ctx.db_path)
    return {
        "status": "ok",
        "tax": {
            "filing_status": cfg.filing_status,
            "tax_year": cfg.tax_year,
            "w2_income": cfg.w2_income,
            "w2_federal_withholding": cfg.w2_federal_withholding,
            "w2_state_withholding": cfg.w2_state_withholding,
            "federal_estimated_paid": cfg.federal_estimated_paid,
            "state_estimated_paid": cfg.state_estimated_paid,
            "enable_qbi_deduction": cfg.enable_qbi_deduction,
            "prior_year_federal_tax": cfg.prior_year_federal_tax,
            "prior_year_state_tax": cfg.prior_year_state_tax,
        },
    }


@router.put("/config/tax")
async def api_config_tax_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    cfg = config_store.load_tax(user_ctx.db_path)
    allowed = {
        "filing_status", "tax_year",
        "w2_income", "w2_federal_withholding", "w2_state_withholding",
        "federal_estimated_paid", "state_estimated_paid",
        "enable_qbi_deduction",
        "prior_year_federal_tax", "prior_year_state_tax",
    }
    bad = set(body) - allowed
    if bad:
        return JSONResponse(
            {"status": "error", "error": f"unknown keys: {sorted(bad)}"}, 400,
        )
    for k, v in body.items():
        setattr(cfg, k, v)
    config_store.save_tax(user_ctx.db_path, cfg, replace_collections=False)
    return {"status": "ok"}


@router.get("/config/tax/years")
async def api_config_tax_years(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    return {"status": "ok", "years": config_store.list_tax_year_rates(user_ctx.db_path)}


@router.put("/config/tax/years/{year}")
async def api_config_tax_years_put(
    year: int, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    try:
        state = config_store.upsert_tax_year_rates(user_ctx.db_path, year, **body)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "state": state}


@router.delete("/config/tax/years/{year}")
async def api_config_tax_years_delete(
    year: int, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    ok = config_store.delete_tax_year_rates(user_ctx.db_path, year)
    return {"status": "ok", "removed": ok}


@router.get("/config/tax/patterns")
async def api_config_tax_patterns(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    return {
        "status": "ok",
        "patterns": config_store.list_tax_patterns(user_ctx.db_path),
    }


@router.put("/config/tax/patterns")
async def api_config_tax_patterns_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Replace-all per-kind. Body: ``{"se_income": [...], "se_expense": [...]}``."""
    from istota.money import config_store
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    try:
        config_store.replace_tax_patterns(
            user_ctx.db_path,
            {k: v for k, v in body.items() if k in ("se_income", "se_expense")},
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/monarch")
async def api_config_monarch(user_ctx: UserContext = Depends(get_user_config)):
    from istota.money import config_store
    cfg = config_store.load_monarch(user_ctx.db_path)
    return {
        "status": "ok",
        "sync": {
            "lookback_days": cfg.sync.lookback_days,
            "default_account": cfg.sync.default_account,
            "recategorize_account": cfg.sync.recategorize_account,
        },
    }


@router.put("/config/monarch")
async def api_config_monarch_put(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    allowed = {"lookback_days", "default_account", "recategorize_account"}
    bad = set(body) - allowed
    if bad:
        return JSONResponse(
            {"status": "error", "error": f"unknown keys: {sorted(bad)}"}, 400,
        )
    cfg = config_store.load_monarch(user_ctx.db_path)
    for k, v in body.items():
        setattr(cfg.sync, k, v)
    config_store.save_monarch(user_ctx.db_path, cfg, replace_collections=False)
    return {"status": "ok"}


@router.get("/config/monarch/profiles")
async def api_config_monarch_profiles(
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    return {
        "status": "ok",
        "profiles": config_store.list_monarch_profiles(user_ctx.db_path),
    }


@router.post("/config/monarch/profiles")
async def api_config_monarch_profiles_post(
    request: Request, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    name = body.get("name")
    if not name:
        return JSONResponse({"status": "error", "error": "name required"}, 400)
    fields = {k: v for k, v in body.items() if k != "name"}
    try:
        prof, state = config_store.upsert_monarch_profile(
            user_ctx.db_path, name, **fields,
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "state": state, "profile": prof}


@router.put("/config/monarch/profiles/{name}")
async def api_config_monarch_profiles_put(
    name: str, request: Request,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    body = await request.json()
    try:
        prof, state = config_store.upsert_monarch_profile(
            user_ctx.db_path, name, **body,
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "state": state, "profile": prof}


@router.delete("/config/monarch/profiles/{name}")
async def api_config_monarch_profiles_delete(
    name: str, user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    ok = config_store.delete_monarch_profile(user_ctx.db_path, name)
    return {"status": "ok", "removed": ok}


def _resolve_profile_query(profile: str | None):
    if profile is None or profile == "" or profile == "global":
        return None
    return profile


@router.get("/config/monarch/account-map")
async def api_config_monarch_account_map(
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    try:
        mapping = config_store.get_account_map(user_ctx.db_path, p)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "mapping": mapping}


@router.put("/config/monarch/account-map")
async def api_config_monarch_account_map_put(
    request: Request,
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    try:
        config_store.replace_account_map(user_ctx.db_path, p, body)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/monarch/category-map")
async def api_config_monarch_category_map(
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    try:
        mapping = config_store.get_category_map(user_ctx.db_path, p)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok", "mapping": mapping}


@router.put("/config/monarch/category-map")
async def api_config_monarch_category_map_put(
    request: Request,
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "body must be an object"}, 400,
        )
    try:
        config_store.replace_category_map(user_ctx.db_path, p, body)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/monarch/tag-filters")
async def api_config_monarch_tag_filters(
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    try:
        return {"status": "ok", "tags": config_store.get_tag_filters(user_ctx.db_path, p)}
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)


@router.put("/config/monarch/tag-filters")
async def api_config_monarch_tag_filters_put(
    request: Request,
    profile: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Body: ``{"include": [...], "exclude": [...]}`` — replaces both lists."""
    from istota.money import config_store
    p = _resolve_profile_query(profile)
    body = await request.json()
    include = body.get("include", []) or []
    exclude = body.get("exclude", []) or []
    try:
        config_store.replace_tag_filters(user_ctx.db_path, p, include, exclude)
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, 400)
    return {"status": "ok"}


@router.get("/config/export")
async def api_config_export(
    section: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    """Export DB config as TOML. Returns ``text/plain``."""
    from istota.money import config_store
    import tomli_w

    db_path = user_ctx.db_path
    if section == "invoicing":
        body = config_store.invoicing_to_toml_dict(
            config_store.load_invoicing(db_path),
        )
        text = tomli_w.dumps(_strip_none(body))
    elif section == "tax":
        body = config_store.tax_to_toml_dict(config_store.load_tax(db_path))
        text = tomli_w.dumps(_strip_none(body))
    elif section == "monarch":
        body = config_store.monarch_to_toml_dict(
            config_store.load_monarch(db_path),
        )
        text = tomli_w.dumps(_strip_none(body))
    else:
        combined: dict = {}
        inv = config_store.invoicing_to_toml_dict(
            config_store.load_invoicing(db_path),
        )
        if inv:
            combined["invoicing"] = inv
        tax = config_store.tax_to_toml_dict(config_store.load_tax(db_path))
        if tax.get("tax"):
            combined["tax"] = tax["tax"]
        mon = config_store.monarch_to_toml_dict(
            config_store.load_monarch(db_path),
        )
        if mon.get("monarch"):
            combined["monarch"] = mon["monarch"]
        text = tomli_w.dumps(_strip_none(combined))
    return Response(content=text, media_type="text/plain")


def _strip_none(value):
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


@router.post("/config/import")
async def api_config_import(
    request: Request,
    section: str | None = None,
    dry_run: int = 0,
    replace: int = 0,
    user_ctx: UserContext = Depends(get_user_config),
    _csrf: None = Depends(verify_origin),
):
    """Import a TOML payload (multipart or JSON {text: ...}).

    Returns a per-section list of ``STATE: …`` entries. With ``dry_run=1``
    nothing is written.
    """
    import tomli
    from istota.money import config_store

    text: str | None = None
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        f = form.get("file")
        if f is not None:
            text = (await f.read()).decode()
    if text is None:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "error": "no payload"}, 400)
        text = body.get("text") if isinstance(body, dict) else None
    if not text:
        return JSONResponse({"status": "error", "error": "no payload"}, 400)

    try:
        parsed = tomli.loads(text)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"status": "error", "error": f"unparseable TOML: {exc}"}, 400,
        )

    sections: list[str] = []
    if section in ("invoicing", "tax", "monarch"):
        sections = [section]
    else:
        invoicing_keys = (
            "companies", "clients", "services", "company",
            "accounting_path", "next_invoice_number", "invoicing",
        )
        if any(k in parsed for k in invoicing_keys):
            sections.append("invoicing")
        if "tax" in parsed:
            sections.append("tax")
        if "monarch" in parsed:
            sections.append("monarch")
    if not sections:
        return JSONResponse(
            {"status": "error", "error": "no recognized sections"}, 400,
        )

    from istota.cli_money import (
        _apply_section_import, _compute_section_diff, _extract_section_data,
    )

    sections_out = []
    for sec in sections:
        section_data = _extract_section_data(parsed, sec)
        if section_data is None:
            continue
        diff = _compute_section_diff(user_ctx, sec, section_data, bool(replace))
        if not dry_run:
            _apply_section_import(user_ctx, sec, section_data, bool(replace))
        sections_out.append({
            "section": sec,
            "states": [{"state": s, "message": m} for s, m in diff],
        })

    return {"status": "ok", "dry_run": bool(dry_run), "sections": sections_out}
