"""Tests for ``opshub graph`` (Phase 8 step D1).

Cover the operator-facing graph traversal queries:

* ``graph related`` — 1-hop neighbours + direction filter + md/json/dot
* ``graph trace`` — backward provenance chains + depth ceiling
* ``graph expand`` — stub state (raises until Phase 8 step C2 lands)

The tests use a migrated SQLite DB and seed ``links`` rows directly
(bypassing the projector) so the CLI surface is exercised end-to-end
without depending on the other event paths that materialise links.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.links import links_table

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


# ---- helpers --------------------------------------------------------------


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))
    return db_path


def _migrate_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


@pytest.fixture
def initialised_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Path]:
    db_path = _isolate_env(monkeypatch, tmp_path)
    _migrate_db(db_path)
    yield db_path


def _seed_link_row(
    db_path: Path,
    *,
    link_id: str,
    from_entity_type: str,
    from_entity_id: str,
    to_entity_type: str,
    to_entity_id: str,
    link_type: str = "manual",
    created_at: datetime | None = None,
) -> None:
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(links_table).values(
                    id=link_id,
                    from_entity_type=from_entity_type,
                    from_entity_id=from_entity_id,
                    to_entity_type=to_entity_type,
                    to_entity_id=to_entity_id,
                    link_type=link_type,
                    created_at=created_at
                    if created_at is not None
                    else datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
                    source_event_id=None,
                    metadata=None,
                )
            )
    finally:
        engine.dispose()


# ---- graph related --------------------------------------------------------


def test_graph_related_renders_md(initialised_env: Path) -> None:
    """``graph related`` md format lists 1-hop neighbours."""
    _seed_link_row(
        initialised_env,
        link_id="01J6GR000000000000000001",
        from_entity_type="task",
        from_entity_id="01J6TASKGR0000000000001",
        to_entity_type="proposal",
        to_entity_id="01J6PROPGR0000000000001",
        link_type="applied_to",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "related", "task:01J6TASKGR0000000000001"],
    )
    assert result.exit_code == 0, result.stdout
    assert "proposal:01J6PROPGR0000000000001" in result.stdout
    assert "applied_to" in result.stdout


def test_graph_related_json(initialised_env: Path) -> None:
    """``--format json`` emits the same shape as ``link list``."""
    _seed_link_row(
        initialised_env,
        link_id="01J6GR000000000000000002",
        from_entity_type="task",
        from_entity_id="01J6TASKGR0000000000002",
        to_entity_type="briefing",
        to_entity_id="01J6BRIEFGR000000000002",
        link_type="referenced_in_briefing",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "related", "task:01J6TASKGR0000000000002", "--format", "json"],
    )
    assert result.exit_code == 0, result.stdout
    payload = cast("list[dict[str, Any]]", json.loads(result.stdout))
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["link_type"] == "referenced_in_briefing"


def test_graph_related_dot(initialised_env: Path) -> None:
    """``--format dot`` emits a Graphviz DOT digraph."""
    _seed_link_row(
        initialised_env,
        link_id="01J6GR000000000000000003",
        from_entity_type="task",
        from_entity_id="01J6TASKGR0000000000003",
        to_entity_type="proposal",
        to_entity_id="01J6PROPGR0000000000003",
        link_type="applied_to",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "related", "task:01J6TASKGR0000000000003", "--format", "dot"],
    )
    assert result.exit_code == 0, result.stdout
    assert "digraph opshub_graph" in result.stdout
    assert "task:01J6TASKGR0000000000003" in result.stdout
    assert '"task:01J6TASKGR0000000000003" -> "proposal:01J6PROPGR0000000000003"' in result.stdout
    assert 'label="applied_to"' in result.stdout


def test_graph_related_direction_filter(initialised_env: Path) -> None:
    """``--direction outgoing`` filters out incoming edges."""
    # Outgoing from task
    _seed_link_row(
        initialised_env,
        link_id="01J6GR000000000000000004",
        from_entity_type="task",
        from_entity_id="01J6TASKGR0000000000004",
        to_entity_type="proposal",
        to_entity_id="01J6PROPGR0000000000004",
    )
    # Incoming to task
    _seed_link_row(
        initialised_env,
        link_id="01J6GR000000000000000005",
        from_entity_type="briefing",
        from_entity_id="01J6BRIEFGR000000000005",
        to_entity_type="task",
        to_entity_id="01J6TASKGR0000000000004",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "graph",
            "related",
            "task:01J6TASKGR0000000000004",
            "--direction",
            "outgoing",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = cast("list[dict[str, Any]]", json.loads(result.stdout))
    assert len(payload) == 1
    assert payload[0]["to_entity_type"] == "proposal"


def test_graph_related_rejects_malformed_entity_arg(initialised_env: Path) -> None:
    """Missing ``:`` in entity arg raises BadParameter (exit 2)."""
    runner = CliRunner()
    result = runner.invoke(app, ["graph", "related", "taskNOCOLON"])
    assert result.exit_code == 2, result.stdout + result.stderr


def test_graph_related_rejects_unknown_direction(initialised_env: Path) -> None:
    """Unknown ``--direction`` exits 2 with a clean error."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "graph",
            "related",
            "task:01J6TASKGR0000000000004",
            "--direction",
            "sideways",
        ],
    )
    assert result.exit_code == 2, result.stdout + result.stderr


# ---- graph trace ----------------------------------------------------------


def test_graph_trace_renders_paths(initialised_env: Path) -> None:
    """``graph trace`` walks backward and renders chains."""
    # source -> proposal -> task chain (trace from task walks backward)
    _seed_link_row(
        initialised_env,
        link_id="01J6TR000000000000000001",
        from_entity_type="proposal",
        from_entity_id="01J6PROPTR0000000000001",
        to_entity_type="task",
        to_entity_id="01J6TASKTR0000000000001",
        link_type="applied_to",
        created_at=datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
    )
    _seed_link_row(
        initialised_env,
        link_id="01J6TR000000000000000002",
        from_entity_type="briefing",
        from_entity_id="01J6BRIEFTR000000000002",
        to_entity_type="proposal",
        to_entity_id="01J6PROPTR0000000000001",
        link_type="generated_from_briefing",
        created_at=datetime(2026, 5, 17, 12, 1, tzinfo=UTC),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "trace", "task:01J6TASKTR0000000000001"],
    )
    assert result.exit_code == 0, result.stdout
    assert "task:01J6TASKTR0000000000001" in result.stdout
    assert "proposal:01J6PROPTR0000000000001" in result.stdout
    assert "briefing:01J6BRIEFTR000000000002" in result.stdout


def test_graph_trace_json_format(initialised_env: Path) -> None:
    """``--format json`` returns a list of path objects."""
    _seed_link_row(
        initialised_env,
        link_id="01J6TR000000000000000003",
        from_entity_type="proposal",
        from_entity_id="01J6PROPTR0000000000003",
        to_entity_type="task",
        to_entity_id="01J6TASKTR0000000000003",
        link_type="applied_to",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "graph",
            "trace",
            "task:01J6TASKTR0000000000003",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = cast("list[dict[str, Any]]", json.loads(result.stdout))
    assert isinstance(payload, list)
    assert len(payload) >= 1
    assert "depth" in payload[0]
    assert "links" in payload[0]


def test_graph_trace_depth_exceeded_exits_2(initialised_env: Path) -> None:
    """``--depth 11`` exceeds the ADR-0017 ceiling and exits 2."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "trace", "task:01J6TASKTR0000000000099", "--depth", "11"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "depth" in (result.stdout + result.stderr).lower()


def test_graph_trace_empty_result_renders_informative_message(
    initialised_env: Path,
) -> None:
    """Trace of an unreferenced entity surfaces a clear ``no incoming links``."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "trace", "task:01J6TASKTR0000000000FF"],
    )
    assert result.exit_code == 0, result.stdout
    assert "no incoming links" in result.stdout.lower()


def test_graph_trace_rejects_malformed_entity_arg(initialised_env: Path) -> None:
    """Missing ``:`` in entity arg raises BadParameter."""
    runner = CliRunner()
    result = runner.invoke(app, ["graph", "trace", "taskNOCOLON"])
    assert result.exit_code == 2, result.stdout + result.stderr


# ---- graph expand (stub until C2 lands) -----------------------------------


def test_graph_expand_unavailable_until_c2(initialised_env: Path) -> None:
    """``graph expand`` exits 2 with a message pointing to C2.

    Phase 8 step D1 ships the CLI shape but the
    :meth:`LinkService.expand` writer lands in C2 (Wave 4 parallel
    PR). The CLI body raises :class:`typer.Exit(2)` with a clear
    ``feature not yet available`` message so the operator does not
    hit an AttributeError; the test pins that behaviour to flag
    when the follow-up PR enables it.
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "expand", "task:01J6TASKEX0000000000001"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "expand" in output.lower()
    assert "c2" in output.lower()


def test_graph_expand_rejects_malformed_entity_arg(initialised_env: Path) -> None:
    """``graph expand`` validates entity arg before failing with the stub message."""
    runner = CliRunner()
    result = runner.invoke(app, ["graph", "expand", "taskNOCOLON"])
    assert result.exit_code == 2, result.stdout + result.stderr


def test_graph_expand_validates_format(initialised_env: Path) -> None:
    """``graph expand --format yaml`` exits 2 before hitting the stub guard."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "expand", "task:01J6TASKEX0000000000002", "--format", "yaml"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
