"""Exact minor-unit arithmetic for declared purchase amounts."""

import re
from decimal import Decimal

MAX_MINOR_UNITS = 2**63 - 1
_EXPONENTS = {"JPY": 0, "KWD": 3, "BHD": 3}


def currency_code(currency: str) -> str:
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter uppercase code")
    return currency


def parse_amount(text: str, currency: str) -> int:
    exponent = _EXPONENTS.get(currency_code(currency), 2)
    if (not isinstance(text, str) or len(text) > 24
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text)):
        raise ValueError("invalid amount")
    value = Decimal(text) * 10**exponent
    if value != value.to_integral_value() or not 0 < value <= MAX_MINOR_UNITS:
        raise ValueError("invalid amount")
    return int(value)


def format_amount(cents: int, currency: str) -> str:
    exponent = _EXPONENTS.get(currency_code(currency), 2)
    if type(cents) is not int:
        raise ValueError("amount must use integer minor units")
    return f"{Decimal(cents) / 10**exponent:.{exponent}f}"
