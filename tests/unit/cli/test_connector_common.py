"""Tests for the shared sync driver (Phase 17-B, ADR-0031).

The legacy ``tests/unit/cli/test_connector.py`` + ``test_connector_debug.py``
pinned the driver invariants on the old ``opshub connector sync <name>``
surface. Phase 17-B moved the driver into
:mod:`opshub.cli._connector_common` and exposes 10 thin per-noun
wrappers (``opshub <connector> sync``). The invariants are now
asserted via the per-noun surface (``opshub <connector> sync``) using
a stub connector registered under one of the real noun names.

Invariants pinned here (carried over from the legacy tests):

1. Unknown name → exit 2 with the "unknown connector" message
   listing available registered names.
2. ``_ProgressSourceProxy`` advances the reporter once per successful
   ``observe`` and zero times for non-observe attribute access /
   raising observes.
3. ``OPSHUB_DEBUG`` default → the ``sync failed: <Type>: <sanitised-msg>``
   summary (sanitised message body **promoted to default** by Phase
   23-B / #532) on stderr, no traceback,
   ``record_sync_failure(error_message=<TypeName>)``.
4. ``OPSHUB_DEBUG=1`` → the sanitised traceback additionally appears on
   stderr; the event-log row's ``error_message`` stays ``<TypeName>``
   (never widens).
5. Every known token shape (``sk-`` / ``ghp_`` / ``github_pat_`` /
   ``xox*-`` / ``AKIA`` / ``AIza`` / ``Bearer …`` / JWT) is rewritten
   to its marker form before any byte hits stderr.
6. The truthy table in :mod:`opshub.cli._connector_common` mirrors
   :data:`opshub.core.logging._TRUTHY` (drift pin).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from typer.testing import CliRunner

from opshub.cli._connector_common import _ProgressSourceProxy  # pyright: ignore[reportPrivateUsage]
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

# Build the canonical token shapes locally so a missing
# ``tests/_secrets`` entry surfaces as an import error rather than a
# silent skip.
FAKE_SK_KEY = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"
FAKE_GHP_KEY = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
FAKE_BEARER_TAIL = "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890"
FAKE_BEARER_HEADER = f"Bearer {FAKE_BEARER_TAIL}"


# ============================================================================
# Scaffolding — stub connector + recording source service
# ============================================================================


class _CountingReporter:
    """Stand-in :class:`ProgressReporter` recording advance calls."""

    def __init__(self) -> None:
        self.count = 0

    def advance(self, n: int = 1) -> None:
        self.count += n

    def update(self, *, total: int | None = None, description: str | None = None) -> None:
        del total, description


class _RecordingSource:
    """In-memory ``SourceService`` stand-in."""

    def __init__(self) -> None:
        self.cursor_set_calls: list[tuple[str, Any, bool]] = []
        self.failure_calls: list[tuple[str, str]] = []

    def cursor_get(self, name: str) -> Any:
        return None

    def cursor_set(self, name: str, value: Any, *, sync_started: bool) -> None:
        self.cursor_set_calls.append((name, value, sync_started))

    def record_sync_failure(self, name: str, *, error_message: str) -> None:
        self.failure_calls.append((name, error_message))


class _FailingConnector:
    """Connector whose ``sync`` always raises a chosen exception.

    Registered under one of the real connector noun names so the per-
    noun ``sync`` callback can dispatch to it via the registry.
    """

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
    """Patch ``_build_source_service`` (in the shared driver) to hand back a stub."""
    source = _RecordingSource()

    def _fake_builder(*, actor: str) -> _RecordingSource:
        return source

    monkeypatch.setattr("opshub.cli._connector_common._build_source_service", _fake_builder)
    return source


# ============================================================================
# Unknown connector → exit 2 (carried over from legacy test_connector)
# ============================================================================


def test_sync_unknown_name_exits_2_with_helpful_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking ``sync`` for a name the registry does not know is a usage error.

    The shared driver eagerly imports every connector subpackage, so
    the live registry contains real noun names by the time the
    ``unknown connector`` arm fires. To exercise the path we patch
    :func:`discover_connectors` to return an empty list so the
    driver surfaces ``available: (none)``.
    """

    def _empty_registry() -> list[Any]:
        return []

    monkeypatch.setattr(
        "opshub.connectors.discover_connectors",
        _empty_registry,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["github", "sync"])
    assert result.exit_code == 2
    assert "github" in result.stderr
    assert "(none)" in result.stderr


# ============================================================================
# _ProgressSourceProxy — observe counting (carried over)
# ============================================================================


def test_progress_proxy_advances_reporter_once_per_observe() -> None:
    """Each successful ``observe`` bumps the progress counter by one."""

    class _FakeSource:
        def __init__(self) -> None:
            self.observed: list[dict[str, object]] = []

        def observe(self, **kwargs: object) -> tuple[str, str]:
            self.observed.append(kwargs)
            return ("source-id", "inbox-id")

    reporter = _CountingReporter()
    inner = _FakeSource()
    proxy = _ProgressSourceProxy(inner, reporter)

    out = proxy.observe(external_id="x", title="t")

    assert out == ("source-id", "inbox-id")
    assert inner.observed == [{"external_id": "x", "title": "t"}]
    assert reporter.count == 1


def test_progress_proxy_forwards_other_attributes_without_counting() -> None:
    """Non-observe calls (cursor_set, ...) forward and do not advance."""

    class _FakeSource:
        def cursor_set(self, name: str, value: str | None, *, sync_started: bool) -> str:
            return f"{name}:{value}:{sync_started}"

    reporter = _CountingReporter()
    proxy = _ProgressSourceProxy(_FakeSource(), reporter)

    assert proxy.cursor_set("slack", "ts-1", sync_started=False) == "slack:ts-1:False"
    assert reporter.count == 0


def test_progress_proxy_does_not_count_failed_observe() -> None:
    """A raising ``observe`` must not inflate the counter."""

    class _BoomSource:
        def observe(self, **kwargs: object) -> tuple[str, str]:
            del kwargs
            raise RuntimeError("boom")

    reporter = _CountingReporter()
    proxy = _ProgressSourceProxy(_BoomSource(), reporter)

    with pytest.raises(RuntimeError, match="boom"):
        proxy.observe(external_id="x")
    assert reporter.count == 0


# ============================================================================
# R3 — default failure: sanitised message body (Phase 23-B / #532), no traceback
# ============================================================================


class TestDefaultRegressionR3:
    """``OPSHUB_DEBUG`` unset / falsy → sanitised body, no traceback.

    Phase 23-B (#532) promotes the previously debug-only sanitised
    message body to the default failure trail so the operator who hits
    a sync error (scope / token / rate-limit) sees the actionable
    recovery text without re-running under ``OPSHUB_DEBUG=1``. The raw
    secret is still scrubbed (sanitised) and the traceback stays gated
    behind debug.
    """

    def test_default_failure_prints_sanitised_message_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        secret_message = f"401 sk={FAKE_SK_KEY}"
        register_connector(_FailingConnector("github", RuntimeError(secret_message)))

        result = CliRunner().invoke(app, ["github", "sync"])

        assert result.exit_code == 1
        # Body promoted to default: type name + sanitised actionable text.
        assert "sync failed: RuntimeError: " in result.stderr
        assert "401 sk=" in result.stderr
        # ... but the raw secret is still scrubbed to its marker.
        assert FAKE_SK_KEY not in result.stderr
        assert "sk-***" in result.stderr
        # Traceback stays gated behind OPSHUB_DEBUG=1.
        assert "Traceback" not in result.stderr

    def test_default_failure_with_actionable_body_surfaces_recovery_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        """A token-free actionable message reaches stderr verbatim by default."""
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        actionable = "missing_scope: run `opshub slack auth set` to grant search:read"
        register_connector(_FailingConnector("github", RuntimeError(actionable)))

        result = CliRunner().invoke(app, ["github", "sync"])

        assert result.exit_code == 1
        assert f"sync failed: RuntimeError: {actionable}" in result.stderr
        assert "Traceback" not in result.stderr

    @pytest.mark.parametrize(
        ("exception_message", "raw_token", "marker"),
        [
            (f"401 token={FAKE_SK_KEY}", FAKE_SK_KEY, "sk-***"),
            (f"403 token={FAKE_GHP_KEY}", FAKE_GHP_KEY, "ghp_***"),
            (f"401 token={FAKE_GITHUB_PAT}", FAKE_GITHUB_PAT, "github_pat_***"),
            (f"401 slack={FAKE_SLACK_BOT_TOKEN}", FAKE_SLACK_BOT_TOKEN, "xoxb"),
            (f"403 aws={FAKE_AWS_ACCESS_KEY}", FAKE_AWS_ACCESS_KEY, "AKIA***"),
            (f"403 google={FAKE_GOOGLE_API_KEY}", FAKE_GOOGLE_API_KEY, "AIza***"),
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
    def test_default_failure_redacts_every_known_token_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
        exception_message: str,
        raw_token: str,
        marker: str,
    ) -> None:
        """ADR-0027: secret redaction holds at default verbosity, not just debug.

        Phase 23-B promotes the message body to the default trail, so
        the per-token-shape redaction guard that previously only ran
        under ``OPSHUB_DEBUG=1`` must hold here too.
        """
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        register_connector(_FailingConnector("github", RuntimeError(exception_message)))

        result = CliRunner().invoke(app, ["github", "sync"])

        assert result.exit_code == 1
        assert raw_token not in result.stderr, (
            f"raw token {raw_token!r} leaked into default stderr:\n{result.stderr}"
        )
        assert marker in result.stderr, (
            f"expected marker {marker!r} missing from default stderr:\n{result.stderr}"
        )

    def test_default_failure_event_log_has_type_name_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        register_connector(
            _FailingConnector("github", RuntimeError(f"upstream said {FAKE_GHP_KEY}"))
        )

        CliRunner().invoke(app, ["github", "sync"])

        assert _recording_source.failure_calls == [("github", "RuntimeError")]

    def test_default_stdout_unchanged_on_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        register_connector(_FailingConnector("github", ValueError("benign")))

        result = CliRunner().invoke(app, ["github", "sync"])

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
        register_connector(_FailingConnector("github", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        result = CliRunner().invoke(app, ["github", "sync"])

        assert result.exit_code == 1
        assert "sync failed: RuntimeError" in result.stderr
        assert "sk-***" in result.stderr
        assert FAKE_SK_KEY not in result.stderr
        assert "Traceback" in result.stderr
        assert "RuntimeError" in result.stderr

    def test_debug_event_log_still_type_name_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        register_connector(_FailingConnector("github", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        CliRunner().invoke(app, ["github", "sync"])

        assert _recording_source.failure_calls == [("github", "RuntimeError")]

    def test_debug_stdout_remains_empty_on_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recording_source: _RecordingSource,
    ) -> None:
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        register_connector(_FailingConnector("github", ValueError("v")))

        result = CliRunner().invoke(app, ["github", "sync"])

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
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        register_connector(_FailingConnector("github", RuntimeError(exception_message)))

        result = CliRunner().invoke(app, ["github", "sync"])

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
        monkeypatch.setenv("OPSHUB_DEBUG", truthy_value)
        register_connector(_FailingConnector("github", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        result = CliRunner().invoke(app, ["github", "sync"])

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
        register_connector(_FailingConnector("github", RuntimeError(f"401 sk={FAKE_SK_KEY}")))

        result = CliRunner().invoke(app, ["github", "sync"])

        # Falsy debug only gates the traceback; the sanitised message
        # body is now part of the default trail (Phase 23-B / #532).
        assert "Traceback" not in result.stderr
        assert "sync failed: RuntimeError: " in result.stderr
        # The redaction marker is present (body is sanitised) and the
        # raw secret never leaks regardless of verbosity.
        assert "sk-***" in result.stderr
        assert FAKE_SK_KEY not in result.stderr


# ============================================================================
# Drift pin — ``_DEBUG_TRUTHY`` mirrors ``opshub.core.logging._TRUTHY``
# ============================================================================


class TestDebugTruthyDriftPin:
    """The two truthy tables must stay in sync."""

    def test_cli_truthy_table_matches_core_logging(self) -> None:
        from opshub.cli._connector_common import (
            _DEBUG_TRUTHY,  # pyright: ignore[reportPrivateUsage]
        )
        from opshub.core.logging import (
            _TRUTHY as CORE_TRUTHY,  # pyright: ignore[reportPrivateUsage]
        )

        assert _DEBUG_TRUTHY == CORE_TRUTHY, (
            "opshub.cli._connector_common._DEBUG_TRUTHY drifted from "
            "opshub.core.logging._TRUTHY. The two tables must accept the "
            "same set of strings for OPSHUB_DEBUG so the CLI in-process "
            "path and the MCP subprocess path agree on what 'truthy' means."
        )


# ============================================================================
# Horizontal redaction — connector-bound structlog events go through T1
# ============================================================================


class TestHorizontalRedaction:
    """``get_logger().bind(connector=name).info(...)`` events are scrubbed."""

    def test_bind_connector_log_does_not_leak_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opshub.core.logging import (
            _redaction_processor,  # pyright: ignore[reportPrivateUsage]
        )

        event_dict = {
            "event": f"upstream auth failure: {FAKE_BEARER_HEADER}",
            "connector": "test-connector",
            "api_key": FAKE_SK_KEY,
            "header": f"token {FAKE_GHP_KEY}",
        }
        result = _redaction_processor(None, "warning", event_dict)

        for key, value in result.items():
            if isinstance(value, str):
                assert FAKE_SK_KEY not in value, f"sk- token leaked into {key!r}"
                assert FAKE_GHP_KEY not in value, f"ghp_ token leaked into {key!r}"
                assert FAKE_BEARER_TAIL not in value, f"Bearer token leaked into {key!r}"
        assert "sk-***" in result["api_key"]
        assert "ghp_***" in result["header"]
        assert "Bearer ***" in result["event"]
        assert result["connector"] == "test-connector"
