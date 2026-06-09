"""Phase 7 connector atomicity / failure-mode tests.

Three failure paths are validated per connector (one parameterised test
function each):

1. **Rate-limit exhaustion** — the fetcher raises
   :class:`ConnectorFailedError` after exhausting the retry budget
   (the connectors' module docstrings document the 1s/2s/4s exponential
   backoff, max 3 retries — see :mod:`opshub.connectors.slack.fetcher`
   / :mod:`opshub.connectors.ms365.fetcher` / :mod:`opshub.connectors.box.fetcher`).
2. **Token expiry / auth failure** — the fetcher raises
   :class:`ConnectorFailedError` with a non-retryable surface message.
3. **Partial success** — the fetcher yields a couple of items
   successfully then raises mid-iterator. The successfully-yielded items
   must persist; the failing item must not; a ``ConnectorSyncFailed``
   event must record the exception type only (ADR-0005 + sanitisation
   contract from :func:`opshub.cli._connector_common.run_connector_sync`
   / :class:`MS365Connector._run_endpoint`).

Why we pin all three:

- Phase 7 plan §3 Sub-issue D bullet #2 calls out rate-limit / token
  expiry / partial success as the three atomicity scenarios closeout
  must cover.
- Each connector has a *different* failure rail: Slack and Box let
  the connector-level loop surface failures (the CLI driver in
  :mod:`opshub.cli._connector_common` catches them); MS365 swallows
  per-endpoint failures and records ``ConnectorSyncFailed`` inside
  :meth:`MS365Connector._run_endpoint` so other endpoints can still
  run.
- ADR-0010 requires every connector to record a sanitised
  ``ConnectorSyncFailed`` event on failure so the operator can audit
  what went wrong without exposing tokens / PII. The atomicity tests
  are the only place that prove the contract holds end-to-end.

Each test uses ``isolated_env`` (a fresh migrated SQLite DB +
``opshub init``) so the atomicity assertions are made against the
real event store + projections pipeline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Phase 7 atomicity requires the 'connectors-slack' extras",
)
pytest.importorskip(
    "msal",
    reason="Phase 7 atomicity requires the 'connectors-ms365' extras",
)
pytest.importorskip(
    "httpx",
    reason="Phase 7 atomicity requires the 'connectors-ms365' extras",
)
pytest.importorskip(
    "boxsdk",
    reason="Phase 7 atomicity requires the 'connectors-box' extras",
)

from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors import register_connector, unregister_all
from opshub.connectors.box.connector import BoxConnector
from opshub.connectors.box.fetcher import RawBoxEvent
from opshub.connectors.ms365.connector import MS365Connector
from opshub.connectors.ms365.fetcher import (
    RawCalendarEvent,
    RawOneDriveItem,
    RawOutlookMessage,
)
from opshub.connectors.slack.connector import SlackConnector
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.core.errors import ConnectorFailedError
from opshub.db.engine import create_engine_for_sqlite

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_count(engine: Engine, table: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def _failed_event_count(engine: Engine) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE event_type = 'connector.sync_failed'")
            ).scalar_one()
        )


def _failed_event_payloads(engine: Engine) -> list[str]:
    """Return the raw JSON payload string for every ``connector.sync_failed`` row."""
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM events WHERE event_type = 'connector.sync_failed'")
        ).all()
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# Slack failure helpers
# ---------------------------------------------------------------------------


def _slack_raw(*, ts: str, text: str) -> RawSlackMessage:
    return RawSlackMessage(
        channel_id="C1",
        channel_name="general",
        ts=ts,
        text=text,
        user_id="U1",
        user_display_name="alice",
        permalink=f"https://acme.slack.com/archives/C1/p{ts.replace('.', '')}",
        raw={},
    )


_SlackPrefixAndError = tuple[list[tuple[str, RawSlackMessage, str | None]], Exception]


def _install_slack_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yields: list[tuple[str, RawSlackMessage, str | None]] | None = None,
    error: Exception | None = None,
    yields_then_error: _SlackPrefixAndError | None = None,
) -> None:
    """Replace :class:`SlackFetcher` with a programmable double.

    Three modes:

    * ``yields`` — happy path (unused in atomicity tests, retained for
      symmetry with the lifecycle suite).
    * ``error`` — raise immediately on first ``fetch_messages`` call.
    * ``yields_then_error`` — yield the prefix list then raise; used by
      the partial-success scenario to prove the successfully-yielded
      items persist before the failure aborts the iteration.
    """
    fake_fetcher_cls = MagicMock()

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
        excludes: Any = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        # ADR-0030 (#466): the connector forwards the resolved
        # ``ExcludeRules`` filter so the real fetcher can short-circuit
        # ``conversations.replies`` calls for excluded parents. This
        # atomicity mock accepts and ignores it.
        del cursor_per_channel, max_per_channel, excludes
        if yields_then_error is not None:
            prefix, raised = yields_then_error
            yield from prefix
            raise raised
        if error is not None:
            raise error
        yield from yields or []

    fake_fetcher_cls.return_value.fetch_messages.side_effect = _fetch_messages
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        fake_fetcher_cls,
    )


@pytest.fixture
def slack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_TOKEN", "xoxp-test")
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__CHANNELS", '["C1"]')
    # Phase 23-H (#538, ADR-0039): stub the workspace-resolving auth.test the
    # single-workspace bind guard calls before any fetch, so the hermetic test
    # does not hit the network (the guard binds T-int; the failure / partial
    # paths the tests assert happen later, inside the stubbed fetcher).
    from opshub.connectors.slack.auth import SlackAuth

    def _stub_test_token(_self: SlackAuth) -> dict[str, str]:
        return {"team": "t", "team_id": "T-int", "user": "u", "user_id": "U1", "principal": "user"}

    monkeypatch.setattr(SlackAuth, "test_token", _stub_test_token)
    yield


# ---------------------------------------------------------------------------
# MS365 failure helpers
# ---------------------------------------------------------------------------


def _ms365_outlook(message_id: str) -> RawOutlookMessage:
    return RawOutlookMessage(
        id=message_id,
        subject=f"phase7 atomicity {message_id}",
        body_preview="atomicity test fixture",
        sender="alice@example.com",
        received_iso="2026-05-16T15:45:00Z",
        web_link=f"https://outlook.office.com/mail/inbox/id/{message_id}",
        raw={"id": message_id},
    )


def _install_ms365_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calendar_error: Exception | None = None,
    outlook_prefix: list[RawOutlookMessage] | None = None,
    outlook_error: Exception | None = None,
) -> None:
    """Replace ``MS365Auth`` + ``MS365Fetcher`` with stubs.

    The partial-success scenario yields ``outlook_prefix`` then raises
    ``outlook_error`` mid-iterator — the per-endpoint loop in
    :meth:`MS365Connector._run_endpoint` should commit the prefix items
    before swallowing the exception into a ``ConnectorSyncFailed``
    event.
    """
    from opshub.connectors.ms365 import auth as auth_module
    from opshub.connectors.ms365 import fetcher as fetcher_module

    class _StubAuth:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def get_access_token(self) -> str:
            return "bearer-stub"

    class _StubFetcher:
        def __init__(self, _auth: object) -> None:
            pass

        def fetch_calendar_events(
            self, *, since_iso: str | None
        ) -> Iterator[tuple[RawCalendarEvent, str]]:
            del since_iso
            if calendar_error is not None:
                raise calendar_error
            return iter(())

        def fetch_onedrive_changes(
            self, *, delta_link: str | None
        ) -> Iterator[tuple[RawOneDriveItem, str]]:
            del delta_link
            return iter(())

        def fetch_outlook_messages(
            self, *, since_iso: str | None
        ) -> Iterator[tuple[RawOutlookMessage, str]]:
            del since_iso
            if outlook_prefix is not None:
                for item in outlook_prefix:
                    yield item, item.received_iso
                if outlook_error is not None:
                    raise outlook_error
                return
            if outlook_error is not None:
                raise outlook_error
            return  # pragma: no cover — empty default path

        def close(self) -> None:
            return None

    monkeypatch.setattr(auth_module, "MS365Auth", _StubAuth)
    monkeypatch.setattr(fetcher_module, "MS365Fetcher", _StubFetcher)


@pytest.fixture
def ms365_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OPSHUB_CONNECTORS__MS365__CLIENT_ID", "test-client-id")
    yield


# ---------------------------------------------------------------------------
# Box failure helpers
# ---------------------------------------------------------------------------


def _box_raw(event_id: str) -> RawBoxEvent:
    return RawBoxEvent(
        event_id=event_id,
        event_type="ITEM_CREATE",
        item_id="12345",
        item_type="file",
        item_name=f"atomicity-{event_id}.pdf",
        item_path=f"/Documents/Atomicity/{event_id}.pdf",
        created_iso="2026-05-17T10:00:00Z",
        actor_id="u-1",
        actor_name="Alice",
        web_url=f"https://app.box.com/file/{event_id}",
        raw={"event_id": event_id, "event_type": "ITEM_CREATE"},
    )


class _BoxFailingFetcher:
    """Programmable :class:`BoxFetcher` double for atomicity scenarios.

    Supports three modes via constructor arguments:

    * ``error`` — raise on every ``fetch_events`` call (rate-limit /
      token-expiry simulation).
    * ``prefix`` + ``error`` — yield the prefix list then raise
      (partial-success simulation).
    """

    def __init__(
        self,
        *,
        prefix: list[tuple[RawBoxEvent, str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._prefix = prefix or []
        self._error = error
        self.calls: list[str | None] = []

    def fetch_events(self, *, stream_position: str | None) -> Iterator[tuple[RawBoxEvent, str]]:
        self.calls.append(stream_position)
        yield from self._prefix
        if self._error is not None:
            raise self._error


def _install_box_stub(
    *,
    prefix: list[tuple[RawBoxEvent, str]] | None = None,
    error: Exception | None = None,
) -> _BoxFailingFetcher:
    from typing import cast

    from opshub.connectors.box.fetcher import BoxFetcher

    stub = _BoxFailingFetcher(prefix=prefix, error=error)
    register_connector(BoxConnector(fetcher_factory=lambda: cast("BoxFetcher", stub)))
    return stub


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _phase7_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    unregister_all()
    register_connector(SlackConnector())
    register_connector(MS365Connector())
    yield
    unregister_all()


# ---------------------------------------------------------------------------
# Slack atomicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "make_error"),
    [
        # Rate-limit exhaustion: the fetcher raises ConnectorFailedError
        # after the 1s/2s/4s retry budget has been consumed. The
        # connector + CLI driver translate this into a sanitised
        # ConnectorSyncFailed event.
        ("rate_limit", lambda: ConnectorFailedError("Slack rate-limit budget exhausted")),
        # Token expiry / auth failure: the SDK raises ``invalid_auth``
        # which the fetcher maps to ConnectorFailedError too. The CLI
        # driver's behaviour is identical to the rate-limit case.
        ("token_expiry", lambda: ConnectorFailedError("Slack invalid_auth: token revoked")),
    ],
)
def test_slack_failure_records_sync_failed(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
    scenario: str,
    make_error: Callable[[], ConnectorFailedError],
) -> None:
    """Both pre-iteration failure modes record a single sync_failed event.

    The CLI driver's ``try / except`` arm
    (:func:`opshub.cli._connector_common.run_connector_sync`) catches every
    exception, records ``ConnectorSyncFailed`` with
    ``type(exc).__name__`` only, and exits 1. The successful-source
    projections must stay empty because the fetcher never reached the
    observe path.
    """
    error = make_error()
    _install_slack_fetcher(monkeypatch, error=error)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync"])
    assert result.exit_code == 1, scenario

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # No source / inbox rows — the failure happened before the
        # connector reached :meth:`SourceService.observe`.
        assert _row_count(engine, "sources") == 0
        assert _row_count(engine, "inbox_items") == 0
        # Exactly one connector.sync_failed row with the sanitised
        # exception type.
        assert _failed_event_count(engine) == 1
        payloads = _failed_event_payloads(engine)
        assert "ConnectorFailedError" in payloads[0]
        # Raw exception message must NOT be persisted (ADR-0005 + the
        # CLI driver's ``type(exc).__name__`` sanitisation contract).
        assert "rate-limit" not in payloads[0]
        assert "invalid_auth" not in payloads[0]
        assert "token revoked" not in payloads[0]
    finally:
        engine.dispose()


def test_slack_partial_success_persists_yielded_messages(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """The fetcher yields two messages, then raises on the third.

    Each yielded ``(channel_id, message, new_cursor)`` triple goes
    through its own :meth:`SourceService.observe` call (one UoW each)
    so the two successful yields land durably before the third raises.
    The CLI driver maps the exception to a ``ConnectorSyncFailed``
    event and exits 1.

    Verified contract:

    * 2 ``sources`` rows + 2 ``inbox_items`` rows persist (the prefix).
    * 1 ``connector.sync_failed`` row is appended (the failure).
    * Exit code is 1 — :func:`opshub.cli._connector_common.run_connector_sync`
      treats any non-config failure path as a process-level fail.
    """
    prefix: list[tuple[str, RawSlackMessage, str | None]] = [
        ("C1", _slack_raw(ts="1700000001.000100", text="message one"), "1700000001.000100"),
        ("C1", _slack_raw(ts="1700000002.000200", text="message two"), "1700000002.000200"),
    ]
    _install_slack_fetcher(
        monkeypatch,
        yields_then_error=(
            prefix,
            ConnectorFailedError("Slack fetch failed for channel C1: ratelimited"),
        ),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync"])
    assert result.exit_code == 1, result.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # Two prefix items are durably committed before the failure.
        assert _row_count(engine, "sources") == 2
        assert _row_count(engine, "inbox_items") == 2
        # Exactly one sync_failed event with the sanitised type name.
        assert _failed_event_count(engine) == 1
        payloads = _failed_event_payloads(engine)
        assert "ConnectorFailedError" in payloads[0]
        # The "ratelimited" substring (verbatim API error code) must
        # not leak into the persisted event.
        assert "ratelimited" not in payloads[0]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# MS365 atomicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "error_message"),
    [
        ("rate_limit", "graph 429 retry budget exhausted"),
        ("token_expiry", "graph 401 refresh failed: invalid_grant"),
    ],
)
def test_ms365_calendar_failure_records_per_endpoint_sync_failed(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    ms365_env: None,
    scenario: str,
    error_message: str,
) -> None:
    """Calendar endpoint raises → ``ConnectorSyncFailed`` recorded,
    OneDrive + Outlook still run (here both are empty, so no source rows).

    The MS365 connector swallows per-endpoint
    :class:`ConnectorFailedError` and records
    ``ConnectorSyncFailed`` via
    :meth:`SourceService.record_sync_failure` inside
    :meth:`MS365Connector._run_endpoint` — see the module docstring of
    :mod:`opshub.connectors.ms365.connector`. The CLI driver therefore
    exits 0 because the connector-level sync ran to completion (with
    one endpoint quietly failing); the failure is auditable in the
    event log.
    """
    _install_ms365_fetcher(monkeypatch, calendar_error=ConnectorFailedError(error_message))

    runner = CliRunner()
    result = runner.invoke(app, ["ms365", "sync"])
    # Per-endpoint failures are isolated: the connector-level sync
    # completes successfully with observed_count=0.
    assert result.exit_code == 0, (scenario, result.stdout, result.stderr)

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # No sources because none of the three endpoints yielded
        # anything (calendar raised, the others were empty).
        assert _row_count(engine, "sources") == 0
        # At least one ConnectorSyncFailed event with the sanitised
        # type-name marker. ``MS365Connector._run_endpoint`` records
        # ``"<cursor_key>: <type-name>"`` so the type name is the
        # sanitised payload.
        assert _failed_event_count(engine) >= 1
        payloads = _failed_event_payloads(engine)
        joined = " ".join(payloads)
        assert "ConnectorFailedError" in joined
        # Sanitisation: the raw error message must not appear.
        assert error_message not in joined
    finally:
        engine.dispose()


def test_ms365_outlook_partial_success_persists_prefix(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    ms365_env: None,
) -> None:
    """Outlook yields 2 messages, then raises → 2 sources + 1 sync_failed.

    The :meth:`MS365Connector._run_endpoint` loop calls
    :meth:`SourceService.observe` per yielded item (one UoW each), so
    the 2 prefix items land durably before the iterator raises.
    Calendar / OneDrive are both empty so the per-endpoint cursor
    write only fires for Outlook (and that fires only on the success
    path; here Outlook fails so the cursor stays put).
    """
    prefix = [_ms365_outlook("msg-prefix-1"), _ms365_outlook("msg-prefix-2")]
    _install_ms365_fetcher(
        monkeypatch,
        outlook_prefix=prefix,
        outlook_error=ConnectorFailedError("graph 503 partial-success simulator"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["ms365", "sync"])
    # Per-endpoint failure semantics: connector-level sync still
    # completes; the CLI exits 0.
    assert result.exit_code == 0, result.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # Two prefix Outlook items persisted.
        assert _row_count(engine, "sources") == 2
        assert _row_count(engine, "inbox_items") == 2
        # One ConnectorSyncFailed event for the failed Outlook endpoint.
        assert _failed_event_count(engine) >= 1
        payloads = _failed_event_payloads(engine)
        joined = " ".join(payloads)
        assert "ConnectorFailedError" in joined
        assert "partial-success simulator" not in joined
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Box atomicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "error_message"),
    [
        ("rate_limit", "Box rate-limit exhausted retries"),
        ("token_expiry", "Box invalid_grant: refresh token revoked"),
    ],
)
def test_box_failure_records_sync_failed(
    isolated_env: _PathsDict,
    scenario: str,
    error_message: str,
) -> None:
    """Box fetcher raises → sync_failed event + exit code 1.

    Box's connector propagates fetcher exceptions verbatim (no
    per-endpoint isolation like MS365), so the CLI driver's
    ``try/except`` arm records the failure and exits 1. Source rows
    must remain empty because the fetcher raised before any item was
    yielded.
    """
    _install_box_stub(error=ConnectorFailedError(error_message))

    runner = CliRunner()
    result = runner.invoke(app, ["box", "sync"])
    assert result.exit_code == 1, (scenario, result.stdout, result.stderr)

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        assert _row_count(engine, "sources") == 0
        assert _row_count(engine, "inbox_items") == 0
        assert _failed_event_count(engine) == 1
        payloads = _failed_event_payloads(engine)
        assert "ConnectorFailedError" in payloads[0]
        assert error_message not in payloads[0]
    finally:
        engine.dispose()


def test_box_partial_success_persists_prefix(
    isolated_env: _PathsDict,
) -> None:
    """Box yields 2 events then raises → 2 sources + 1 sync_failed.

    The Box connector loop in :meth:`BoxConnector.sync` calls
    :meth:`SourceService.observe` once per yielded event (one UoW
    each). The prefix events land durably before the iterator raises
    mid-stream; the CLI driver maps the exception to a
    ``ConnectorSyncFailed`` event and exits 1.
    """
    prefix = [
        (_box_raw("evt-prefix-1"), "pos-1"),
        (_box_raw("evt-prefix-2"), "pos-1"),
    ]
    _install_box_stub(
        prefix=prefix,
        error=ConnectorFailedError("Box events API mid-stream failure"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box", "sync"])
    assert result.exit_code == 1, result.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # 2 prefix events durably committed before the failure.
        assert _row_count(engine, "sources") == 2
        assert _row_count(engine, "inbox_items") == 2
        # Exactly one sync_failed event with the sanitised type name.
        assert _failed_event_count(engine) == 1
        payloads = _failed_event_payloads(engine)
        assert "ConnectorFailedError" in payloads[0]
        assert "mid-stream failure" not in payloads[0]
    finally:
        engine.dispose()


# Re-export ``pytest`` so static analysers see this module is a pytest test.
_ = pytest
