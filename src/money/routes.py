"""FastAPI router for the money web API.

The host application (istota) mounts ``router`` at its chosen prefix and
overrides ``require_auth`` via ``app.dependency_overrides`` so that the
session/cookie/OIDC concerns stay with the host. Per-user data config is
resolved per request through ``money.config.resolve_user_config``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from money.cli import UserContext
from money.config import UserNotFoundError, resolve_user_config


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


def get_user_config(user: dict = Depends(require_auth)) -> UserContext:
    try:
        return resolve_user_config(user["username"])
    except UserNotFoundError:
        raise HTTPException(404, "user not configured")


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
    from money.core.ledger import run_bean_query

    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if not ledger_path:
        return JSONResponse({"error": "ledger not found"}, status_code=404)

    where = f" WHERE year = {int(year)}" if year else ""
    bql = f"SELECT account, sum(position){where} GROUP BY account ORDER BY account"

    try:
        rows = run_bean_query(ledger_path, bql)
        return {"status": "ok", "accounts": rows}
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


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
    from money.core.ledger import run_bean_query, _sanitize_bql_string

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
        f"SELECT date, flag, payee, narration, account, position, tags"
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
    from money.core.ledger import run_bean_query, _sanitize_bql_string

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


@router.get("/report/{report_type}")
async def api_report(
    report_type: str,
    ledger: str | None = None,
    year: int | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from money.core.ledger import report

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
    from money.core.ledger import check

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
    from money.core.invoicing import parse_invoicing_config

    if not user_ctx.invoicing_config_path or not user_ctx.invoicing_config_path.exists():
        return {"status": "ok", "clients": []}

    try:
        config = parse_invoicing_config(user_ctx.invoicing_config_path)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

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
    from money.core.invoicing import build_line_items, parse_invoicing_config
    from money.work import get_invoice_numbers, get_entries_for_invoice

    if not user_ctx.invoicing_config_path or not user_ctx.invoicing_config_path.exists():
        return {"status": "ok", "invoices": [], "invoice_count": 0, "outstanding_count": 0}

    data_dir = user_ctx.data_dir
    if not data_dir:
        return {"status": "ok", "invoices": [], "invoice_count": 0, "outstanding_count": 0}

    try:
        config = parse_invoicing_config(user_ctx.invoicing_config_path)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

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
    from money.core.invoicing import parse_invoicing_config

    if not user_ctx.invoicing_config_path or not user_ctx.invoicing_config_path.exists():
        return {"status": "ok", "entities": [], "services": [], "defaults": {}}

    try:
        config = parse_invoicing_config(user_ctx.invoicing_config_path)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

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
    from money.core.invoicing import build_line_items, parse_invoicing_config
    from money.work import get_entries_for_invoice

    if not user_ctx.invoicing_config_path or not user_ctx.invoicing_config_path.exists():
        return JSONResponse({"error": "no invoicing config"}, status_code=404)

    data_dir = user_ctx.data_dir
    if not data_dir:
        return JSONResponse({"error": "no data dir"}, status_code=404)

    try:
        config = parse_invoicing_config(user_ctx.invoicing_config_path)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        return JSONResponse({"error": "invoice not found"}, status_code=404)

    service_entries = [e for e in entries if e.service != "_manual"]
    items = build_line_items(service_entries, config.services)

    for e in entries:
        if e.service == "_manual":
            from money.core.models import InvoiceLineItem
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


@router.get("/tax/estimate")
async def api_tax_estimate(
    ledger: str | None = None,
    method: str = "annualized",
    quarter: int | None = None,
    year: int | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from datetime import date

    from money.core.models import TaxConfig
    from money.core.tax import (
        estimate_quarterly_tax,
        load_tax_inputs,
        parse_tax_config,
        payment_quarter_from_date,
        query_se_income,
    )

    if not user_ctx.tax_config_path or not user_ctx.tax_config_path.exists():
        return JSONResponse({"error": "no tax config"}, status_code=404)

    try:
        config = parse_tax_config(user_ctx.tax_config_path)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    saved = load_tax_inputs(user_ctx.db_path)

    tax_year = year or config.tax_year
    today = date.today()
    current_quarter = quarter or payment_quarter_from_date(today)
    use_method = method if method != "annualized" else saved.get("method", method)

    se_income_ytd = 0.0
    ledger_path = _resolve_user_ledger(user_ctx, ledger)
    if ledger_path:
        try:
            config_for_query = TaxConfig(
                **{**config.__dict__, "tax_year": tax_year}
            ) if tax_year != config.tax_year else config
            se_income_ytd = query_se_income(ledger_path, config_for_query, current_quarter)
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
        config=config,
    )
    return {"status": "ok", **result.__dict__}


@router.post("/tax/estimate")
async def api_tax_estimate_recalculate(
    request: Request,
    ledger: str | None = None,
    user_ctx: UserContext = Depends(get_user_config),
):
    from datetime import date

    from money.core.models import TaxConfig
    from money.core.tax import (
        estimate_quarterly_tax,
        parse_tax_config,
        payment_quarter_from_date,
        query_se_income,
        save_tax_inputs,
    )

    if not user_ctx.tax_config_path or not user_ctx.tax_config_path.exists():
        return JSONResponse({"error": "no tax config"}, status_code=404)

    try:
        config = parse_tax_config(user_ctx.tax_config_path)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    body = await request.json()
    tax_year = body.get("year", config.tax_year)
    method = body.get("method", "annualized")
    today = date.today()
    current_quarter = body.get("quarter") or payment_quarter_from_date(today)

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
            se_income_ytd = query_se_income(ledger_path, config_for_query, current_quarter)
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
        config=config,
    )
    return {"status": "ok", **result.__dict__}
