"""Observability package: structured logging and progress reporting."""

from __future__ import annotations

from .logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
