"""Tests for ``opshub slack status`` (Phase 23-F, #536).

Exercises :func:`opshub.cli._slack_status.render_status` against a fake source
service + a patched config (no SQLite), plus a couple of CliRunner-level
checks (status runs end-to-end; the old ``cursor show`` is gone).
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
    channels: list[str],
    sync_since: str | None = None,
    backfill_on_floor_lower: bool = True,
) -> None:
    from opshub.core.config import SlackChannelSpec, SlackConnectorSettings

    slack = SlackConnectorSettings(
        channels=[SlackChannelSpec(id=c) for c in channels],
        sync_since=sync_since,
        backfill_on_floor_lower=backfill_on_floor_lower,
    )
    fake = types.SimpleNamespace(connectors=types.SimpleNamespace(slack=slack))
    monkeypatch.setattr("opshub.core.config.OpsHubSettings", lambda: fake)


def _names_empty(_factory: object) -> dict[str, str]:
    return {}


def _patch_no_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degrade channel-name resolution to ids (no DB in unit tests)."""
    monkeypatch.setattr("opshub.cli._slack_status._channel_names", _names_empty)


def _ts(date: str) -> str:
    return since_to_ts(parse_since(date))


def _render(*, output_format: str = "table", verbose: bool = False) -> str:
    from opshub.cli._slack_status import render_status

    return render_status(output_format=output_format, verbose=verbose)


# ----- daily view --------------------------------------------------------


def test_status_no_cursor_shows_unsynced(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "未取得" in out
    assert "no cursor persisted yet" in out


def test_status_no_channels_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, channels=[])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "configured channels なし" in out


def test_status_high_and_low_water_as_separate_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},'
        f'"backfill":{{"C1":"{_ts("2026-01-15")}"}},'
        f'"threads":{{"C1:t1":"1.0","C1:t2":"2.0"}}}}'
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
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},"backfill":{{}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "先頭まで" in out


def test_status_configured_but_unsynced_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},"backfill":{{}},"threads":{{}}}}'
    )
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
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},'
        f'"backfill":{{"C1":"{_ts("2026-03-01")}"}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_no_names(monkeypatch)

    out = _render()
    assert "次回 sync で過去取り直し予定: C1" in out
    assert "floor 2026-01-01" in out


def test_status_no_pending_backfill_when_floor_above_low_water(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},'
        f'"backfill":{{"C1":"{_ts("2026-01-01")}"}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"], sync_since="2026-06-01")
    _patch_no_names(monkeypatch)

    out = _render()
    assert "次回 sync で過去取り直し予定" not in out


def test_status_pending_backfill_suppressed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},'
        f'"backfill":{{"C1":"{_ts("2026-03-01")}"}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(
        monkeypatch, channels=["C1"], sync_since="2026-01-01", backfill_on_floor_lower=False
    )
    _patch_no_names(monkeypatch)

    out = _render()
    assert "次回 sync で過去取り直し予定" not in out


def test_status_contiguity_disclaimer_present(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},"backfill":{{}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "連続被覆は保証しない" in out
    assert "--verbose" in out


def test_status_channel_name_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},"backfill":{{}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])

    def _names_general(_factory: object) -> dict[str, str]:
        return {"C1": "general"}

    monkeypatch.setattr("opshub.cli._slack_status._channel_names", _names_general)

    out = _render()
    assert "#general (C1)" in out


def test_status_verbose_dumps_raw_three_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource('{"channels":{"C1":"200.000000"},"backfill":{},"threads":{}}')
    _patch_source(monkeypatch, source)

    out = _render(verbose=True)
    assert "[channels]" in out
    assert "[backfill]" in out
    assert "[threads]" in out
    assert "C1 = 200.000000" in out


def test_status_json_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},'
        f'"backfill":{{"C1":"{_ts("2026-03-01")}"}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"], sync_since="2026-01-01")
    _patch_no_names(monkeypatch)

    payload: dict[str, Any] = json.loads(_render(output_format="json"))
    assert payload["synced"] is True
    assert [r["channel_id"] for r in payload["channels"]] == ["C1"]
    assert payload["pending_backfill"][0]["channel_id"] == "C1"


def test_status_legacy_flat_dict_raises_configerror(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pre-Phase-20-B flat dict — _load_cursors rejects it, status surfaces
    # the same ConfigError the sync path raises (the CLI maps it to exit 1).
    _patch_source(monkeypatch, _FakeSource('{"C1":"1700000001.000100"}'))
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    with pytest.raises(ConfigError):
        _render()


# ----- CLI surface -------------------------------------------------------


def test_cli_status_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from opshub.cli.app import app

    _patch_source(monkeypatch, _FakeSource(None))
    _patch_config(monkeypatch, channels=[])
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


# ----- Phase 23-H bound-workspace display (#538, ADR-0039) ---------------


def test_status_shows_bound_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},"backfill":{{}},'
        f'"threads":{{}},"team_id":"T-WS1"}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "bound workspace: team_id=T-WS1" in out


def test_status_unbound_workspace_when_no_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        f'{{"channels":{{"C1":"{_ts("2026-06-09")}"}},"backfill":{{}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    _patch_config(monkeypatch, channels=["C1"])
    _patch_no_names(monkeypatch)

    out = _render()
    assert "bound workspace: 未取得" in out


def test_status_verbose_shows_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        '{"channels":{"C1":"200.000000"},"backfill":{},"threads":{},"team_id":"T-WS1"}'
    )
    _patch_source(monkeypatch, source)

    out = _render(verbose=True)
    assert "[team_id] T-WS1" in out
