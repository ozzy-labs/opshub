"""structlog configuration.

Single entry point: ``configure_logging()`` is idempotent and safe to call
from both the CLI bootstrap (`opshub init` / every subcommand) and from
tests. The first call wins; subsequent calls are no-ops to keep test output
stable when multiple modules touch the logger.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog
from structlog.typing import FilteringBoundLogger, Processor

_configured = False


def configure_logging(*, level: str = "INFO", json: bool | None = None) -> None:
    """Configure structlog once for the process.

    ``json=None`` auto-detects: JSON when stderr is not a TTY (CI / piped
    output / `opshub` driven by an agent), pretty console otherwise.
    """
    global _configured
    if _configured:
        return

    use_json = json if json is not None else not sys.stderr.isatty()
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=log_level)

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if use_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Return a configured structlog logger.

    Auto-calls ``configure_logging()`` with defaults on first use so library
    code can call ``get_logger()`` without worrying about bootstrap order.
    """
    if not _configured:
        configure_logging()
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    return cast(FilteringBoundLogger, logger)
