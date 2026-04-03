"""Moneyman accounting operations -- dual-mode client (CLI or HTTP).

Local mode (MONEYMAN_CLI_PATH set): runs moneyman CLI as subprocess.
Remote mode (MONEYMAN_API_URL set): HTTP client for the Moneyman REST API.
"""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path
import sys


def _mode() -> str:
    """Determine transport mode: 'cli' or 'http'."""
    if os.environ.get("MONEYMAN_CLI_PATH"):
        return "cli"
    if os.environ.get("MONEYMAN_API_URL"):
        return "http"
    print(json.dumps({"error": "MONEYMAN_CLI_PATH or MONEYMAN_API_URL must be set"}))
    sys.exit(1)


def _run_cli(args: list[str]) -> dict:
    """Run moneyman CLI command and return parsed JSON output."""
    cli_path = os.environ["MONEYMAN_CLI_PATH"]
    user = os.environ.get("MONEYMAN_USER", "")
    config = os.environ.get("MONEYMAN_CONFIG", "")

    # Default: config.toml next to the venv (cli_path/../../../config.toml)
    if not config:
        default_config = Path(cli_path).resolve().parent.parent.parent / "config.toml"
        if default_config.exists():
            config = str(default_config)

    cmd = [cli_path]
    if config:
        cmd += ["-c", config]
    if user:
        cmd += ["-u", user]
    cmd += args

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return {"status": "error", "error": f"moneyman CLI not found: {cli_path}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "moneyman CLI timed out"}

    stdout = result.stdout.strip()
    if not stdout:
        if result.returncode != 0:
            return {"status": "error", "error": result.stderr.strip() or f"Exit code {result.returncode}"}
        return {"status": "error", "error": "No output from moneyman CLI"}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": f"Invalid JSON from CLI: {stdout[:200]}"}


def _derive_user_key(master_key: str, username: str) -> str:
    """Derive a per-user API key: HMAC-SHA256(master_key, username)."""
    return hmac.new(
        master_key.encode(), username.encode(), hashlib.sha256,
    ).hexdigest()


def _http_client():
    """Create an httpx client for remote mode.

    When both MONEYMAN_API_KEY and MONEYMAN_USER are set, derives a per-user
    key via HMAC so the API scopes access to that user.
    """
    import httpx

    base_url = os.environ.get("MONEYMAN_API_URL", "")
    api_key = os.environ.get("MONEYMAN_API_KEY", "")
    user = os.environ.get("MONEYMAN_USER", "")

    headers = {}
    if api_key and user:
        # Derive per-user key — API verifies HMAC(master, user)
        headers["X-API-Key"] = _derive_user_key(api_key, user)
        headers["X-User"] = user
    elif api_key:
        headers["X-API-Key"] = api_key

    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=60.0,
        follow_redirects=True,
    )


def _output(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _handle_response(resp):
    """Parse HTTP response, handle errors."""
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"detail": resp.text}
        _output({"status": "error", "error": body.get("detail", str(body))})
        sys.exit(1)
    return resp.json()


# ---------------------------------------------------------------------------
# Ledger commands
# ---------------------------------------------------------------------------


def cmd_list(args):
    if _mode() == "cli":
        _output(_run_cli(["list"]))
    else:
        with _http_client() as client:
            resp = client.get("/api/ledgers")
            _output(_handle_response(resp))


def cmd_check(args):
    if _mode() == "cli":
        cli_args = ["check"]
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        params = {}
        if args.ledger:
            params["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.get("/api/check", params=params)
            _output(_handle_response(resp))


def cmd_balances(args):
    if _mode() == "cli":
        cli_args = ["balances"]
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        if args.account:
            cli_args += ["--account", args.account]
        _output(_run_cli(cli_args))
    else:
        params = {}
        if args.ledger:
            params["ledger"] = args.ledger
        if args.account:
            params["account"] = args.account
        with _http_client() as client:
            resp = client.get("/api/balances", params=params)
            _output(_handle_response(resp))


def cmd_query(args):
    if _mode() == "cli":
        cli_args = ["query", args.bql]
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        body = {"bql": args.bql}
        if args.ledger:
            body["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.post("/api/query", json=body)
            _output(_handle_response(resp))


def cmd_report(args):
    if _mode() == "cli":
        cli_args = ["report", args.report_type]
        if args.year:
            cli_args += ["--year", str(args.year)]
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        params = {}
        if args.year:
            params["year"] = args.year
        if args.ledger:
            params["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.get(f"/api/reports/{args.report_type}", params=params)
            _output(_handle_response(resp))


def cmd_lots(args):
    if _mode() == "cli":
        cli_args = ["lots", args.symbol]
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        params = {}
        if args.ledger:
            params["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.get(f"/api/lots/{args.symbol}", params=params)
            _output(_handle_response(resp))


def cmd_wash_sales(args):
    if _mode() == "cli":
        cli_args = ["wash-sales"]
        if args.year:
            cli_args += ["--year", str(args.year)]
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        params = {}
        if args.year:
            params["year"] = args.year
        if args.ledger:
            params["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.get("/api/wash-sales", params=params)
            _output(_handle_response(resp))


# ---------------------------------------------------------------------------
# Transaction commands
# ---------------------------------------------------------------------------


def cmd_add_transaction(args):
    if _mode() == "cli":
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
        _output(_run_cli(cli_args))
    else:
        body = {
            "date": args.txn_date,
            "payee": args.payee,
            "narration": args.narration,
            "debit": args.debit,
            "credit": args.credit,
            "amount": args.amount,
            "currency": args.currency,
        }
        if args.ledger:
            body["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.post("/api/transactions", json=body)
            _output(_handle_response(resp))


def cmd_sync_monarch(args):
    if _mode() == "cli":
        cli_args = ["sync-monarch"]
        if args.dry_run:
            cli_args.append("--dry-run")
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        body = {"dry_run": args.dry_run}
        if args.ledger:
            body["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.post("/api/transactions/sync-monarch", json=body)
            _output(_handle_response(resp))


def cmd_import_csv(args):
    if _mode() == "cli":
        cli_args = ["import-csv", args.file, "--account", args.account]
        if args.tag:
            for t in args.tag:
                cli_args += ["--tag", t]
        if args.exclude_tag:
            for t in args.exclude_tag:
                cli_args += ["--exclude-tag", t]
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        body = {
            "file": args.file,
            "account": args.account,
        }
        if args.ledger:
            body["ledger"] = args.ledger
        if args.tag:
            body["include_tags"] = list(args.tag)
        if args.exclude_tag:
            body["exclude_tags"] = list(args.exclude_tag)
        with _http_client() as client:
            resp = client.post("/api/transactions/import-csv", json=body)
            _output(_handle_response(resp))


# ---------------------------------------------------------------------------
# Invoice commands
# ---------------------------------------------------------------------------


def cmd_invoice_generate(args):
    if _mode() == "cli":
        cli_args = ["invoice", "generate"]
        if args.period:
            cli_args += ["--period", args.period]
        if args.client:
            cli_args += ["--client", args.client]
        if args.entity:
            cli_args += ["--entity", args.entity]
        if args.dry_run:
            cli_args.append("--dry-run")
        _output(_run_cli(cli_args))
    else:
        body = {"dry_run": args.dry_run}
        if args.period:
            body["period"] = args.period
        if args.client:
            body["client"] = args.client
        if args.entity:
            body["entity"] = args.entity
        with _http_client() as client:
            resp = client.post("/api/invoices/generate", json=body)
            _output(_handle_response(resp))


def cmd_invoice_list(args):
    if _mode() == "cli":
        cli_args = ["invoice", "list"]
        if args.client:
            cli_args += ["--client", args.client]
        if args.show_all:
            cli_args.append("--all")
        _output(_run_cli(cli_args))
    else:
        params = {}
        if args.client:
            params["client"] = args.client
        if args.show_all:
            params["all"] = True
        with _http_client() as client:
            resp = client.get("/api/invoices", params=params)
            _output(_handle_response(resp))


def cmd_invoice_paid(args):
    if _mode() == "cli":
        cli_args = ["invoice", "paid", args.invoice_number, "--date", args.payment_date]
        if args.bank:
            cli_args += ["--bank", args.bank]
        if args.no_post:
            cli_args.append("--no-post")
        if args.ledger:
            cli_args += ["--ledger", args.ledger]
        _output(_run_cli(cli_args))
    else:
        body = {
            "invoice_number": args.invoice_number,
            "date": args.payment_date,
            "no_post": args.no_post,
        }
        if args.bank:
            body["bank"] = args.bank
        if args.ledger:
            body["ledger"] = args.ledger
        with _http_client() as client:
            resp = client.post("/api/invoices/paid", json=body)
            _output(_handle_response(resp))


def cmd_invoice_create(args):
    if _mode() == "cli":
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
        _output(_run_cli(cli_args))
    else:
        body = {"client_key": args.client_key}
        if args.service:
            body["service"] = args.service
        if args.qty is not None:
            body["qty"] = args.qty
        if args.description:
            body["description"] = args.description
        if args.entity:
            body["entity"] = args.entity
        if args.item:
            body["items"] = list(args.item)
        with _http_client() as client:
            resp = client.post("/api/invoices/create", json=body)
            _output(_handle_response(resp))


def cmd_invoice_void(args):
    if _mode() == "cli":
        cli_args = ["invoice", "void", args.invoice_number]
        if args.force:
            cli_args.append("--force")
        if args.delete_pdf:
            cli_args.append("--delete-pdf")
        _output(_run_cli(cli_args))
    else:
        body = {
            "invoice_number": args.invoice_number,
            "force": args.force,
            "delete_pdf": args.delete_pdf,
        }
        with _http_client() as client:
            resp = client.post("/api/invoices/void", json=body)
            _output(_handle_response(resp))


# ---------------------------------------------------------------------------
# Work commands
# ---------------------------------------------------------------------------


def cmd_work_list(args):
    if _mode() == "cli":
        cli_args = ["work", "list"]
        if args.client:
            cli_args += ["--client", args.client]
        if args.period:
            cli_args += ["--period", args.period]
        if args.uninvoiced:
            cli_args.append("--uninvoiced")
        if args.invoiced:
            cli_args.append("--invoiced")
        _output(_run_cli(cli_args))
    else:
        params = {}
        if args.client:
            params["client"] = args.client
        if args.period:
            params["period"] = args.period
        if args.uninvoiced:
            params["uninvoiced"] = True
        if args.invoiced:
            params["invoiced"] = True
        with _http_client() as client:
            resp = client.get("/api/work/", params=params)
            _output(_handle_response(resp))


def cmd_work_add(args):
    if _mode() == "cli":
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
        _output(_run_cli(cli_args))
    else:
        body = {
            "date": args.entry_date,
            "client": args.client,
            "service": args.service,
        }
        if args.qty is not None:
            body["qty"] = args.qty
        if args.amount is not None:
            body["amount"] = args.amount
        if args.discount is not None:
            body["discount"] = args.discount
        if args.description:
            body["description"] = args.description
        if args.entity:
            body["entity"] = args.entity
        with _http_client() as client:
            resp = client.post("/api/work/", json=body)
            _output(_handle_response(resp))


def cmd_work_update(args):
    if _mode() == "cli":
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
        _output(_run_cli(cli_args))
    else:
        body = {}
        if args.entry_date is not None:
            body["date"] = args.entry_date
        if args.client is not None:
            body["client"] = args.client
        if args.service is not None:
            body["service"] = args.service
        if args.qty is not None:
            body["qty"] = args.qty
        if args.amount is not None:
            body["amount"] = args.amount
        if args.discount is not None:
            body["discount"] = args.discount
        if args.description is not None:
            body["description"] = args.description
        if args.entity is not None:
            body["entity"] = args.entity
        if not body:
            _output({"status": "error", "error": "No fields to update"})
            sys.exit(1)
        with _http_client() as client:
            resp = client.put(f"/api/work/{args.entry_id}", json=body)
            _output(_handle_response(resp))


def cmd_work_remove(args):
    if _mode() == "cli":
        _output(_run_cli(["work", "remove", str(args.entry_id)]))
    else:
        with _http_client() as client:
            resp = client.delete(f"/api/work/{args.entry_id}")
            _output(_handle_response(resp))


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.moneyman",
        description="Moneyman accounting operations",
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
