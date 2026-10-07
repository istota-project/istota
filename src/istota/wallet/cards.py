"""Card metadata and encrypted values, owned by one user."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import re
import sqlite3

from istota.credentials import store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Billing:
    line1: str = ""
    line2: str = ""
    city: str = ""
    region: str = ""
    postcode: str = ""
    country: str = ""


@dataclass(frozen=True, repr=False)
class CardInput:
    label: str
    number: str
    cvc: str
    exp_month: int
    exp_year: int
    name: str = ""
    billing: Billing = field(default_factory=Billing)

    def __repr__(self):
        return "CardInput(<redacted>)"


@dataclass(frozen=True)
class CardView:
    id: int
    user_id: str
    label: str
    brand: str
    last_four: str
    exp_month: int
    exp_year: int
    name: str
    billing: Billing
    state: str
    issuer: str
    issuer_ref: str | None


class CardError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def valid_number(number: str) -> bool:
    if not re.fullmatch(r"[0-9]{12,19}", number):
        return False
    total = 0
    for index, digit in enumerate(reversed(number)):
        value = int(digit)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0 and any(digit != "0" for digit in number)


def _brand(number):
    if number.startswith("4"):
        return "visa"
    if 51 <= int(number[:2]) <= 55 or 2221 <= int(number[:4]) <= 2720:
        return "mastercard"
    if number.startswith(("34", "37")):
        return "amex"
    if (number.startswith(("6011", "65")) or 644 <= int(number[:3]) <= 649
            or 622126 <= int(number[:6]) <= 622925):
        return "discover"
    return "other"


def expired(card, now=None):
    now = now or datetime.now(timezone.utc)
    return (card.exp_year, card.exp_month) < (now.year, now.month)


def _metadata(label, month, year, name, billing, *, check_expiry=True):
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 64:
        raise CardError("label", "Use a label between 1 and 64 characters")
    if type(month) is not int or not 1 <= month <= 12:
        raise CardError("exp_month", "Use a month between 1 and 12")
    if type(year) is not int or not 1 <= year <= 9999:
        raise CardError("exp_year", "Use a four-digit expiry year")
    now = datetime.now(timezone.utc)
    if check_expiry and (year, month) < (now.year, now.month):
        raise CardError("exp_year", "That card has expired")
    if not isinstance(name, str) or len(name) > 200:
        raise CardError("name", "Cardholder name must be text of at most 200 characters")
    if not isinstance(billing, Billing) or any(not isinstance(v, str) or len(v) > 200 for v in asdict(billing).values()):
        raise CardError("billing", "Billing fields must be text of at most 200 characters")


def _begin(conn):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def add_card(conn, user_id: str, card: CardInput) -> int:
    _metadata(card.label, card.exp_month, card.exp_year, card.name, card.billing)
    number = card.number.replace(" ", "").replace("-", "") if isinstance(card.number, str) else ""
    if not valid_number(number):
        raise CardError("number", "That card number is not valid")
    if not isinstance(card.cvc, str) or not re.fullmatch(r"[0-9]{3,4}", card.cvc):
        raise CardError("cvc", "Use a three- or four-digit security code")
    _begin(conn)
    try:
        cursor = conn.execute(
            "INSERT INTO wallet_cards (user_id,label,brand,last_four,exp_month,exp_year,holder_name,billing) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, card.label.strip(), _brand(number), number[-4:], card.exp_month, card.exp_year,
             card.name, json.dumps(asdict(card.billing))),
        )
    except sqlite3.IntegrityError:
        raise CardError("label", "A card with that label already exists") from None
    ident = cursor.lastrowid
    for key, value in (("number", number), ("cvc", card.cvc)):
        store.set_secret(None, user_id, "wallet", f"card:{ident}:{key}", value, connection=conn)
    return ident


def _view(row):
    return CardView(row["id"], row["user_id"], row["label"], row["brand"], row["last_four"],
                    row["exp_month"], row["exp_year"], row["holder_name"], Billing(**json.loads(row["billing"])),
                    row["state"], row["issuer"], row["issuer_ref"])


def get_card(conn, user_id, card_id_or_label):
    column = "id" if isinstance(card_id_or_label, int) else "label"
    row = conn.execute(f"SELECT * FROM wallet_cards WHERE user_id=? AND {column}=?",
                       (user_id, card_id_or_label)).fetchone()
    return _view(row) if row else None


def list_cards(conn, user_id):
    return [_view(row) for row in conn.execute("SELECT * FROM wallet_cards WHERE user_id=? ORDER BY id", (user_id,))]


def update_card(conn, user_id, card_id, *, label=None, exp_month=None, exp_year=None,
                name=None, billing=None, state=None):
    _begin(conn)
    card = get_card(conn, user_id, card_id)
    if card is None:
        raise CardError("id", "Card not found")
    label = card.label if label is None else label
    month = card.exp_month if exp_month is None else exp_month
    year = card.exp_year if exp_year is None else exp_year
    name = card.name if name is None else name
    billing = card.billing if billing is None else billing
    state = card.state if state is None else state
    _metadata(label, month, year, name, billing, check_expiry=exp_month is not None or exp_year is not None)
    if state not in ("active", "paused"):
        raise CardError("state", "Invalid card state")
    try:
        conn.execute("UPDATE wallet_cards SET label=?, exp_month=?, exp_year=?, holder_name=?, billing=?, state=?, "
                     "updated_at=datetime('now') WHERE id=? AND user_id=?",
                     (label.strip(), month, year, name, json.dumps(asdict(billing)), state, card_id, user_id))
    except sqlite3.IntegrityError:
        raise CardError("label", "A card with that label already exists") from None


def remove_card(conn, user_id, card_id) -> int:
    _begin(conn)
    if get_card(conn, user_id, card_id) is None:
        return 0
    count = conn.execute("UPDATE wallet_purchases SET state='cancelled', updated_at=datetime('now') "
                         "WHERE user_id=? AND card_id=? AND state IN ('held','authorized','filled')",
                         (user_id, card_id)).rowcount
    conn.execute("UPDATE wallet_purchases SET card_id=NULL WHERE user_id=? AND card_id=?", (user_id, card_id))
    for key in ("number", "cvc"):
        store.delete_secret(None, user_id, "wallet", f"card:{card_id}:{key}", connection=conn)
    conn.execute("DELETE FROM wallet_cards WHERE id=? AND user_id=?", (card_id, user_id))
    return count


def read_card_secrets(conn, user_id, card_id) -> dict:
    card = get_card(conn, user_id, card_id)
    if card is None:
        raise CardError("id", "Card not found")
    values = {}
    for key in ("number", "cvc"):
        value = store.get_secret(None, user_id, "wallet", f"card:{card_id}:{key}", connection=conn)
        if not isinstance(value, str) or not value:
            logger.error("wallet card secrets unavailable card_id=%s", card_id)
            raise CardError("card", "Card secrets unavailable")
        values[key] = value
    return {**values, "exp_month": f"{card.exp_month:02}", "exp_year": str(card.exp_year), "name": card.name}
