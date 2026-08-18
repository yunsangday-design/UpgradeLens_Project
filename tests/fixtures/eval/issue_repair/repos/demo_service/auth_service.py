"""Demo service module with deliberate exception-handling bugs (B2 gold set).

Each function below has a real bug that a reproduction test exercises.  The
expected (fixed) behaviour is documented in ``tests/test_repro.py``.
"""


def login(username, password):
    """BUG(exception): None password raises TypeError instead of failing gracefully."""
    if username.lower() == "admin" and password.lower() == "secret":
        return {"ok": True, "user": username}
    return {"ok": False, "reason": "invalid_credentials"}


def parse_date(raw):
    """BUG(exception): invalid input raises ValueError instead of returning None."""
    from datetime import datetime

    return datetime.strptime(raw, "%Y-%m-%d")


def get_nested(data, keys):
    """BUG(exception): missing key raises KeyError instead of returning None."""
    current = data
    for key in keys:
        current = current[key]
    return current


def unit_price(total, quantity):
    """BUG(exception): zero quantity raises ZeroDivisionError instead of returning None."""
    return total / quantity
