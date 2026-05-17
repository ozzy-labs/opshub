"""Tests for ``opshub graph`` (Phase 8 step D1 + D2).

Cover the operator-facing graph traversal queries:

* ``graph related`` — 1-hop neighbours + direction filter + md/json/dot
* ``graph trace`` — backward provenance chains + depth ceiling
* ``graph expand`` — bidirectional N-hop expansion + depth ceiling +
  type filter + md/json/dot rendering (D2 wires this to the
  :meth:`LinkService.expand` writer)

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


# ---- graph expand (Phase 8 D2) --------------------------------------------


def test_graph_expand_renders_md(initialised_env: Path) -> None:
    """``graph expand`` md format renders nodes + edges around the root."""
    # task <- briefing -> proposal: a small 3-node subset that the
    # 2-hop expansion from the briefing should reach in full.
    _seed_link_row(
        initialised_env,
        link_id="01J6EX000000000000000001",
        from_entity_type="briefing",
        from_entity_id="01J6BRIEFEX000000000001",
        to_entity_type="task",
        to_entity_id="01J6TASKEX0000000000001",
        link_type="referenced_in_briefing",
    )
    _seed_link_row(
        initialised_env,
        link_id="01J6EX000000000000000002",
        from_entity_type="briefing",
        from_entity_id="01J6BRIEFEX000000000001",
        to_entity_type="proposal",
        to_entity_id="01J6PROPEX0000000000001",
        link_type="applied_to",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "expand", "briefing:01J6BRIEFEX000000000001"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The Markdown output carries a Nodes table + Edges section; the
    # root briefing + both neighbours should land in the Nodes table.
    assert "briefing:01J6BRIEFEX000000000001" in result.stdout
    assert "01J6TASKEX0000000000001" in result.stdout
    assert "01J6PROPEX0000000000001" in result.stdout
    assert "## Nodes" in result.stdout
    assert "## Edges" in result.stdout


def test_graph_expand_renders_json(initialised_env: Path) -> None:
    """``graph expand --format json`` emits the GraphSubset payload."""
    _seed_link_row(
        initialised_env,
        link_id="01J6EX000000000000000003",
        from_entity_type="task",
        from_entity_id="01J6TASKEX0000000000003",
        to_entity_type="proposal",
        to_entity_id="01J6PROPEX0000000000003",
        link_type="applied_to",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "graph",
            "expand",
            "task:01J6TASKEX0000000000003",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = cast("dict[str, Any]", json.loads(result.stdout))
    assert payload["root"] == {
        "entity_type": "task",
        "entity_id": "01J6TASKEX0000000000003",
    }
    assert payload["depth"] == 2
    # Nodes include the root + the proposal neighbour
    node_keys = {(entry["entity_type"], entry["entity_id"]) for entry in payload["nodes"]}
    assert ("task", "01J6TASKEX0000000000003") in node_keys
    assert ("proposal", "01J6PROPEX0000000000003") in node_keys
    # One edge in the payload
    assert len(payload["edges"]) == 1
    assert payload["edges"][0]["link_type"] == "applied_to"


def test_graph_expand_renders_dot(initialised_env: Path) -> None:
    """``--format dot`` emits a valid Graphviz DOT digraph."""
    _seed_link_row(
        initialised_env,
        link_id="01J6EX000000000000000004",
        from_entity_type="task",
        from_entity_id="01J6TASKEX0000000000004",
        to_entity_type="proposal",
        to_entity_id="01J6PROPEX0000000000004",
        link_type="applied_to",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "graph",
            "expand",
            "task:01J6TASKEX0000000000004",
            "--format",
            "dot",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The DOT body must be a digraph block; the focus root carries a
    # ``[shape="box"]`` attribute via render_links_dot.
    assert "digraph opshub_graph {" in result.stdout
    assert result.stdout.rstrip().endswith("}")
    assert '"task:01J6TASKEX0000000000004"' in result.stdout
    assert 'label="applied_to"' in result.stdout


def test_graph_expand_depth_exceeded_exits_2(initialised_env: Path) -> None:
    """``--depth 6`` exceeds the ADR-0017 expand ceiling (5) and exits 2."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "expand", "task:01J6TASKEX0000000000099", "--depth", "6"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "depth" in (result.stdout + result.stderr).lower()


def test_graph_expand_negative_depth_exits_2(initialised_env: Path) -> None:
    """``--depth -1`` violates the >= 0 floor and exits 2."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "expand", "task:01J6TASKEX00000000000FF", "--depth", "-1"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr


def test_graph_expand_respects_type_filter(initialised_env: Path) -> None:
    """``--type applied_to`` restricts expansion to that link type only."""
    # Two outgoing links from the root with different link_types.
    _seed_link_row(
        initialised_env,
        link_id="01J6EX000000000000000010",
        from_entity_type="task",
        from_entity_id="01J6TASKEX00000000000A0",
        to_entity_type="proposal",
        to_entity_id="01J6PROPEX0000000000A01",
        link_type="applied_to",
    )
    _seed_link_row(
        initialised_env,
        link_id="01J6EX000000000000000011",
        from_entity_type="task",
        from_entity_id="01J6TASKEX00000000000A0",
        to_entity_type="briefing",
        to_entity_id="01J6BRIEFEX00000000000A2",
        link_type="referenced_in_briefing",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "graph",
            "expand",
            "task:01J6TASKEX00000000000A0",
            "--type",
            "applied_to",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = cast("dict[str, Any]", json.loads(result.stdout))
    node_keys = {(entry["entity_type"], entry["entity_id"]) for entry in payload["nodes"]}
    assert ("proposal", "01J6PROPEX0000000000A01") in node_keys
    assert ("briefing", "01J6BRIEFEX00000000000A2") not in node_keys
    edge_types = {edge["link_type"] for edge in payload["edges"]}
    assert edge_types == {"applied_to"}


def test_graph_expand_rejects_malformed_entity_arg(initialised_env: Path) -> None:
    """``graph expand`` validates the entity arg before reaching the service."""
    runner = CliRunner()
    result = runner.invoke(app, ["graph", "expand", "taskNOCOLON"])
    assert result.exit_code == 2, result.stdout + result.stderr


def test_graph_expand_validates_format(initialised_env: Path) -> None:
    """``graph expand --format yaml`` exits 2 with an invalid-format error."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["graph", "expand", "task:01J6TASKEX0000000000002", "--format", "yaml"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
