"""structlog configuration.

Single entry point: ``configure_logging()`` is idempotent and safe to call
from both the CLI bootstrap (`opshub init` / every subcommand) and from
tests. The first call wins; subsequent calls are no-ops to keep test output
stable when multiple modules touch the logger.

This module is the foundation for the opshub observability surface
(ADR-0027). It owns four concerns:

* a structlog **redaction processor** that scrubs every log event value
  (including the ``exception`` traceback expanded by
  :func:`structlog.processors.format_exc_info`) through the shared
  :func:`opshub.core.sanitise.sanitise_error_message` regex set so that
  no log sink ever sees a bearer token or API key in the clear (R1).
* :func:`resolve_log_settings` which folds CLI flags
  (``-v`` / ``-q`` / ``--debug`` / ``--log-format`` / ``--log-file``) and
  environment variables (``OPSHUB_LOG_LEVEL`` / ``OPSHUB_LOG_FORMAT`` /
  ``OPSHUB_DEBUG`` / ``OPSHUB_LOG_FILE``) into a single
  :class:`LogSettings` record. Priority: CLI > env > default.
* :func:`configure_logging` itself, now accepting an optional
  ``log_file`` argument. When set, a file handler is added whose target
  file is created with **mode 0600** through :func:`os.open` so the file
  bits are tight from byte zero (R5). The file content goes through the
  same processor chain, so redaction applies (R5).
* :func:`format_debug_traceback` for the ``--debug`` error wrapper: it
  formats an exception traceback through :func:`traceback.format_exception`
  and pipes the result through ``sanitise_error_message`` so the print
  path can never leak a raw ``str(exc)`` (R2).

Dependency rule (ADR-0004 foundation tier): this module imports only
from ``opshub.core.sanitise``. No event / domain / projection imports;
no circular risk.

Environment variable contract (SSOT for the env layer; CLI wiring is the
T2 responsibility — T1 only resolves values):

* ``OPSHUB_LOG_LEVEL`` — case-insensitive level name (``DEBUG`` / ``INFO``
  / ``WARNING`` / ``ERROR``). Unknown values fall back to ``INFO``.
* ``OPSHUB_LOG_FORMAT`` — ``json`` / ``console`` / ``auto``. ``auto`` is
  the default and resolves at ``configure_logging`` time from the
  ``sys.stderr.isatty()`` probe (console for TTY, JSON otherwise).
* ``OPSHUB_DEBUG`` — truthy values (``1`` / ``true`` / ``yes`` / ``on``
  / ``debug``, case-insensitive) imply ``DEBUG`` level **and** flip the
  ``debug`` flag on the resolved :class:`LogSettings` so the caller can
  decide to print full sanitised tracebacks.
* ``OPSHUB_LOG_FILE`` — absolute or relative path. The file is created
  on first use with mode ``0600`` and append-opened thereafter.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from opshub.core.sanitise import sanitise_error_message

_configured = False

# Truthy / falsy strings accepted by ``OPSHUB_DEBUG`` (mirrors the
# convention used by ``OPSHUB_PROGRESS`` in ``cli/_progress.py`` — see
# ADR-0026). Matched case-insensitively, with surrounding whitespace
# stripped. Unknown values are treated as ``False``.
_TRUTHY = frozenset({"1", "true", "yes", "on", "debug"})
_FALSY = frozenset({"0", "false", "no", "off", ""})

# Format names accepted by ``--log-format`` and ``OPSHUB_LOG_FORMAT``.
# ``auto`` resolves at ``configure_logging`` time based on the stderr
# TTY probe; ``json`` and ``console`` force the renderer explicitly.
_VALID_FORMATS: frozenset[str] = frozenset({"auto", "json", "console"})

# Verbosity-to-level table for ``resolve_log_settings``. ``-v`` lifts
# the default ``INFO`` to ``DEBUG`` (1) or stays at ``INFO`` (1 only if
# already at default? no — see resolution below); ``-q`` drops to
# ``WARNING`` (1) / ``ERROR`` (2). ``-vvv`` and beyond clamp to
# ``DEBUG``; ``-qqq`` and beyond clamp to ``CRITICAL``.
_DEFAULT_LEVEL = "INFO"


@dataclass(frozen=True, slots=True)
class LogSettings:
    """Resolved logging settings from CLI flags + env vars.

    The dataclass is the single shape that :func:`configure_logging`
    consumes downstream. T2 wires the CLI root callback to call
    :func:`resolve_log_settings` and pass the result through.
    """

    level: str
    """Resolved log level name (``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR`` / ``CRITICAL``)."""

    log_format: str
    """``auto`` / ``json`` / ``console``. ``auto`` defers TTY detection to ``configure_logging``."""

    log_file: Path | None
    """Optional file path. When set, ``configure_logging`` adds a 0600-mode file handler."""

    debug: bool
    """True when ``--debug`` (or ``OPSHUB_DEBUG``) was set.

    T2 uses this to switch the error wrapper to full sanitised traceback.
    """


def _coerce_bool_env(value: str | None) -> bool:
    """Return ``True`` when ``value`` is a recognised truthy string."""
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY


def _coerce_level(value: str | None) -> str | None:
    """Return ``value`` upper-cased iff it names a known stdlib level."""
    if value is None:
        return None
    candidate = value.strip().upper()
    if not candidate:
        return None
    if getattr(logging, candidate, None) is not None and isinstance(
        getattr(logging, candidate), int
    ):
        return candidate
    return None


def _coerce_format(value: str | None) -> str | None:
    """Return ``value`` lower-cased iff it names a known format."""
    if value is None:
        return None
    candidate = value.strip().lower()
    if candidate in _VALID_FORMATS:
        return candidate
    return None


def resolve_log_settings(
    *,
    verbose: int = 0,
    quiet: int = 0,
    debug: bool = False,
    log_format: str | None = None,
    log_file: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> LogSettings:
    """Fold CLI flags and environment variables into a :class:`LogSettings`.

    Priority (highest wins):

    1. CLI flags. ``-v`` (``verbose=1``) → ``INFO``, ``-vv`` (``verbose=2``) → ``DEBUG``.
       ``-q`` (``quiet=1``) → ``WARNING``, ``-qq`` (``quiet=2``) → ``ERROR``.
       ``--debug`` forces ``DEBUG`` and also sets ``debug=True`` so the
       caller knows to print sanitised tracebacks. ``-v`` and ``-q`` are
       mutually compatible at the parser level but in practice T2 will
       reject the combination; here we let the larger absolute value
       win (``quiet`` after ``verbose``).
    2. Environment variables. ``OPSHUB_LOG_LEVEL`` overrides the
       level only if no CLI flag bumped it; ``OPSHUB_DEBUG`` likewise
       only fires when ``--debug`` was not set on the CLI.
    3. Default: ``INFO`` / ``auto`` / no file / not debug.

    ``env`` is injectable for tests. ``None`` means
    ``os.environ`` is consulted directly (with normal precedence rules).
    """
    environ = env if env is not None else os.environ

    # --- Step 1: derive a CLI-only level proposal ---
    cli_level: str | None = None
    if debug:
        cli_level = "DEBUG"
    elif verbose >= 2:
        cli_level = "DEBUG"
    elif verbose == 1:
        cli_level = "INFO"
    if quiet >= 2:
        cli_level = "ERROR"
    elif quiet == 1:
        cli_level = "WARNING"

    # --- Step 2: env level proposal, only consulted if CLI did not pin ---
    env_level = _coerce_level(environ.get("OPSHUB_LOG_LEVEL"))
    env_debug = _coerce_bool_env(environ.get("OPSHUB_DEBUG"))

    resolved_debug = bool(debug or env_debug)

    if cli_level is not None:
        resolved_level = cli_level
    elif env_level is not None:
        resolved_level = env_level
    elif env_debug:
        resolved_level = "DEBUG"
    else:
        resolved_level = _DEFAULT_LEVEL

    # --- Step 3: format ---
    cli_format = _coerce_format(log_format)
    env_format = _coerce_format(environ.get("OPSHUB_LOG_FORMAT"))
    resolved_format = cli_format or env_format or "auto"

    # --- Step 4: log file ---
    if log_file is not None:
        resolved_file: Path | None = Path(log_file)
    else:
        env_file = environ.get("OPSHUB_LOG_FILE")
        resolved_file = Path(env_file) if env_file else None

    return LogSettings(
        level=resolved_level,
        log_format=resolved_format,
        log_file=resolved_file,
        debug=resolved_debug,
    )


def _scrub_value(value: object) -> object:
    """Recursively pipe string leaves through :func:`sanitise_error_message`.

    Containers (lists / tuples / dicts) are walked once. Non-string,
    non-container leaves are returned as-is — they cannot syntactically
    embed a token. The walk is intentionally shallow-by-recursion
    rather than serialising-then-redacting because the latter would
    force every value to be JSON-stringifiable before the renderer
    runs.
    """
    if isinstance(value, str):
        return sanitise_error_message(value)
    if isinstance(value, list):
        items_list: list[object] = cast("list[object]", value)
        return [_scrub_value(item) for item in items_list]
    if isinstance(value, tuple):
        items_tuple: tuple[object, ...] = cast("tuple[object, ...]", value)
        return tuple(_scrub_value(item) for item in items_tuple)
    if isinstance(value, dict):
        items_dict: dict[object, object] = cast("dict[object, object]", value)
        return {key: _scrub_value(item) for key, item in items_dict.items()}
    return value


def _redaction_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Structlog processor that scrubs every event value (R1).

    Inserted **after** :func:`structlog.processors.format_exc_info` so
    the traceback string it produces under the ``exception`` key is
    also covered. Renderers (``JSONRenderer`` / ``ConsoleRenderer``)
    must follow this processor — otherwise they freeze the un-scrubbed
    value into a serialised string the processor can no longer reach.
    """
    for key in list(event_dict.keys()):
        event_dict[key] = _scrub_value(event_dict[key])
    return event_dict


def _open_log_file_stream(path: Path) -> Any:
    """Return an append-mode text stream backed by a 0600 file (R5).

    The file is created with :func:`os.open` (``O_CREAT | O_WRONLY |
    O_APPEND``, mode ``0o600``) so the permission bits are correct from
    byte zero. If the file already exists, the mode argument is a
    no-op per POSIX semantics; we leave existing files untouched (the
    operator may have intentionally tightened or loosened them).
    ``O_APPEND`` makes concurrent writes safe at the byte level on
    POSIX. The returned stream is then wrapped in
    :class:`_TeeWriteLogger` below to satisfy the structlog
    ``WriteLogger`` shape.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


class _TeeWriteLogger:
    """Minimal structlog logger that writes rendered output to N streams.

    structlog calls a single ``msg`` (or aliased ``debug`` / ``info`` /
    ``warning`` / ``error`` / ``critical``) method with the
    already-rendered string. We append a newline and write to every
    configured stream. The stderr stream is the canonical sink; the
    optional log_file stream is added when ``--log-file`` is set.

    Keeping the implementation in-house avoids reaching for stdlib
    logging (which would force us to undo the structlog rendering and
    re-format on the way out). It also means the file content is
    byte-identical to the stderr content, which makes test assertions
    straightforward.
    """

    __slots__ = ("_streams",)

    def __init__(self, *streams: Any) -> None:
        self._streams = tuple(streams)

    def msg(self, message: str) -> None:
        line = message + "\n"
        for stream in self._streams:
            try:
                stream.write(line)
                stream.flush()
            except (OSError, ValueError):
                # ``ValueError`` covers "I/O operation on closed file"
                # which test fixtures can race during teardown.
                continue

    # structlog dispatches by method name (debug/info/warning/error/
    # critical/exception/log/fatal); all of them ultimately render to
    # a single string, so we alias them to ``msg``.
    debug = info = warning = error = critical = exception = fatal = msg

    def log(self, _level: int, message: str) -> None:
        self.msg(message)


class _TeeWriteLoggerFactory:
    """structlog logger factory producing :class:`_TeeWriteLogger` instances.

    The factory is constructed once at :func:`configure_logging` time
    so the streams are reused across loggers (no re-open per
    ``get_logger`` call).
    """

    __slots__ = ("_streams",)

    def __init__(self, *streams: Any) -> None:
        self._streams = streams

    def __call__(self, *_args: Any, **_kwargs: Any) -> _TeeWriteLogger:
        return _TeeWriteLogger(*self._streams)


def configure_logging(
    *,
    level: str = "INFO",
    json: bool | None = None,
    log_file: str | os.PathLike[str] | None = None,
) -> None:
    """Configure structlog once for the process.

    ``json=None`` auto-detects: JSON when stderr is not a TTY (CI / piped
    output / `opshub` driven by an agent), pretty console otherwise.

    ``log_file`` adds a tee handler whose backing file is created with
    mode ``0600`` (R5). The file content goes through the same
    structlog processor chain as the stderr output, so the redaction
    processor (R1) covers it automatically.

    The function is idempotent: the first call wins. Subsequent calls
    with different arguments are no-ops so that library code can call
    :func:`get_logger` without worrying about bootstrap order.
    """
    global _configured
    if _configured:
        return

    use_json = json if json is not None else not sys.stderr.isatty()
    log_level = getattr(logging, level.upper(), logging.INFO)

    # stdlib ``logging.basicConfig`` is kept for backwards
    # compatibility with any caller that uses :mod:`logging` directly
    # (the structlog pipeline does **not** route through stdlib here).
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=log_level)

    # Build the tee logger factory: stderr is always written; the
    # optional ``log_file`` adds a second 0600-mode sink.
    sinks: list[Any] = [sys.stderr]
    if log_file is not None:
        sinks.append(_open_log_file_stream(Path(log_file)))

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Redaction MUST follow ``format_exc_info`` so the traceback
        # string it expands under the ``exception`` key is scrubbed,
        # and MUST precede the renderer so the scrubbed dict is what
        # gets serialised.
        _redaction_processor,
    ]
    if use_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=_TeeWriteLoggerFactory(*sinks),
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


def format_debug_traceback(exc: BaseException) -> str:
    """Return the formatted, sanitised traceback for ``exc`` (R2).

    The output of :func:`traceback.format_exception` is concatenated
    and piped through :func:`sanitise_error_message`. Callers (the T2
    ``main()`` error wrapper) print this string directly when
    ``--debug`` is on; we never reach for ``str(exc)`` unsanitised.
    """
    parts = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return sanitise_error_message("".join(parts))


__all__ = [
    "LogSettings",
    "configure_logging",
    "format_debug_traceback",
    "get_logger",
    "resolve_log_settings",
]
