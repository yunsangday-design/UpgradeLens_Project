"""Reproduction tests for the demo_service bugs (B2 gold set).

All tests document the *fixed* behaviour and therefore FAIL against the
deliberately broken fixtures — that is the point: they must fail before the
fix.  The issue-repair evaluator asserts exactly that (red before green).

This file is excluded from the main suite via ``norecursedirs`` and is only
executed in a subprocess by ``eval/issue_repair_eval.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_service import get_nested, login, parse_date, unit_price  # noqa: E402
from cart_service import (  # noqa: E402
    apply_discount,
    cart_total,
    first_item,
    paginate,
)
from config_loader import get_config, get_port, load_settings  # noqa: E402


def test_login_none_password_returns_failure():
    assert login("admin", None)["ok"] is False


def test_parse_date_invalid_returns_none():
    assert parse_date("not-a-date") is None


def test_get_nested_missing_key_returns_none():
    assert get_nested({"a": {"b": 1}}, ["a", "z"]) is None


def test_unit_price_zero_quantity_returns_none():
    assert unit_price(100, 0) is None


def test_cart_total_empty_cart_returns_zero():
    assert cart_total([]) == 0


def test_first_item_empty_list_returns_none():
    assert first_item([]) is None


def test_paginate_page_zero_clamps_to_first_page():
    items = list(range(10))
    assert paginate(items, 0, 3) == items[0:3]


def test_apply_discount_negative_quantity_returns_zero():
    assert apply_discount(10.0, -2) == 0


def test_get_config_missing_key_returns_default():
    assert get_config({"host": "x"}, "port") is None


def test_get_port_returns_int():
    import os

    os.environ["DEMO_SVC_PORT"] = "9000"
    try:
        port = get_port("DEMO_SVC_PORT")
    finally:
        del os.environ["DEMO_SVC_PORT"]
    assert port == 9000 and not isinstance(port, str)


def test_load_settings_invalid_json_returns_empty():
    assert load_settings("{not json") == {}


def test_gold_set_is_red_before_fix():
    """Meta-assertion: the fixture bugs are still present (fix-then-green)."""
    try:
        login("admin", None)
    except TypeError:
        pass
    else:
        raise AssertionError("fixture already fixed: login accepts None password")
