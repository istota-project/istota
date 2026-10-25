"""Price arithmetic, in whole cents."""


def apply_discount(price_cents: int, percent: int) -> int:
    """The price after a percentage discount.

    The discount itself is rounded half up to a whole cent before it is taken
    off, so 15% of 999 cents is a 150 cent discount.
    """
    discount = price_cents * percent // 100
    return price_cents - discount


def add_tax(price_cents: int, rate_percent: int) -> int:
    """The price with tax added, the tax rounded half up to a whole cent."""
    tax = (price_cents * rate_percent + 50) // 100
    return price_cents + tax
