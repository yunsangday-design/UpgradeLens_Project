from __future__ import annotations

import io
import json
import logging

import pytest

from upgradelens.observability.logging import JsonFormatter, configure_logging, get_logger


def test_json_formatter_emits_structured_line() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("upgradelens.test")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    handler.setFormatter(JsonFormatter())

    logger.info("scan started", extra={"data_repo": "tests/fixtures/x"})

    payload = json.loads(stream.getvalue().splitlines()[-1])
    assert payload["level"] == "INFO"
    assert payload["message"] == "scan started"
    assert payload["repo"] == "tests/fixtures/x"


def test_json_formatter_includes_exception_text() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("upgradelens.test.exc")
    logger.handlers = [handler]
    logger.setLevel(logging.ERROR)

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("scan failed")

    payload = json.loads(stream.getvalue().splitlines()[-1])
    assert "ValueError: boom" in payload["exc"]


def test_configure_logging_uses_namespaced_logger() -> None:
    configure_logging("WARNING")
    logger = get_logger("manifest")
    assert logger.name == "upgradelens.manifest"
    assert logger.level <= logging.WARNING


@pytest.mark.parametrize("structured", [True, False])
def test_configure_logging_supports_both_formatters(structured: bool) -> None:
    configure_logging("DEBUG", structured=structured)
    root = logging.getLogger("upgradelens")
    formatter = root.handlers[0].formatter
    assert isinstance(formatter, JsonFormatter) is structured
    assert root.level == logging.DEBUG


def test_configure_logging_falls_back_on_unknown_level() -> None:
    configure_logging("NOT_A_LEVEL")
    assert logging.getLogger("upgradelens").level == logging.INFO
