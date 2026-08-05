"""Structured logging for UpgradeLens.

Uses only the standard library so the stage 0 baseline has no extra runtime
dependencies. Records structured fields as JSON lines while keeping the message
human readable. Secrets must never be passed as ``extra`` fields.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_STRUCTURED = True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line with standard fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Only include explicitly provided structured fields, never the raw args.
        for key, value in record.__dict__.items():
            if key.startswith("data_"):
                payload[key[5:]] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, structured: bool = _STRUCTURED) -> None:
    """Configure the root UpgradeLens logger.

    An unknown ``level`` falls back to ``INFO`` instead of raising, so a typo in
    configuration never prevents the tool from starting.
    """
    root = logging.getLogger("upgradelens")
    handler = logging.StreamHandler(sys.stderr)
    if structured:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.handlers = [handler]
    root.setLevel(logging.getLevelNamesMapping().get(level.upper(), logging.INFO))
    root.propagate = False


def get_logger(name: str = "upgradelens") -> logging.Logger:
    """Return a namespaced UpgradeLens logger."""
    if not name.startswith("upgradelens"):
        name = f"upgradelens.{name}"
    return logging.getLogger(name)
