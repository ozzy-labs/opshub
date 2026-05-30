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
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
    that drifts on argument names trips a TypeError immediately. Phase 10
    (ADR-0020) added ``body`` / ``provenance_origin`` / ``provenance_trust``
    keywords — the double accepts (and records) all three so the
    connector contract is pinned end-to-end here.
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
        body: str | None = None,
        provenance_origin: str | None = None,
        provenance_trust: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "connector_name": connector_name,
                "external_id": external_id,
                "source_type": source_type,
                "title": title,
                "url": url,
                "summary": summary,
                "body": body,
                "provenance_origin": provenance_origin,
                "provenance_trust": provenance_trust,
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
        # ``warning()`` needs to be callable on the excludes-repo skip
        # path (Phase 10 audit Cluster 3). The original tests passed
        # ``None`` because no logger surface was exercised; we use a
        # MagicMock now so both the legacy paths and the new exclude
        # path satisfy the connector's attribute access.
        logger=MagicMock(),
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


def test_sync_skips_when_repo_in_excludes(
    monkeypatch: pytest.MonkeyPatch,
    github_env: None,
    tmp_path: Path,
) -> None:
    """ADR-0020 §(b): an ``owner/repo`` in ``excludes.yaml`` triggers no observe.

    The connector must short-circuit before fetching — listing primitives
    are not even consulted. The prior cursor stays untouched (no progress,
    no movement) so flipping the exclude back off resumes exactly where
    the last real sync left off.
    """
    from unittest.mock import MagicMock

    from opshub.connectors.github import api as github_api

    # Write an excludes.yaml under a tmp config dir and point
    # ``default_config_dir`` at it so ``load_excludes()`` resolves there.
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(
        "repos:\n  - owner/repo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "opshub.core.excludes.default_config_dir",
        lambda: cfg_dir,
    )

    # Listing primitives must not run; trip a clear AssertionError if they do.
    forbidden = MagicMock(side_effect=AssertionError("listing called for excluded repo"))
    monkeypatch.setattr(github_api, "list_issues_since", forbidden)
    monkeypatch.setattr(github_api, "list_pulls_since", forbidden)
    monkeypatch.setattr(github_api, "list_notifications", forbidden)

    prior_cursor = "2026-05-15T10:20:30Z"
    ctx, service = _context(cursor_value=prior_cursor)
    result = GitHubConnector().sync(ctx)

    assert result.observed_count == 0
    assert result.new_cursor == prior_cursor
    assert service.calls == []


def test_sync_forwards_body_and_provenance_to_source_service(
    monkeypatch: pytest.MonkeyPatch,
    github_env: None,
) -> None:
    """ADR-0020: ``GitHubItem.body`` reaches ``observe(body=..., provenance_*=...)``.

    The connector must thread the full body (untruncated) plus
    ``provenance_origin="external"`` + ``provenance_trust="untrusted"``
    so the agent / LLM context treats SaaS-origin bodies as reference
    material, never instructions (indirect prompt-injection mitigation
    per ADR-0020 §(e)).
    """
    from opshub.connectors.github import api as github_api
    from opshub.connectors.github.api import GitHubItem

    long_body = "Full issue body — paragraph " * 20
    fake_issue = GitHubItem(
        source_type="issue",
        external_id="owner/repo#1",
        title="issue #1",
        url="https://github.com/owner/repo/issues/1",
        summary="Full issue body — paragraph",
        updated_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
        body=long_body,
    )

    def _yield_one_issue(*_args: object, **_kwargs: object) -> Iterator[GitHubItem]:
        yield fake_issue

    monkeypatch.setattr(github_api, "list_issues_since", _yield_one_issue)
    monkeypatch.setattr(github_api, "list_pulls_since", _empty_iter)
    monkeypatch.setattr(github_api, "list_notifications", _empty_iter)

    ctx, service = _context(cursor_value=None)
    GitHubConnector().sync(ctx)

    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["body"] == long_body
    assert call["provenance_origin"] == "external"
    assert call["provenance_trust"] == "untrusted"
