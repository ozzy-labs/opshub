"""Tests for opshub.core.logging.

Covers four contracts pinned by ADR-0027:

1. The structlog redaction processor scrubs every event value (string,
   list, tuple, dict) **and** the ``exception`` traceback expanded by
   :func:`structlog.processors.format_exc_info` before any renderer
   sees it (R1).
2. :func:`resolve_log_settings` folds CLI flags and environment
   variables into a :class:`LogSettings` record with priority
   ``CLI > env > default``.
3. :func:`configure_logging` with ``log_file=`` creates the target file
   with mode ``0600`` (R5) and writes redaction-applied content.
4. :func:`format_debug_traceback` returns a sanitised traceback string
   for the ``--debug`` error wrapper to print (R2).
5. ``configure_logging`` is idempotent — second call wins nothing
   (preserves the existing contract relied on by library code).

These tests rely on process-level state (``configure_logging`` is
idempotent by design — first call wins for the lifetime of the
interpreter). To keep the contract testable, tests that need a fresh
configuration reach into the module-private ``_configured`` flag with
a context manager.
"""

from __future__ import annotations

import logging
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import structlog
from structlog.typing import EventDict

from opshub.core import logging as opshub_logging
from opshub.core.logging import (
    LogSettings,
    _redaction_processor,  # pyright: ignore[reportPrivateUsage]
    configure_logging,
    format_debug_traceback,
    get_logger,
    resolve_log_settings,
)
from tests._secrets import (
    FAKE_AWS_ACCESS_KEY,
    FAKE_GITHUB_PAT,
    FAKE_GOOGLE_API_KEY,
    FAKE_JWT,
    FAKE_SLACK_BOT_TOKEN,
)

# ---- canonical token shapes (built from concat to dodge secret scanning) ----

# OpenAI / Anthropic ``sk-`` shape: ``sk-`` + 20+ [A-Za-z0-9].
FAKE_SK_KEY = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"
# GitHub classic PAT: ``ghp_`` + 30+ [A-Za-z0-9].
FAKE_GHP_KEY = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
# ``Authorization: Bearer`` shape.
FAKE_BEARER_TAIL = "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890"
FAKE_BEARER_HEADER = f"Bearer {FAKE_BEARER_TAIL}"


@contextmanager
def _reset_logging() -> Generator[None]:
    """Drop the ``_configured`` guard so ``configure_logging`` reruns.

    Restores the original value on exit so other tests that rely on
    the configured logger keep working.
    """
    previous = opshub_logging._configured  # pyright: ignore[reportPrivateUsage]
    opshub_logging._configured = False  # pyright: ignore[reportPrivateUsage]
    # Also drop the structlog config + root handlers so the next call
    # rebuilds them from scratch.
    structlog.reset_defaults()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        yield
    finally:
        opshub_logging._configured = previous  # pyright: ignore[reportPrivateUsage]
        structlog.reset_defaults()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# ============================================================================
# Redaction processor (R1)
# ============================================================================


def _run_processor(event_dict: EventDict) -> EventDict:
    """Convenience: run the redaction processor against a copy of the dict."""
    return _redaction_processor(None, "info", dict(event_dict))


class TestRedactionProcessor:
    """All event values (event / kwargs / nested containers / traceback) are scrubbed."""

    def test_event_string_is_scrubbed(self) -> None:
        result = _run_processor({"event": f"failed with {FAKE_SK_KEY}"})
        assert FAKE_SK_KEY not in result["event"]
        assert "sk-***" in result["event"]

    def test_bound_kwarg_value_is_scrubbed(self) -> None:
        result = _run_processor({"event": "ping", "api_key": FAKE_SK_KEY})
        assert result["api_key"] == "sk-***"

    def test_bearer_authorization_header_is_scrubbed(self) -> None:
        result = _run_processor({"event": f"401 from upstream: {FAKE_BEARER_HEADER}"})
        assert FAKE_BEARER_TAIL not in result["event"]
        assert "Bearer ***" in result["event"]

    def test_ghp_token_in_kwarg_is_scrubbed(self) -> None:
        result = _run_processor({"event": "github call", "header": f"token {FAKE_GHP_KEY}"})
        assert FAKE_GHP_KEY not in result["header"]
        assert "ghp_***" in result["header"]

    def test_github_pat_in_event_is_scrubbed(self) -> None:
        result = _run_processor({"event": f"PAT leaked: {FAKE_GITHUB_PAT}"})
        assert FAKE_GITHUB_PAT not in result["event"]
        assert "github_pat_***" in result["event"]

    def test_slack_token_in_kwarg_is_scrubbed(self) -> None:
        result = _run_processor({"event": "slack", "token": FAKE_SLACK_BOT_TOKEN})
        assert FAKE_SLACK_BOT_TOKEN not in result["token"]
        # The Slack marker keeps the prefix family hint (``xoxb-***``).
        assert result["token"].startswith("xoxb")
        assert "***" in result["token"]

    def test_aws_access_key_in_event_is_scrubbed(self) -> None:
        result = _run_processor({"event": f"AWS {FAKE_AWS_ACCESS_KEY} 403"})
        assert FAKE_AWS_ACCESS_KEY not in result["event"]
        assert "AKIA***" in result["event"]

    def test_google_api_key_in_event_is_scrubbed(self) -> None:
        result = _run_processor({"event": f"google quota {FAKE_GOOGLE_API_KEY}"})
        assert FAKE_GOOGLE_API_KEY not in result["event"]
        assert "AIza***" in result["event"]

    def test_jwt_in_event_is_scrubbed(self) -> None:
        result = _run_processor({"event": f"id_token={FAKE_JWT}"})
        assert FAKE_JWT not in result["event"]
        assert "[JWT REDACTED]" in result["event"]

    def test_nested_list_values_are_scrubbed(self) -> None:
        result = _run_processor(
            {
                "event": "batch",
                "items": [f"first {FAKE_SK_KEY}", "benign", f"third {FAKE_GHP_KEY}"],
            }
        )
        assert FAKE_SK_KEY not in result["items"][0]
        assert "sk-***" in result["items"][0]
        assert FAKE_GHP_KEY not in result["items"][2]
        assert "ghp_***" in result["items"][2]
        assert result["items"][1] == "benign"

    def test_nested_dict_values_are_scrubbed(self) -> None:
        result = _run_processor(
            {
                "event": "nested",
                "context": {"auth": FAKE_BEARER_HEADER, "user": "alice"},
            }
        )
        assert FAKE_BEARER_TAIL not in result["context"]["auth"]
        assert "Bearer ***" in result["context"]["auth"]
        assert result["context"]["user"] == "alice"

    def test_traceback_string_under_exception_key_is_scrubbed(self) -> None:
        """``format_exc_info`` expands into a string at ``event_dict['exception']``.

        The redaction processor must catch tokens hiding inside that
        multi-line traceback string — this is the safety net that
        makes ``--debug`` shareable.
        """
        try:
            raise RuntimeError(f"401 token={FAKE_SK_KEY}")
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()

        event_dict: EventDict = {"event": "boom", "exc_info": exc_info}
        # Run format_exc_info (which is the upstream processor) and
        # then our redactor (downstream). Their relative order matters
        # — the redactor MUST run after format_exc_info.
        event_dict = structlog.processors.format_exc_info(None, "error", event_dict)
        event_dict = _redaction_processor(None, "error", event_dict)

        assert "exception" in event_dict
        assert FAKE_SK_KEY not in event_dict["exception"]
        assert "sk-***" in event_dict["exception"]

    def test_non_string_leaves_pass_through(self) -> None:
        """Numbers / booleans / None cannot syntactically embed a token."""
        result = _run_processor({"event": "ok", "count": 42, "ok": True, "missing": None})
        assert result["count"] == 42
        assert result["ok"] is True
        assert result["missing"] is None

    def test_benign_event_is_unchanged(self) -> None:
        before: EventDict = {"event": "ping", "ms": 12}
        after = _run_processor(before)
        assert after == {"event": "ping", "ms": 12}


# ============================================================================
# resolve_log_settings (priority CLI > env > default)
# ============================================================================


class TestResolveLogSettings:
    def test_default_when_no_flags_and_no_env(self) -> None:
        settings = resolve_log_settings(env={})
        assert settings == LogSettings(level="INFO", log_format="auto", log_file=None, debug=False)

    def test_single_v_lifts_to_info(self) -> None:
        # Default is already INFO; -v keeps it explicit at INFO (no jump).
        settings = resolve_log_settings(verbose=1, env={})
        assert settings.level == "INFO"
        assert settings.debug is False

    def test_double_v_lifts_to_debug(self) -> None:
        settings = resolve_log_settings(verbose=2, env={})
        assert settings.level == "DEBUG"
        assert settings.debug is False  # ``-vv`` alone does not flip ``debug``.

    def test_single_q_drops_to_warning(self) -> None:
        settings = resolve_log_settings(quiet=1, env={})
        assert settings.level == "WARNING"

    def test_double_q_drops_to_error(self) -> None:
        settings = resolve_log_settings(quiet=2, env={})
        assert settings.level == "ERROR"

    def test_debug_flag_forces_debug_and_sets_debug_field(self) -> None:
        settings = resolve_log_settings(debug=True, env={})
        assert settings.level == "DEBUG"
        assert settings.debug is True

    def test_cli_overrides_env_level(self) -> None:
        # CLI says ``-vv`` (DEBUG); env says WARNING. CLI wins.
        settings = resolve_log_settings(verbose=2, env={"OPSHUB_LOG_LEVEL": "WARNING"})
        assert settings.level == "DEBUG"

    def test_env_used_when_no_cli_level(self) -> None:
        settings = resolve_log_settings(env={"OPSHUB_LOG_LEVEL": "WARNING"})
        assert settings.level == "WARNING"

    def test_env_debug_flag_also_lifts_level_and_sets_debug(self) -> None:
        settings = resolve_log_settings(env={"OPSHUB_DEBUG": "1"})
        assert settings.level == "DEBUG"
        assert settings.debug is True

    def test_env_debug_truthy_variants(self) -> None:
        for value in ["1", "true", "TRUE", "Yes", " on ", "DEBUG"]:
            settings = resolve_log_settings(env={"OPSHUB_DEBUG": value})
            assert settings.debug is True, f"value={value!r} should be truthy"

    def test_env_debug_falsy_variants(self) -> None:
        for value in ["0", "false", "no", "off", "", "  "]:
            settings = resolve_log_settings(env={"OPSHUB_DEBUG": value})
            assert settings.debug is False, f"value={value!r} should be falsy"

    def test_cli_debug_overrides_env_falsy_debug(self) -> None:
        settings = resolve_log_settings(debug=True, env={"OPSHUB_DEBUG": "0"})
        assert settings.debug is True
        assert settings.level == "DEBUG"

    def test_unknown_env_log_level_falls_through_to_default(self) -> None:
        settings = resolve_log_settings(env={"OPSHUB_LOG_LEVEL": "VERBOSE"})
        assert settings.level == "INFO"

    def test_cli_log_format_overrides_env(self) -> None:
        settings = resolve_log_settings(log_format="json", env={"OPSHUB_LOG_FORMAT": "console"})
        assert settings.log_format == "json"

    def test_env_log_format_used_when_no_cli(self) -> None:
        settings = resolve_log_settings(env={"OPSHUB_LOG_FORMAT": "console"})
        assert settings.log_format == "console"

    def test_unknown_log_format_falls_back_to_auto(self) -> None:
        settings = resolve_log_settings(log_format="yaml", env={})
        assert settings.log_format == "auto"

    def test_cli_log_file_overrides_env(self, tmp_path: Path) -> None:
        cli_path = tmp_path / "cli.log"
        env_path = tmp_path / "env.log"
        settings = resolve_log_settings(log_file=cli_path, env={"OPSHUB_LOG_FILE": str(env_path)})
        assert settings.log_file == cli_path

    def test_env_log_file_used_when_no_cli(self, tmp_path: Path) -> None:
        env_path = tmp_path / "env.log"
        settings = resolve_log_settings(env={"OPSHUB_LOG_FILE": str(env_path)})
        assert settings.log_file == env_path

    def test_quiet_takes_precedence_over_verbose(self) -> None:
        # Edge case: both flags supplied. The conservative behaviour is
        # for ``-q`` to win, lowering verbosity (matching the safer default
        # for a CLI shipped with secret-bearing logs).
        settings = resolve_log_settings(verbose=2, quiet=1, env={})
        assert settings.level == "WARNING"


# ============================================================================
# configure_logging(log_file=...) — 0600 (R5)
# ============================================================================


class TestLogFileMode:
    def test_log_file_is_created_with_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "subdir" / "opshub.log"
        with _reset_logging():
            configure_logging(level="INFO", json=True, log_file=target)
            logger = get_logger("file_test")
            logger.info("hello", note="benign")
            # Flush all handlers so the bytes actually hit disk.
            for handler in logging.getLogger().handlers:
                handler.flush()

        assert target.exists(), "log file should have been created"
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_log_file_content_is_redacted(self, tmp_path: Path) -> None:
        target = tmp_path / "redacted.log"
        with _reset_logging():
            configure_logging(level="INFO", json=True, log_file=target)
            logger = get_logger("file_test")
            logger.info("upload failed", api_key=FAKE_SK_KEY)
            for handler in logging.getLogger().handlers:
                handler.flush()

        content = target.read_text(encoding="utf-8")
        assert FAKE_SK_KEY not in content
        assert "sk-***" in content

    def test_log_file_append_does_not_truncate(self, tmp_path: Path) -> None:
        target = tmp_path / "append.log"
        target.write_text("preexisting line\n", encoding="utf-8")
        # Preserve existing bytes through configure_logging + a write.
        with _reset_logging():
            configure_logging(level="INFO", json=True, log_file=target)
            logger = get_logger("append_test")
            logger.info("new line", marker="kept")
            for handler in logging.getLogger().handlers:
                handler.flush()

        content = target.read_text(encoding="utf-8")
        assert content.startswith("preexisting line\n"), "existing bytes must survive"
        assert "kept" in content


# ============================================================================
# format_debug_traceback (R2)
# ============================================================================


class TestFormatDebugTraceback:
    def test_traceback_with_token_is_sanitised(self) -> None:
        try:
            raise RuntimeError(f"upstream 401 sk={FAKE_SK_KEY}")
        except RuntimeError as exc:
            rendered = format_debug_traceback(exc)

        # The raw token must be absent; the marker must be present.
        assert FAKE_SK_KEY not in rendered
        assert "sk-***" in rendered
        # The traceback frame structure must still be present so the
        # operator can locate the failure site.
        assert "Traceback" in rendered
        assert "RuntimeError" in rendered

    def test_traceback_with_bearer_header_is_sanitised(self) -> None:
        try:
            raise RuntimeError(f"403 header: {FAKE_BEARER_HEADER}")
        except RuntimeError as exc:
            rendered = format_debug_traceback(exc)

        assert FAKE_BEARER_TAIL not in rendered
        assert "Bearer ***" in rendered

    def test_traceback_with_github_pat_is_sanitised(self) -> None:
        try:
            raise RuntimeError(f"pat={FAKE_GITHUB_PAT}")
        except RuntimeError as exc:
            rendered = format_debug_traceback(exc)

        assert FAKE_GITHUB_PAT not in rendered
        assert "github_pat_***" in rendered

    def test_traceback_with_jwt_is_sanitised(self) -> None:
        try:
            raise RuntimeError(f"id_token={FAKE_JWT}")
        except RuntimeError as exc:
            rendered = format_debug_traceback(exc)

        assert FAKE_JWT not in rendered
        assert "[JWT REDACTED]" in rendered

    def test_traceback_with_aws_and_google_keys_is_sanitised(self) -> None:
        try:
            raise RuntimeError(f"aws={FAKE_AWS_ACCESS_KEY} google={FAKE_GOOGLE_API_KEY}")
        except RuntimeError as exc:
            rendered = format_debug_traceback(exc)

        assert FAKE_AWS_ACCESS_KEY not in rendered
        assert FAKE_GOOGLE_API_KEY not in rendered
        assert "AKIA***" in rendered
        assert "AIza***" in rendered


# ============================================================================
# Idempotency (existing contract preserved)
# ============================================================================


class TestIdempotency:
    def test_configure_logging_is_idempotent(self) -> None:
        # Second call must not raise even though json kwarg differs.
        configure_logging(json=True)
        configure_logging(json=False)

    def test_second_call_does_not_reconfigure_root_handlers(self, tmp_path: Path) -> None:
        """Once configured, a second call with a different ``log_file`` is a no-op."""
        first = tmp_path / "first.log"
        second = tmp_path / "second.log"
        with _reset_logging():
            configure_logging(level="INFO", json=True, log_file=first)
            handler_count_after_first = len(logging.getLogger().handlers)
            configure_logging(level="DEBUG", json=False, log_file=second)
            handler_count_after_second = len(logging.getLogger().handlers)

        assert handler_count_after_first == handler_count_after_second, (
            "second configure_logging call must be a no-op (idempotent contract)"
        )
        # The second file must not have been created.
        assert not second.exists(), "second log_file must be ignored (idempotent)"

    def test_get_logger_returns_usable_bound_logger(self) -> None:
        logger = get_logger("test")
        # Smoke: bound logger must accept structured kwargs without raising.
        logger.info("ping", key="value")

    def test_get_logger_without_name_returns_usable_logger(self) -> None:
        logger = get_logger()
        logger.info("ok")
