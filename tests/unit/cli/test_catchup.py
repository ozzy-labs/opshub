"""Tests for ``opshub catchup`` (Phase 25-E, epic #566).

Cover the operator-facing catchup surface through
:class:`typer.testing.CliRunner` against a real migrated SQLite engine:

* ``catchup`` (default) surfaces the diff + advances the seen marker, so a
  second run reports a non-null ``since`` (the diff window narrowed);
* ``--no-advance`` is a dry preview that leaves the marker untouched;
* ``--format json`` emits the structured digest;
* an unsupported ``--format`` exits 2.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.commitments import commitments_table

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"
_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))
    # catchup never calls the LLM, but keep the backend off for parity.
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "disabled")
    return db_path


def _migrate_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


@pytest.fixture
def initialised_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    db_path = _isolate_env(monkeypatch, tmp_path)
    _migrate_db(db_path)
    yield db_path


def _seed_commitment(db_path: Path, *, text: str, due: str | None = None) -> str:
    commitment_id = new_ulid()
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(commitments_table).values(
                    id=commitment_id,
                    source_id=new_ulid(),
                    source_type="slack_message",
                    direction="owed_to_me",
                    counterparty=None,
                    due=due,
                    text=text,
                    confidence="medium",
                    state="open",
                    model_id="stub-llm",
                    tokens_in=0,
                    tokens_out=0,
                    extracted_at=_T0,
                    updated_at=_T0,
                )
            )
    finally:
        engine.dispose()
    return commitment_id


def test_catchup_empty_first_run(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["catchup"])
    assert result.exit_code == 0, result.output
    assert "Catchup since the beginning" in result.output
    assert "seen marker advanced" in result.output


def test_catchup_advances_marker_then_second_run_has_since(initialised_env: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(app, ["catchup"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["catchup", "--no-advance", "-f", "json"])
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    # The first run advanced the marker, so the second run has a non-null
    # ``since`` and (dry preview) a null ``advanced_to``.
    assert payload["since"] is not None
    assert payload["advanced_to"] is None


def test_catchup_no_advance_leaves_marker(initialised_env: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(app, ["catchup", "--no-advance", "-f", "json"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["advanced_to"] is None

    # A second dry preview still sees the whole history (marker untouched).
    second = runner.invoke(app, ["catchup", "--no-advance", "-f", "json"])
    assert json.loads(second.output)["since"] is None


def test_catchup_json_surfaces_open_commitments(initialised_env: Path) -> None:
    _seed_commitment(initialised_env, text="ship the release", due="2000-01-01")
    runner = CliRunner()
    result = runner.invoke(app, ["catchup", "--no-advance", "-f", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["open_commitments_total"] == 1
    assert payload["overdue_commitments_total"] == 1
    assert payload["open_commitments"][0]["text"] == "ship the release"
    assert payload["open_commitments"][0]["overdue"] is True


def test_catchup_invalid_format_exits_2(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["catchup", "-f", "xml"])
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "unsupported format" in combined.lower()
