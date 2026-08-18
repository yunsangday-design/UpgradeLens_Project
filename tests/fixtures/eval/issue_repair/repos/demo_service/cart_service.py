"""Demo service module with deliberate boundary-condition bugs (B2 gold set)."""


def cart_total(items):
    """BUG(boundary): empty cart returns None instead of 0."""
    if not items:
        return None
    return sum(item["price"] * item["qty"] for item in items)


def first_item(items):
    """BUG(boundary): empty list raises IndexError instead of returning None."""
    return items[0]


def paginate(items, page, size):
    """BUG(boundary): page 0 computes a negative offset and slices wrong data."""
    offset = (page - 1) * size
    return items[offset : offset + size]


def apply_discount(price, qty):
    """BUG(boundary): negative quantity produces a negative total instead of 0."""
    return price * qty * 0.9
