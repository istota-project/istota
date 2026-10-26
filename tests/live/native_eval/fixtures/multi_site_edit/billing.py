"""Order arithmetic. This file uses CRLF line endings; keep them."""


def compute_total(items):
    return sum(price * qty for price, qty in items)


def invoice_total(items, shipping):
    return compute_total(items) + shipping
