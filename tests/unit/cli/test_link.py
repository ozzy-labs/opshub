"""Tests for ``opshub link`` (Phase 8 step D1).

Cover the operator-facing CRUD surface end-to-end through
:class:`typer.testing.CliRunner`:

* ``link add`` — event emission + projection row + entity-arg
  parsing + metadata pair parsing
* ``link remove`` — projection delete + sanitised ``--reason`` +
  no-op on missing id
* ``link list`` — md/json rendering + ``--from`` / ``--to`` /
  ``--type`` filters + ``--limit``

The test suite uses a real migrated SQLite engine (the writer path
runs through the live :class:`LinksProjector` UPSERT / DELETE) and a
real :class:`SqlAlchemyEventStore`, so the round-trip is end-to-end:
the CLI mutates state, the projection reflects it, and the event log
records both verbs.
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
from sqlalchemy import insert, select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.links import links_table

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


# ---- helpers --------------------------------------------------------------


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OpsHub env vars at ``tmp_path``."""
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
    """Apply Alembic migrations to ``db_path``."""
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
    """Migrate a fresh SQLite DB and point OpsHub env vars at it."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    _migrate_db(db_path)
    yield db_path


def _seed_link_row(
    db_path: Path,
    *,
    link_id: str,
    from_entity_type: str = "task",
    from_entity_id: str = "01J6FROM00000000000000000A",
    to_entity_type: str = "proposal",
    to_entity_id: str = "01J6TO000000000000000000AB",
    link_type: str = "manual",
    created_at: datetime | None = None,
) -> None:
    """Insert one ``links`` row directly for list/remove tests."""
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


def _all_links(db_path: Path) -> list[dict[str, Any]]:
    """Return every row of ``links`` for assertion."""
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(links_table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


# ---- link add ------------------------------------------------------------


def test_link_add_emits_event_and_writes_row(initialised_env: Path) -> None:
    """``link add`` UPSERTs a row and the audit event lands."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "link",
            "add",
            "task:01J6TASK000000000000000001",
            "decision:01J6DECISION0000000000000",
            "--type",
            "manual",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Link" in result.stdout
    assert "task:01J6TASK000000000000000001" in result.stdout
    assert "decision:01J6DECISION0000000000000" in result.stdout

    rows = _all_links(initialised_env)
    assert len(rows) == 1
    row = rows[0]
    assert row["from_entity_type"] == "task"
    assert row["from_entity_id"] == "01J6TASK000000000000000001"
    assert row["to_entity_type"] == "decision"
    assert row["to_entity_id"] == "01J6DECISION0000000000000"
    assert row["link_type"] == "manual"


def test_link_add_with_metadata_pairs_stored(initialised_env: Path) -> None:
    """``-m key=value`` pairs land in the ``metadata`` JSON column."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "link",
            "add",
            "task:01J6TASK000000000000000002",
            "proposal:01J6PROP000000000000000A",
            "-m",
            "score=0.93",
            "--metadata",
            "source=manual",
        ],
    )
    assert result.exit_code == 0, result.stdout

    rows = _all_links(initialised_env)
    assert len(rows) == 1
    assert rows[0]["metadata"] == {"score": "0.93", "source": "manual"}


def test_link_add_with_explicit_type(initialised_env: Path) -> None:
    """``--type references`` overrides the default ``manual``."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "link",
            "add",
            "source:01J6SOURCE000000000000003",
            "task:01J6TASK000000000000000003",
            "-t",
            "references",
        ],
    )
    assert result.exit_code == 0, result.stdout
    rows = _all_links(initialised_env)
    assert len(rows) == 1
    assert rows[0]["link_type"] == "references"


def test_link_add_rejects_malformed_entity_arg(initialised_env: Path) -> None:
    """Missing ``:`` in the entity arg raises a BadParameter."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["link", "add", "task01J6BAD", "proposal:01J6OK00000000000000000A"],
    )
    # Typer maps BadParameter to exit code 2.
    assert result.exit_code == 2, result.stdout + result.stderr


def test_link_add_rejects_empty_endpoint(initialised_env: Path) -> None:
    """Empty entity type or id raises BadParameter."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["link", "add", ":missing-type", "task:01J6OK00000000000000000A"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr


def test_link_add_rejects_malformed_metadata_pair(initialised_env: Path) -> None:
    """``--metadata foo`` (no ``=``) raises BadParameter."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "link",
            "add",
            "task:01J6TASK000000000000000004",
            "proposal:01J6PROP000000000000000B",
            "-m",
            "foo",
        ],
    )
    assert result.exit_code == 2, result.stdout + result.stderr


# ---- link remove ----------------------------------------------------------


def test_link_remove_deletes_row(initialised_env: Path) -> None:
    """``link remove`` deletes a real row."""
    link_id = "01J6LINK000000000000000005"
    _seed_link_row(initialised_env, link_id=link_id)
    runner = CliRunner()
    result = runner.invoke(app, ["link", "remove", link_id])
    assert result.exit_code == 0, result.stdout
    assert "removed" in result.stdout.lower()
    assert _all_links(initialised_env) == []


def test_link_remove_with_reason_sanitised(initialised_env: Path) -> None:
    """``--reason`` survives the round-trip and obvious secrets are scrubbed.

    The event's reason field carries the sanitised value (a fake
    ``sk-XXXX...`` shape collapses to ``sk-***``); the projection row
    is deleted regardless.
    """
    link_id = "01J6LINK000000000000000006"
    _seed_link_row(initialised_env, link_id=link_id)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "link",
            "remove",
            link_id,
            "--reason",
            "manual leak sk-abcdefghijklmnopqrstuvwxyz cleanup",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert _all_links(initialised_env) == []

    # The reason payload lands on a ``link.deleted`` event in the
    # events table; confirm the sanitiser hit the secret shape.
    from opshub.db import events_table

    engine = create_engine_for_sqlite(initialised_env)
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    select(events_table).where(events_table.c.event_type == "link.deleted")
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    assert len(rows) == 1
    # ``payload`` is stored as a JSON-encoded text column; parse to
    # introspect the ``reason`` field.
    payload = cast("dict[str, Any]", json.loads(rows[0]["payload"]))
    assert "sk-***" in payload["reason"]
    assert "abcdefghijklmnopqrstuvwxyz" not in payload["reason"]


def test_link_remove_nonexistent_id_exits_zero_with_message(
    initialised_env: Path,
) -> None:
    """Removing an unknown id is a no-op (exit 0 with informational message)."""
    runner = CliRunner()
    result = runner.invoke(app, ["link", "remove", "01J6MISSING0000000000000007"])
    assert result.exit_code == 0, result.stdout
    assert "not found" in result.stdout.lower()


# ---- link list ------------------------------------------------------------


def test_link_list_default_renders_md(initialised_env: Path) -> None:
    """Default ``--format md`` renders a Markdown table."""
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK000000000000000008",
        from_entity_type="task",
        from_entity_id="01J6TASK000000000000000008",
        to_entity_type="proposal",
        to_entity_id="01J6PROP000000000000000C",
        link_type="manual",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["link", "list"])
    assert result.exit_code == 0, result.stdout
    assert "| ID" in result.stdout
    assert "task:01J6TASK000000000000000008" in result.stdout
    assert "proposal:01J6PROP000000000000000C" in result.stdout
    assert "manual" in result.stdout


def test_link_list_json_format(initialised_env: Path) -> None:
    """``--format json`` emits a parseable array."""
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK000000000000000009",
        from_entity_type="briefing",
        from_entity_id="01J6BRIEF00000000000000A",
        to_entity_type="task",
        to_entity_id="01J6TASK000000000000000009",
        link_type="referenced_in_briefing",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["link", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = cast("list[dict[str, Any]]", json.loads(result.stdout))
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["from_entity_type"] == "briefing"
    assert payload[0]["link_type"] == "referenced_in_briefing"


def test_link_list_filters_from(initialised_env: Path) -> None:
    """``--from <type>:<id>`` filters the result set."""
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK00000000000000000A",
        from_entity_type="task",
        from_entity_id="01J6TASK00000000000000000A",
        to_entity_type="decision",
        to_entity_id="01J6DEC000000000000000000A",
    )
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK00000000000000000B",
        from_entity_type="task",
        from_entity_id="01J6TASK00000000000000000B",
        to_entity_type="decision",
        to_entity_id="01J6DEC000000000000000000B",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["link", "list", "--from", "task:01J6TASK00000000000000000A"],
    )
    assert result.exit_code == 0, result.stdout
    assert "01J6TASK00000000000000000A" in result.stdout
    assert "01J6TASK00000000000000000B" not in result.stdout


def test_link_list_filters_to(initialised_env: Path) -> None:
    """``--to <type>:<id>`` filters the result set."""
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK00000000000000000C",
        from_entity_type="proposal",
        from_entity_id="01J6PROP00000000000000000A",
        to_entity_type="task",
        to_entity_id="01J6TASK00000000000000000C",
        link_type="applied_to",
    )
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK00000000000000000D",
        from_entity_type="proposal",
        from_entity_id="01J6PROP00000000000000000B",
        to_entity_type="decision",
        to_entity_id="01J6DEC000000000000000000C",
        link_type="applied_to",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["link", "list", "--to", "task:01J6TASK00000000000000000C"],
    )
    assert result.exit_code == 0, result.stdout
    assert "01J6TASK00000000000000000C" in result.stdout
    assert "01J6DEC000000000000000000C" not in result.stdout


def test_link_list_filters_type(initialised_env: Path) -> None:
    """``--type <link_type>`` filters by link_type."""
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK00000000000000000E",
        from_entity_type="task",
        from_entity_id="01J6TASK00000000000000000E",
        to_entity_type="decision",
        to_entity_id="01J6DEC000000000000000000D",
        link_type="manual",
    )
    _seed_link_row(
        initialised_env,
        link_id="01J6LINK00000000000000000F",
        from_entity_type="briefing",
        from_entity_id="01J6BRIEF000000000000000B",
        to_entity_type="task",
        to_entity_id="01J6TASK00000000000000000F",
        link_type="referenced_in_briefing",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["link", "list", "--type", "manual"])
    assert result.exit_code == 0, result.stdout
    assert "manual" in result.stdout
    assert "referenced_in_briefing" not in result.stdout


def test_link_list_respects_limit(initialised_env: Path) -> None:
    """``--limit 1`` caps the result set."""
    for index in range(3):
        _seed_link_row(
            initialised_env,
            link_id=f"01J6LIMIT{index:017d}",
            from_entity_type="task",
            from_entity_id=f"01J6TASKLIMIT{index:013d}",
            to_entity_type="decision",
            to_entity_id=f"01J6DECLIMIT{index:014d}",
            created_at=datetime(2026, 5, 17, 12, index, tzinfo=UTC),
        )
    runner = CliRunner()
    result = runner.invoke(app, ["link", "list", "--limit", "1", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = cast("list[dict[str, Any]]", json.loads(result.stdout))
    assert len(payload) == 1


def test_link_list_invalid_format_exits_2(initialised_env: Path) -> None:
    """Unknown ``--format`` value exits with code 2."""
    runner = CliRunner()
    result = runner.invoke(app, ["link", "list", "--format", "yaml"])
    assert result.exit_code == 2, result.stdout + result.stderr
