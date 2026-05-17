"""End-to-end :class:`GitHubConnector` lifecycle against a real SQLite database.

Drives the connector through the same ``isolated_env`` fixture used by
:mod:`tests.integration.test_coordination_lifecycle`, with one twist:
the GitHub fetch primitives (:mod:`opshub.connectors.github.api`) are
monkeypatched so the suite never reaches the network. Each test
returns a controlled list of :class:`GitHubItem` payloads from
``list_issues_since`` / ``list_pulls_since`` / ``list_notifications``;
the connector then runs through the real
:class:`~opshub.services.source_service.SourceService`,
``SqlAlchemyEventStore`` and projection reducers, so we exercise the
contract end-to-end.

Why integration-level (not pure unit):

* The :meth:`SourceService.observe` contract is "one SourceObserved +
  one ItemEnqueued per item, atomically". Pinning the observed effect
  on disk (sources + inbox_items + events rows) is the only honest way
  to prove the contract still holds when we add the connector in front
  of the service.
* The first-sync / second-sync interplay (re-observation produces new
  events but the sources projection upserts to a single row) is a
  documented Phase 3 behaviour; a unit test against a stubbed service
  cannot prove it.

The CLI driver (``opshub connector sync github``) lives in
``cli/connector.py`` and has its own surface (cursor bracket +
exception sanitisation). That path is exercised by the placeholder
tests shipped with PR #48 / PR #51; this module focuses purely on the
:class:`Connector.sync` contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from opshub.connectors.context import ConnectorContext
from opshub.connectors.github.api import GitHubItem
from opshub.connectors.github.connector import GitHubConnector
from opshub.core.errors import ConfigError
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_PathsDict = dict[str, Path]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _row_count(engine: Engine, table_name: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())


def _issue(number: int, *, updated_at: datetime) -> GitHubItem:
    return GitHubItem(
        source_type="issue",
        external_id=f"owner/repo#{number}",
        title=f"issue #{number}",
        url=f"https://github.com/owner/repo/issues/{number}",
        summary=f"summary of issue {number}",
        updated_at=updated_at,
    )


def _pull(number: int, *, updated_at: datetime) -> GitHubItem:
    return GitHubItem(
        source_type="pull_request",
        external_id=f"owner/repo#{number}",
        title=f"pr #{number}",
        url=f"https://github.com/owner/repo/pull/{number}",
        summary=f"summary of pr {number}",
        updated_at=updated_at,
    )


def _notification(notification_id: str, *, updated_at: datetime) -> GitHubItem:
    return GitHubItem(
        source_type="notification",
        external_id=notification_id,
        title=f"notification {notification_id}",
        url=f"https://api.github.com/notifications/{notification_id}",
        summary="mention",
        updated_at=updated_at,
    )


def _patch_github_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    issues: list[GitHubItem],
    pulls: list[GitHubItem],
    notifications: list[GitHubItem],
) -> None:
    """Replace the three :mod:`opshub.connectors.github.api` list_* helpers.

    The connector imports the module bound name (``github_api``) rather
    than the individual symbols, so monkeypatching the attributes on the
    module object is the cheapest possible test seam — no fake httpx
    transport is needed for the integration test, because the fetch
    primitives themselves are covered separately in
    :mod:`tests.unit.connectors.github.test_api`.
    """
    from opshub.connectors.github import api as github_api

    def _fake_issues(*_args: object, **_kwargs: object) -> Iterator[GitHubItem]:
        return iter(issues)

    def _fake_pulls(*_args: object, **_kwargs: object) -> Iterator[GitHubItem]:
        return iter(pulls)

    def _fake_notifications(*_args: object, **_kwargs: object) -> Iterator[GitHubItem]:
        return iter(notifications)

    monkeypatch.setattr(github_api, "list_issues_since", _fake_issues)
    monkeypatch.setattr(github_api, "list_pulls_since", _fake_pulls)
    monkeypatch.setattr(github_api, "list_notifications", _fake_notifications)


@pytest.fixture
def github_creds(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject the repo + PAT env vars the connector requires."""
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_test")
    yield


# ----------------------------------------------------------------------
# Happy path: first sync observes every item, advances cursor
# ----------------------------------------------------------------------


def test_first_sync_observes_all_items_and_advances_cursor(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    github_creds: None,
) -> None:
    """End-to-end first sync: 4 items observed, projections populated, cursor advances."""
    # Lazy import: build_source_service pulls SQLAlchemy / config, which
    # the test module deliberately doesn't import at top level.
    from opshub.cli._wiring import build_source_service

    db_path = isolated_env["db_path"]
    times = [
        datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
    ]
    issues = [_issue(1, updated_at=times[0]), _issue(2, updated_at=times[1])]
    pulls = [_pull(10, updated_at=times[2])]
    notifications = [_notification("n1", updated_at=times[3])]
    _patch_github_api(monkeypatch, issues=issues, pulls=pulls, notifications=notifications)

    service = build_source_service(actor="connector:github")
    context = ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=None,
    )

    result = GitHubConnector().sync(context)

    # ---- SyncResult ------------------------------------------------------
    assert result.observed_count == 4
    # ``new_cursor`` must be ``max(updated_at)`` serialised back to
    # ``...Z`` form so the next sync can parse it via ``_parse_cursor``.
    assert result.new_cursor == "2026-05-16T12:00:00Z"

    # ---- On-disk state ---------------------------------------------------
    engine = create_engine_for_sqlite(db_path)
    try:
        # SourceObserved + ItemEnqueued per item → 8 events total.
        assert _row_count(engine, "events") == 8
        # Sources projection upserts on (connector_name, external_id), so
        # the 4 distinct items land as 4 rows.
        assert _row_count(engine, "sources") == 4
        # Inbox projection adds 4 pending rows (one per observation).
        assert _row_count(engine, "inbox_items") == 4

        # Spot-check the row shapes: every source row carries the
        # connector name + an external_id pointing at one of our seeds.
        from sqlalchemy import select

        with engine.connect() as conn:
            source_rows = conn.execute(select(sources_table)).mappings().all()
            inbox_rows = conn.execute(select(inbox_items_table)).mappings().all()
        assert {row["connector_name"] for row in source_rows} == {"github"}
        assert {row["external_id"] for row in source_rows} == {
            "owner/repo#1",
            "owner/repo#2",
            "owner/repo#10",
            "n1",
        }
        # Every inbox row links back through ``source_ref``.
        assert all(row["state"] == "pending" for row in inbox_rows)
        assert {row["source_ref"] for row in inbox_rows} == {
            "github:owner/repo#1",
            "github:owner/repo#2",
            "github:owner/repo#10",
            "github:n1",
        }
    finally:
        engine.dispose()


# ----------------------------------------------------------------------
# Second sync: re-observation re-emits events, sources dedupe, inbox grows
# ----------------------------------------------------------------------


def test_second_sync_with_same_data_re_emits_observations_but_sources_dedupe(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
    github_creds: None,
) -> None:
    """Re-observation = fresh events (ADR-0002 immutability) + projection dedup.

    The second sync feeds the same payloads back through the connector.
    Per ADR-0002 every observation is a *new* event in the log; the
    ``sources`` projection collapses them on the
    ``(connector_name, external_id)`` UNIQUE constraint so the row count
    stays put. Each observation also emits an :class:`ItemEnqueued`,
    and the inbox projection has no dedup yet (deferred to Phase 3.x /
    Phase 4) — so inbox row count grows linearly with sync runs.
    """
    from opshub.cli._wiring import build_source_service

    db_path = isolated_env["db_path"]
    t = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    issues = [_issue(1, updated_at=t)]
    pulls = [_pull(10, updated_at=t)]
    notifications = [_notification("n1", updated_at=t)]
    _patch_github_api(monkeypatch, issues=issues, pulls=pulls, notifications=notifications)

    service = build_source_service(actor="connector:github")

    # ---- first sync ------------------------------------------------------
    first_result = GitHubConnector().sync(
        ConnectorContext(source_service=service, cursor_value=None, secrets=None, logger=None)
    )
    assert first_result.observed_count == 3
    assert first_result.new_cursor == "2026-05-15T12:00:00Z"

    # ---- second sync (resume from the cursor we just produced) ----------
    second_result = GitHubConnector().sync(
        ConnectorContext(
            source_service=service,
            cursor_value=first_result.new_cursor,
            secrets=None,
            logger=None,
        )
    )
    assert second_result.observed_count == 3
    assert second_result.new_cursor == "2026-05-15T12:00:00Z"

    # ---- On-disk state ---------------------------------------------------
    engine = create_engine_for_sqlite(db_path)
    try:
        # Each item -> 2 events (SourceObserved + ItemEnqueued); 3 items
        # times 2 syncs times 2 events = 12 rows in ``events``.
        assert _row_count(engine, "events") == 12
        # Sources upserts on (connector_name, external_id) → still 3.
        assert _row_count(engine, "sources") == 3
        # Inbox has no dedup yet: every observation enqueues a fresh
        # row. This is the documented Phase 3 behaviour — inbox-side
        # dedup is intentionally deferred (see module docstring).
        assert _row_count(engine, "inbox_items") == 6
    finally:
        engine.dispose()


# ----------------------------------------------------------------------
# Fail-fast: missing config / missing token raise ConfigError
# ----------------------------------------------------------------------


def test_sync_with_no_repo_env_raises_config_error(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``OPSHUB_CONNECTOR_GITHUB_REPO`` surfaces an actionable error."""
    from opshub.cli._wiring import build_source_service

    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_test")
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_REPO", raising=False)

    service = build_source_service(actor="connector:github")
    context = ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=None,
    )

    with pytest.raises(ConfigError) as excinfo:
        GitHubConnector().sync(context)
    assert "OPSHUB_CONNECTOR_GITHUB_REPO" in str(excinfo.value)


def test_sync_with_no_token_raises_config_error(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing PAT (env + keyring) surfaces the :func:`get_github_token` ``ConfigError``."""
    from opshub.cli._wiring import build_source_service

    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_REPO", "owner/repo")
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)

    # ``get_secret`` falls back to the keyring backend; force a
    # ``None`` return regardless of what the host keychain contains so
    # the test stays hermetic on developer machines that already have
    # a PAT stored.
    def _no_secret(_key: str) -> str | None:
        return None

    monkeypatch.setattr("opshub.connectors.github.auth.get_secret", _no_secret)

    service = build_source_service(actor="connector:github")
    context = ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=None,
    )

    with pytest.raises(ConfigError) as excinfo:
        GitHubConnector().sync(context)
    # The error originates in ``get_github_token`` (PR #51); message
    # points the user at both configuration surfaces.
    message = str(excinfo.value)
    assert "OPSHUB_CONNECTOR_GITHUB_PAT" in message
    assert "opshub connector auth set github" in message
