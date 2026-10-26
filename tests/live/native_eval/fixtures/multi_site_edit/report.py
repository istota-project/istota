from billing import compute_total


def summary(items):
    total = compute_total(items)
    return f"{len(items)} items, {total} total"


def grand_total(orders):
    return sum(compute_total(items) for items in orders)
