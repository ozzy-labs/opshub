"""Tests for ``opshub decision`` (record / list).

Every test isolates the CLI invocation via ``monkeypatch.setenv`` so the
user's real ``~/.config/opshub`` / ``~/.local/share/opshub`` directories
are never touched. ``opshub init`` is invoked through the CLI to
provision the schema before each command exercise — that mirrors how a
real user reaches ``opshub decision ...``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.ids import parse_ulid_timestamp_ms
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.decisions import decisions_table


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every OpsHub path env var inside ``tmp_path`` and return the DB path."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))

    return db_path


def _run_init(runner: CliRunner) -> None:
    """Run ``opshub init`` and assert it succeeded."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout


# ---- decision record ------------------------------------------------------


def test_decision_record_prints_ulid_and_persists_event_and_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["decision", "record", "use python 3.13"])
    assert result.exit_code == 0, result.stdout

    decision_id = result.stdout.strip()
    # Output is exactly one 26-character ULID.
    assert len(decision_id) == 26
    # And it round-trips through the ULID parser.
    parse_ulid_timestamp_ms(decision_id)

    # The events table has one row, the decisions projection has one row.
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            events_table = decisions_table.metadata.tables["events"]
            events = conn.execute(
                select(events_table.c.event_type, events_table.c.aggregate_id)
            ).all()
            decisions = conn.execute(select(decisions_table)).mappings().all()
    finally:
        engine.dispose()

    assert len(events) == 1
    assert events[0].event_type == "decision.recorded"
    assert events[0].aggregate_id == decision_id

    assert len(decisions) == 1
    row = decisions[0]
    assert row["id"] == decision_id
    assert row["text"] == "use python 3.13"
    assert row["context"] is None
    assert row["actor"] == "cli:decision"


def test_decision_record_with_context_and_actor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(
        app,
        [
            "decision",
            "record",
            "merge as-is",
            "--context",
            "approved in standup",
            "--actor",
            "agent:planner",
        ],
    )
    assert result.exit_code == 0, result.stdout
    decision_id = result.stdout.strip()

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            events_table = decisions_table.metadata.tables["events"]
            event_rows = conn.execute(
                select(events_table.c.actor, events_table.c.aggregate_id)
            ).all()
            decision_row = (
                conn.execute(select(decisions_table).where(decisions_table.c.id == decision_id))
                .mappings()
                .one()
            )
    finally:
        engine.dispose()

    assert len(event_rows) == 1
    assert event_rows[0].actor == "agent:planner"
    assert decision_row["context"] == "approved in standup"
    assert decision_row["actor"] == "agent:planner"


def test_decision_record_rejects_empty_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["decision", "record", ""])
    assert result.exit_code != 0


# ---- decision list --------------------------------------------------------


def test_decision_list_json_returns_array_of_recorded_decisions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    first = runner.invoke(app, ["decision", "record", "first"])
    assert first.exit_code == 0, first.stdout
    second = runner.invoke(app, ["decision", "record", "second"])
    assert second.exit_code == 0, second.stdout

    result = runner.invoke(app, ["decision", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout

    payload: list[dict[str, Any]] = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 2
    texts = {row["text"] for row in payload}
    assert texts == {"first", "second"}
    # Every row exposes the canonical columns.
    for row in payload:
        for key in ("id", "text", "context", "actor", "recorded_at"):
            assert key in row


def test_decision_list_md_contains_markdown_table_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    create_result = runner.invoke(app, ["decision", "record", "md test"])
    assert create_result.exit_code == 0, create_result.stdout

    result = runner.invoke(app, ["decision", "list", "--format", "md"])
    assert result.exit_code == 0, result.stdout

    # Markdown table headers + separator row.
    assert "| ID |" in result.stdout
    assert "| Text |" in result.stdout
    assert "| --- |" in result.stdout
    # The recorded decision text shows up.
    assert "md test" in result.stdout


def test_decision_list_table_default_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    create_result = runner.invoke(app, ["decision", "record", "table test"])
    assert create_result.exit_code == 0, create_result.stdout

    result = runner.invoke(app, ["decision", "list"])
    assert result.exit_code == 0, result.stdout
    # Aligned-column header.
    assert "ID" in result.stdout
    assert "TEXT" in result.stdout
    assert "RECORDED" in result.stdout
    assert "table test" in result.stdout


def test_decision_list_invalid_format_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["decision", "list", "--format", "invalid"])
    assert result.exit_code != 0
    # The ValidationError mentions the bad value so users can self-correct.
    assert result.exception is not None
    assert "invalid" in str(result.exception).lower()


def test_decision_list_empty_database_returns_header_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["decision", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


def test_decision_list_against_uninitialised_db_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running ``decision list`` before ``opshub init`` surfaces a ConfigError."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    # Note: NO opshub init call here.
    result = runner.invoke(app, ["decision", "list"])
    assert result.exit_code != 0
    assert result.exception is not None
    assert "opshub init" in str(result.exception)
