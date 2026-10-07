"""Task-bound purchase authorization and the bounded card-fill claim.

Writers use the caller's connection and leave commit to the caller. Request
and fill take an immediate write lock before reading policy or authorization.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json

from istota import db
from istota.wallet import cards, policy
from istota.wallet.hosts import normalize_merchant
from istota.wallet.money import MAX_MINOR_UNITS, currency_code, format_amount

OPEN_STATES = ("held", "authorized", "filled")
_PUBLIC_REASONS = frozenset({"card_not_found", "card_paused", "card_expired", "wallet_request_limit", "over_ceiling"})


class WalletRefusal(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PurchaseResult:
    status: str
    purchase_id: int | None
    approval: str | None = None
    reason: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, repr=False)
class FillGrant:
    fields: dict[str, str]
    bound_hosts: list[str]

    def __repr__(self):
        return "FillGrant(<redacted>)"


def _stamp(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _atomic(conn):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT wallet_operation")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO wallet_operation")
        conn.execute("RELEASE wallet_operation")
        raise
    else:
        conn.execute("RELEASE wallet_operation")


def _result(row):
    reason = row["reason"]
    if row["state"] == "refused" and reason not in _PUBLIC_REASONS:
        reason = "wallet_unavailable"
    return PurchaseResult(row["state"], row["id"], row["approval"], reason, row["expires_at"])


def _amount(value):
    if type(value) is not int or not 0 < value <= MAX_MINOR_UNITS:
        raise ValueError("amount must be positive integer minor units")


def request(conn, config, *, user_id, task_id, card, merchant, amount_cents, currency,
            description="", extra_hosts=(), request_key=None) -> PurchaseResult:
    _amount(amount_cents)
    currency_code(currency)
    merchant_host = normalize_merchant(merchant)
    hosts = sorted({normalize_merchant(host) for host in extra_hosts} - {merchant_host})
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError("description must be text of at most 500 characters")
    description = " ".join(description.split())
    if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128):
        raise ValueError("request key must be text of 1 to 128 characters")
    with _atomic(conn):
        task = db.get_task(conn, task_id)
        card_view = cards.get_card(conn, user_id, card)
        unavailable = policy.unavailable_reason(conn, config, user_id, task)
        if request_key is not None:
            old = conn.execute("SELECT * FROM wallet_purchases WHERE user_id=? AND task_id=? AND request_key=?",
                               (user_id, task_id, request_key)).fetchone()
            if old is not None:
                if unavailable:
                    return PurchaseResult("refused", old["id"], reason="wallet_unavailable")
                same_card = (old["card_id"] == card_view.id if card_view else
                             old["card_id"] is None and old["card_label"] == str(card))
                if (same_card and old["merchant_host"] == merchant_host and json.loads(old["extra_hosts"]) == hosts
                        and old["amount_cents"] == amount_cents and old["currency"] == currency
                        and old["description"] == description):
                    return _result(old)
                return PurchaseResult("refused", old["id"], reason="request_key_conflict")
        decision = policy.decide(conn, config, user_id=user_id, task=task, card=card_view,
                                 amount_cents=amount_cents, currency=currency, merchant_host=merchant_host, extra_hosts=hosts)
        now = datetime.now(timezone.utc)
        authorized_at = _stamp(now) if decision.state == "authorized" else None
        minutes = max(5, min(240, config.security.wallet_authorization_minutes))
        expires_at = _stamp(now + timedelta(minutes=minutes)) if authorized_at else None
        cur = conn.execute(
            "INSERT INTO wallet_purchases (user_id,task_id,card_id,card_label,card_last_four,merchant_host,extra_hosts,"
            "amount_cents,currency,description,request_key,state,approval,reason,authorized_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, task_id, card_view.id if card_view else None, card_view.label if card_view else str(card)[:64],
             card_view.last_four if card_view else "", merchant_host, json.dumps(hosts), amount_cents, currency,
             description, request_key, decision.state, decision.approval, decision.reason, authorized_at, expires_at),
        )
        return _result(get_purchase(conn, user_id, cur.lastrowid))


def authorize_held(conn, purchase_id, digest, *, authorization_minutes=30):
    if not isinstance(digest, str) or not digest:
        raise WalletRefusal("purchase_not_authorized")
    with _atomic(conn):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=max(5, min(240, authorization_minutes)))
        changed = conn.execute("UPDATE wallet_purchases SET state='authorized', approval='user', approved_digest=?, "
                               "authorized_at=?, expires_at=?, updated_at=? WHERE id=? AND state='held'",
                               (digest, _stamp(now), _stamp(expires), _stamp(now), purchase_id)).rowcount
        if not changed:
            raise WalletRefusal("purchase_not_authorized")


def claim_fill(conn, config, *, user_id, task_id, purchase_id) -> FillGrant:
    with _atomic(conn):
        row = get_purchase(conn, user_id, purchase_id)
        if row is None or row["task_id"] != task_id:
            raise WalletRefusal("purchase_not_found")
        if row["state"] == "expired":
            raise WalletRefusal("purchase_expired")
        if row["state"] not in ("authorized", "filled"):
            raise WalletRefusal("purchase_not_authorized")
        if not row["expires_at"] or _stamp() >= row["expires_at"]:
            raise WalletRefusal("purchase_expired")
        if row["fill_count"] >= config.security.wallet_fills_per_purchase:
            raise WalletRefusal("purchase_fill_limit")
        card = cards.get_card(conn, user_id, row["card_id"])
        if card is None or card.state != "active" or cards.expired(card):
            raise WalletRefusal("purchase_not_authorized")
        if policy.unavailable_reason(conn, config, user_id, db.get_task(conn, task_id)):
            raise WalletRefusal("wallet_unavailable")
        conn.execute("UPDATE wallet_purchases SET state='filled', fill_count=fill_count+1, "
                     "last_filled_at=?, updated_at=? WHERE id=?", (_stamp(), _stamp(), purchase_id))
        try:
            fields = cards.read_card_secrets(conn, user_id, card.id)
        except cards.CardError:
            raise WalletRefusal("wallet_unavailable") from None
        return FillGrant(fields, [row["merchant_host"], *json.loads(row["extra_hosts"])])


def get_purchase(conn, user_id, purchase_id):
    row = conn.execute("SELECT * FROM wallet_purchases WHERE id=? AND user_id=?", (purchase_id, user_id)).fetchone()
    return dict(row) if row else None


def list_purchases(conn, user_id, limit=50):
    return [dict(row) for row in conn.execute("SELECT * FROM wallet_purchases WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                            (user_id, max(0, min(500, limit))))]


def _owned_open(conn, user_id, purchase_id, task_id, allowed):
    row = get_purchase(conn, user_id, purchase_id)
    if row is None or (task_id is not None and row["task_id"] != task_id):
        raise WalletRefusal("purchase_not_found")
    if row["state"] not in allowed:
        raise WalletRefusal("purchase_not_authorized")
    return row


def complete(conn, *, user_id, task_id, purchase_id, order_ref=None, amount_cents=None):
    if amount_cents is not None:
        _amount(amount_cents)
    if order_ref is not None and (not isinstance(order_ref, str) or len(order_ref) > 200):
        raise ValueError("order reference must be text of at most 200 characters")
    with _atomic(conn):
        _owned_open(conn, user_id, purchase_id, task_id, ("filled",))
        conn.execute("UPDATE wallet_purchases SET state='completed', order_ref=?, reported_amount_cents=?, updated_at=? "
                     "WHERE id=?", (order_ref, amount_cents, _stamp(), purchase_id))


def fail(conn, *, user_id, task_id, purchase_id, reason=None):
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise ValueError("reason must be text of at most 500 characters")
    with _atomic(conn):
        _owned_open(conn, user_id, purchase_id, task_id, ("authorized", "filled"))
        conn.execute("UPDATE wallet_purchases SET state='failed', reason=?, updated_at=? WHERE id=?",
                     (reason, _stamp(), purchase_id))


def cancel(conn, *, user_id, purchase_id, task_id=None):
    with _atomic(conn):
        _owned_open(conn, user_id, purchase_id, task_id, OPEN_STATES)
        conn.execute("UPDATE wallet_purchases SET state='cancelled', updated_at=? WHERE id=?", (_stamp(), purchase_id))


def close_for_task(conn, task_id, state):
    if state not in ("declined", "expired", "cancelled"):
        raise ValueError("invalid held-purchase close state")
    return conn.execute("UPDATE wallet_purchases SET state=?, updated_at=? WHERE task_id=? AND state='held'",
                        (state, _stamp(), task_id)).rowcount


def expire(conn, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    with _atomic(conn):
        expired = conn.execute("UPDATE wallet_purchases SET state='expired', updated_at=? "
                               "WHERE state='authorized' AND expires_at<=?", (_stamp(now), _stamp(now))).rowcount
        unreported = conn.execute("UPDATE wallet_purchases SET state='unreported', updated_at=? "
                                  "WHERE state='filled' AND expires_at<=?",
                                  (_stamp(now), _stamp(now-timedelta(hours=1)))).rowcount
        return expired + unreported


def compose_preview(purchase, authorization_minutes=30) -> str:
    from istota.executor import _one_line
    from istota.lib.untrusted import frame_untrusted

    amount = format_amount(purchase["amount_cents"], purchase["currency"])
    preview = (f"Purchase approval: {amount} {_one_line(purchase['currency'])[:3]} at "
               f"{_one_line(purchase['merchant_host'])[:300]} on card "
               f"\"{_one_line(purchase['card_label'])[:64]}\" ending {_one_line(purchase['card_last_four'])[:4]}.")
    hosts = json.loads(purchase["extra_hosts"])
    if hosts:
        preview += "\nAlso fills card fields on: " + ", ".join(_one_line(host)[:300] for host in hosts)
    preview += "\n" + frame_untrusted(_one_line(purchase["description"])[:500], "purchase description")
    preview += (f"\nPurchase #{purchase['id']}. Approving lets this task fill the card once "
                f"within {max(5, min(240, authorization_minutes))} minutes.")
    return preview
