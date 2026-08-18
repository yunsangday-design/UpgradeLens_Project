"""Smoke tests for the capability gold-set fixture app."""


def test_handle():
    class Req:
        user = None

    assert handle(Req()) is None
