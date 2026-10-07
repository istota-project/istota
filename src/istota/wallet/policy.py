"""Purchase policy, evaluated while the caller holds the write lock."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from istota import db
from istota.credentials import vault
from istota.credentials.broker.grants import _task_context
from istota.rooms.scopes import canonical_token, withheld_for_task
from istota.wallet import cards
from istota.wallet.hosts import PAYMENT_FRAME_HOSTS
from istota.wallet.money import MAX_MINOR_UNITS, currency_code


@dataclass(frozen=True)
class Policy:
    currency: str = "USD"
    auto_limit_cents: int = 0
    auto_budget_cents: int = 0
    ceiling_cents: int | None = None
    allow_scheduled: bool = False


@dataclass(frozen=True)
class Decision:
    state: str
    approval: str | None = None
    reason: str | None = None


def get_policy(conn, user_id) -> Policy:
    row = conn.execute("SELECT * FROM wallet_policies WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        return Policy()
    return Policy(row["currency"], row["auto_limit_cents"], row["auto_budget_cents"],
                  row["ceiling_cents"], bool(row["allow_scheduled"]))


def put_policy(conn, user_id, policy: Policy):
    currency_code(policy.currency)
    for value in (policy.auto_limit_cents, policy.auto_budget_cents,
                  *(() if policy.ceiling_cents is None else (policy.ceiling_cents,))):
        if type(value) is not int or not 0 <= value <= MAX_MINOR_UNITS:
            raise ValueError("policy amounts must be non-negative integer minor units")
    if type(policy.allow_scheduled) is not bool:
        raise ValueError("allow_scheduled must be boolean")
    conn.execute("INSERT INTO wallet_policies (user_id,currency,auto_limit_cents,auto_budget_cents,ceiling_cents,allow_scheduled) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET currency=excluded.currency, "
                 "auto_limit_cents=excluded.auto_limit_cents, auto_budget_cents=excluded.auto_budget_cents, "
                 "ceiling_cents=excluded.ceiling_cents, allow_scheduled=excluded.allow_scheduled, updated_at=datetime('now')",
                 (user_id, policy.currency, policy.auto_limit_cents, policy.auto_budget_cents,
                  policy.ceiling_cents, policy.allow_scheduled))


def _auto_purchases(conn, user_id, now=None):
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute(
        "SELECT amount_cents, currency FROM wallet_purchases WHERE user_id=? AND approval='auto' "
        "AND state IN ('authorized','filled','completed','unreported') AND created_at>=?",
        (user_id, start),
    ).fetchall()


def auto_spent_cents(conn, user_id, now=None):
    # Sum in Python so multiple individually valid SQLite integers cannot overflow SUM.
    return sum(row["amount_cents"] for row in _auto_purchases(conn, user_id, now))


def unavailable_reason(conn, config, user_id, task):
    if "wallet" not in config.experimental.features:
        return "feature_disabled"
    refusal = vault.vault_isolation_refusal(config, user_id)
    if refusal:
        return refusal
    if task is None or task.user_id != user_id:
        return "task_not_found"
    if withheld_for_task(conn, task, skill_index={}):
        return "scopes_withheld"
    return None


def decide(conn, config, *, user_id, task, card, amount_cents, currency, merchant_host, extra_hosts):
    unavailable = unavailable_reason(conn, config, user_id, task)
    if unavailable:
        return Decision("refused", reason=unavailable)
    if card is None or card.user_id != user_id:
        return Decision("refused", reason="card_not_found")
    if card.state != "active":
        return Decision("refused", reason="card_paused")
    if cards.expired(card):
        return Decision("refused", reason="card_expired")
    count = conn.execute("SELECT count(*) FROM wallet_purchases WHERE task_id=?", (task.id,)).fetchone()[0]
    if count >= config.security.wallet_requests_per_task:
        return Decision("refused", reason="wallet_request_limit")
    policy = get_policy(conn, user_id)
    if currency == policy.currency and policy.ceiling_cents is not None and amount_cents > policy.ceiling_cents:
        return Decision("refused", reason="over_ceiling")
    _, scheduled = _task_context(conn, task.id, user_id)
    room = canonical_token(conn, task.conversation_token)
    shared = task.is_group_chat or bool(room and db.room_was_ever_shared(conn, room))
    history = _auto_purchases(conn, user_id)
    spent = sum(row["amount_cents"] for row in history)
    # A policy currency change cannot turn old minor units into new ones.
    # Hold until that spending leaves the window, rather than invent a rate.
    mixed_currency = any(row["currency"] != currency for row in history)
    if (currency == policy.currency and not mixed_currency and amount_cents <= policy.auto_limit_cents
            and spent + amount_cents <= policy.auto_budget_cents
            and (not scheduled or policy.allow_scheduled) and not shared
            and set(extra_hosts) <= PAYMENT_FRAME_HOSTS):
        return Decision("authorized", "auto")
    return Decision("held")
