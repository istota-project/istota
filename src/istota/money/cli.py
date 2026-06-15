"""Click CLI for moneyman."""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import click

from istota.experimental import requires_feature


def _output(result: dict) -> None:
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "error":
        sys.exit(1)


@dataclass
class UserContext:
    """Per-user configuration resolved from a [users.*] section."""
    data_dir: Path
    ledgers: list[dict] = field(default_factory=list)
    invoicing_config_path: Path | None = None
    monarch_config_path: Path | None = None
    tax_config_path: Path | None = None
    db_path: Path | None = None


class Context:
    """CLI context holding resolved configuration.

    All paths are resolved to absolute filesystem paths at config load time.
    """
    def __init__(self):
        self.data_dir: Path | None = None
        self.ledgers: list[dict] = []
        self.monarch_config_path: Path | None = None
        self.invoicing_config_path: Path | None = None
        self.tax_config_path: Path | None = None
        self.db_path: Path | None = None
        self.secrets: dict | None = None
        self.api_key: str | None = None
        self.users: dict[str, UserContext] = {}
        self.active_user: str | None = None

    @property
    def has_single_user(self) -> bool:
        return len(self.users) <= 1

    @property
    def available_users(self) -> list[str]:
        return sorted(self.users.keys())

    def activate_user(self, user_key: str) -> None:
        """Activate a user, setting data_dir/ledgers/etc from their config."""
        if user_key not in self.users:
            raise click.ClickException(f"Unknown user: {user_key}")
        uctx = self.users[user_key]
        self.active_user = user_key
        self.data_dir = uctx.data_dir
        self.ledgers = uctx.ledgers
        self.invoicing_config_path = uctx.invoicing_config_path
        self.monarch_config_path = uctx.monarch_config_path
        self.tax_config_path = uctx.tax_config_path
        self.db_path = uctx.db_path

    def for_user(self, user_key: str) -> Context:
        """Return a shallow copy with the given user activated.

        Safe for concurrent use (each request gets its own copy).
        """
        ctx = copy.copy(self)
        ctx.users = self.users  # share the users dict (read-only)
        ctx.activate_user(user_key)
        return ctx

    def for_default_user(self) -> Context:
        """Return a copy with the single/default user activated."""
        if not self.users:
            return self
        key = next(iter(self.users))
        return self.for_user(key)


pass_ctx = click.make_pass_decorator(Context, ensure=True)


def _resolve(data_dir: Path, raw: str) -> Path:
    """Resolve a path relative to data_dir. Absolute paths are returned as-is."""
    p = Path(raw)
    if p.is_absolute():
        return p
    return data_dir / raw


def resolve_ledger(ledger: str | None, config_ledgers: list[dict]) -> Path:
    if not config_ledgers:
        raise click.ClickException("No ledgers configured")
    if ledger:
        for entry in config_ledgers:
            if entry["name"].lower() == ledger.lower():
                return entry["path"]
        available = [entry["name"] for entry in config_ledgers]
        raise click.ClickException(f"Ledger '{ledger}' not found. Available: {', '.join(available)}")
    return config_ledgers[0]["path"]


def _require_db(ctx: Context):
    """Get a DB connection or fail."""
    conn = _get_db_conn(ctx)
    if not conn:
        raise click.ClickException("No database configured")
    return conn


def _require_active_user(ctx: Context) -> None:
    """Ensure a user is active when multi-user config is used."""
    if not ctx.has_single_user and ctx.active_user is None:
        raise click.ClickException(
            "Multiple users configured. Use --user to select one: "
            + ", ".join(ctx.available_users)
        )


def _require_data_dir(ctx: Context) -> Path:
    """Get data_dir or fail."""
    if not ctx.data_dir:
        _require_active_user(ctx)
        raise click.ClickException("No data_dir configured")
    return ctx.data_dir


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


def _load_invoicing_config(ctx: Context):
    """Parse invoicing config and resolve accounting_path + invoice_output_dir.

    Returns (config, accounting_path, invoice_output_dir) where all paths are
    absolute.  ``invoice_output_dir`` is resolved relative to ``data_dir`` so
    that PDF writes always land inside the data directory, regardless of what
    ``accounting_path`` points to.
    """
    from istota.money import config_store
    if ctx.db_path is None or not config_store.has_invoicing_data(ctx.db_path):
        raise click.ClickException(
            "No invoicing config in the DB for this user. Seed it via "
            "`istota money config import` or `istota money client|company|service add`.",
        )
    config = config_store.load_invoicing(ctx.db_path)
    if ctx.data_dir:
        accounting_path = _resolve(ctx.data_dir, config.accounting_path)
        invoice_output_dir = _resolve(ctx.data_dir, config.invoice_output)
    else:
        accounting_path = Path(config.accounting_path).resolve()
        invoice_output_dir = Path(config.invoice_output).resolve()
    return config, accounting_path, invoice_output_dir


def _require_injected_context(ctx) -> "Context":
    """The money CLI runs only with an istota-resolved Context injected.

    There is no standalone config loader anymore — money is part of istota.
    Both entry points (the ``istota money …`` operator CLI and the money skill)
    resolve the user via ``istota.money.resolve_for_user`` (DB-backed) and inject
    the :class:`Context` through ``CliRunner.invoke(obj=...)``.
    """
    if isinstance(ctx.obj, Context) and ctx.obj.users:
        return ctx.obj
    raise click.ClickException(
        "money CLI must be invoked through istota (e.g. `istota money …`); "
        "there is no standalone config.",
    )


@click.group()
@click.option("--user", "-u", "user_key", help="Active user key (resolved by istota)")
@click.pass_context
def cli(ctx, user_key):
    """Money — accounting operations CLI (driven by istota).

    The caller (the ``istota money …`` CLI or the money skill) resolves the
    user's :class:`Context` via ``istota.money.resolve_for_user`` and injects it
    through ``CliRunner.invoke(obj=...)``. There is no file-based config loader.
    """
    mctx = _require_injected_context(ctx)
    if user_key and user_key in mctx.users:
        mctx.activate_user(user_key)


@cli.command("users")
@pass_ctx
def list_users(ctx):
    """List configured users."""
    users = []
    for key in ctx.available_users:
        uctx = ctx.users[key]
        users.append({
            "key": key,
            "data_dir": str(uctx.data_dir),
            "ledger_count": len(uctx.ledgers),
        })
    _output({
        "status": "ok",
        "user_count": len(users),
        "users": users,
    })


# =============================================================================
# Work entry commands
# =============================================================================


@cli.group()
def work():
    """Manage work log entries."""
    pass


@work.command("add")
@click.option("--date", "-d", "entry_date", required=True, help="Date (YYYY-MM-DD)")
@click.option("--client", "-c", required=True, help="Client key")
@click.option("--service", "-s", required=True, help="Service key")
@click.option("--qty", "-q", type=float, help="Quantity (hours, days, etc.)")
@click.option("--amount", "-a", type=float, help="Fixed amount (for 'other' service type)")
@click.option("--discount", type=float, default=0, help="Discount amount")
@click.option("--description", help="Description of work")
@click.option("--entity", "-e", help="Entity override")
@pass_ctx
def work_add(ctx, entry_date, client, service, qty, amount, discount, description, entity):
    """Add a work entry."""
    from istota.money.work import add_work_entry

    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
        return

    data_dir = _require_data_dir(ctx)
    entry_id = add_work_entry(
        data_dir, entry_date, client.lower(), service,
        qty=qty, amount=amount, discount=discount,
        description=description or "", entity=entity or "",
    )
    _output({"status": "ok", "id": entry_id, "message": f"Added work entry #{entry_id}"})


@work.command("list")
@click.option("--client", "-c", help="Filter by client")
@click.option("--period", "-p", help="Filter by period (YYYY-MM)")
@click.option("--uninvoiced", is_flag=True, help="Show only uninvoiced entries")
@click.option("--invoiced", is_flag=True, help="Show only invoiced entries")
@pass_ctx
def work_list(ctx, client, period, uninvoiced, invoiced):
    """List work entries."""
    from istota.money.work import list_work_entries

    invoiced_filter = None
    if uninvoiced:
        invoiced_filter = False
    elif invoiced:
        invoiced_filter = True

    data_dir = _require_data_dir(ctx)
    entries = list_work_entries(data_dir, client=client, invoiced=invoiced_filter, period=period)

    _output({
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
    })


@work.command("remove")
@click.argument("entry_id", type=int)
@pass_ctx
def work_remove(ctx, entry_id):
    """Remove an uninvoiced work entry."""
    from istota.money.work import remove_work_entry

    data_dir = _require_data_dir(ctx)
    if remove_work_entry(data_dir, entry_id):
        _output({"status": "ok", "message": f"Removed work entry #{entry_id}"})
    else:
        _output({"status": "error", "error": f"Entry #{entry_id} not found or already invoiced"})


@work.command("update")
@click.argument("entry_id", type=int)
@click.option("--date", "-d", "entry_date", help="Date (YYYY-MM-DD)")
@click.option("--client", "-c", help="Client key")
@click.option("--service", "-s", help="Service key")
@click.option("--qty", "-q", type=float, help="Quantity")
@click.option("--amount", "-a", type=float, help="Fixed amount")
@click.option("--discount", type=float, help="Discount")
@click.option("--description", help="Description")
@click.option("--entity", "-e", help="Entity override")
@click.option("--invoice", help="Manually assign invoice number")
@pass_ctx
def work_update(ctx, entry_id, entry_date, client, service, qty, amount, discount, description, entity, invoice):
    """Update a work entry."""
    from istota.money.work import update_work_entry

    fields = {}
    if entry_date is not None:
        fields["date"] = entry_date
    if client is not None:
        fields["client"] = client.lower()
    if service is not None:
        fields["service"] = service
    if qty is not None:
        fields["qty"] = qty
    if amount is not None:
        fields["amount"] = amount
    if discount is not None:
        fields["discount"] = discount
    if description is not None:
        fields["description"] = description
    if entity is not None:
        fields["entity"] = entity
    if invoice is not None:
        fields["invoice"] = invoice

    if not fields:
        _output({"status": "error", "error": "No fields to update"})
        return

    data_dir = _require_data_dir(ctx)
    if update_work_entry(data_dir, entry_id, **fields):
        _output({"status": "ok", "message": f"Updated work entry #{entry_id}"})
    else:
        _output({"status": "error", "error": f"Entry #{entry_id} not found or already invoiced"})


# =============================================================================
# Ledger commands
# =============================================================================


@cli.command("list")
@pass_ctx
def list_ledgers(ctx):
    """List available ledgers."""
    _require_active_user(ctx)
    if ctx.ledgers:
        _output({
            "status": "ok",
            "ledger_count": len(ctx.ledgers),
            "ledgers": [{"name": e["name"], "path": str(e["path"])} for e in ctx.ledgers],
        })
    else:
        _output({"status": "error", "error": "No ledgers configured"})


@cli.command()
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def check(ctx, ledger):
    """Validate ledger file."""
    from istota.money.core.ledger import check as ledger_check
    _output(ledger_check(resolve_ledger(ledger, ctx.ledgers)))


@cli.command()
@click.option("--account", "-a", help="Filter by account pattern (regex)")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def balances(ctx, account, ledger):
    """Show account balances."""
    from istota.money.core.ledger import balances as ledger_balances
    _output(ledger_balances(resolve_ledger(ledger, ctx.ledgers), account))


@cli.command()
@click.argument("bql")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def query(ctx, bql, ledger):
    """Run a BQL query."""
    from istota.money.core.ledger import query as ledger_query
    _output(ledger_query(resolve_ledger(ledger, ctx.ledgers), bql))


@cli.command()
@click.argument("report_type", type=click.Choice(["income-statement", "balance-sheet"]))
@click.option("--year", "-y", type=int, help="Year for report")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def report(ctx, report_type, year, ledger):
    """Generate financial report."""
    from istota.money.core.ledger import report as ledger_report
    _output(ledger_report(resolve_ledger(ledger, ctx.ledgers), report_type, year))


@cli.command()
@click.argument("symbol")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
@requires_feature("money_tax")
def lots(ctx, symbol, ledger):
    """Show open lots for a security. Experimental — gated by ``money_tax``."""
    from istota.money.core.ledger import lots as ledger_lots
    _output(ledger_lots(resolve_ledger(ledger, ctx.ledgers), symbol))


@cli.command("wash-sales")
@click.option("--year", "-y", type=int, help="Year to analyze")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
@requires_feature("money_wash_sales")
def wash_sales(ctx, year, ledger):
    """Detect wash sale violations. Experimental — gated by ``money_wash_sales``."""
    from istota.money.core.ledger import wash_sales as ledger_wash_sales
    _output(ledger_wash_sales(resolve_ledger(ledger, ctx.ledgers), year))


# =============================================================================
# Transaction commands
# =============================================================================


@cli.command("backfill-ids")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def backfill_ids(ctx, ledger):
    """Stamp a stable ``id:`` on every transaction lacking one.

    One-time, idempotent migration that makes transactions editable. Backs up
    each touched file, validates with ``bean-check``, and rolls back on
    failure.
    """
    from istota.money.core.edit import backfill_ledger_ids
    _output(backfill_ledger_ids(resolve_ledger(ledger, ctx.ledgers)))


@cli.command("add-transaction")
@click.option("--date", "-d", "txn_date", required=True, help="Transaction date (YYYY-MM-DD)")
@click.option("--payee", "-p", required=True, help="Payee name")
@click.option("--narration", "-n", required=True, help="Transaction description")
@click.option("--debit", required=True, help="Debit account")
@click.option("--credit", required=True, help="Credit account")
@click.option("--amount", "-a", required=True, type=float, help="Transaction amount")
@click.option("--currency", default="USD", help="Currency")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def add_transaction(ctx, txn_date, payee, narration, debit, credit, amount, currency, ledger):
    """Add a transaction to the ledger."""
    from istota.money.core.transactions import add_transaction as core_add
    try:
        parsed_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
    except ValueError:
        _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
        return
    _output(core_add(
        resolve_ledger(ledger, ctx.ledgers),
        parsed_date, payee, narration, debit, credit, amount, currency,
    ))


@cli.command("edit-transaction")
@click.option("--id", "txn_id", required=True, help="Stable transaction id")
@click.option("--old-account", help="Account of the posting to edit (disambiguator)")
@click.option("--old-position", help="Amount of the posting to edit (disambiguator)")
@click.option("--date", "-d", "new_date", help="New date (YYYY-MM-DD)")
@click.option("--payee", "-p", "new_payee", help="New payee")
@click.option("--narration", "-n", "new_narration", help="New narration")
@click.option("--account", "-a", "new_account", help="New posting account")
@click.option("--position", "new_position", help="New posting amount (e.g. '-12.50 USD')")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def edit_transaction(
    ctx, txn_id, old_account, old_position,
    new_date, new_payee, new_narration, new_account, new_position, ledger,
):
    """Edit a transaction located by its stable ``id:`` metadata.

    Validates the result with ``bean-check`` and rolls back on failure (e.g.
    an amount edit that unbalances the entry).
    """
    from istota.money.core.edit import edit_transaction as core_edit

    if new_date:
        try:
            datetime.strptime(new_date, "%Y-%m-%d")
        except ValueError:
            _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
            return

    _output(core_edit(
        resolve_ledger(ledger, ctx.ledgers),
        txn_id,
        old_account=old_account,
        old_position=old_position,
        new_date=new_date,
        new_payee=new_payee,
        new_narration=new_narration,
        new_account=new_account,
        new_position=new_position,
    ))


@cli.command("import-csv")
@click.argument("file", type=click.Path(exists=True))
@click.option("--account", "-a", required=True, help="Bank/credit card account")
@click.option("--tag", "-t", multiple=True, help="Only include transactions with this tag")
@click.option("--exclude-tag", "-x", multiple=True, help="Exclude transactions with this tag")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def import_csv(ctx, file, account, tag, exclude_tag, ledger):
    """Import transactions from Monarch Money CSV export."""
    from istota.money.core.transactions import import_csv as core_import
    db_conn = _get_db_conn(ctx)
    try:
        _output(core_import(
            ledger_path=resolve_ledger(ledger, ctx.ledgers),
            file_path=Path(file), account=account, db_conn=db_conn,
            include_tags=list(tag) if tag else None,
            exclude_tags=list(exclude_tag) if exclude_tag else None,
        ))
    finally:
        if db_conn:
            db_conn.commit()
            db_conn.close()


def _run_monarch_sync(ctx, dry_run: bool, ledger: str | None) -> dict:
    """Shared monarch sync implementation. Returns a result dict.

    Used by the ``sync-monarch`` command and folded into ``run-scheduled``
    so periodic syncs happen as part of the daily run.
    """
    from istota.money import config_store
    from istota.money.core.transactions import (
        sync_all_profiles,
        sync_monarch as core_sync,
    )
    if ctx.db_path is None or not config_store.has_monarch_data(ctx.db_path):
        return {
            "status": "error",
            "error": "No monarch config in the DB for this user. Seed it via "
                     "`istota money monarch …` or `istota money config import`.",
        }
    config = config_store.load_monarch(ctx.db_path, secrets=ctx.secrets)
    db_conn = _get_db_conn(ctx)
    try:
        if ledger:
            # Specific ledger: find matching profile(s) or use flat config
            ledger_path = resolve_ledger(ledger, ctx.ledgers)
            matching = [p for p in config.profiles if p.ledger.lower() == ledger.lower()]
            if matching:
                # Sync only the matching profile(s)
                import asyncio
                from istota.money.core.transactions import fetch_monarch_transactions
                lookback = max(p.sync.lookback_days for p in matching)
                txns = asyncio.run(fetch_monarch_transactions(config, lookback))
                from istota.money.core.models import MonarchConfig as MC
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
                        ledger_path, profile_config, db_conn=db_conn,
                        dry_run=dry_run, transactions=txns, profile=profile.name,
                    )
                    r["name"] = profile.name
                    r["ledger"] = profile.ledger
                    results.append(r)
                return {"status": "ok", "profiles": results}
            else:
                return core_sync(
                    ledger_path, config, db_conn=db_conn, dry_run=dry_run,
                )
        else:
            return sync_all_profiles(
                config, ctx.ledgers, db_conn=db_conn, dry_run=dry_run,
            )
    finally:
        if db_conn:
            db_conn.commit()
            db_conn.close()


@cli.command("sync-monarch")
@click.option("--dry-run", is_flag=True, help="Preview without writing")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def sync_monarch(ctx, dry_run, ledger):
    """Sync transactions from Monarch Money API.

    Without --ledger, syncs all configured profiles (or the default ledger
    if no profiles are defined). With --ledger, syncs only profiles targeting
    that ledger.
    """
    _output(_run_monarch_sync(ctx, dry_run, ledger))


@cli.command("debug-monarch")
@pass_ctx
def debug_monarch(ctx):
    """Health-check Monarch credentials.

    Calls the cheapest possible GraphQL query (``me { id email }``) so
    operators / heartbeats can quickly tell whether the stored cookies
    still authenticate. Output is a JSON envelope:

    - ``{"status":"ok", "auth_ok":true, "who":{...}}`` on success
    - ``{"status":"error", "auth_ok":false, "error":"..."}`` on rejection
      or missing creds.
    """
    import asyncio

    from istota.money import config_store
    from istota.money._vendor.monarch_client import (
        MonarchAuthError, MonarchClient, MonarchCookieAuth,
    )

    if ctx.db_path is None or not config_store.has_monarch_data(ctx.db_path):
        _output({
            "status": "error", "auth_ok": False,
            "error": "No monarch config in the DB for this user",
        })
        return
    config = config_store.load_monarch(ctx.db_path, secrets=ctx.secrets)

    creds = config.credentials
    if not (creds.session_id and creds.csrftoken):
        _output({
            "status": "error", "auth_ok": False,
            "error": "Missing session_id and/or csrftoken cookies. "
                     "Set them via the money settings page.",
        })
        return

    async def _probe() -> dict:
        client = MonarchClient(MonarchCookieAuth(
            session_id=creds.session_id, csrftoken=creds.csrftoken,
        ))
        return await client.whoami()

    try:
        who = asyncio.run(_probe())
    except MonarchAuthError as exc:
        _output({"status": "error", "auth_ok": False, "error": str(exc)})
        return
    except Exception as exc:  # noqa: BLE001
        _output({"status": "error", "auth_ok": False, "error": str(exc)})
        return
    _output({"status": "ok", "auth_ok": True, "who": who})


# =============================================================================
# Invoice commands
# =============================================================================


@cli.group()
def invoice():
    """Invoice management."""
    pass


@invoice.command("generate")
@click.option("--period", "-p", help="Billing period upper bound (YYYY-MM)")
@click.option("--client", "-c", help="Filter by client key")
@click.option("--entity", "-e", help="Filter by entity key")
@click.option("--dry-run", is_flag=True, help="Preview without generating files")
@pass_ctx
def invoice_generate(ctx, period, client, entity, dry_run):
    """Generate invoices for uninvoiced work entries."""
    from istota.money.core.invoicing import generate_invoices_for_period

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    data_dir = _require_data_dir(ctx)
    try:
        results = generate_invoices_for_period(
            config=config, config_path=ctx.invoicing_config_path,
            accounting_path=accounting_path, data_dir=data_dir,
            period=period, client_filter=client,
            entity_filter=entity, dry_run=dry_run,
            invoice_output_dir=invoice_output_dir,
            db_path=ctx.db_path,
        )
    except Exception as e:
        _output({"status": "error", "error": str(e)})
        return

    if not results:
        period_desc = f" for period {period}" if period else ""
        _output({"status": "ok", "message": f"No uninvoiced entries found{period_desc}", "invoices": []})
        return

    total = sum(r["total"] for r in results)
    result = {
        "status": "ok",
        "invoice_count": len(results),
        "total": round(total, 2),
        "dry_run": dry_run,
        "invoices": results,
    }
    if period:
        result["period"] = period
    _output(result)


@invoice.command("list")
@click.option("--client", "-c", help="Filter by client")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all invoices (including paid)")
@pass_ctx
def invoice_list(ctx, client, show_all):
    """List invoices (outstanding by default)."""
    from istota.money.core.invoicing import build_line_items
    from istota.money.work import get_invoice_numbers, get_entries_for_invoice

    try:
        config, _, _ = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    data_dir = _require_data_dir(ctx)
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
            "date": inv_date.isoformat(),
            "total": round(total, 2),
            "status": "paid" if is_paid else "outstanding",
        }
        if is_paid and paid_date_val:
            invoice_info["paid_date"] = paid_date_val.isoformat()
        invoices.append(invoice_info)

    outstanding = [i for i in invoices if i["status"] == "outstanding"]
    _output({
        "status": "ok",
        "invoice_count": len(invoices),
        "outstanding_count": len(outstanding),
        "invoices": invoices,
    })


@invoice.command("paid")
@click.argument("invoice_number")
@click.option("--date", "-d", "payment_date", required=True, help="Payment date (YYYY-MM-DD)")
@click.option("--bank", "-b", help="Bank account")
@click.option("--no-post", is_flag=True, help="Skip ledger posting")
@click.option("--ledger", "-l", help="Ledger name")
@pass_ctx
def invoice_paid(ctx, invoice_number, payment_date, bank, no_post, ledger):
    """Record payment for an invoice."""
    from istota.money.core.invoicing import (
        compute_income_lines, create_income_posting,
        resolve_bank_account, resolve_currency, resolve_entity,
    )
    from istota.money.core.transactions import append_to_ledger
    from istota.money.core.ledger import run_bean_check
    from istota.money.work import get_entries_for_invoice, record_invoice_payment

    try:
        parsed_date = datetime.strptime(payment_date, "%Y-%m-%d").date()
    except ValueError:
        _output({"status": "error", "error": "Invalid date format. Use YYYY-MM-DD"})
        return

    try:
        config, _, _ = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    data_dir = _require_data_dir(ctx)
    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        _output({"status": "error", "error": f"Invoice {invoice_number} not found"})
        return

    if all(e.paid_date is not None for e in entries):
        _output({"status": "error", "error": f"Invoice {invoice_number} is already paid"})
        return

    first_entry = entries[0]
    client_config = config.clients.get(first_entry.client)
    if not client_config:
        _output({"status": "error", "error": f"Client '{first_entry.client}' not found in config"})
        return

    entity = resolve_entity(config, entry=first_entry, client_config=client_config)
    bank_account = bank or resolve_bank_account(entity, config)
    currency = resolve_currency(entity, config)

    income_lines = compute_income_lines(entries, config.services)
    if not income_lines:
        _output({"status": "error", "error": f"No billable items found for {invoice_number}"})
        return

    total = sum(income_lines.values())
    ledger_path = None

    should_post = not no_post and client_config.ledger_posting
    if should_post:
        posting = create_income_posting(
            invoice_number=invoice_number, client_name=client_config.name,
            income_lines=income_lines, payment_date=parsed_date,
            bank_account=bank_account, currency=currency,
        )
        ledger_path = resolve_ledger(ledger, ctx.ledgers)
        append_to_ledger(ledger_path, [posting])

        success, errors = run_bean_check(ledger_path)
        if not success:
            _output({
                "status": "error",
                "error": "Payment recorded but ledger validation failed",
                "validation_errors": errors[:5],
                "file": str(ledger_path),
            })
            return

    record_invoice_payment(data_dir, invoice_number, parsed_date.isoformat())

    result = {
        "status": "ok",
        "invoice_number": invoice_number,
        "client": client_config.name,
        "amount": round(total, 2),
        "payment_date": parsed_date.isoformat(),
        "bank_account": bank_account,
    }
    if ledger_path:
        result["file"] = str(ledger_path)
    if not should_post:
        result["no_post"] = True
    _output(result)


@invoice.command("create")
@click.argument("client_key")
@click.option("--service", "-s", help="Service key")
@click.option("--qty", "-q", type=float, help="Quantity")
@click.option("--description", help="Line item description")
@click.option("--item", multiple=True, help='Manual item: "description" amount')
@click.option("--entity", "-e", help="Entity key")
@pass_ctx
def invoice_create(ctx, client_key, service, qty, description, item, entity):
    """Create a manual single invoice.

    Creates work entries with the invoice number pre-assigned and
    generates a PDF. The invoice is visible to 'invoice list' and 'invoice paid'.
    """
    from istota.money.core.invoicing import (
        generate_invoice_html, generate_invoice_pdf,
        format_invoice_number, highest_existing_invoice_number,
        persist_next_invoice_number,
        resolve_entity as resolve_entity_fn, build_line_items,
    )
    from istota.money.core.models import InvoiceLineItem, Invoice
    from istota.money.work import add_work_entry, get_entries_for_invoice

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException as e:
        _output({"status": "error", "error": str(e)})
        return

    client_config = config.clients.get(client_key)
    if not client_config:
        available = list(config.clients.keys())
        _output({"status": "error", "error": f"Client '{client_key}' not found. Available: {', '.join(available)}"})
        return

    if entity:
        if entity not in config.companies:
            available = list(config.companies.keys())
            _output({"status": "error", "error": f"Entity '{entity}' not found. Available: {', '.join(available)}"})
            return
        resolved_entity = config.companies[entity]
    else:
        resolved_entity = resolve_entity_fn(config, client_config=client_config)

    service_entries = []
    if service:
        if service not in config.services:
            available = list(config.services.keys())
            _output({"status": "error", "error": f"Service '{service}' not found. Available: {', '.join(available)}"})
            return
        service_entries.append((service, qty, description or "", entity or ""))

    manual_items = []
    if item:
        for item_str in item:
            parts = item_str.rsplit(" ", 1)
            if len(parts) != 2:
                _output({"status": "error", "error": f"Invalid item format: {item_str}. Use: \"description\" amount"})
                return
            desc = parts[0].strip('"').strip("'")
            try:
                amt = float(parts[1])
            except ValueError:
                _output({"status": "error", "error": f"Invalid amount in item: {parts[1]}"})
                return
            manual_items.append((desc, amt))

    if not service_entries and not manual_items:
        _output({"status": "error", "error": "No line items specified. Use --service/--qty or --item"})
        return

    data_dir = _require_data_dir(ctx)
    invoice_number = max(
        config.next_invoice_number,
        highest_existing_invoice_number(data_dir) + 1,
    )
    invoice_date = date.today()
    number_str = format_invoice_number(invoice_number)

    # Insert service-based work entries with invoice pre-assigned
    for svc_key, svc_qty, svc_desc, svc_entity in service_entries:
        add_work_entry(
            data_dir, invoice_date.isoformat(), client_key, svc_key,
            qty=svc_qty, description=svc_desc, entity=svc_entity,
            invoice=number_str,
        )

    # Insert manual items with invoice pre-assigned
    for desc, amt in manual_items:
        add_work_entry(
            data_dir, invoice_date.isoformat(), client_key, "_manual",
            amount=amt, description=desc, entity=entity or "",
            invoice=number_str,
        )

    # Build line items from the entries we just created
    inv_entries = get_entries_for_invoice(data_dir, number_str)

    items = build_line_items(
        [e for e in inv_entries if e.service != "_manual"],
        config.services,
    )
    for e in inv_entries:
        if e.service == "_manual":
            items.append(InvoiceLineItem(
                display_name=e.description or "Manual item",
                description="",
                quantity=1, rate=e.amount or 0,
                discount=0, amount=e.amount or 0,
            ))

    total = sum(i.amount for i in items)
    due_date = invoice_date + timedelta(days=client_config.terms) if isinstance(client_config.terms, int) else None

    inv = Invoice(
        number=number_str,
        date=invoice_date, due_date=due_date,
        client=client_config, company=resolved_entity,
        items=items, total=total, group_name="",
    )

    logo_path = None
    if resolved_entity.logo:
        logo_path = accounting_path / resolved_entity.logo
        if not logo_path.exists():
            logo_path = None
    html = generate_invoice_html(inv, logo_path=logo_path)
    output_dir = invoice_output_dir / str(invoice_date.year)
    pdf_filename = f"Invoice-{invoice_number:06d}-{invoice_date.strftime('%m_%d_%Y')}.pdf"
    pdf_path = output_dir / pdf_filename
    generate_invoice_pdf(html, pdf_path)

    persist_next_invoice_number(
        invoice_number + 1, db_path=ctx.db_path,
        config_path=ctx.invoicing_config_path,
    )

    _output({
        "status": "ok",
        "invoice_number": inv.number,
        "client": client_config.name,
        "total": round(total, 2),
        "due_date": due_date.isoformat() if due_date else str(client_config.terms),
        "file": str(pdf_path),
    })


@invoice.command("void")
@click.argument("invoice_number")
@click.option("--force", is_flag=True, help="Void even if invoice has been paid")
@click.option("--delete-pdf", is_flag=True, help="Delete the generated PDF file")
@pass_ctx
def invoice_void(ctx, invoice_number, force, delete_pdf):
    """Void an invoice, clearing it from work entries.

    Removes the invoice number and paid_date from all associated work entries,
    cleans up DB state (overdue notifications), and optionally deletes the PDF.
    """
    from istota.money.work import get_entries_for_invoice, void_invoice

    data_dir = _require_data_dir(ctx)
    entries = get_entries_for_invoice(data_dir, invoice_number)
    if not entries:
        _output({"status": "error", "error": f"Invoice {invoice_number} not found"})
        return

    is_paid = any(e.paid_date is not None for e in entries)
    if is_paid and not force:
        _output({
            "status": "error",
            "error": f"Invoice {invoice_number} has been marked as paid. Use --force to void anyway.",
        })
        return

    count = void_invoice(data_dir, invoice_number)

    # Clean up DB state
    db_cleanup = {}
    db_conn = _get_db_conn(ctx)
    if db_conn:
        try:
            from istota.money.db import clear_invoice_state
            db_cleanup = clear_invoice_state(db_conn, invoice_number)
            db_conn.commit()
        finally:
            db_conn.close()

    # Optionally delete PDF
    pdf_deleted = False
    if delete_pdf:
        try:
            _, _, invoice_output_dir = _load_invoicing_config(ctx)
            from istota.money.core.invoicing import delete_invoice_pdf
            pdf_deleted = delete_invoice_pdf(invoice_output_dir, invoice_number)
        except click.ClickException:
            pass

    result = {
        "status": "ok",
        "invoice_number": invoice_number,
        "entries_voided": count,
        "was_paid": is_paid,
        "message": f"Voided invoice {invoice_number} ({count} work entries cleared)",
    }
    if db_cleanup:
        result["db_cleanup"] = db_cleanup
    if delete_pdf:
        result["pdf_deleted"] = pdf_deleted
    _output(result)


def _apply_monarch_status(out: dict, monarch_result: dict | None) -> None:
    """Roll up nested Monarch sync errors to the outer envelope.

    The scheduler's JSON-error envelope detector (see
    `scheduler._execute_command_task`) only fires on top-level
    `status == "error"`. Without this rollup a broken Monarch sync would
    nest its error under `out["monarch"]` and the task would silently
    succeed. We promote a hard error if the sync itself failed, or a
    `partial_error` if any individual profile failed.
    """
    if monarch_result is None:
        return
    if monarch_result.get("status") == "error":
        out["status"] = "error"
        out["error"] = f"monarch sync failed: {monarch_result.get('error', 'unknown error')}"
        return
    profiles = monarch_result.get("profiles") or []
    failed = [p for p in profiles if isinstance(p, dict) and p.get("status") == "error"]
    if failed:
        out["status"] = "partial_error"
        names = ", ".join(p.get("name") or p.get("ledger") or "?" for p in failed)
        out["monarch_errors"] = f"{len(failed)} profile(s) failed: {names}"


@cli.command("run-scheduled")
@click.option("--dry-run", is_flag=True, help="Preview without generating files")
@click.option("--skip-monarch", is_flag=True, help="Skip the monarch sync step")
@pass_ctx
def run_scheduled(ctx, dry_run, skip_monarch):
    """Run periodic money tasks: monarch sync (if configured) + invoice schedule check.

    Meant to be called periodically by cron. The monarch sync runs first
    when ``monarch_config`` is set; the invoice scheduler then checks each
    client's invoicing schedule and generates invoices when due. Either
    half is optional — users with only one feature configured get only
    that step.
    """
    from istota.money.core.invoicing import check_scheduled_invoices, generate_invoices_for_period
    from istota.money.db import set_invoice_schedule_generation

    monarch_result: dict | None = None
    if ctx.monarch_config_path and not skip_monarch:
        monarch_result = _run_monarch_sync(ctx, dry_run=dry_run, ledger=None)

    try:
        config, accounting_path, invoice_output_dir = _load_invoicing_config(ctx)
    except click.ClickException:
        # No invoicing config — return whatever monarch did (if anything)
        out = {"status": "ok", "message": "No invoicing config; nothing to schedule"}
        if monarch_result is not None:
            out["monarch"] = monarch_result
        _apply_monarch_status(out, monarch_result)
        _output(out)
        return

    data_dir = _require_data_dir(ctx)
    db_conn = _require_db(ctx)

    try:
        due_clients = check_scheduled_invoices(config, db_conn)
        if not due_clients:
            out = {
                "status": "ok",
                "message": "No scheduled invoices due",
                "clients_checked": len(
                    [c for c in config.clients.values() if c.schedule == "monthly"]
                ),
            }
            if monarch_result is not None:
                out["monarch"] = monarch_result
            _apply_monarch_status(out, monarch_result)
            _output(out)
            return

        all_results = []
        for client_key in due_clients:
            results = generate_invoices_for_period(
                config=config, config_path=ctx.invoicing_config_path,
                accounting_path=accounting_path, data_dir=data_dir,
                client_filter=client_key, dry_run=dry_run,
                invoice_output_dir=invoice_output_dir,
                db_path=ctx.db_path,
            )
            if results and not dry_run:
                set_invoice_schedule_generation(db_conn, client_key)
            for r in results:
                r["client_key"] = client_key
            all_results.extend(results)

        db_conn.commit()

        total = sum(r["total"] for r in all_results)
        out = {
            "status": "ok",
            "dry_run": dry_run,
            "clients_due": due_clients,
            "invoice_count": len(all_results),
            "total": round(total, 2),
            "invoices": all_results,
        }
        if monarch_result is not None:
            out["monarch"] = monarch_result
        _apply_monarch_status(out, monarch_result)
        _output(out)
    finally:
        db_conn.commit()
        db_conn.close()


if __name__ == "__main__":
    cli()
