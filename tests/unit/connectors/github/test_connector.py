"""Unit tests for :class:`opshub.connectors.github.connector.GitHubConnector`.

These tests pin the connector-level contract independently of the
larger end-to-end lifecycle (covered in
``tests/integration/test_github_connector_lifecycle.py``):

* ``name`` matches the registry key the CLI dispatches on.
* The cursor round-trip ``str -> datetime -> str`` is symmetric so the
  ``new_cursor`` we hand back to the CLI driver re-parses cleanly on
  the next sync.
* A sync that observes zero items preserves the previous cursor
  (regression guard against accidentally writing ``None`` on top of a
  real value).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from opshub.connectors.context import ConnectorContext
from opshub.connectors.github.connector import (
    GitHubConnector,
    _parse_cursor,  # pyright: ignore[reportPrivateUsage]
)


def test_connector_name_is_github() -> None:
    """The registry / CLI dispatch key must be exactly ``"github"``."""
    assert GitHubConnector.name == "github"
    assert GitHubConnector().name == "github"


def test_parse_cursor_round_trip() -> None:
    """``_parse_cursor`` reverses the ``...Z`` ISO 8601 form the connector emits.

    The connector serialises ``max(updated_at)`` via
    ``isoformat().replace("+00:00", "Z")``; the next sync must parse
    that string back into a tz-aware UTC datetime equal to the original.
    """
    dt = datetime(2026, 5, 17, 9, 30, 45, tzinfo=UTC)
    serialised = dt.isoformat().replace("+00:00", "Z")
    assert _parse_cursor(serialised) == dt


def test_parse_cursor_none_passes_through() -> None:
    """``None`` cursor → ``None`` datetime (first-sync semantics)."""
    assert _parse_cursor(None) is None


class _RecordingSourceService:
    """Test double for :class:`SourceService` that records ``observe`` calls.

    Mirrors the field signature used by the real service so a connector
    that drifts on argument names trips a TypeError immediately.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "connector_name": connector_name,
                "external_id": external_id,
                "source_type": source_type,
                "title": title,
                "url": url,
                "summary": summary,
            }
        )


@pytest.fixture
def github_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the documented env vars so :meth:`sync` reaches the fetch primitives."""
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_test")
    yield


def _empty_iter(*_args: object, **_kwargs: object) -> Iterator[Any]:
    """Typed stub that pretends to be a fetch primitive but yields nothing.

    Pyright in strict mode refuses untyped lambdas at ``monkeypatch.setattr``
    call sites; a freestanding helper with explicit annotations is cheaper
    than per-call ``# pyright: ignore`` comments.
    """
    return iter(())


def _context(
    *, cursor_value: str | None = None
) -> tuple[ConnectorContext, _RecordingSourceService]:
    service = _RecordingSourceService()
    ctx = ConnectorContext(
        source_service=service,
        cursor_value=cursor_value,
        secrets=None,
        logger=None,
    )
    return ctx, service


def test_empty_sync_preserves_cursor(
    monkeypatch: pytest.MonkeyPatch,
    github_env: None,
) -> None:
    """A sync that observes zero items must preserve the prior cursor.

    Regression guard: writing ``None`` (or any other sentinel) on top
    of a healthy cursor would silently re-fetch the whole history on
    the next sync. The contract is "no progress, no movement".
    """
    from opshub.connectors.github import api as github_api

    monkeypatch.setattr(github_api, "list_issues_since", _empty_iter)
    monkeypatch.setattr(github_api, "list_pulls_since", _empty_iter)
    monkeypatch.setattr(github_api, "list_notifications", _empty_iter)

    prior_cursor = "2026-05-15T10:20:30Z"
    ctx, service = _context(cursor_value=prior_cursor)
    result = GitHubConnector().sync(ctx)

    assert result.observed_count == 0
    assert result.new_cursor == prior_cursor
    assert service.calls == []


def test_empty_first_sync_keeps_none_cursor(
    monkeypatch: pytest.MonkeyPatch,
    github_env: None,
) -> None:
    """First-sync (cursor=None) with zero items stays at ``None`` — no fabricated value."""
    from opshub.connectors.github import api as github_api

    monkeypatch.setattr(github_api, "list_issues_since", _empty_iter)
    monkeypatch.setattr(github_api, "list_pulls_since", _empty_iter)
    monkeypatch.setattr(github_api, "list_notifications", _empty_iter)

    ctx, _ = _context(cursor_value=None)
    result = GitHubConnector().sync(ctx)

    assert result.observed_count == 0
    assert result.new_cursor is None
