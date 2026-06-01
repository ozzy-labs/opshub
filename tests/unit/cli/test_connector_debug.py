"""Tests for ``opshub connector sync`` ``--debug`` opt-in error trail.

Phase 14 T3 (#320, parent epic #317): the connector sync failure path
gains an opt-in debug trail wired off ``OPSHUB_DEBUG=1`` (T2's root
callback sets the env var when ``--debug`` / ``-vv`` is passed, and
the operator can set it directly for the MCP subprocess path).

Three invariants pinned here:

1. **R3 — default regression**: with ``OPSHUB_DEBUG`` unset / falsy,
   sync failure prints **only** ``sync failed: <TypeName>`` to stderr
   and persists ``error_message=<TypeName>`` to the event log via
   :meth:`SourceService.record_sync_failure`. No exception message
   body, no traceback — the byte-for-byte same surface as before T3.
2. **R2 / R4 — opt-in debug trail**: with ``OPSHUB_DEBUG=1`` the
   failure path additionally writes a sanitised exception message +
   a sanitised traceback to **stderr**. Stdout (the summary line) is
   unchanged so scripts piping the CLI keep working. Every known
   token shape (``sk-`` / ``ghp_`` / ``github_pat_`` / ``xox*-`` /
   ``AKIA`` / ``AIza`` / ``Bearer …`` / JWT) is rewritten to its
   marker form before any byte hits the terminal — the regex set
   lives in :mod:`opshub.core.sanitise` and is shared with the
   structlog redaction processor (T1) and the MCP boundary redactor
   (:mod:`opshub.mcp._redact`).
3. **Event-log permanence**: the ``error_message`` parameter passed
   to ``record_sync_failure`` stays ``type(exc).__name__`` even when
   ``--debug`` is on — the audit row never grows a token surface.

The horizontal-redaction check (point 3 in #320's test plan) lives
in :func:`test_bind_connector_log_does_not_leak_tokens` below.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors import (
    SyncResult,
    register_connector,
    unregister_all,
)
from tests._secrets import (
    FAKE_AWS_ACCESS_KEY,
    FAKE_GITHUB_PAT,
    FAKE_GOOGLE_API_KEY,
    FAKE_JWT,
    FAKE_SLACK_BOT_TOKEN,
)

# Build the canonical token shapes locally (mirrors
# ``tests/unit/core/test_logging.py``) so a missing ``tests/_secrets``
# entry surfaces as an import error rather than a silent skip.
FAKE_SK_KEY = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"
FAKE_GHP_KEY = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
FAKE_BEARER_TAIL = "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890"
FAKE_BEARER_HEADER = f"Bearer {FAKE_BEARER_TAIL}"


# ============================================================================
# Test scaffolding — stub connector + recording source service
# ============================================================================


class _RecordingSource:
    """In-memory ``SourceService`` stand-in.

    Captures the ``error_message`` argument :meth:`record_sync_failure`
    receives so the R3 invariant (event-log permanence = type name
    only) is directly assertable.
    """

    def __init__(self) -> None:
        self.cursor_set_calls: list[tuple[str, Any, bool]] = []
        self.failure_calls: list[tuple[str, str]] = []

    def cursor_get(self, name: str) -> Any:
        return None

    def cursor_set(self, name: str, value: Any, *, sync_started: bool) -> None:
        self.cursor_set_calls.append((name, value, sync_started))

    def record_sync_failure(self, name: str, *, error_message: str) -> None:
        self.failure_calls.append((name, error_message))

    # No observe() — the failing connectors here raise before any items
    # land. ``_ProgressSourceProxy.__getattr__`` would forward any
    # missing attribute to this object; an ``AttributeError`` at that
    # point would surface as a test failure, which is what we want.


class _FailingConnector:
    """Connector whose ``sync`` always raises a chosen exception."""

    def __init__(self, name: str, exc: BaseException) -> None:
        self.name = name
        self._exc = exc

    def sync(self, context: Any) -> SyncResult:
        raise self._exc


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Isolate every test from the process-wide connector registry."""
    unregister_all()
    yield
    unregister_all()


@pytest.fixture
def _recording_source(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> _RecordingSource:
    """Patch ``_build_source_service`` to hand back a recording stub."""
    source = _RecordingSource()

    def _fake_builder(*, actor: str) -> _RecordingSource:  # noqa: ARG001
        return source

    monkeypatch.setattr("opshub.cli.connector._build_source_service", _fake_builder)
    return source


# ============================================================================
# R3 — default regression: type name only, no message, no traceback
# ============================================================================


class TestDefaultRegressionR3:
    """``OPSHUB_DEBUG`` unset / falsy → no message body, no traceback."""

    def test_default_failure_prints_type_name_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        # Hard-clear OPSHUB_DEBUG so an operator's shell environment
        # cannot perturb the regression check.
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        secret_message = f"401 sk={FAKE_SK_KEY}"
        register_connector(_FailingConnector("stub", RuntimeError(secret_message)))

        result = CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert result.exit_code == 1
        # The summary line is byte-identical to the pre-T3 surface.
        assert "sync failed: RuntimeError" in result.stderr
        # The exception **message** must not appear anywhere on stderr
        # — neither raw, nor sanitised. R3 (event log + terminal) keeps
        # the default surface free of any message body at all.
        assert secret_message not in result.stderr
        # The marker (``sk-***``) only appears under ``--debug``; its
        # absence here pins the gated semantics.
        assert "sk-***" not in result.stderr
        # No traceback frames either.
        assert "Traceback" not in result.stderr

    def test_default_failure_event_log_has_type_name_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        register_connector(_FailingConnector("stub", RuntimeError(f"upstream said {FAKE_GHP_KEY}")))

        CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert _recording_source.failure_calls == [("stub", "RuntimeError")]

    def test_default_stdout_unchanged_on_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        """The stdout summary line shape stays the same.

        Failing syncs do not print a stdout summary at all — the
        success-only line lives after ``cursor_set(sync_started=False)``.
        We assert stdout is empty so a future refactor cannot smuggle
        the exception message onto stdout by accident.
        """
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        register_connector(_FailingConnector("stub", ValueError("benign")))

        result = CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert result.exit_code == 1
        assert result.stdout == ""


# ============================================================================
# R2 / R4 — opt-in debug: sanitised message + traceback on stderr
# ============================================================================


class TestDebugOptInR2R4:
    """``OPSHUB_DEBUG=1`` → sanitised message + traceback on stderr."""

    def test_debug_adds_sanitised_message_and_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        register_connector(_FailingConnector("stub", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        result = CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert result.exit_code == 1
        # Summary line still present.
        assert "sync failed: RuntimeError" in result.stderr
        # Sanitised message + traceback now present.
        assert "sk-***" in result.stderr
        assert FAKE_SK_KEY not in result.stderr
        assert "Traceback" in result.stderr
        assert "RuntimeError" in result.stderr

    def test_debug_event_log_still_type_name_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        """Event log permanence: ``--debug`` must not widen the audit row."""
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        register_connector(_FailingConnector("stub", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert _recording_source.failure_calls == [("stub", "RuntimeError")]

    def test_debug_stdout_remains_empty_on_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        register_connector(_FailingConnector("stub", ValueError("v")))

        result = CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert result.stdout == ""

    @pytest.mark.parametrize(
        ("exception_message", "raw_token", "marker"),
        [
            (f"401 token={FAKE_SK_KEY}", FAKE_SK_KEY, "sk-***"),
            (f"403 token={FAKE_GHP_KEY}", FAKE_GHP_KEY, "ghp_***"),
            (
                f"401 token={FAKE_GITHUB_PAT}",
                FAKE_GITHUB_PAT,
                "github_pat_***",
            ),
            (f"401 slack={FAKE_SLACK_BOT_TOKEN}", FAKE_SLACK_BOT_TOKEN, "xoxb"),
            (f"403 aws={FAKE_AWS_ACCESS_KEY}", FAKE_AWS_ACCESS_KEY, "AKIA***"),
            (
                f"403 google={FAKE_GOOGLE_API_KEY}",
                FAKE_GOOGLE_API_KEY,
                "AIza***",
            ),
            (f"401 id_token={FAKE_JWT}", FAKE_JWT, "[JWT REDACTED]"),
            (f"403 header: {FAKE_BEARER_HEADER}", FAKE_BEARER_TAIL, "Bearer ***"),
        ],
        ids=[
            "sk_key",
            "ghp_key",
            "github_pat",
            "slack_xoxb",
            "aws_access_key",
            "google_api_key",
            "jwt",
            "bearer_header",
        ],
    )
    def test_debug_redacts_every_known_token_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
        exception_message: str,
        raw_token: str,
        marker: str,
    ) -> None:
        """R4 — every token shape recognised by ``core/sanitise`` is rewritten.

        The same regex set powers the T1 structlog processor and the
        :mod:`opshub.mcp._redact` MCP-boundary redactor, so this is
        also a horizontal contract: anything that surfaces here must
        match what those callers strip.
        """
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        register_connector(_FailingConnector("stub", RuntimeError(exception_message)))

        result = CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert result.exit_code == 1
        assert raw_token not in result.stderr, (
            f"raw token {raw_token!r} leaked into stderr:\n{result.stderr}"
        )
        assert marker in result.stderr, (
            f"expected marker {marker!r} missing from stderr:\n{result.stderr}"
        )

    @pytest.mark.parametrize(
        "truthy_value",
        ["1", "true", "TRUE", "yes", "on", "DEBUG"],
    )
    def test_debug_truthy_env_values_enable_opt_in(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
        truthy_value: str,
    ) -> None:
        """Mirror the truthy table in :mod:`opshub.core.logging`."""
        monkeypatch.setenv("OPSHUB_DEBUG", truthy_value)
        register_connector(_FailingConnector("stub", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        result = CliRunner().invoke(app, ["connector", "sync", "stub"])

        assert "sk-***" in result.stderr, f"truthy {truthy_value!r} did not enable debug"
        assert "Traceback" in result.stderr

    @pytest.mark.parametrize(
        "falsy_value",
        ["0", "false", "no", "off", ""],
    )
    def test_debug_falsy_env_values_keep_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
        falsy_value: str,
    ) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", falsy_value)
        register_connector(_FailingConnector("stub", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        result = CliRunner().invoke(app, ["connector", "sync", "stub"])

        # Default surface only.
        assert "sk-***" not in result.stderr
        assert "Traceback" not in result.stderr
        assert "sync failed: RuntimeError" in result.stderr


# ============================================================================
# Horizontal redaction — connector-bound structlog events go through T1
# ============================================================================


class TestHorizontalRedaction:
    """``get_logger().bind(connector=name).info(...)`` events are scrubbed."""

    def test_bind_connector_log_does_not_leak_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A token mistakenly bound onto a connector logger is redacted.

        T1's redaction processor sits on the structlog pipeline
        unconditionally (see :func:`opshub.core.logging.configure_logging`).
        Verify the processor itself rewrites token-shaped values when a
        ``bind(connector=name)`` chain feeds it an event-dict — this is
        the horizontal contract every connector relies on for WARN /
        DEBUG paths. We exercise the processor in-process (rather than
        scraping rendered stderr) so the test is independent of
        process-scope ``configure_logging`` ordering across the suite.
        """
        from opshub.core.logging import (
            _redaction_processor,  # pyright: ignore[reportPrivateUsage]
        )

        # Simulate what ``structlog`` would feed the processor after a
        # ``bind(connector="test-connector").warning("...", api_key=...)``
        # chain: the bound kwargs are merged into the event dict before
        # the processor pipeline runs.
        event_dict = {
            "event": f"upstream auth failure: {FAKE_BEARER_HEADER}",
            "connector": "test-connector",
            "api_key": FAKE_SK_KEY,
            "header": f"token {FAKE_GHP_KEY}",
        }
        result = _redaction_processor(None, "warning", event_dict)

        # No raw token shape survives in any value.
        for key, value in result.items():
            if isinstance(value, str):
                assert FAKE_SK_KEY not in value, f"sk- token leaked into {key!r}"
                assert FAKE_GHP_KEY not in value, f"ghp_ token leaked into {key!r}"
                assert FAKE_BEARER_TAIL not in value, f"Bearer token leaked into {key!r}"
        # And the markers are visible so the operator sees *something*.
        assert "sk-***" in result["api_key"]
        assert "ghp_***" in result["header"]
        assert "Bearer ***" in result["event"]
        # Non-token values pass through untouched.
        assert result["connector"] == "test-connector"
