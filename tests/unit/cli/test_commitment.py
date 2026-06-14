"""Tests for ``opshub commitment`` (Phase 25-C, ADR-0042).

Cover the operator-facing commitment-ledger surface through
:class:`typer.testing.CliRunner` against a real migrated SQLite engine:

* ``commitment scan`` short-circuits with a clean hint + exit 2 when the
  LLM backend is disabled (the on-demand extraction path that needs the
  LLM — the heavy LLM call itself is exercised in the service tests);
* ``commitment list`` renders table + json, filters by direction / state,
  and works with the LLM disabled (閲覧 LLM 不要);
* ``commitment resolve / dismiss / reopen`` flip state + error paths.
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
    # Default the LLM backend off so the scan short-circuit path is the
    # deterministic CLI behaviour; individual tests flip it as needed.
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


def _seed_commitment(
    db_path: Path,
    *,
    direction: str = "owed_to_me",
    state: str = "open",
    counterparty: str | None = None,
    text: str = "review the PR",
    due: str | None = None,
) -> str:
    commitment_id = new_ulid()
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(commitments_table).values(
                    id=commitment_id,
                    source_id=new_ulid(),
                    source_type="slack_message",
                    direction=direction,
                    counterparty=counterparty,
                    due=due,
                    text=text,
                    confidence="medium",
                    state=state,
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


# ---- scan -----------------------------------------------------------------


def test_scan_disabled_backend_short_circuits(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "scan"])
    assert result.exit_code == 2
    assert "disabled" in result.stdout.lower() or "disabled" in (result.stderr or "").lower()


# ---- list -----------------------------------------------------------------


def test_list_empty(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "list"])
    assert result.exit_code == 0
    assert "no commitments" in result.stdout.lower()


def test_list_renders_table(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env, text="ship the release", due="2026-06-20")
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "list"])
    assert result.exit_code == 0, result.stdout
    assert cid in result.stdout
    assert "ship the release" in result.stdout
    assert "2026-06-20" in result.stdout


def test_list_json_format(initialised_env: Path) -> None:
    _seed_commitment(initialised_env, direction="i_owe", text="send the deck")
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    assert payload[0]["direction"] == "i_owe"
    assert payload[0]["text"] == "send the deck"


def test_list_filters_direction(initialised_env: Path) -> None:
    _seed_commitment(initialised_env, direction="i_owe", text="mine")
    _seed_commitment(initialised_env, direction="owed_to_me", text="theirs")
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "list", "--direction", "i-owe", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert [c["direction"] for c in payload] == ["i_owe"]


def test_list_open_only_hides_resolved(initialised_env: Path) -> None:
    _seed_commitment(initialised_env, state="open", text="still open")
    _seed_commitment(initialised_env, state="resolved", text="done")
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "list", "--open", "--format", "json"])
    payload = json.loads(result.stdout)
    assert {c["text"] for c in payload} == {"still open"}


def test_list_rejects_bad_direction(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "list", "--direction", "sideways"])
    assert result.exit_code == 2


def test_list_rejects_bad_format(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "list", "--format", "yaml"])
    assert result.exit_code == 2


# ---- resolve / dismiss / reopen -------------------------------------------


def test_resolve_flips_state(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env)
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "resolve", cid])
    assert result.exit_code == 0, result.stdout
    listed = runner.invoke(app, ["commitment", "list", "--format", "json"])
    payload = json.loads(listed.stdout)
    assert payload[0]["state"] == "resolved"


def test_dismiss_with_reason(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env)
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "dismiss", cid, "--reason", "false positive"])
    assert result.exit_code == 0, result.stdout
    listed = runner.invoke(app, ["commitment", "list", "--format", "json"])
    assert json.loads(listed.stdout)[0]["state"] == "dismissed"


def test_reopen_resolved(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env, state="resolved")
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "reopen", cid])
    assert result.exit_code == 0, result.stdout
    listed = runner.invoke(app, ["commitment", "list", "--format", "json"])
    assert json.loads(listed.stdout)[0]["state"] == "open"


def test_resolve_missing_commitment_exit_1(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["commitment", "resolve", new_ulid()])
    assert result.exit_code == 1


def test_double_resolve_exit_1(initialised_env: Path) -> None:
    cid = _seed_commitment(initialised_env)
    runner = CliRunner()
    assert runner.invoke(app, ["commitment", "resolve", cid]).exit_code == 0
    assert runner.invoke(app, ["commitment", "resolve", cid]).exit_code == 1
