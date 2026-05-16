"""Tests for opshub.core.logging.

These tests rely on process-level state (``configure_logging`` is idempotent
by design — first call wins for the lifetime of the interpreter). The asserts
focus on contract rather than internal state, so test ordering does not matter.
"""

from __future__ import annotations

from opshub.core.logging import configure_logging, get_logger


def test_configure_logging_is_idempotent() -> None:
    # Second call must not raise even though json kwarg differs.
    configure_logging(json=True)
    configure_logging(json=False)


def test_get_logger_returns_usable_bound_logger() -> None:
    logger = get_logger("test")
    # Smoke: bound logger must accept structured kwargs without raising.
    logger.info("ping", key="value")


def test_get_logger_without_name_returns_usable_logger() -> None:
    logger = get_logger()
    logger.info("ok")
