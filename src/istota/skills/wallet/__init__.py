"""Task-owned wallet intents. Card secrets are available only to the fill proxy."""

import argparse
from dataclasses import asdict
import logging
import os
from pathlib import Path

from .._cli import error_envelope, parse_and_resolve, run_skill_cli

logger = logging.getLogger(__name__)


def _dispatch(args):
    from istota import db
    from istota.config import load_config
    from istota.notifications.store import deliver_pending
    from istota.wallet import cards, policy, purchases
    from istota.wallet.money import parse_amount

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    path = os.environ.get("ISTOTA_DB_PATH", "")
    if not user_id or not task_id.isascii() or not task_id.isdecimal() or not path:
        raise purchases.WalletRefusal("task_unavailable")
    task_id = int(task_id)
    config = load_config()
    notification = None
    with db.get_db(Path(path)) as conn:
        task = db.get_task(conn, task_id)
        if task is None or task.user_id != user_id or task.status != "running":
            raise purchases.WalletRefusal("task_unavailable")
        if args.command == "request":
            result = purchases.request(
                conn, config, user_id=user_id, task_id=task_id,
                card=int(args.card) if args.card.isascii() and args.card.isdecimal() else args.card,
                merchant=args.merchant, amount_cents=parse_amount(args.amount, args.currency),
                currency=args.currency, description=args.description,
                extra_hosts=args.frame_host, request_key=args.request_key,
            )
            output = {key: getattr(result, key) for key in
                      ("status", "purchase_id", "approval", "reason", "expires_at")}
            notification = result.notification
        else:
            if policy.unavailable_reason(conn, config, user_id, task):
                raise purchases.WalletRefusal("wallet_unavailable")
            if args.command == "cards":
                spending = policy.get_policy(conn, user_id)
                output = {"status": "ok", "cards": [asdict(card) for card in cards.list_cards(conn, user_id)],
                          "currency": spending.currency,
                          "remaining_auto_budget_cents": max(0, spending.auto_budget_cents - policy.auto_spent_cents(conn, user_id))}
            else:
                purchase = purchases.get_purchase(conn, user_id, args.purchase_id)
                if purchase is None:
                    raise purchases.WalletRefusal("purchase_not_found")
                if args.command == "status":
                    output = {"status": "ok", "purchase": purchase}
                else:
                    kwargs = dict(user_id=user_id, task_id=task_id, purchase_id=args.purchase_id)
                    if args.command == "complete":
                        amount = parse_amount(args.amount, purchase["currency"]) if args.amount is not None else None
                        purchases.complete(conn, **kwargs, order_ref=args.order_ref, amount_cents=amount)
                    elif args.command == "fail":
                        purchases.fail(conn, **kwargs, reason=args.reason)
                    else:
                        purchases.cancel(conn, **kwargs)
                    output = {"status": "ok", "purchase_id": args.purchase_id}
    if notification is not None:
        deliver_pending(config, [notification])
    return output


def _error(exc):
    from istota.relay.requests import RequestError
    from istota.wallet.purchases import WalletRefusal

    if isinstance(exc, WalletRefusal):
        reason = exc.reason
    elif isinstance(exc, RequestError) and str(exc) in {"confirmation_pending", "invalid_preview"}:
        reason = str(exc)
    elif isinstance(exc, ValueError):
        reason = "invalid_request"
    else:
        logger.error("wallet request failed exception=%s", type(exc).__name__)
        reason = "wallet_error"
    return error_envelope("Wallet request refused", reason=reason)


def build_parser():
    parser = argparse.ArgumentParser(description="Request and track a purchase")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("cards")
    request = commands.add_parser("request")
    for name in ("card", "merchant", "amount", "currency", "description"):
        request.add_argument(f"--{name}", required=True)
    request.add_argument("--frame-host", action="append", default=[])
    request.add_argument("--request-key")
    for name in ("status", "complete", "fail", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("purchase_id", type=int)
        if name == "complete":
            command.add_argument("--order-ref")
            command.add_argument("--amount")
        elif name == "fail":
            command.add_argument("--reason")
    return parser


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    run_skill_cli({name: _dispatch for name in ("cards", "request", "status", "complete", "fail", "cancel")},
                  args, on_exception=_error)
