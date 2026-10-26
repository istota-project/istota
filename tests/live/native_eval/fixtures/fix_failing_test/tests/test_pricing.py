from shop.pricing import add_tax, apply_discount


def test_round_discount():
    assert apply_discount(1000, 10) == 900


def test_discount_rounds_half_up():
    assert apply_discount(999, 15) == 849


def test_tax_rounds_half_up():
    assert add_tax(999, 15) == 1149
