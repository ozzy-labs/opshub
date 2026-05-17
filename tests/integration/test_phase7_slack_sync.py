"""End-to-end :class:`SlackConnector` lifecycle against a real SQLite database.

Drives the connector through the same ``isolated_env`` fixture used by
:mod:`tests.integration.test_github_connector_lifecycle`, with two
twists:

1. :class:`opshub.connectors.slack.fetcher.SlackFetcher` is
   monkeypatched to yield controlled :class:`RawSlackMessage`
   payloads so the suite never reaches Slack's API.
2. The Slack bot token is injected through the
   ``OPSHUB_CONNECTOR_SLACK_BOT_TOKEN`` env var override so the
   ``[secrets]`` keyring backend is never consulted (matches the
   Phase 3 GitHub precedent and keeps CI hermetic).

Why integration-level (not pure unit):

* The :meth:`SourceService.observe` contract is "one
  ``SourceObserved`` + one ``ItemEnqueued`` per item, atomically".
  Pinning the observed effect on disk (sources + inbox_items + events
  rows) is the only honest way to prove the contract still holds when
  we add the Slack connector in front of the service.
* Cursor idempotency — a second sync with no new messages must
  advance the ``connector_cursors`` row's ``updated_at`` but not
  re-emit any source rows. The :class:`ConnectorCursorsProjection`
  reducer is the indirection that makes this work and the integration
  test is the only place that exercises it end-to-end for Slack.
* Failure semantics — a fetcher exception (token revoked, channel
  not found, rate-limit budget exhausted) must surface as a
  :class:`ConnectorSyncFailed` event with a sanitised message. The
  CLI driver's ``try/except`` arm owns the sanitisation; only the
  integration test reaches that arm.

The CLI driver (``opshub connector sync slack``) lives in
:mod:`opshub.cli.connector` and its own surface (Typer command,
cursor bracket, exception sanitisation) is exercised here through
:class:`~typer.testing.CliRunner` so the contract is validated end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

pytest.importorskip(
    "slack_sdk",
    reason="Slack connector tests require the 'connectors-slack' extras",
)

from opshub.cli.app import app
from opshub.connectors import register_connector, unregister_all
from opshub.connectors.slack.connector import SlackConnector
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.core.errors import ConnectorFailedError
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------- helpers


def _row_count(engine: Engine, table_name: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())


def _raw_message(
    *,
    channel_id: str = "C1",
    channel_name: str = "general",
    ts: str,
    text: str,
    user_display_name: str = "alice",
    permalink: str | None = None,
) -> RawSlackMessage:
    """Build a :class:`RawSlackMessage` with reasonable defaults.

    Tests override only the fields their scenario cares about; the
    rest stay at the documented happy-path defaults to keep each
    test focused on one behaviour at a time.
    """
    if permalink is None:
        permalink = f"https://acme.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"
    return RawSlackMessage(
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        text=text,
        user_id="U1",
        user_display_name=user_display_name,
        permalink=permalink,
        raw={},
    )


def _patch_slack_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yields: list[tuple[str, RawSlackMessage, str | None]] | None = None,
    error: Exception | None = None,
) -> None:
    """Replace :class:`SlackFetcher` so :meth:`fetch_messages` returns ``yields``.

    The connector imports :class:`SlackFetcher` from its own
    namespace (``opshub.connectors.slack.connector.SlackFetcher``) so
    we patch the attribute on the connector module, not the fetcher
    module itself.

    ``error`` swaps the success path for a raising one — useful for
    the ``ConnectorSyncFailed`` recording test.
    """
    from unittest.mock import MagicMock

    fake_fetcher_cls = MagicMock()

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        del cursor_per_channel, max_per_channel
        if error is not None:
            raise error
        return iter(yields or [])

    fake_fetcher_cls.return_value.fetch_messages.side_effect = _fetch_messages
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        fake_fetcher_cls,
    )


@pytest.fixture
def slack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject the Slack bot token + channel list the connector requires.

    * The bot token override (``OPSHUB_CONNECTOR_SLACK_BOT_TOKEN``)
      keeps :class:`SlackAuth` away from the keyring so the test is
      hermetic on dev machines without ``[secrets]`` installed.
    * The channel list (``OPSHUB_CONNECTORS__SLACK__CHANNELS``) is
      a JSON-encoded list per :mod:`pydantic_settings` conventions for
      nested list overrides.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__CHANNELS", '["C1"]')
    yield


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Restore the slack connector around each test.

    Sibling Phase 7 integration suites (``test_phase7_box_sync.py`` /
    ``test_phase7_ms365_sync.py``) call :func:`unregister_all` to
    isolate their stubs; if they run before this module the slack
    connector's import-time registration has already happened but
    the registry now omits it. Re-registering on entry restores the
    process-wide invariant ``"slack" in discover_connectors()`` that
    the CLI driver depends on. The exit-side :func:`unregister_all`
    keeps subsequent tests in the same process from inheriting our
    state.
    """
    unregister_all()
    register_connector(SlackConnector())
    yield
    unregister_all()


# ---------------------------------------------------------------------- happy path


def test_slack_sync_creates_sources(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """End-to-end first sync: 2 messages observed, projections populated, cursor advances.

    Drives the full ``opshub connector sync slack`` path through
    :class:`CliRunner`:

    1. ``conftest.isolated_env`` runs ``opshub init`` so the schema
       is provisioned.
    2. ``slack_env`` injects the token + channels into the env.
    3. ``_patch_slack_fetcher`` replaces the SDK boundary so no real
       Slack API call leaves the process.
    4. The CLI exits 0 and the ``sources`` / ``inbox_items`` /
       ``connector_cursors`` projections reflect the two messages.
    """
    yields: list[tuple[str, RawSlackMessage, str | None]] = [
        (
            "C1",
            _raw_message(ts="1700000001.000100", text="first message"),
            "1700000001.000100",
        ),
        (
            "C1",
            _raw_message(ts="1700000002.000200", text="second message"),
            "1700000002.000200",
        ),
    ]
    _patch_slack_fetcher(monkeypatch, yields=yields)

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "sync", "slack"])
    assert result.exit_code == 0, result.stdout

    # ---- On-disk state ---------------------------------------------------
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # SourceObserved + ItemEnqueued per message → 4 events. Plus
        # the connector sync run bracket (``ConnectorSyncStarted``
        # + ``ConnectorSyncCompleted``) → 6 total.
        assert _row_count(engine, "events") == 6
        # Sources projection upserts on (connector_name, external_id):
        # two distinct messages → two rows.
        assert _row_count(engine, "sources") == 2
        # Inbox projection adds one row per observation.
        assert _row_count(engine, "inbox_items") == 2

        from sqlalchemy import select

        with engine.connect() as conn:
            source_rows = conn.execute(select(sources_table)).mappings().all()
            inbox_rows = conn.execute(select(inbox_items_table)).mappings().all()

        assert {row["connector_name"] for row in source_rows} == {"slack"}
        assert {row["source_type"] for row in source_rows} == {"slack_message"}
        assert {row["external_id"] for row in source_rows} == {
            "C1:1700000001.000100",
            "C1:1700000002.000200",
        }
        # Title shape matches the mapper contract.
        assert {row["title"] for row in source_rows} == {"alice in #general"}
        # Summary is the (un-truncated) text since both fit under cap.
        assert {row["summary"] for row in source_rows} == {"first message", "second message"}
        # Every inbox row links back through ``source_ref``.
        assert all(row["state"] == "pending" for row in inbox_rows)
        assert {row["source_ref"] for row in inbox_rows} == {
            "slack:C1:1700000001.000100",
            "slack:C1:1700000002.000200",
        }
    finally:
        engine.dispose()


# ---------------------------------------------------------------------- idempotency


def test_slack_sync_is_idempotent_when_no_new_messages(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """Second sync with no new yields → cursor stays put, no new source rows.

    The cursor round-trip is the contract: sync #1 advances
    ``cursor_value`` to ``{"C1": "1700000001.000100"}``; sync #2
    receives that string in ``ConnectorContext.cursor_value``, parses
    it, hands it to the fetcher, and yields nothing.
    """
    # First sync: one message.
    _patch_slack_fetcher(
        monkeypatch,
        yields=[
            (
                "C1",
                _raw_message(ts="1700000001.000100", text="first"),
                "1700000001.000100",
            ),
        ],
    )
    runner = CliRunner()
    first = runner.invoke(app, ["connector", "sync", "slack"])
    assert first.exit_code == 0, first.stdout

    # Second sync: fetcher yields nothing. The connector still drives
    # the full bracket (started → completed), so the events table
    # gains 2 more rows but the source / inbox projections are
    # unchanged.
    _patch_slack_fetcher(monkeypatch, yields=[])
    second = runner.invoke(app, ["connector", "sync", "slack"])
    assert second.exit_code == 0, second.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # First sync: 1 SourceObserved + 1 ItemEnqueued + started/completed = 4
        # Second sync: started + completed = 2 → total 6.
        assert _row_count(engine, "events") == 6
        assert _row_count(engine, "sources") == 1
        assert _row_count(engine, "inbox_items") == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------- failure recording


def test_slack_sync_records_failure_event_on_fetcher_error(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """A :class:`ConnectorFailedError` → :class:`ConnectorSyncFailed` event + non-zero exit.

    The fetcher's ``invalid_auth`` / ``channel_not_found`` /
    rate-limit-budget-exhausted paths all funnel through
    :class:`ConnectorFailedError`. The CLI driver catches it,
    records a :class:`ConnectorSyncFailed` event with the sanitised
    exception type name (per :mod:`opshub.cli.connector` —
    ``type(exc).__name__`` rather than ``str(exc)`` so a Slack
    error message that echoed a token would be filtered out
    automatically), and exits 1.
    """
    _patch_slack_fetcher(
        monkeypatch,
        error=ConnectorFailedError("Slack fetch failed for channel C1: rate_limited"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "sync", "slack"])
    assert result.exit_code == 1
    # CLI exception path: only the type name reaches stderr (the
    # message is intentionally not echoed because it could carry
    # connector-supplied detail; the type alone is enough for an
    # operator to map back to the docs).
    assert "ConnectorFailedError" in result.stderr

    # The event log must carry a single ConnectorSyncFailed row with
    # the sanitised message (the type name).
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            failed_rows: list[dict[str, Any]] = [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT event_type, payload FROM events WHERE event_type "
                        "= 'connector.sync_failed'"
                    )
                ).mappings()
            ]
        assert len(failed_rows) == 1
        # Payload is JSON-serialised on disk; the sanitised
        # ``error_message`` field is the exception type name. We
        # check via substring so a future schema bump that adds
        # surrounding JSON keys doesn't break the test.
        payload = failed_rows[0]["payload"]
        assert "ConnectorFailedError" in payload
        assert "rate_limited" not in payload  # raw message is NOT persisted
    finally:
        engine.dispose()


# ---------------------------------------------------------------------- summary truncation


def test_slack_sync_truncates_long_message_text(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """ADR-0005 enforcement: a 500-char message lands as a 200-char ``summary``.

    The mapper truncation is the single point of enforcement; the
    projection stores ``summary`` as free-form TEXT. This test
    proves the truncation actually fires on the end-to-end path
    (not just in the unit tests).
    """
    long_text = "x" * 500
    _patch_slack_fetcher(
        monkeypatch,
        yields=[
            (
                "C1",
                _raw_message(ts="1700000001.000100", text=long_text),
                "1700000001.000100",
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "sync", "slack"])
    assert result.exit_code == 0, result.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        from sqlalchemy import select

        with engine.connect() as conn:
            (summary,) = conn.execute(select(sources_table.c.summary)).first() or ("",)
        assert summary is not None
        assert len(summary) == 200
        assert summary.endswith("…")
    finally:
        engine.dispose()
