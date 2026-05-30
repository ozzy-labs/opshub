"""Tests for ``opshub search`` (Phase 10 step B2).

The search CLI is the FTS5-backed counterpart to ``opshub recall``,
shipped as part of Sub-issue B (ADR-0012 改訂版 §4 + ADR-0020 +
phase-10-plan §3 / §4-B). These tests cover the CLI shape end-to-end
through :class:`typer.testing.CliRunner`:

* :class:`~opshub.core.errors.ConfigError` raised by
  :class:`~opshub.services.search_service.SearchService` (empty query)
  is rendered as ``Error: ...`` with exit code 2.
* Empty hit list renders as ``no hits for '<query>'``.
* Default ``table`` format renders score / connector / entity_id /
  title columns.
* Filter argument propagation (``--limit`` / ``--connector`` /
  ``--raw``).

The :func:`build_search_service` factory is monkeypatched to return a
``_StubSearchService`` so these tests stay laser-focused on the CLI
shape — the FTS5 + sources JOIN behaviour is exercised by
:mod:`tests.unit.services.test_search_service`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.errors import ConfigError
from opshub.services.search_service import SearchHit


class _StubSearchService:
    """Stub :class:`SearchService` with a configurable response.

    Records every ``search`` call so tests can assert filter args
    propagate from the CLI to the service. The recorded ``kwargs``
    dict mirrors the keyword-only parameters of the real service so
    a future signature change surfaces here as well.
    """

    def __init__(
        self,
        *,
        hits: list[SearchHit] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._hits = hits or []
        self._raises = raises
        self.calls: list[dict[str, object]] = []

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        connector_name: str | None = None,
        raw_query: bool = False,
    ) -> list[SearchHit]:
        self.calls.append(
            {
                "query": query,
                "limit": limit,
                "connector_name": connector_name,
                "raw_query": raw_query,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._hits


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point OpsHub env vars at ``tmp_path`` so the user's dirs stay clean.

    Search has no backend short-circuit, but the stub injection
    bypasses :func:`build_search_service` so the engine never opens
    and we only need to keep pydantic-settings from picking up a
    stale config from the user's home dir.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))


def _install_stub_service(monkeypatch: pytest.MonkeyPatch, stub: _StubSearchService) -> None:
    """Monkeypatch :func:`build_search_service` so the CLI sees ``stub``."""
    monkeypatch.setattr(
        "opshub.cli._wiring.build_search_service",
        lambda: stub,
    )


def _make_hit(
    *,
    entity_id: str = "01ABCDEFGH0000000000000000",
    connector_name: str = "github",
    title: str = "example hit",
    url: str | None = None,
    snippet: str | None = None,
    score: float = 1.5,
) -> SearchHit:
    return SearchHit(
        entity_id=entity_id,
        connector_name=connector_name,
        source_type="issue",
        title=title,
        url=url,
        snippet=snippet if snippet is not None else title,
        score=score,
    )


# ---- ConfigError propagation ---------------------------------------------


def test_search_propagates_config_error_from_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Service-raised :class:`ConfigError` surfaces as ``Error: ...`` + exit 2."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubSearchService(raises=ConfigError("must not be empty"))
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["search", "   "])

    assert result.exit_code == 2, result.stdout
    assert "Error: must not be empty" in result.stderr


# ---- empty result --------------------------------------------------------


def test_search_returns_no_hits_for_empty_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty hit list renders as ``no hits for 'query'`` on stdout, exit 0."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubSearchService(hits=[])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["search", "needle"])

    assert result.exit_code == 0, result.stdout
    assert "no hits for 'needle'" in result.stdout


# ---- output formats ------------------------------------------------------


def test_search_renders_table_format_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default ``table`` format aligns columns (score / connector / entity_id / title)."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubSearchService(
        hits=[
            _make_hit(title="first hit", score=2.5),
            _make_hit(
                entity_id="01ZZZZZZZZ0000000000000000",
                connector_name="slack",
                title="second hit",
                score=2.0,
            ),
        ]
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["search", "anything"])

    assert result.exit_code == 0, result.stdout
    assert "SCORE" in result.stdout
    assert "CONNECTOR" in result.stdout
    assert "ENTITY_ID" in result.stdout
    assert "TITLE" in result.stdout
    assert "first hit" in result.stdout
    assert "second hit" in result.stdout


# ---- filter propagation --------------------------------------------------


def test_search_propagates_limit_and_connector_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--limit`` / ``--connector`` reach the service unchanged."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubSearchService(hits=[_make_hit()])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["search", "query", "--limit", "5", "--connector", "slack"],
    )

    assert result.exit_code == 0, result.stdout
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["query"] == "query"
    assert call["limit"] == 5
    assert call["connector_name"] == "slack"
    assert call["raw_query"] is False


def test_search_propagates_raw_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--raw`` toggles ``raw_query=True`` on the service call."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubSearchService(hits=[_make_hit()])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["search", "alpha OR beta", "--raw"])

    assert result.exit_code == 0, result.stdout
    assert stub.calls[0]["raw_query"] is True
    assert stub.calls[0]["query"] == "alpha OR beta"
