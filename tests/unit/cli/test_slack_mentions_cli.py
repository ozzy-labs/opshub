"""Tests for ``opshub slack mentions list`` (Phase 18-B, ADR-0033).

Exercises the debug CLI end-to-end against a tmp-isolated SQLite DB:

* ``opshub init`` provisions schema (migration 0029 applied).
* A small set of digest rows is seeded by writing directly to the
  ``slack_demand_digest`` table — the projection logic itself is
  pinned in
  :mod:`tests.unit.projections.test_slack_demand_digest`; this file
  focuses on CLI argument parsing, output shape, and filtering.
* Each test invokes the command via :class:`typer.testing.CliRunner`
  so the real Typer wiring + exit-code path are covered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import insert
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db import create_engine_for_sqlite
from opshub.projections.slack_demand_digest import slack_demand_digest_table


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OpsHub env vars at ``tmp_path`` and return the SQLite path."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))

    return db_path


def _seed_digest_rows(db_path: Path) -> None:
    """Seed the digest table with three rows spanning channel types + kinds.

    Row inventory:

    * public mention (``C100AAA``, ts 1700000200)
    * private mention (``G200BBB``, ts 1700000100)
    * DM (``D300CCC``, ts 1700000300)

    Sort order in the CLI defaults to ``last_demand_ts DESC`` so the
    DM row lands first, public mention second, private mention last.
    """
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
            conn.execute(
                insert(slack_demand_digest_table).values(
                    [
                        {
                            "channel_id": "C100AAA",
                            "channel_type": "public",
                            "channel_name": "general",
                            "demand_kind": "mention",
                            "last_demand_ts": 1700000200.0,
                            "last_demand_user_id": None,
                            "last_demand_excerpt": "hey <@U_SELF> ping",
                            "last_demand_permalink": "https://example.slack.com/p1",
                            "last_source_id": None,
                            "updated_at": now,
                        },
                        {
                            "channel_id": "G200BBB",
                            "channel_type": "private",
                            "channel_name": "leadership",
                            "demand_kind": "mention",
                            "last_demand_ts": 1700000100.0,
                            "last_demand_user_id": None,
                            "last_demand_excerpt": "private mention",
                            "last_demand_permalink": None,
                            "last_source_id": None,
                            "updated_at": now,
                        },
                        {
                            "channel_id": "D300CCC",
                            "channel_type": "im",
                            "channel_name": None,
                            "demand_kind": "dm",
                            "last_demand_ts": 1700000300.0,
                            "last_demand_user_id": None,
                            "last_demand_excerpt": "hi!",
                            "last_demand_permalink": None,
                            "last_source_id": None,
                            "updated_at": now,
                        },
                    ]
                )
            )
    finally:
        engine.dispose()


# ---- help / shape ---------------------------------------------------------


def test_slack_mentions_help_lists_list() -> None:
    """``opshub slack mentions --help`` lists the ``list`` subcommand."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "mentions", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "list" in result.stdout


def test_slack_mentions_list_help_resolves_cleanly() -> None:
    """``opshub slack mentions list --help`` resolves cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "mentions", "list", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "--format" in result.stdout
    assert "--types" in result.stdout
    assert "--demand-kind" in result.stdout
    assert "--limit" in result.stdout


# ---- table format ---------------------------------------------------------


def test_list_table_default_shows_all_rows_sorted_desc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default table output renders every seeded row, newest demand first."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_digest_rows(db_path)

    result = runner.invoke(app, ["slack", "mentions", "list"])
    assert result.exit_code == 0, result.stdout
    # Header columns
    assert "CHANNEL" in result.stdout
    assert "TYPE" in result.stdout
    assert "KIND" in result.stdout
    assert "LAST_DEMAND" in result.stdout
    # The DM row has the newest ts; it should appear before the public mention.
    dm_pos = result.stdout.find("D300CCC")
    public_pos = result.stdout.find("C100AAA")
    private_pos = result.stdout.find("G200BBB")
    assert dm_pos != -1 and public_pos != -1 and private_pos != -1
    assert dm_pos < public_pos < private_pos


# ---- filter: --types ------------------------------------------------------


def test_list_types_filter_includes_only_requested_kinds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--types im,private`` excludes public-channel rows."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_digest_rows(db_path)

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--types", "im,private"],
    )
    assert result.exit_code == 0, result.stdout
    assert "D300CCC" in result.stdout  # im
    assert "G200BBB" in result.stdout  # private
    assert "C100AAA" not in result.stdout  # public excluded


def test_list_types_filter_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown ``--types`` value surfaces as a ValidationError (exit 2)."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--types", "im,bogus"],
    )
    assert result.exit_code != 0
    # ValidationError → exit code 2 via cli.app.main wrapping (the
    # runner surfaces the exception on ``result.exception``).
    assert "bogus" in str(result.exception)


# ---- filter: --demand-kind ------------------------------------------------


def test_list_demand_kind_filter_dm_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--demand-kind dm`` returns only DM rows."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_digest_rows(db_path)

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--demand-kind", "dm"],
    )
    assert result.exit_code == 0, result.stdout
    assert "D300CCC" in result.stdout
    assert "C100AAA" not in result.stdout
    assert "G200BBB" not in result.stdout


def test_list_demand_kind_filter_mention_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--demand-kind mention`` returns only mention rows (both channels)."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_digest_rows(db_path)

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--demand-kind", "mention"],
    )
    assert result.exit_code == 0, result.stdout
    assert "C100AAA" in result.stdout
    assert "G200BBB" in result.stdout
    assert "D300CCC" not in result.stdout


# ---- JSON format ----------------------------------------------------------


def test_list_format_json_returns_full_row_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--format json`` returns the full row schema as a JSON array."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_digest_rows(db_path)

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--format", "json"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 3
    # Each row carries every column documented in the JSON schema —
    # operator + automation alike rely on this stable shape.
    expected_keys = {
        "channel_id",
        "channel_type",
        "channel_name",
        "demand_kind",
        "last_demand_ts",
        "last_demand_user_id",
        "last_demand_excerpt",
        "last_demand_permalink",
        "last_source_id",
        "updated_at",
    }
    for entry in payload:
        assert expected_keys.issubset(entry.keys())
    # Sort order is preserved in the JSON output (DM first).
    assert payload[0]["channel_id"] == "D300CCC"


def test_list_format_unknown_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unknown ``--format`` value raises a ValidationError (exit 2)."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--format", "xml"],
    )
    assert result.exit_code != 0
    assert "xml" in str(result.exception)


# ---- empty-state ----------------------------------------------------------


def test_list_empty_table_only_renders_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty projection still renders the table header for piping into ``head``."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(app, ["slack", "mentions", "list"])
    assert result.exit_code == 0, result.stdout
    assert "CHANNEL" in result.stdout
    # No seeded row → no body line; the output is the header only.
    assert "D300CCC" not in result.stdout


def test_list_empty_json_returns_empty_array(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty projection renders ``[]`` under ``--format json``."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--format", "json"],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


# ---- limit ----------------------------------------------------------------


def test_list_limit_caps_row_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--limit 1`` returns only the most recent demand row."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_digest_rows(db_path)

    result = runner.invoke(
        app,
        ["slack", "mentions", "list", "--format", "json", "--limit", "1"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    assert payload[0]["channel_id"] == "D300CCC"
