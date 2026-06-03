"""End-to-end :class:`SlackConnector` lifecycle against a real SQLite database.

Drives the connector through the same ``isolated_env`` fixture used by
:mod:`tests.integration.test_github_connector_lifecycle`, with two
twists:

1. :class:`opshub.connectors.slack.fetcher.SlackFetcher` is
   monkeypatched to yield controlled :class:`RawSlackMessage`
   payloads so the suite never reaches Slack's API. The pagination
   regression test for issue #339 is the exception: it patches
   :class:`slack_sdk.WebClient` instead so the real fetcher's
   buffer-then-sort logic is exercised end-to-end.
2. The Slack OAuth access token is injected through the
   ``OPSHUB_CONNECTOR_SLACK_TOKEN`` env var override so the
   ``[secrets]`` keyring backend is never consulted (matches the
   Phase 3 GitHub precedent and keeps CI hermetic). Per ADR-0018
   the test uses a User Token (``xoxp-``) — the first-class
   principal — but the override accepts either prefix.

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

The CLI driver (``opshub slack sync``) lives in
:mod:`opshub.cli.slack` (with the shared sync driver in
:mod:`opshub.cli._connector_common`) and its own surface (Typer
command, cursor bracket, exception sanitisation) is exercised here
through :class:`~typer.testing.CliRunner` so the contract is
validated end-to-end.
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
    """Inject the Slack OAuth access token + channel list the connector requires.

    * The token override (``OPSHUB_CONNECTOR_SLACK_TOKEN``) keeps
      :class:`SlackAuth` away from the keyring so the test is
      hermetic on dev machines without ``[secrets]`` installed.
      Per ADR-0018 the value is a User Token (``xoxp-``); Bot Tokens
      (``xoxb-``) are accepted equivalently.
    * The channel list (``OPSHUB_CONNECTORS__SLACK__CHANNELS``) is
      a JSON-encoded list per :mod:`pydantic_settings` conventions for
      nested list overrides.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_TOKEN", "xoxp-test")
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

    Drives the full ``opshub slack sync`` path through
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
    result = runner.invoke(app, ["slack", "sync"])
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
        # Title shape matches the mapper contract — issue #367 added
        # the body excerpt suffix so search results are recognisable
        # without joining back to the ``body`` column. Two distinct
        # messages → two distinct titles.
        assert {row["title"] for row in source_rows} == {
            "alice in #general: first message",
            "alice in #general: second message",
        }
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


def test_slack_sync_handles_empty_text_message(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """Regression: a Slack message with ``text == ""`` must not abort the sync (issue #332).

    Slackbot notifications, ``channel_join`` subtypes, and ``files``-only
    messages arrive without a ``text`` field. Before the fix the
    mapper passed ``summary=""`` through to
    :class:`SourceService.observe`, whose ``is None`` fallback let the
    empty string reach :class:`ItemEnqueued`'s ``min_length=1``
    validator — :class:`pydantic.ValidationError` propagated up,
    crashed the connector run, and stranded the cursor before the
    offending message.

    Post-fix the mapper normalises empty text to ``None`` and
    ``SourceService.observe`` applies the
    ``f"{source_type}: {title}"`` fallback for the inbox preview, so
    the sync completes 0, the projection rows land, and the cursor
    advances past the empty-text message.
    """
    yields: list[tuple[str, RawSlackMessage, str | None]] = [
        (
            "C1",
            _raw_message(ts="1700000001.000100", text="first message"),
            "1700000001.000100",
        ),
        (
            "C1",
            # The realistic empty-text case (Slackbot / channel_join /
            # file_share). user_display_name + channel_name still
            # provide enough metadata for the fallback summary to be
            # identifiable, which is the contract this test pins.
            _raw_message(ts="1700000002.000200", text="", user_display_name="bob"),
            "1700000002.000200",
        ),
        (
            "C1",
            _raw_message(ts="1700000003.000300", text="third message"),
            "1700000003.000300",
        ),
    ]
    _patch_slack_fetcher(monkeypatch, yields=yields)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync"])
    # The empty-text message must not abort the run — exit 0, no
    # ``ValidationError`` in stderr.
    assert result.exit_code == 0, result.stdout
    assert "ValidationError" not in result.stderr

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # Three messages → 3 SourceObserved + 3 ItemEnqueued + sync
        # bracket (started + completed) = 8 events. Sources + inbox
        # projections gain one row per message.
        assert _row_count(engine, "events") == 8
        assert _row_count(engine, "sources") == 3
        assert _row_count(engine, "inbox_items") == 3

        from sqlalchemy import select
        from sqlalchemy import text as sql_text

        with engine.connect() as conn:
            source_rows = conn.execute(select(sources_table)).mappings().all()
            inbox_rows = conn.execute(select(inbox_items_table)).mappings().all()
            cursor_row = (
                conn.execute(
                    sql_text(
                        "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                    )
                )
                .mappings()
                .one()
            )

        # The empty-text source row stores NULL ``summary`` (mapper
        # normalises empty → None for symmetry with ``body``).
        empty_text_external_id = "C1:1700000002.000200"
        empty_text_source = next(
            row for row in source_rows if row["external_id"] == empty_text_external_id
        )
        assert empty_text_source["summary"] is None
        # Title now carries the ``"(no text)"`` placeholder (issue
        # #367) so an empty-body sync result is still recognisable
        # as a Slack post by ``bob`` in ``#general``.
        assert empty_text_source["title"] == "bob in #general: (no text)"

        # The inbox row for the empty-text message falls back to the
        # ``f"{source_type}: {title}"`` shape so the inbox stays
        # legible without a join back to ``sources``. The title now
        # carries the ``"(no text)"`` placeholder.
        empty_text_inbox = next(
            row for row in inbox_rows if row["source_ref"] == f"slack:{empty_text_external_id}"
        )
        assert empty_text_inbox["summary"] == "slack_message: bob in #general: (no text)"

        # Cursor advanced through every message, including the empty
        # one — no take-back / stranding.
        assert cursor_row["cursor_value"] is not None
        assert "1700000003.000300" in cursor_row["cursor_value"]
    finally:
        engine.dispose()


def test_slack_sync_handles_whitespace_only_message(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """Regression: whitespace-only ``text`` lands a fallback summary (issue #337).

    Audit followup to #332. The empty-text fix normalised ``text=""``
    to ``None`` but whitespace-only payloads (``"  "`` / ``"\\t\\n"``)
    slipped through ``_truncate`` and reached
    :class:`ItemEnqueued.summary` as visually-blank previews (2+
    chars, so they cleared ``min_length=1``). The sync would not
    crash — unlike the #332 case — but inbox / brief / propose
    surfaces showed blank rows that wasted operator attention.

    Post-fix the mapper strips ``text`` before truncation
    (``summary=None``) and :meth:`SourceService.observe` falls back to
    ``f"{source_type}: {title}"`` for the inbox preview. The cursor
    advances past the whitespace-only message just like the
    empty-text case so subsequent syncs do not re-process it.
    """
    yields: list[tuple[str, RawSlackMessage, str | None]] = [
        (
            "C1",
            _raw_message(ts="1700000001.000100", text="first message"),
            "1700000001.000100",
        ),
        (
            "C1",
            # Whitespace-only payload (HTML renderer artifact, padded
            # bot notification, etc). user_display_name + channel_name
            # still provide enough metadata for the fallback summary
            # to be identifiable.
            _raw_message(ts="1700000002.000200", text="  ", user_display_name="carol"),
            "1700000002.000200",
        ),
        (
            "C1",
            _raw_message(ts="1700000003.000300", text="third message"),
            "1700000003.000300",
        ),
    ]
    _patch_slack_fetcher(monkeypatch, yields=yields)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync"])
    # The whitespace-only message must not abort the run — exit 0
    # and no ``ValidationError`` in stderr.
    assert result.exit_code == 0, result.stdout
    assert "ValidationError" not in result.stderr

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # Three messages → 3 SourceObserved + 3 ItemEnqueued + sync
        # bracket (started + completed) = 8 events.
        assert _row_count(engine, "events") == 8
        assert _row_count(engine, "sources") == 3
        assert _row_count(engine, "inbox_items") == 3

        from sqlalchemy import select
        from sqlalchemy import text as sql_text

        with engine.connect() as conn:
            source_rows = conn.execute(select(sources_table)).mappings().all()
            inbox_rows = conn.execute(select(inbox_items_table)).mappings().all()
            cursor_row = (
                conn.execute(
                    sql_text(
                        "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                    )
                )
                .mappings()
                .one()
            )

        # Mapper flattens whitespace-only summaries to NULL; the body
        # retains verbatim whitespace per ADR-0020 §(d).
        ws_external_id = "C1:1700000002.000200"
        ws_source = next(row for row in source_rows if row["external_id"] == ws_external_id)
        assert ws_source["summary"] is None
        # Title's excerpt collapses to the empty-body placeholder
        # (issue #367) because ``_truncate_body`` strips whitespace
        # before the length check.
        assert ws_source["title"] == "carol in #general: (no text)"

        # Inbox preview falls back to the identifiable
        # ``f"{source_type}: {title}"`` shape rather than landing a
        # 2-char whitespace string in the projection.
        ws_inbox = next(row for row in inbox_rows if row["source_ref"] == f"slack:{ws_external_id}")
        assert ws_inbox["summary"] == "slack_message: carol in #general: (no text)"

        # Cursor advanced through every message, including the
        # whitespace one.
        assert cursor_row["cursor_value"] is not None
        assert "1700000003.000300" in cursor_row["cursor_value"]
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
    first = runner.invoke(app, ["slack", "sync"])
    assert first.exit_code == 0, first.stdout

    # Second sync: fetcher yields nothing. The connector still drives
    # the full bracket (started → completed), so the events table
    # gains 2 more rows but the source / inbox projections are
    # unchanged.
    _patch_slack_fetcher(monkeypatch, yields=[])
    second = runner.invoke(app, ["slack", "sync"])
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


# ---------------------------------------------------------------------- chronological pagination


def test_slack_sync_advances_to_latest_across_pagination(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """End-to-end: a paginated Slack history yields ts-ascending and the cursor
    persists the **maximum** ``ts`` across all pages (issue #339 regression guard).

    Drives the real :class:`SlackFetcher` against a mocked
    :class:`slack_sdk.WebClient` so the buffer-then-sort logic and
    its interaction with the connector's ``_max_ts`` guard are both
    exercised end-to-end. Pre-#339 the persisted
    ``connector_cursors.cursor_value`` JSON pointed at the **oldest
    ts of the oldest page** — strictly behind messages that had
    already been committed — and the next sync re-fetched and
    re-enqueued the gap, inflating ``inbox_items`` indefinitely.

    Post-#339 the cursor JSON contains the **latest ts** observed
    across **every** page, so a follow-up sync with ``oldest=cursor``
    correctly receives zero messages and ``inbox_items`` does not
    grow on re-runs.
    """
    from unittest.mock import MagicMock

    # Two-page history. Page 1 is the newest chunk and ``next_cursor``
    # walks to the older page — matches the documented Slack API
    # contract (and the pattern that pre-#339 broke).
    def _msg(ts: str, text: str) -> dict[str, Any]:
        return {"ts": ts, "text": text, "user": "U1"}

    page1 = {
        "ok": True,
        "messages": [
            _msg("1700000005.000500", "msg-5"),
            _msg("1700000004.000400", "msg-4"),
            _msg("1700000003.000300", "msg-3"),
        ],
        "has_more": True,
        "response_metadata": {"next_cursor": "older"},
    }
    page2 = {
        "ok": True,
        "messages": [
            _msg("1700000002.000200", "msg-2"),
            _msg("1700000001.000100", "msg-1"),
        ],
        "has_more": False,
        "response_metadata": {"next_cursor": ""},
    }

    web_client = MagicMock()
    web_client.conversations_history.side_effect = [page1, page2]
    web_client.conversations_info.return_value = {
        "ok": True,
        "channel": {"id": "C1", "name": "general"},
    }
    web_client.users_info.return_value = {
        "ok": True,
        "user": {
            "id": "U1",
            "name": "alice",
            "profile": {"display_name": "alice", "real_name": "Alice"},
        },
    }

    def _permalink_fn(*, channel: str, message_ts: str) -> dict[str, Any]:
        slug = message_ts.replace(".", "")
        return {
            "ok": True,
            "permalink": f"https://acme.slack.com/archives/{channel}/p{slug}",
        }

    web_client.chat_getPermalink.side_effect = _permalink_fn

    # ``WebClient`` is imported lazily inside ``fetch_messages`` from
    # ``slack_sdk`` itself — patch the symbol on the SDK module so the
    # ``from slack_sdk import WebClient`` lookup at the lazy-import
    # site resolves to our factory.
    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", MagicMock(return_value=web_client))

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync"])
    assert result.exit_code == 0, result.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        from sqlalchemy import text as sql_text

        # All five messages landed in projections (no take-backs).
        assert _row_count(engine, "sources") == 5
        assert _row_count(engine, "inbox_items") == 5

        with engine.connect() as conn:
            cursor_row = (
                conn.execute(
                    sql_text(
                        "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                    )
                )
                .mappings()
                .one()
            )
        # The persisted cursor advanced to the **latest** ts across
        # both pages — pre-#339 this would have been
        # ``1700000001.000100`` (oldest ts of oldest page) instead.
        assert cursor_row["cursor_value"] is not None
        assert "1700000005.000500" in cursor_row["cursor_value"]
        assert "1700000001.000100" not in cursor_row["cursor_value"]
    finally:
        engine.dispose()


def test_slack_sync_first_then_resume_no_duplicate_ingest(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """First sync drains a paginated history; second sync with the same
    stubbed Slack history ingests zero rows (cursor-driven resume).

    Audit followup for #345 (PR 1 of #339). The integration suite
    already pins:

    * :func:`test_slack_sync_advances_to_latest_across_pagination`
      — first sync persists the **maximum** ts across pages.
    * :func:`test_slack_sync_is_idempotent_when_no_new_messages`
      — second sync with an *empty* fetcher does not re-ingest.

    Neither test exercises the load-bearing case the #345 fix
    actually targets: a second sync where the **stubbed Slack
    workspace still holds the same history** must skip every
    message via the ``oldest=cursor`` + ``inclusive=False`` resume
    path. Pre-#345 the persisted cursor pointed at the oldest ts of
    the oldest page, so the second sync's ``oldest=cursor`` request
    re-fetched the entire gap and re-emitted ``SourceObserved`` /
    ``ItemEnqueued`` events — the projection-inflation cascade
    described in issue #339.

    Post-fix the persisted cursor is the maximum ts, so the second
    sync's ``oldest=cursor`` filter (Slack-side) returns zero
    messages and the projection rows stay at their post-first-sync
    counts. We exercise the real :class:`SlackFetcher` against a
    mocked :class:`slack_sdk.WebClient` whose
    ``conversations_history`` honours the ``oldest`` kwarg the
    fetcher passes — that is what closes the end-to-end loop pre/post.

    Composition rationale: this test does not replace the two prior
    tests. They pin orthogonal contracts (first-sync chronological
    ordering; idempotency with a literally-empty fetcher). The
    first→resume composition is the property that pre-#345 was
    *silently broken* even though the two prior tests would have
    passed — exactly the gap the post-merge audit flagged.
    """
    from unittest.mock import MagicMock

    # Three pages totalling five messages, matching the API contract
    # (page 1 = newest chunk, ``next_cursor`` walks toward older
    # history). Persistent across both sync calls so the fixture
    # models a stable Slack workspace.
    def _msg(ts: str, text: str) -> dict[str, Any]:
        return {"ts": ts, "text": text, "user": "U1"}

    page1_full = {
        "ok": True,
        "messages": [
            _msg("1700000005.000500", "msg-5"),
            _msg("1700000004.000400", "msg-4"),
        ],
        "has_more": True,
        "response_metadata": {"next_cursor": "page2"},
    }
    page2_full = {
        "ok": True,
        "messages": [
            _msg("1700000003.000300", "msg-3"),
            _msg("1700000002.000200", "msg-2"),
        ],
        "has_more": True,
        "response_metadata": {"next_cursor": "page3"},
    }
    page3_full = {
        "ok": True,
        "messages": [_msg("1700000001.000100", "msg-1")],
        "has_more": False,
        "response_metadata": {"next_cursor": ""},
    }
    # Post-resume Slack response shape: ``oldest=1700000005.000500`` +
    # ``inclusive=False`` (the fetcher pins this kwarg shape in
    # :func:`test_fetch_messages_skips_already_fetched_when_oldest_set`)
    # means Slack server-side filters to messages **strictly newer**
    # than the cursor — zero messages, no pagination needed.
    empty_response: dict[str, Any] = {
        "ok": True,
        "messages": [],
        "has_more": False,
        "response_metadata": {"next_cursor": ""},
    }

    web_client = MagicMock()

    # ``conversations_history`` side-effect routes on whether
    # ``oldest`` is set: no cursor → drain the three pages in
    # newest→oldest API order; cursor set → return empty (Slack-side
    # filter). We track the call order via ``call_args_list`` rather
    # than a stateful generator so a regression that drops the
    # ``oldest`` kwarg trips on the assertion below.
    def _history_side_effect(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("oldest") is None:
            # First sync: serve the paginated history. ``cursor``
            # kwarg routes between pages.
            page_cursor = kwargs.get("cursor")
            if page_cursor is None:
                return page1_full
            if page_cursor == "page2":
                return page2_full
            if page_cursor == "page3":
                return page3_full
            raise AssertionError(f"unexpected page cursor: {page_cursor!r}")
        # Resume sync: ``oldest`` set → Slack server filters everything
        # out. The fetcher must not page further (``has_more=False``).
        return empty_response

    web_client.conversations_history.side_effect = _history_side_effect
    web_client.conversations_info.return_value = {
        "ok": True,
        "channel": {"id": "C1", "name": "general"},
    }
    web_client.users_info.return_value = {
        "ok": True,
        "user": {
            "id": "U1",
            "name": "alice",
            "profile": {"display_name": "alice", "real_name": "Alice"},
        },
    }

    def _permalink_fn(*, channel: str, message_ts: str) -> dict[str, Any]:
        slug = message_ts.replace(".", "")
        return {
            "ok": True,
            "permalink": f"https://acme.slack.com/archives/{channel}/p{slug}",
        }

    web_client.chat_getPermalink.side_effect = _permalink_fn

    # ``WebClient`` lazy-imported inside ``fetch_messages``; patching
    # on the SDK module itself catches the import at call time.
    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", MagicMock(return_value=web_client))

    runner = CliRunner()

    # ---- First sync: drains the three pages, cursor advances to max ts.
    first = runner.invoke(app, ["slack", "sync"])
    assert first.exit_code == 0, first.stdout

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        from sqlalchemy import text as sql_text

        assert _row_count(engine, "sources") == 5
        assert _row_count(engine, "inbox_items") == 5

        with engine.connect() as conn:
            cursor_after_first = (
                conn.execute(
                    sql_text(
                        "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                    )
                )
                .mappings()
                .one()["cursor_value"]
            )
        assert cursor_after_first is not None
        assert "1700000005.000500" in cursor_after_first

        # The first sync consumed three pages, so
        # ``conversations_history`` was called three times — all
        # without ``oldest`` (first-sync path).
        first_calls = list(web_client.conversations_history.call_args_list)
        assert len(first_calls) == 3
        assert all(c.kwargs.get("oldest") is None for c in first_calls)

        # ---- Second sync: same stubbed Slack history; cursor-driven resume.
        second = runner.invoke(app, ["slack", "sync"])
        assert second.exit_code == 0, second.stdout

        # Projection counts unchanged — pre-#345 these would have
        # grown to 10 each (re-fetched gap re-emitted).
        assert _row_count(engine, "sources") == 5
        assert _row_count(engine, "inbox_items") == 5

        # Cursor unchanged (no new messages to advance past).
        with engine.connect() as conn:
            cursor_after_second = (
                conn.execute(
                    sql_text(
                        "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                    )
                )
                .mappings()
                .one()["cursor_value"]
            )
        assert cursor_after_second == cursor_after_first

        # Exactly one additional ``conversations_history`` call (the
        # resume sync's empty page), and that call carried the
        # ``oldest=max_ts`` + ``inclusive=False`` kwargs the fetcher
        # pins. Without ``oldest`` the stub would re-serve page 1 and
        # the projection counts would diverge.
        all_calls = list(web_client.conversations_history.call_args_list)
        assert len(all_calls) == 4
        resume_call = all_calls[3]
        assert resume_call.kwargs.get("oldest") == "1700000005.000500"
        assert resume_call.kwargs.get("inclusive") is False
    finally:
        engine.dispose()


# ------------------------------------------- mid-iteration failure recovery (issue #339 Bug 2)


def test_slack_sync_resumes_without_duplicates_after_mid_iteration_failure(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """Issue #339 Bug 2: a sync that crashes mid-iteration must not re-ingest
    on retry.

    Pre-fix the connector's success-path return was the only place
    that wrote the new cursor, so a fetcher exception left the
    projection's ``cursor_value`` stuck at the prior run's value
    while ``observe`` had already committed the N successful
    messages. The next sync (whether automatic or manual) then
    re-fetched and re-enqueued the gap, inflating ``inbox_items``
    by N per aborted-then-retried run — the second half of the
    cascade described in issue #339.

    Post-fix the ``try / finally`` arm in
    :meth:`SlackConnector.sync` writes the partial-progress cursor
    via ``cursor_set(sync_started=True)`` before the exception
    propagates. The
    :class:`~opshub.projections.connector_cursors.ConnectorCursorsProjection`
    reducer upserts ``cursor_value`` on every
    ``ConnectorSyncStarted`` event, so the next sync's
    ``oldest=cursor`` Slack request returns only messages strictly
    newer than the last-committed one — zero duplicates.

    We exercise this end-to-end via the public CLI (``opshub
    connector sync slack``) so the test would catch any future
    refactor that moved the cursor write into a place the CLI
    driver does not invoke on the failure path (e.g. a refactor
    that pushed the checkpoint into the CLI's ``except`` arm
    instead of the connector's ``finally`` arm).
    """
    # First sync: yield two messages, then raise on the third. We use a
    # stateful counter rather than ``side_effect=[...]`` because the
    # connector calls ``fetch_messages`` once and consumes the iterator
    # — the iterator itself is what raises after the second yield.
    msg_1 = _raw_message(ts="1700000001.000100", text="first")
    msg_2 = _raw_message(ts="1700000002.000200", text="second")

    def _first_sync_fetch(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        del cursor_per_channel, max_per_channel
        yield ("C1", msg_1, "1700000001.000100")
        yield ("C1", msg_2, "1700000002.000200")
        raise ConnectorFailedError("Slack fetch failed for channel C1: rate_limited")

    from unittest.mock import MagicMock

    first_fetcher_cls = MagicMock()
    first_fetcher_cls.return_value.fetch_messages.side_effect = _first_sync_fetch
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        first_fetcher_cls,
    )

    runner = CliRunner()
    first = runner.invoke(app, ["slack", "sync"])
    # Exit 1 because the fetcher raised; the CLI maps it to
    # ConnectorSyncFailed + exit code 1.
    assert first.exit_code == 1, first.stdout
    assert "ConnectorFailedError" in first.stderr

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        from sqlalchemy import text as sql_text

        # Two messages reached observe before the crash; both
        # ``sources`` and ``inbox_items`` rows landed.
        assert _row_count(engine, "sources") == 2
        assert _row_count(engine, "inbox_items") == 2

        # The cursor was advanced by the partial-progress checkpoint:
        # the projection now reflects the latest observed ts even
        # though no ``ConnectorSyncCompleted`` event was emitted (the
        # sync failed). Pre-fix this would have been NULL (or the
        # prior-run value), and the next sync would re-fetch from
        # the beginning.
        with engine.connect() as conn:
            cursor_after_failure = (
                conn.execute(
                    sql_text(
                        "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                    )
                )
                .mappings()
                .one()["cursor_value"]
            )
        assert cursor_after_failure is not None
        assert "1700000002.000200" in cursor_after_failure

        # ---- Second sync: resume with no new messages. The fetcher
        # patch now yields nothing (Slack-side filter on
        # ``oldest=cursor`` would return zero messages — we mirror that
        # by yielding an empty iterator).
        _patch_slack_fetcher(monkeypatch, yields=[])

        second = runner.invoke(app, ["slack", "sync"])
        assert second.exit_code == 0, second.stdout

        # Critical invariant: projection counts unchanged. Pre-fix
        # these would have grown to 4 each (the two messages
        # re-fetched and re-enqueued).
        assert _row_count(engine, "sources") == 2
        assert _row_count(engine, "inbox_items") == 2

        # The cursor is now sealed by the success run's
        # ConnectorSyncCompleted event with the same value the
        # checkpoint wrote.
        with engine.connect() as conn:
            cursor_after_resume = (
                conn.execute(
                    sql_text(
                        "SELECT cursor_value FROM connector_cursors WHERE connector_name = 'slack'"
                    )
                )
                .mappings()
                .one()["cursor_value"]
            )
        assert "1700000002.000200" in cursor_after_resume
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
    exception type name (per :mod:`opshub.cli._connector_common` —
    ``type(exc).__name__`` rather than ``str(exc)`` so a Slack
    error message that echoed a token would be filtered out
    automatically), and exits 1.
    """
    _patch_slack_fetcher(
        monkeypatch,
        error=ConnectorFailedError("Slack fetch failed for channel C1: rate_limited"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync"])
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


# ---------------------------------------------------------------------- github extra isolation


def test_slack_sync_works_without_github_extra(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    slack_env: None,
) -> None:
    """Regression (#198): ``sync slack`` must not require the ``connectors-github`` extra.

    The CLI driver imports *every* built-in connector to populate the
    registry. The github package used to pull ``httpx`` (a
    ``connectors-github`` extra) at *import* time — via
    ``github/connector.py`` importing ``api`` at module level — so an
    operator who installed only ``connectors-slack`` hit
    ``ModuleNotFoundError: No module named 'httpx'`` on ``opshub
    connector sync slack``.

    We reproduce that environment hermetically: block ``httpx`` in
    ``sys.modules`` and evict the already-imported github modules so the
    CLI re-imports them fresh under the block. With the deferred-import
    fix the github package is import-clean, so it still registers (the
    assertion below proves we exercise the real fix, not just the CLI's
    defensive ``ImportError`` swallow), and Slack sync completes 0.

    Test-isolation note (opshub#348): ``monkeypatch.delitem`` /
    ``setitem`` only rolls back ``sys.modules`` entries on teardown; it
    does NOT undo the parent-package ``.auth`` / ``.connector`` etc.
    *attribute* rebinding that happens when the CLI re-imports the
    github subpackage under the ``httpx`` block. After teardown the
    restored ``sys.modules['opshub.connectors.github.auth']`` and the
    parent package's ``opshub.connectors.github.auth`` attribute can
    point at **different** module objects, and a later test that does
    ``import opshub.connectors.github.auth as github_auth`` +
    ``monkeypatch.setattr(github_auth, "test_token", ...)`` patches the
    parent-attr module while the ``auth test`` verifier dispatch (today: ``opshub.cli._auth_common.run_auth_test``; pre-Phase-17-B: ``opshub.cli.connector._resolve_auth_test_verifier``)
    looks up ``test_token`` via the ``sys.modules`` entry — the patch
    does not take effect and ``opshub github auth test`` calls
    the real ``test_token``. We pin a deterministic post-condition here:
    evict the github subpackage from ``sys.modules`` again on teardown
    so the next consumer triggers a fresh import that re-binds both
    surfaces consistently.
    """
    import sys

    # Simulate the ``connectors-github`` extra not being installed.
    monkeypatch.setitem(sys.modules, "httpx", None)
    # Evict cached github modules so the CLI's ``import
    # opshub.connectors.github`` re-executes (and would re-pull httpx if
    # the import were not deferred).
    #
    # We do NOT use ``monkeypatch.delitem`` here: its restore semantics
    # re-insert the *original* module objects on teardown, but the CLI's
    # ``import opshub.connectors.github`` (run under the ``httpx`` block
    # above) rebinds the parent package's ``.<submodule>`` attributes to
    # *new* module objects. After teardown ``sys.modules['…github.auth']``
    # would point at the restored original while
    # ``opshub.connectors.github.auth`` (the parent attribute) points at
    # the new one. Later tests that ``import opshub.connectors.github.auth
    # as github_auth`` resolve via the parent attribute and patch the new
    # module, while the ``auth test`` verifier dispatch (today: ``opshub.cli._auth_common.run_auth_test``; pre-Phase-17-B: ``opshub.cli.connector._resolve_auth_test_verifier``)
    # does ``from opshub.connectors.github.auth import test_token`` which
    # resolves via the ``sys.modules`` entry — the patch silently misses
    # and the call hits the real ``test_token`` → 1 / keyring error
    # (opshub#348). Manage the eviction by hand instead and evict again
    # on teardown so the next consumer triggers a fresh, consistent
    # import (both ``sys.modules`` and the parent attribute resolve to
    # the same module object).
    _evicted: dict[str, object] = {}
    for mod_name in list(sys.modules):
        if mod_name == "opshub.connectors.github" or mod_name.startswith(
            "opshub.connectors.github."
        ):
            _evicted[mod_name] = sys.modules.pop(mod_name)

    def _restore_github_consistently() -> None:
        """Drop every github module (and registry slot) so the next
        import re-binds ``sys.modules`` and the parent attributes
        atomically. See opshub#348 for the divergence we are guarding."""
        from opshub.connectors._registry import (
            _REGISTRY,  # pyright: ignore[reportPrivateUsage]
        )

        for mod_name in list(sys.modules):
            if mod_name == "opshub.connectors.github" or mod_name.startswith(
                "opshub.connectors.github."
            ):
                sys.modules.pop(mod_name, None)
        _REGISTRY.pop("github", None)

    _patch_slack_fetcher(
        monkeypatch,
        yields=[
            (
                "C1",
                _raw_message(ts="1700000001.000100", text="hello"),
                "1700000001.000100",
            ),
        ],
    )

    runner = CliRunner()
    try:
        result = runner.invoke(app, ["slack", "sync"])
        assert result.exit_code == 0, result.stdout

        # The github package is import-clean, so it registers even with httpx
        # absent — proving the deferred-import fix, not merely the CLI's
        # defensive ImportError guard, is what makes Slack sync work.
        from opshub.connectors import discover_connectors

        assert "github" in {c.name for c in discover_connectors()}
    finally:
        _restore_github_consistently()
        # The original modules are intentionally discarded: any reference
        # to them held by other test modules will still resolve to the
        # same objects (Python imports are lazy / cached at module load),
        # but ``sys.modules`` and the parent attributes will be re-bound
        # together on the next import. ``_evicted`` is kept alive only
        # to defer GC of the originals until after this teardown so any
        # in-flight finalisers from the CLI invoke run cleanly.
        _evicted.clear()


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
    result = runner.invoke(app, ["slack", "sync"])
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
