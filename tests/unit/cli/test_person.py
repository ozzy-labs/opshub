"""Tests for ``opshub person`` (Phase 25-B, ADR-0043).

Cover the operator-facing person-axis surface end-to-end through
:class:`typer.testing.CliRunner` against a real migrated SQLite engine:

* ``person list`` — resolves author handles into persons, renders
  table + json, and is idempotent across repeated calls;
* ``person merge`` — merges two fuzzy-matched persons + error paths;
* ``person split`` — detaches one identity + malformed-arg / missing-id
  error paths.
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
from opshub.projections.sources import sources_table

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


def _seed_source(
    db_path: Path, *, connector: str, external_id: str, handle: str, display: str
) -> None:
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(sources_table).values(
                    id=new_ulid(),
                    connector_name=connector,
                    external_id=external_id,
                    source_type="message",
                    title="msg",
                    url=None,
                    summary=None,
                    observed_at=_T0,
                    updated_at=_T0,
                    fingerprint=None,
                    body="hello",
                    provenance_origin=None,
                    provenance_trust=None,
                    author_handle=handle,
                    author_display=display,
                    author_connector=connector,
                )
            )
    finally:
        engine.dispose()


# ---- list -----------------------------------------------------------------


def test_person_list_resolves_and_renders_table(initialised_env: Path) -> None:
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice"
    )
    _seed_source(initialised_env, connector="github", external_id="42", handle="bob", display="Bob")

    runner = CliRunner()
    result = runner.invoke(app, ["person", "list"])
    assert result.exit_code == 0, result.stdout
    assert "Alice" in result.stdout
    assert "Bob" in result.stdout
    assert "slack:U_a" in result.stdout
    assert "github:bob" in result.stdout


def test_person_list_json_format(initialised_env: Path) -> None:
    _seed_source(
        initialised_env,
        connector="google_mail",
        external_id="g1",
        handle="alice@example.com",
        display="Alice",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["person", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    person = payload[0]
    assert person["display_name"] == "Alice"
    assert person["identities"][0]["connector"] == "google_mail"
    assert person["identities"][0]["handle"] == "alice@example.com"


def test_person_list_is_idempotent(initialised_env: Path) -> None:
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice"
    )
    runner = CliRunner()
    first = runner.invoke(app, ["person", "list", "--format", "json"])
    second = runner.invoke(app, ["person", "list", "--format", "json"])
    assert first.exit_code == 0 and second.exit_code == 0
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert len(json.loads(second.stdout)) == 1


def test_person_list_empty(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["person", "list"])
    assert result.exit_code == 0
    assert "no persons" in result.stdout.lower()


def test_person_list_rejects_bad_format(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["person", "list", "--format", "yaml"])
    assert result.exit_code == 2


# ---- merge ----------------------------------------------------------------


def test_person_merge_collapses_two_persons(initialised_env: Path) -> None:
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alex"
    )
    _seed_source(
        initialised_env, connector="github", external_id="9", handle="alex", display="Alex"
    )
    runner = CliRunner()
    listed = runner.invoke(app, ["person", "list", "--format", "json"])
    persons = json.loads(listed.stdout)
    assert len(persons) == 2
    a, b = persons[0]["id"], persons[1]["id"]

    merged = runner.invoke(app, ["person", "merge", a, b])
    assert merged.exit_code == 0, merged.stdout
    assert min(a, b) in merged.stdout

    after = json.loads(runner.invoke(app, ["person", "list", "--format", "json"]).stdout)
    assert len(after) == 1
    assert len(after[0]["identities"]) == 2


def test_person_merge_self_exits_1(initialised_env: Path) -> None:
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alex"
    )
    runner = CliRunner()
    persons = json.loads(runner.invoke(app, ["person", "list", "--format", "json"]).stdout)
    pid = persons[0]["id"]
    result = runner.invoke(app, ["person", "merge", pid, pid])
    assert result.exit_code == 1


# ---- split ----------------------------------------------------------------


def test_person_split_detaches_identity(initialised_env: Path) -> None:
    _seed_source(
        initialised_env, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alex"
    )
    _seed_source(
        initialised_env, connector="github", external_id="9", handle="alex", display="Alex"
    )
    runner = CliRunner()
    persons = json.loads(runner.invoke(app, ["person", "list", "--format", "json"]).stdout)
    a, b = persons[0]["id"], persons[1]["id"]
    runner.invoke(app, ["person", "merge", a, b])
    assert len(json.loads(runner.invoke(app, ["person", "list", "--format", "json"]).stdout)) == 1

    result = runner.invoke(app, ["person", "split", "github:alex"])
    assert result.exit_code == 0, result.stdout
    after = json.loads(runner.invoke(app, ["person", "list", "--format", "json"]).stdout)
    assert len(after) == 2


def test_person_split_rejects_malformed_identity(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["person", "split", "no-colon"])
    assert result.exit_code == 2


def test_person_split_missing_identity_exits_1(initialised_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["person", "split", "slack:U_nope"])
    assert result.exit_code == 1
