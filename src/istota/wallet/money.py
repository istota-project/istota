"""Exact minor-unit arithmetic for declared purchase amounts."""

import re
from decimal import Decimal

MAX_MINOR_UNITS = 2**63 - 1
# Server-owned currency precision, seeded from Intl data including legacy codes.
CURRENCY_EXPONENTS = {
    "ADP": 0, "AFN": 0, "ALL": 0, "BHD": 3, "BIF": 0, "BYR": 0,
    "CLF": 4, "CLP": 0, "COP": 0, "DJF": 0, "ESP": 0, "GNF": 0,
    "HUF": 0, "IDR": 0, "IQD": 0, "IRR": 0, "ISK": 0, "ITL": 0,
    "JOD": 3, "JPY": 0, "KMF": 0, "KPW": 0, "KRW": 0, "KWD": 3,
    "LAK": 0, "LBP": 0, "LUF": 0, "LYD": 3, "MGA": 0, "MGF": 0,
    "MMK": 0, "MRO": 0, "OMR": 3, "PKR": 0, "PYG": 0, "RWF": 0,
    "SLL": 0, "SOS": 0, "STD": 0, "SYP": 0, "TMM": 0, "TND": 3,
    "TRL": 0, "UGX": 0, "UYI": 0, "UYW": 4, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0, "YER": 0, "ZMK": 0, "ZWD": 0,
}


def currency_code(currency: str) -> str:
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter uppercase code")
    return currency


def parse_amount(text: str, currency: str) -> int:
    exponent = CURRENCY_EXPONENTS.get(currency_code(currency), 2)
    if (not isinstance(text, str) or len(text) > 24
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text)):
        raise ValueError("invalid amount")
    value = Decimal(text) * 10**exponent
    if value != value.to_integral_value() or not 0 < value <= MAX_MINOR_UNITS:
        raise ValueError("invalid amount")
    return int(value)


def format_amount(cents: int, currency: str) -> str:
    exponent = CURRENCY_EXPONENTS.get(currency_code(currency), 2)
    if type(cents) is not int:
        raise ValueError("amount must use integer minor units")
    return f"{Decimal(cents) / 10**exponent:.{exponent}f}"
