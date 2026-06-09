"""Tests for ``opshub slack cursor`` (Phase 22-E, ADR-0038).

Exercises the ``show`` / ``reset`` / ``backfill`` logic in
:mod:`opshub.cli._slack_cursor` against a fake source service (no SQLite),
plus a couple of CliRunner-level argument-validation checks.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack cursor tests require the 'connectors-slack' extras",
)

from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.core.errors import ConfigError
from opshub.core.time import parse_since, since_to_ts


class _FakeSource:
    """Minimal stand-in for :class:`SourceService` (cursor + observe)."""

    def __init__(self, cursor_value: str | None = None) -> None:
        self._cursor = cursor_value
        self.cursor_set_calls: list[dict[str, Any]] = []
        self.observed: list[str] = []

    def cursor_get(self, name: str) -> str | None:
        del name
        return self._cursor

    def cursor_set(self, name: str, value: str | None, *, sync_started: bool = False) -> None:
        self.cursor_set_calls.append({"name": name, "value": value, "sync_started": sync_started})
        self._cursor = value

    def observe(self, *, external_id: str, **_kw: Any) -> None:
        self.observed.append(external_id)


def _patch_source(monkeypatch: pytest.MonkeyPatch, source: _FakeSource) -> None:
    monkeypatch.setattr("opshub.cli._wiring.build_source_service", lambda actor="x": source)


# ----- show --------------------------------------------------------------


def test_cursor_show_json_renders_three_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.cli._slack_cursor import render_cursor_show

    source = _FakeSource(
        '{"backfill":{"C1":"100.000000"},"channels":{"C1":"200.000000"},"threads":{}}'
    )
    _patch_source(monkeypatch, source)

    import json

    out = json.loads(render_cursor_show(output_format="json"))
    assert out == {
        "channels": {"C1": "200.000000"},
        "backfill": {"C1": "100.000000"},
        "threads": {},
    }


def test_cursor_show_table_lists_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.cli._slack_cursor import render_cursor_show

    source = _FakeSource('{"channels":{"C1":"200.000000"},"backfill":{},"threads":{}}')
    _patch_source(monkeypatch, source)

    out = render_cursor_show(output_format="table")
    assert "[channels]" in out
    assert "[backfill]" in out
    assert "[threads]" in out
    assert "C1 = 200.000000" in out


def test_cursor_show_no_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.cli._slack_cursor import render_cursor_show

    _patch_source(monkeypatch, _FakeSource(None))
    out = render_cursor_show(output_format="table")
    assert "no cursor persisted yet" in out


# ----- reset -------------------------------------------------------------


def test_cursor_reset_channel_removes_only_that_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opshub.cli._slack_cursor import run_cursor_reset
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    source = _FakeSource(
        '{"channels":{"C1":"200.000000","C2":"300.000000"},'
        '"backfill":{"C1":"100.000000"},'
        '"threads":{"C1:t1":"150.000000","C2:t2":"250.000000"}}'
    )
    _patch_source(monkeypatch, source)

    removed, new_value = run_cursor_reset(channels=["C1"], reset_all=False)

    assert removed == 1
    state = _load_cursors(new_value)
    # C1 gone from every axis; C2 untouched.
    assert state["channels"] == {"C2": "300.000000"}
    assert state["backfill"] == {}
    assert state["threads"] == {"C2:t2": "250.000000"}
    # Persisted via a ConnectorSyncCompleted (sync_started=False).
    assert source.cursor_set_calls[-1]["sync_started"] is False


def test_cursor_reset_all_clears_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.cli._slack_cursor import run_cursor_reset
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    source = _FakeSource(
        '{"channels":{"C1":"200.000000"},"backfill":{"C1":"100.000000"},"threads":{}}'
    )
    _patch_source(monkeypatch, source)

    removed, new_value = run_cursor_reset(channels=None, reset_all=True)

    # Phase 23-A (#531): the ``--all`` path hard-drops without parsing the
    # prior cursor, so it returns -1 ("count unknown") rather than a
    # concrete entry count.
    assert removed == -1
    assert _load_cursors(new_value) == {"channels": {}, "backfill": {}, "threads": {}}


def test_cursor_reset_all_recovers_pre_20b_flat_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset --all`` recovers a pre-Phase-20-B flat-dict cursor (#531).

    The flat dict (``{<channel_id>: <ts>}``) cannot be parsed by
    ``_load_cursors`` — it raises ``ConfigError``. The ``--all`` path must
    NOT call ``_load_cursors`` (it overwrites the whole cursor anyway), so
    it is the working escape hatch the sync error now steers operators to.
    Persisting the empty compound through ``cursor_set`` also records a
    ``ConnectorSyncCompleted`` whose payload is the empty compound, so a
    later ``opshub projections rebuild`` replays the empty compound rather
    than regenerating the flat dict.
    """
    from opshub.cli._slack_cursor import run_cursor_reset
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    # The legacy flat-dict shape — no ``channels`` / ``threads`` wrapper.
    source = _FakeSource('{"C1":"1700000001.000100","C2":"1700000002.000200"}')
    _patch_source(monkeypatch, source)

    # Must not raise (a regression that re-introduced ``_load_cursors`` on
    # this path would raise ConfigError here, the dead-end #531 fixes).
    removed, new_value = run_cursor_reset(channels=None, reset_all=True)

    assert removed == -1
    assert _load_cursors(new_value) == {"channels": {}, "backfill": {}, "threads": {}}
    # The persisted replacement is the empty compound (rebuild-safe).
    assert source.cursor_set_calls[-1]["value"] == new_value
    assert source.cursor_set_calls[-1]["sync_started"] is False


def test_cli_reset_all_reports_cleared_for_flat_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: ``opshub slack cursor reset --all -y`` exits 0 on a flat dict.

    Drives the CLI surface (not just ``run_cursor_reset``) so the -1 →
    "all channel entries cleared" rendering is pinned and a flat-dict
    cursor does not surface a ConfigError at the CLI boundary (#531).
    """
    from typer.testing import CliRunner

    from opshub.cli.app import app

    source = _FakeSource('{"C1":"1700000001.000100"}')
    _patch_source(monkeypatch, source)

    result = CliRunner().invoke(app, ["slack", "cursor", "reset", "--all", "-y"])
    assert result.exit_code == 0, result.stdout
    assert "all channel entries cleared" in result.stdout


def test_cursor_reset_no_cursor_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.cli._slack_cursor import run_cursor_reset

    source = _FakeSource(None)
    _patch_source(monkeypatch, source)
    removed, _ = run_cursor_reset(channels=["C1"], reset_all=False)
    assert removed == 0


# ----- backfill ----------------------------------------------------------


def _patch_backfill_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gap_yields: list[tuple[str, RawSlackMessage, str | None]],
) -> dict[str, dict[str, str | None]]:
    """Patch SlackFetcher/SlackAuth used by ``backfill_channel`` and capture args."""
    from unittest.mock import MagicMock

    captured: dict[str, dict[str, str | None]] = {}

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        latest_per_channel: dict[str, str | None] | None = None,
        max_per_channel: int = 100,
        excludes: Any = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        del max_per_channel, excludes
        captured["cursor_per_channel"] = dict(cursor_per_channel)
        captured["latest_per_channel"] = dict(latest_per_channel or {})
        return iter(gap_yields)

    fake_cls = MagicMock()
    fake_cls.return_value.fetch_messages.side_effect = _fetch_messages
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackFetcher", fake_cls)

    fake_auth = MagicMock()
    fake_auth.return_value.token = "xoxb-fake"
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackAuth", fake_auth)
    return captured


def _raw(channel_id: str, ts: str) -> RawSlackMessage:
    return RawSlackMessage(
        channel_id=channel_id,
        channel_name="general",
        ts=ts,
        text="gap",
        user_id="U1",
        user_display_name="alice",
        permalink="https://acme.slack.com/archives/C1/p1",
        raw={},
    )


def test_cursor_backfill_fetches_window_and_advances_low_water(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opshub.cli._slack_cursor import run_cursor_backfill
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    since_ts = since_to_ts(parse_since("2026-01-01"))
    until_ts = since_to_ts(parse_since("2026-03-01"))
    gap_ts = since_to_ts(parse_since("2026-02-01"))

    # Post-feature channel: low-water recorded at 2026-03-01.
    source = _FakeSource(
        f'{{"channels":{{"C1":"{since_to_ts(parse_since("2026-06-01"))}"}},'
        f'"backfill":{{"C1":"{until_ts}"}},"threads":{{}}}}'
    )
    _patch_source(monkeypatch, source)
    captured = _patch_backfill_fetcher(monkeypatch, gap_yields=[("C1", _raw("C1", gap_ts), gap_ts)])

    # No --until → defaults to the recorded low-water (2026-03-01).
    observed = run_cursor_backfill(channel_id="C1", since="2026-01-01", until=None)

    assert observed == 1
    assert source.observed == [f"C1:{gap_ts}"]
    assert captured["cursor_per_channel"] == {"C1": since_ts}
    assert captured["latest_per_channel"] == {"C1": until_ts}
    # Low-water advanced down to the new floor.
    state = _load_cursors(source.cursor_set_calls[-1]["value"])
    assert state["backfill"] == {"C1": since_ts}


def test_cursor_backfill_requires_until_for_pre_feature_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opshub.cli._slack_cursor import run_cursor_backfill

    # Pre-feature channel: present in channels, no backfill entry.
    source = _FakeSource('{"channels":{"C1":"600.000000"},"threads":{}}')
    _patch_source(monkeypatch, source)
    _patch_backfill_fetcher(monkeypatch, gap_yields=[])

    with pytest.raises(ConfigError, match="no recorded low-water"):
        run_cursor_backfill(channel_id="C1", since="2026-01-01", until=None)


def test_cursor_backfill_rejects_since_not_older_than_until(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opshub.cli._slack_cursor import run_cursor_backfill

    source = _FakeSource(None)
    _patch_source(monkeypatch, source)
    _patch_backfill_fetcher(monkeypatch, gap_yields=[])

    with pytest.raises(ConfigError, match="must be strictly older"):
        run_cursor_backfill(channel_id="C1", since="2026-03-01", until="2026-01-01")


# ----- CLI argument validation -------------------------------------------


def test_cli_reset_rejects_all_with_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from opshub.cli.app import app

    # Should fail at arg validation before touching the DB.
    result = CliRunner().invoke(app, ["slack", "cursor", "reset", "--all", "--channel", "C1"])
    assert result.exit_code == 2


def test_cli_reset_requires_channel_or_all(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from opshub.cli.app import app

    result = CliRunner().invoke(app, ["slack", "cursor", "reset"])
    assert result.exit_code == 2
