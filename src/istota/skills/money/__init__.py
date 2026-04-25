"""Money accounting operations -- in-process facade.

Invokes the in-tree ``istota.money`` Click CLI in-process. The user's
:class:`UserContext` is resolved up front via :func:`istota.money.resolve_for_user`
against the istota config and injected into Click via ``obj=``. No env-var
marshaling, no subprocess, no HTTP.
"""

import argparse
import json
import os
import sys


def _run(args: list[str]) -> dict:
    """Resolve the user's UserContext, invoke money.cli.cli, return parsed JSON."""
    from click.testing import CliRunner

    from istota.config import load_config
    from istota.money import (
        UserNotFoundError,
        load_user_secrets,
        resolve_for_user,
    )
    from istota.money.cli import Context, cli

    user_id = os.environ.get("MONEY_USER", "") or ""
    if not user_id:
        return {"status": "error", "error": "MONEY_USER not set"}

    istota_cfg = load_config()
    try:
        user_ctx = resolve_for_user(user_id, istota_cfg)
    except UserNotFoundError as e:
        return {"status": "error", "error": str(e)}

    obj = Context()
    obj.users[user_id] = user_ctx
    obj.activate_user(user_id)
    obj.secrets = load_user_secrets(user_id, istota_cfg) or None

    runner = CliRunner()
    result = runner.invoke(
        cli, ["-u", user_id, *args],
        obj=obj,
        standalone_mode=False,
        catch_exceptions=True,
    )

    if result.exception is not None and not isinstance(result.exception, SystemExit):
        return {"status": "error", "error": f"{type(result.exception).__name__}: {result.exception}"}
    if result.exit_code not in (0, None):
        return {
            "status": "error",
            "error": (result.output or f"exit {result.exit_code}").strip(),
        }

    output = (result.output or "").strip()
    if not output:
        return {"status": "error", "error": "no output from money CLI"}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"status": "error", "error": f"invalid JSON from CLI: {output[:200]}"}


def _output(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Ledger commands
# ---------------------------------------------------------------------------


def cmd_list(args):
    _output(_run(["list"]))


def cmd_check(args):
    cli_args = ["check"]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_balances(args):
    cli_args = ["balances"]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    if args.account:
        cli_args += ["--account", args.account]
    _output(_run(cli_args))


def cmd_query(args):
    cli_args = ["query", args.bql]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_report(args):
    cli_args = ["report", args.report_type]
    if args.year:
        cli_args += ["--year", str(args.year)]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_lots(args):
    cli_args = ["lots", args.symbol]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_wash_sales(args):
    cli_args = ["wash-sales"]
    if args.year:
        cli_args += ["--year", str(args.year)]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


# ---------------------------------------------------------------------------
# Transaction commands
# ---------------------------------------------------------------------------


def cmd_add_transaction(args):
    cli_args = [
        "add-transaction",
        "--date", args.txn_date,
        "--payee", args.payee,
        "--narration", args.narration,
        "--debit", args.debit,
        "--credit", args.credit,
        "--amount", str(args.amount),
        "--currency", args.currency,
    ]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_sync_monarch(args):
    cli_args = ["sync-monarch"]
    if args.dry_run:
        cli_args.append("--dry-run")
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_import_csv(args):
    cli_args = ["import-csv", args.file, "--account", args.account]
    if args.tag:
        for t in args.tag:
            cli_args += ["--tag", t]
    if args.exclude_tag:
        for t in args.exclude_tag:
            cli_args += ["--exclude-tag", t]
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


# ---------------------------------------------------------------------------
# Invoice commands
# ---------------------------------------------------------------------------


def cmd_invoice_generate(args):
    cli_args = ["invoice", "generate"]
    if args.period:
        cli_args += ["--period", args.period]
    if args.client:
        cli_args += ["--client", args.client]
    if args.entity:
        cli_args += ["--entity", args.entity]
    if args.dry_run:
        cli_args.append("--dry-run")
    _output(_run(cli_args))


def cmd_invoice_list(args):
    cli_args = ["invoice", "list"]
    if args.client:
        cli_args += ["--client", args.client]
    if args.show_all:
        cli_args.append("--all")
    _output(_run(cli_args))


def cmd_invoice_paid(args):
    cli_args = ["invoice", "paid", args.invoice_number, "--date", args.payment_date]
    if args.bank:
        cli_args += ["--bank", args.bank]
    if args.no_post:
        cli_args.append("--no-post")
    if args.ledger:
        cli_args += ["--ledger", args.ledger]
    _output(_run(cli_args))


def cmd_invoice_create(args):
    cli_args = ["invoice", "create", args.client_key]
    if args.service:
        cli_args += ["--service", args.service]
    if args.qty is not None:
        cli_args += ["--qty", str(args.qty)]
    if args.description:
        cli_args += ["--description", args.description]
    if args.entity:
        cli_args += ["--entity", args.entity]
    if args.item:
        for i in args.item:
            cli_args += ["--item", i]
    _output(_run(cli_args))


def cmd_invoice_void(args):
    cli_args = ["invoice", "void", args.invoice_number]
    if args.force:
        cli_args.append("--force")
    if args.delete_pdf:
        cli_args.append("--delete-pdf")
    _output(_run(cli_args))


# ---------------------------------------------------------------------------
# Work commands
# ---------------------------------------------------------------------------


def cmd_work_list(args):
    cli_args = ["work", "list"]
    if args.client:
        cli_args += ["--client", args.client]
    if args.period:
        cli_args += ["--period", args.period]
    if args.uninvoiced:
        cli_args.append("--uninvoiced")
    if args.invoiced:
        cli_args.append("--invoiced")
    _output(_run(cli_args))


def cmd_work_add(args):
    cli_args = [
        "work", "add",
        "--date", args.entry_date,
        "--client", args.client,
        "--service", args.service,
    ]
    if args.qty is not None:
        cli_args += ["--qty", str(args.qty)]
    if args.amount is not None:
        cli_args += ["--amount", str(args.amount)]
    if args.discount is not None:
        cli_args += ["--discount", str(args.discount)]
    if args.description:
        cli_args += ["--description", args.description]
    if args.entity:
        cli_args += ["--entity", args.entity]
    _output(_run(cli_args))


def cmd_work_update(args):
    cli_args = ["work", "update", str(args.entry_id)]
    if args.entry_date is not None:
        cli_args += ["--date", args.entry_date]
    if args.client is not None:
        cli_args += ["--client", args.client]
    if args.service is not None:
        cli_args += ["--service", args.service]
    if args.qty is not None:
        cli_args += ["--qty", str(args.qty)]
    if args.amount is not None:
        cli_args += ["--amount", str(args.amount)]
    if args.discount is not None:
        cli_args += ["--discount", str(args.discount)]
    if args.description is not None:
        cli_args += ["--description", args.description]
    if args.entity is not None:
        cli_args += ["--entity", args.entity]
    _output(_run(cli_args))


def cmd_work_remove(args):
    _output(_run(["work", "remove", str(args.entry_id)]))


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.money",
        description="Money accounting operations",
    )
    sub = parser.add_subparsers(dest="command")

    # --- Ledger commands ---
    sub.add_parser("list", help="List available ledgers")

    p_check = sub.add_parser("check", help="Validate ledger")
    p_check.add_argument("--ledger", "-l", help="Ledger name")

    p_bal = sub.add_parser("balances", help="Show account balances")
    p_bal.add_argument("--ledger", "-l", help="Ledger name")
    p_bal.add_argument("--account", "-a", help="Filter by account pattern")

    p_query = sub.add_parser("query", help="Run a BQL query")
    p_query.add_argument("bql", help="BQL query string")
    p_query.add_argument("--ledger", "-l", help="Ledger name")

    p_report = sub.add_parser("report", help="Generate financial report")
    p_report.add_argument("report_type", choices=["income-statement", "balance-sheet"])
    p_report.add_argument("--year", "-y", type=int, help="Year for report")
    p_report.add_argument("--ledger", "-l", help="Ledger name")

    p_lots = sub.add_parser("lots", help="Show open lots for a security")
    p_lots.add_argument("symbol", help="Security symbol")
    p_lots.add_argument("--ledger", "-l", help="Ledger name")

    p_ws = sub.add_parser("wash-sales", help="Detect wash sale violations")
    p_ws.add_argument("--year", "-y", type=int, help="Year to analyze")
    p_ws.add_argument("--ledger", "-l", help="Ledger name")

    # --- Transaction commands ---
    p_add = sub.add_parser("add-transaction", help="Add a transaction")
    p_add.add_argument("--date", "-d", dest="txn_date", required=True, help="Date (YYYY-MM-DD)")
    p_add.add_argument("--payee", "-p", required=True, help="Payee name")
    p_add.add_argument("--narration", "-n", required=True, help="Description")
    p_add.add_argument("--debit", required=True, help="Debit account")
    p_add.add_argument("--credit", required=True, help="Credit account")
    p_add.add_argument("--amount", "-a", required=True, type=float, help="Amount")
    p_add.add_argument("--currency", default="USD", help="Currency")
    p_add.add_argument("--ledger", "-l", help="Ledger name")

    p_sync = sub.add_parser("sync-monarch", help="Sync from Monarch Money")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview without writing")
    p_sync.add_argument("--ledger", "-l", help="Ledger name")

    p_csv = sub.add_parser("import-csv", help="Import transactions from CSV")
    p_csv.add_argument("file", help="CSV file path")
    p_csv.add_argument("--account", "-a", required=True, help="Bank account")
    p_csv.add_argument("--tag", "-t", action="append", help="Include tag")
    p_csv.add_argument("--exclude-tag", "-x", action="append", help="Exclude tag")
    p_csv.add_argument("--ledger", "-l", help="Ledger name")

    # --- Invoice commands (nested subparser) ---
    p_inv = sub.add_parser("invoice", help="Invoice management")
    inv_sub = p_inv.add_subparsers(dest="invoice_command")

    p_inv_gen = inv_sub.add_parser("generate", help="Generate invoices")
    p_inv_gen.add_argument("--period", "-p", help="Billing period (YYYY-MM)")
    p_inv_gen.add_argument("--client", "-c", help="Filter by client")
    p_inv_gen.add_argument("--entity", "-e", help="Filter by entity")
    p_inv_gen.add_argument("--dry-run", action="store_true", help="Preview only")

    p_inv_list = inv_sub.add_parser("list", help="List invoices")
    p_inv_list.add_argument("--client", "-c", help="Filter by client")
    p_inv_list.add_argument("--all", "-a", dest="show_all", action="store_true", help="Include paid")

    p_inv_paid = inv_sub.add_parser("paid", help="Record payment")
    p_inv_paid.add_argument("invoice_number", help="Invoice number")
    p_inv_paid.add_argument("--date", "-d", dest="payment_date", required=True, help="Payment date")
    p_inv_paid.add_argument("--bank", "-b", help="Bank account")
    p_inv_paid.add_argument("--no-post", action="store_true", help="Skip ledger posting")
    p_inv_paid.add_argument("--ledger", "-l", help="Ledger name")

    p_inv_create = inv_sub.add_parser("create", help="Create manual invoice")
    p_inv_create.add_argument("client_key", help="Client key")
    p_inv_create.add_argument("--service", "-s", help="Service key")
    p_inv_create.add_argument("--qty", "-q", type=float, help="Quantity")
    p_inv_create.add_argument("--description", help="Description")
    p_inv_create.add_argument("--entity", "-e", help="Entity key")
    p_inv_create.add_argument("--item", action="append", help="Manual item: \"description\" amount")

    p_inv_void = inv_sub.add_parser("void", help="Void an invoice")
    p_inv_void.add_argument("invoice_number", help="Invoice number")
    p_inv_void.add_argument("--force", action="store_true", help="Void even if paid")
    p_inv_void.add_argument("--delete-pdf", action="store_true", help="Delete PDF file")

    # --- Work commands (nested subparser) ---
    p_work = sub.add_parser("work", help="Work log management")
    work_sub = p_work.add_subparsers(dest="work_command")

    p_wl = work_sub.add_parser("list", help="List work entries")
    p_wl.add_argument("--client", "-c", help="Filter by client")
    p_wl.add_argument("--period", "-p", help="Filter by period (YYYY-MM)")
    p_wl.add_argument("--uninvoiced", action="store_true", help="Uninvoiced only")
    p_wl.add_argument("--invoiced", action="store_true", help="Invoiced only")

    p_wa = work_sub.add_parser("add", help="Add work entry")
    p_wa.add_argument("--date", "-d", dest="entry_date", required=True, help="Date (YYYY-MM-DD)")
    p_wa.add_argument("--client", "-c", required=True, help="Client key")
    p_wa.add_argument("--service", "-s", required=True, help="Service key")
    p_wa.add_argument("--qty", "-q", type=float, help="Quantity")
    p_wa.add_argument("--amount", type=float, help="Fixed amount")
    p_wa.add_argument("--discount", type=float, help="Discount")
    p_wa.add_argument("--description", help="Description")
    p_wa.add_argument("--entity", "-e", help="Entity override")

    p_wu = work_sub.add_parser("update", help="Update work entry")
    p_wu.add_argument("entry_id", type=int, help="Entry ID")
    p_wu.add_argument("--date", "-d", dest="entry_date", help="Date (YYYY-MM-DD)")
    p_wu.add_argument("--client", "-c", help="Client key")
    p_wu.add_argument("--service", "-s", help="Service key")
    p_wu.add_argument("--qty", "-q", type=float, help="Quantity")
    p_wu.add_argument("--amount", type=float, help="Fixed amount")
    p_wu.add_argument("--discount", type=float, help="Discount")
    p_wu.add_argument("--description", help="Description")
    p_wu.add_argument("--entity", "-e", help="Entity override")

    p_wr = work_sub.add_parser("remove", help="Remove work entry")
    p_wr.add_argument("entry_id", type=int, help="Entry ID")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "list": cmd_list,
        "check": cmd_check,
        "balances": cmd_balances,
        "query": cmd_query,
        "report": cmd_report,
        "lots": cmd_lots,
        "wash-sales": cmd_wash_sales,
        "add-transaction": cmd_add_transaction,
        "sync-monarch": cmd_sync_monarch,
        "import-csv": cmd_import_csv,
    }

    if args.command == "invoice":
        invoice_commands = {
            "generate": cmd_invoice_generate,
            "list": cmd_invoice_list,
            "paid": cmd_invoice_paid,
            "create": cmd_invoice_create,
            "void": cmd_invoice_void,
        }
        fn = invoice_commands.get(getattr(args, "invoice_command", None))
        if fn:
            fn(args)
        else:
            parser.parse_args(["invoice", "--help"])
    elif args.command == "work":
        work_commands = {
            "list": cmd_work_list,
            "add": cmd_work_add,
            "update": cmd_work_update,
            "remove": cmd_work_remove,
        }
        fn = work_commands.get(getattr(args, "work_command", None))
        if fn:
            fn(args)
        else:
            parser.parse_args(["work", "--help"])
    else:
        fn = commands.get(args.command)
        if fn:
            fn(args)
        else:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
