"""Tests for ``opshub recall`` (Phase 4 step C2).

The recall CLI is the primary user-facing surface for Phase 4 MVP: it
runs semantic search over task / decision / inbox_item / source via
the active embedding backend. These tests cover the CLI shape end-to-end
through :class:`typer.testing.CliRunner`:

* ``backend=disabled`` short-circuit (exit 2 + stderr hint).
* :class:`~opshub.core.errors.ConfigError` from
  :class:`~opshub.services.recall_service.RecallService` rendered as a
  clean error line (exit 2).
* Empty hit list rendered as ``no hits for '<query>'``.
* Three output formats (``table`` / ``json`` / ``md``) round-tripped
  through the shared :mod:`opshub.cli._render` helpers.
* Filter argument propagation (``--type`` / ``--limit`` / ``--state``).
* Score formatting (3 decimals) and entity_id truncation (first 8
  chars) match the rendered convention from
  :mod:`opshub.cli._render`.

The :func:`build_recall_service` factory is monkeypatched to return a
``_StubRecallService`` so these tests stay laser-focused on the CLI
shape — the full integration path (embedder + sqlite-vec + projections)
is already covered by :mod:`tests.unit.services.test_recall_service`
and the optional ``tests/integration/test_recall_cli_lifecycle.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.errors import ConfigError
from opshub.services.recall_service import RecallHit

# ---- helpers --------------------------------------------------------------


class _StubRecallService:
    """Stub :class:`RecallService` with a configurable response.

    Records every ``recall`` call so tests can assert filter args
    propagate from the CLI to the service. The recorded ``kwargs``
    dict mirrors the keyword-only parameters of the real service so
    a future signature change surfaces here as well.
    """

    def __init__(
        self,
        *,
        hits: list[RecallHit] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._hits = hits or []
        self._raises = raises
        self.calls: list[dict[str, object]] = []

    def recall(
        self,
        query: str,
        *,
        entity_type: str | None = None,
        limit: int = 10,
        state: str | None = None,
    ) -> list[RecallHit]:
        self.calls.append(
            {
                "query": query,
                "entity_type": entity_type,
                "limit": limit,
                "state": state,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._hits


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point OpsHub env vars at ``tmp_path`` to keep the user's dirs untouched.

    The recall CLI does not run ``opshub init`` for the
    ``backend=disabled`` path (the short-circuit fires before the
    engine is opened), so we only need to isolate config / data so
    pydantic-settings doesn't surface a stale config from the user's
    home dir.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))


def _install_stub_service(monkeypatch: pytest.MonkeyPatch, stub: _StubRecallService) -> None:
    """Monkeypatch :func:`build_recall_service` so the CLI sees ``stub``.

    The recall command uses a lazy ``from opshub.cli._wiring import
    build_recall_service`` inside the command body, so patching the
    name on :mod:`opshub.cli._wiring` is sufficient — every Typer
    invocation re-evaluates the ``from`` import.
    """
    monkeypatch.setattr(
        "opshub.cli._wiring.build_recall_service",
        lambda: stub,
    )


def _make_hit(
    *,
    entity_type: str = "task",
    entity_id: str = "01ABCDEFGH0000000000000000",
    title: str = "example title",
    snippet: str | None = None,
    score: float = 0.5,
) -> RecallHit:
    """Build a :class:`RecallHit` with sensible test defaults."""
    return RecallHit(
        entity_type=entity_type,
        entity_id=entity_id,
        title=title,
        snippet=snippet if snippet is not None else title,
        score=score,
    )


# ---- backend=disabled -----------------------------------------------------


def test_recall_disabled_backend_exits_with_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``backend=disabled`` → exit 2 + stderr mentions ``embeddings rebuild``.

    The check fires before :func:`build_recall_service`, so the test
    deliberately does NOT install a stub service: a regression that
    moves the check below the wiring call would crash on the missing
    engine and fail the assertion.
    """
    _isolate_env(monkeypatch, tmp_path)
    # ``disabled`` is the default; setting it explicitly keeps the test
    # robust against a future flip of the default value.
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "disabled")
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "anything"])

    assert result.exit_code == 2, result.stdout
    assert "Embedding backend is disabled" in result.stderr
    assert "opshub embeddings rebuild" in result.stderr


# ---- ConfigError propagation ---------------------------------------------


def test_recall_propagates_config_error_from_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Service-raised :class:`ConfigError` surfaces as ``Error: ...`` + exit 2.

    Mirrors the operator UX when ``embeddings rebuild`` has not been
    run yet for the active model — the service raises ConfigError with
    a "rebuild required" message, the CLI renders it cleanly without
    a traceback.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    stub = _StubRecallService(raises=ConfigError("rebuild required"))
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "anything"])

    assert result.exit_code == 2, result.stdout
    assert "Error: rebuild required" in result.stderr


# ---- empty result --------------------------------------------------------


def test_recall_returns_no_hits_for_empty_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty hit list renders as ``no hits for 'query'`` on stdout, exit 0."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    stub = _StubRecallService(hits=[])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "needle"])

    assert result.exit_code == 0, result.stdout
    assert "no hits for 'needle'" in result.stdout


# ---- output formats ------------------------------------------------------


def test_recall_renders_table_format_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default ``table`` format aligns columns (score / entity_type / entity_id / title)."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    stub = _StubRecallService(
        hits=[
            _make_hit(title="first hit", score=0.9),
            _make_hit(
                entity_type="decision",
                entity_id="01ZZZZZZZZ0000000000000000",
                title="second hit",
                score=0.8,
            ),
        ]
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "anything"])

    assert result.exit_code == 0, result.stdout
    # Header row uses upper-cased column names (see _render.render_table).
    assert "SCORE" in result.stdout
    assert "ENTITY_TYPE" in result.stdout
    assert "ENTITY_ID" in result.stdout
    assert "TITLE" in result.stdout
    assert "first hit" in result.stdout
    assert "second hit" in result.stdout


def test_recall_renders_json_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--format json`` emits a parseable JSON list of hit dicts."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    stub = _StubRecallService(
        hits=[
            _make_hit(title="alpha", score=0.75),
            _make_hit(
                entity_type="inbox_item",
                entity_id="01INBOX0000000000000000000",
                title="beta",
                score=0.5,
            ),
        ]
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "anything", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    payload = cast(
        list[dict[str, object]],
        json.loads(result.stdout.strip().splitlines()[-1]),
    )
    assert isinstance(payload, list)
    assert len(payload) == 2
    # Documented keys match the Column header → json_key derivation
    # (lower-cased / spaces → underscores; see _render.Column).
    assert set(payload[0].keys()) == {"score", "entity_type", "entity_id", "title"}
    assert payload[0]["title"] == "alpha"
    assert payload[1]["entity_type"] == "inbox_item"


def test_recall_renders_md_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--format md`` emits a GitHub-flavoured Markdown table."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    stub = _StubRecallService(hits=[_make_hit(title="md row", score=0.5)])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "anything", "--format", "md"])

    assert result.exit_code == 0, result.stdout
    assert "| score | entity_type | entity_id | title |" in result.stdout


# ---- filter argument propagation -----------------------------------------


def test_recall_passes_filter_args_to_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--type`` / ``--limit`` / ``--state`` propagate to ``service.recall``."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    stub = _StubRecallService(hits=[_make_hit()])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "recall",
            "q",
            "--type",
            "task",
            "--limit",
            "20",
            "--state",
            "active",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["query"] == "q"
    assert call["entity_type"] == "task"
    assert call["limit"] == 20
    assert call["state"] == "active"


# ---- formatting conventions ----------------------------------------------


def test_recall_truncates_entity_id_to_8_chars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hit ``entity_id`` (a 26-char ULID) renders as the first 8 chars.

    Matches the convention used by every other CLI list view in the
    repo (see :func:`opshub.cli._render.id_prefix`).
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    full_id = "01ABCDEFGHJKMNPQRSTVWXYZ12"
    stub = _StubRecallService(hits=[_make_hit(entity_id=full_id)])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "anything", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    payload = cast(
        list[dict[str, object]],
        json.loads(result.stdout.strip().splitlines()[-1]),
    )
    assert payload[0]["entity_id"] == full_id[:8]
    # The remainder of the ULID must not appear anywhere in stdout.
    assert full_id not in result.stdout


def test_recall_score_format_3_decimals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Score 0.987654 renders as ``0.988`` (3-decimal banker's-round)."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    stub = _StubRecallService(hits=[_make_hit(score=0.987654)])
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["recall", "anything"])

    assert result.exit_code == 0, result.stdout
    assert "0.988" in result.stdout
