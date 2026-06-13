"""Tests for ``opshub slack status`` (Phase 23-F #536; per-workspace Phase 24-C).

Exercises :func:`opshub.cli._slack_status.render_status` against a fake source
service + a patched config (no SQLite), plus a couple of CliRunner-level
checks (status runs end-to-end; the old ``cursor show`` is gone).

Phase 24-C ([ADR-0041](../../../docs/adr/0041-slack-multi-workspace.md)
§(f)): the cursor is the per-alias envelope and ``status`` renders one
block per configured workspace (plus cursor-only orphans), with
``--workspace`` narrowing the view.
"""

from __future__ import annotations

import json
import types
from typing import Any

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack status tests require the 'connectors-slack' extras",
)

from opshub.core.errors import ConfigError
from opshub.core.time import parse_since, since_to_ts


class _FakeSource:
    """Minimal stand-in for :class:`SourceService` (cursor_get only)."""

    def __init__(self, cursor_value: str | None = None) -> None:
        self._cursor = cursor_value

    def cursor_get(self, name: str) -> str | None:
        del name
        return self._cursor


def _patch_source(monkeypatch: pytest.MonkeyPatch, source: _FakeSource) -> None:
    monkeypatch.setattr("opshub.cli._wiring.build_source_service", lambda actor="x": source)


def _patch_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    channels: list[str] | None = None,
    workspaces: dict[str, list[str]] | None = None,
    sync_since: str | None = None,
    backfill_on_floor_lower: bool = True,
) -> None:
    """Patch settings; ``channels`` is the single-``acme``-workspace shorthand."""
    from opshub.core.config import SlackConnectorSettings

    if workspaces is None:
        workspaces = {"acme": channels or []}
    slack = SlackConnectorSettings.model_validate(
        {
            "workspaces": {alias: {"channels": ids} for alias, ids in workspaces.items()},
            "sync_since": sync_since,
            "backfill_on_floor_lower": backfill_on_floor_lower,
        }
    )
    fake = types.SimpleNamespace(connectors=types.SimpleNamespace(slack=slack))
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake)


def _names_empty(_factory: object, _team_id: str | None) -> dict[str, str]:
    return {}


def _patch_no_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degrade channel-name resolution to ids (no DB in unit tests)."""
    monkeypatch.setattr("opshub.cli._slack_status._channel_names", _names_empty)


def _ts(date: str) -> str:
    return since_to_ts(parse_since(date))


def _state_json(
    *,
    channels: str = "{}",
    backfill: str = "{}",
    threads: str = "{}",
    team_id: str | None = None,
) -> str:
    """Compose one alias's inner state JSON fragment."""
    team = "null" if team_id is None else f'"{team_id}"'
    return f'{{"channels":{channels},"backfill":{backfill},"threads":{threads},"team_id":{team}}}'


def _envelope(state: str, alias: str = "acme") -> str:
    """Wrap one alias's state JSON in the Phase 24-C envelope."""
    return f'{{"workspaces":{{"{alias}":{state}}}}}'


def _render(
    *, output_format: str = "table", verbose: bool = False, workspace: str | None = None
) -> str:
    from opshub.cli._slack_status import render_status

    return render_status(output_format=output_format, verbose=verbose, workspace=workspace)


# ----- daily view --------------------------------------------------------


def test_status_no_cursor_shows_unsynced(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "未取得" in out
    assert "no cursor persisted yet" in out


def test_status_no_workspaces_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero workspace tables → the empty-state hint names the table form."""
    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, workspaces={})
    _patch_no_names(monkeypatch)

    out = _render()
    assert "configured workspaces なし" in out
    assert "[connectors.slack.workspaces.<alias>]" in out


def test_status_workspace_with_no_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, channels=[])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "=== workspace: acme" in out
    assert "channels なし" in out
    assert "[connectors.slack.workspaces.acme]" in out


def test_status_high_and_low_water_as_separate_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        _envelope(
            _state_json(
                channels=f'{{"C1":"{_ts("2026-06-09")}"}}',
                backfill=f'{{"C1":"{_ts("2026-01-15")}"}}',
                threads='{"C1:t1":"1.0","C1:t2":"2.0"}',
                team_id="T1",
            )
        )
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "前進取得済み:   〜2026-06-09" in out
    assert "過去取得下限:   2026-01-15" in out
    assert "追跡中スレッド: 2" in out
    # Never asserts a continuous covered range.
    assert "2026-01-15〜2026-06-09" not in out


def test_status_full_backfill_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(_envelope(_state_json(channels=f'{{"C1":"{_ts("2026-06-09")}"}}')))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "先頭まで" in out


def test_status_configured_but_unsynced_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(_envelope(_state_json(channels=f'{{"C1":"{_ts("2026-06-09")}"}}')))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1", "C2"])
    _patch_no_names(monkeypatch)

    out = _render()
    # C2 is configured but has no cursor entry.
    assert "C2" in out
    assert "未取得" in out


def test_status_pending_backfill_fires_when_floor_below_low_water(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FakeSource(
        _envelope(
            _state_json(
                channels=f'{{"C1":"{_ts("2026-06-09")}"}}',
                backfill=f'{{"C1":"{_ts("2026-03-01")}"}}',
            )
        )
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_no_names(monkeypatch)

    out = _render()
    assert "次回 sync で過去取り直し予定: C1" in out
    assert "floor 2026-01-01" in out


def test_status_pending_backfill_uses_workspace_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 24-C: the prediction resolves the 3-step floor (workspace wins)."""
    from opshub.core.config import SlackConnectorSettings

    slack = SlackConnectorSettings.model_validate(
        {
            "workspaces": {"acme": {"channels": ["C1"], "sync_since": "2026-02-01"}},
            "sync_since": "2026-05-01",
        }
    )
    fake = types.SimpleNamespace(connectors=types.SimpleNamespace(slack=slack))
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake)
    source = _FakeSource(
        _envelope(
            _state_json(
                channels=f'{{"C1":"{_ts("2026-06-09")}"}}',
                backfill=f'{{"C1":"{_ts("2026-03-01")}"}}',
            )
        )
    )
    _patch_source(monkeypatch, source)
    _patch_no_names(monkeypatch)

    out = _render()
    # Workspace floor 2026-02-01 < low-water 2026-03-01 → pending fires
    # (with the connector-wide 2026-05-01 it would not).
    assert "次回 sync で過去取り直し予定: C1" in out
    assert "floor 2026-02-01" in out


def test_status_no_pending_backfill_when_floor_above_low_water(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FakeSource(
        _envelope(
            _state_json(
                channels=f'{{"C1":"{_ts("2026-06-09")}"}}',
                backfill=f'{{"C1":"{_ts("2026-01-01")}"}}',
            )
        )
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"], sync_since="2026-06-01")
    _patch_no_names(monkeypatch)

    out = _render()
    assert "次回 sync で過去取り直し予定" not in out


def test_status_pending_backfill_suppressed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        _envelope(
            _state_json(
                channels=f'{{"C1":"{_ts("2026-06-09")}"}}',
                backfill=f'{{"C1":"{_ts("2026-03-01")}"}}',
            )
        )
    )
    _patch_source(monkeypatch, source)
    _patch_config(
        monkeypatch, channels=["C1"], sync_since="2026-01-01", backfill_on_floor_lower=False
    )
    _patch_no_names(monkeypatch)

    out = _render()
    assert "次回 sync で過去取り直し予定" not in out


def test_status_contiguity_disclaimer_present(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(_envelope(_state_json(channels=f'{{"C1":"{_ts("2026-06-09")}"}}')))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "連続被覆は保証しない" in out
    assert "--verbose" in out


def test_status_channel_name_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(_envelope(_state_json(channels=f'{{"C1":"{_ts("2026-06-09")}"}}')))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])

    def _names_general(_factory: object, _team_id: str | None) -> dict[str, str]:
        return {"C1": "general"}

    monkeypatch.setattr("opshub.cli._slack_status._channel_names", _names_general)

    out = _render()
    assert "#general (C1)" in out


def test_status_verbose_dumps_raw_axes_per_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(_envelope(_state_json(channels='{"C1":"200.000000"}')))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])

    out = _render(verbose=True)
    assert "=== workspace: acme" in out
    assert "[channels]" in out
    assert "[backfill]" in out
    assert "[threads]" in out
    assert "C1 = 200.000000" in out


def test_status_json_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        _envelope(
            _state_json(
                channels=f'{{"C1":"{_ts("2026-06-09")}"}}',
                backfill=f'{{"C1":"{_ts("2026-03-01")}"}}',
                team_id="T1",
            )
        )
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_no_names(monkeypatch)

    payload: dict[str, Any] = json.loads(_render(output_format="json"))
    assert payload["synced"] is True
    block = payload["workspaces"]["acme"]
    assert [r["channel_id"] for r in block["channels"]] == ["C1"]
    assert block["pending_backfill"][0]["channel_id"] == "C1"
    assert block["bound_team_id"] == "T1"
    assert block["configured"] is True


def test_status_legacy_pre_24_cursor_raises_configerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pre-Phase-24 shape (no workspaces nest) — _load_cursors rejects it,
    # status surfaces the same ConfigError the sync path raises (exit 1).
    _patch_source(monkeypatch, _FakeSource('{"C1":"1700000001.000100"}'))
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    with pytest.raises(ConfigError):
        _render()


# ----- multi-workspace blocks (Phase 24-C, ADR-0041 §(f)) ----------------


def test_status_renders_one_block_per_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        '{"workspaces":{'
        f'"acme":{_state_json(channels=f'{{"C1":"{_ts("2026-06-09")}"}}', team_id="T1")},'
        f'"oss":{_state_json(team_id="T2")}'
        "}}"
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, workspaces={"acme": ["C1"], "oss": ["C2"]})
    _patch_no_names(monkeypatch)

    out = _render()
    assert "Slack 取得状況 (2 workspace)" in out
    assert "=== workspace: acme — team_id=T1" in out
    assert "=== workspace: oss — team_id=T2" in out


def test_status_workspace_flag_narrows_view(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"workspaces":{{"acme":{_state_json(team_id="T1")},"oss":{_state_json(team_id="T2")}}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, workspaces={"acme": ["C1"], "oss": ["C2"]})
    _patch_no_names(monkeypatch)

    out = _render(workspace="oss")
    assert "=== workspace: oss" in out
    assert "=== workspace: acme" not in out


def test_status_unknown_workspace_flag_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    with pytest.raises(ConfigError, match="unknown Slack workspace alias 'typo'"):
        _render(workspace="typo")


def test_status_orphan_cursor_entry_names_cleanup_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cursor entry whose alias left the config is shown with the cleanup hint."""
    source = _FakeSource(_envelope(_state_json(team_id="T-GONE"), alias="gone"))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "=== workspace: gone" in out
    assert "cursor reset --workspace gone --all" in out


def test_status_unbound_workspace_when_no_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(_envelope(_state_json(channels=f'{{"C1":"{_ts("2026-06-09")}"}}')))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "未 bind" in out
    assert "ADR-0041" in out


def test_status_verbose_shows_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(_envelope(_state_json(channels='{"C1":"200.000000"}', team_id="T-WS1")))
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])

    out = _render(verbose=True)
    assert "[team_id] T-WS1" in out


# ----- CLI surface -------------------------------------------------------


def test_cli_status_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from opshub.cli.app import app

    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, workspaces={})
    _patch_no_names(monkeypatch)

    result = CliRunner().invoke(app, ["slack", "status"])
    assert result.exit_code == 0, result.stdout
    assert "Slack 取得状況" in result.stdout


def test_cli_cursor_show_removed() -> None:
    from typer.testing import CliRunner

    from opshub.cli.app import app

    # `cursor show` was promoted to `slack status`; the subcommand is gone.
    result = CliRunner().invoke(app, ["slack", "cursor", "show"])
    assert result.exit_code != 0


# ----- channel-name lookup is team-scoped (Phase 24-D, ADR-0041 §(g)) -----


def test_channel_names_filters_by_team_id(tmp_path: object) -> None:
    """``_channel_names`` only returns the requested workspace's labels.

    Phase 24-D re-keys the demand digest on ``(team_id, channel_id,
    demand_kind)``; the status name-resolution sugar filters on the
    block's bound team_id so a channel id that collides across
    workspaces can no longer pick up the *other* workspace's name.
    """
    from pathlib import Path

    from sqlalchemy import insert

    from opshub.cli._slack_status import (
        _channel_names,  # pyright: ignore[reportPrivateUsage]
    )
    from opshub.db.engine import create_engine_for_sqlite
    from opshub.projections.slack_demand_digest import slack_demand_digest_table
    from opshub.projections.sources import sources_table

    assert isinstance(tmp_path, Path)
    engine = create_engine_for_sqlite(tmp_path / "digest.sqlite")
    try:
        sources_table.create(engine)
        slack_demand_digest_table.create(engine)
        from datetime import UTC, datetime

        now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
        with engine.begin() as conn:
            conn.execute(
                insert(slack_demand_digest_table).values(
                    [
                        {
                            "team_id": "T-A",
                            "channel_id": "C1",
                            "channel_type": "public",
                            "channel_name": "general-a",
                            "demand_kind": "mention",
                            "last_demand_ts": 1700000000.0,
                            "updated_at": now,
                        },
                        {
                            "team_id": "T-B",
                            "channel_id": "C1",
                            "channel_type": "public",
                            "channel_name": "general-b",
                            "demand_kind": "mention",
                            "last_demand_ts": 1700000001.0,
                            "updated_at": now,
                        },
                    ]
                )
            )

        # NOTE: ``_channel_names`` disposes the engine it builds; hand it
        # a factory returning a fresh engine per call so each lookup is
        # self-contained (mirrors the production ``build_engine`` shape).
        def _factory() -> object:
            return create_engine_for_sqlite(tmp_path / "digest.sqlite")

        assert _channel_names(_factory, "T-A") == {"C1": "general-a"}
        assert _channel_names(_factory, "T-B") == {"C1": "general-b"}
        # Unbound workspace (never synced) → short-circuit empty map.
        assert _channel_names(_factory, None) == {}
    finally:
        engine.dispose()
