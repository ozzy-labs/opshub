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


# ----- show (promoted to ``opshub slack status``, Phase 23-F #536) -------
# The read-only cursor view moved to ``opshub slack status`` (raw 3-axis dump
# behind ``status --verbose``); see ``tests/unit/cli/test_slack_status.py``.


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
    assert _load_cursors(new_value) == {
        "channels": {},
        "backfill": {},
        "threads": {},
        "team_id": None,
    }


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
    assert _load_cursors(new_value) == {
        "channels": {},
        "backfill": {},
        "threads": {},
        "team_id": None,
    }
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
    # Phase 24-B (ADR-0041 §(a)): ``backfill_channel`` now runs the
    # single-workspace bind guard before fetching, so the auth double must
    # resolve a workspace identity (mirrors the sync-path test helper).
    fake_auth.return_value.test_token.return_value = {
        "team": "t",
        "team_id": "T-test",
        "user": "u",
        "user_id": "U1",
        "principal": "bot",
    }
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackAuth", fake_auth)
    return captured


def _raw(channel_id: str, ts: str) -> RawSlackMessage:
    return RawSlackMessage(
        team_id="T-test",
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
    assert source.observed == [f"T-test:C1:{gap_ts}"]
    assert captured["cursor_per_channel"] == {"C1": since_ts}
    assert captured["latest_per_channel"] == {"C1": until_ts}
    # Low-water advanced down to the new floor.
    state = _load_cursors(source.cursor_set_calls[-1]["value"])
    assert state["backfill"] == {"C1": since_ts}


def test_cursor_backfill_requires_until_when_no_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opshub.cli._slack_cursor import run_cursor_backfill

    # Pre-feature channel (no backfill entry) AND no ingested messages to
    # infer --until from → still requires explicit --until (Phase 23-F-2).
    source = _FakeSource('{"channels":{"C1":"600.000000"},"threads":{}}')
    _patch_source(monkeypatch, source)
    _patch_backfill_fetcher(monkeypatch, gap_yields=[])

    def _no_oldest(_factory: object, *, channel_id: str) -> str | None:
        return None

    monkeypatch.setattr("opshub.cli._slack_cursor._oldest_observed_ts", _no_oldest)

    with pytest.raises(ConfigError, match="no ingested messages"):
        run_cursor_backfill(channel_id="C1", since="2026-01-01", until=None)


def test_cursor_backfill_defaults_until_to_oldest_ingested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opshub.cli._slack_cursor import run_cursor_backfill
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    since_ts = since_to_ts(parse_since("2026-01-01"))
    oldest_ts = since_to_ts(parse_since("2026-03-01"))
    gap_ts = since_to_ts(parse_since("2026-02-01"))
    head_ts = since_to_ts(parse_since("2026-06-01"))

    # Pre-feature channel: no backfill entry → --until inferred from the
    # oldest ingested message ts (2026-03-01) rather than erroring.
    source = _FakeSource(f'{{"channels":{{"C1":"{head_ts}"}},"threads":{{}}}}')
    _patch_source(monkeypatch, source)
    captured = _patch_backfill_fetcher(monkeypatch, gap_yields=[("C1", _raw("C1", gap_ts), gap_ts)])

    def _oldest(_factory: object, *, channel_id: str) -> str | None:
        return oldest_ts

    monkeypatch.setattr("opshub.cli._slack_cursor._oldest_observed_ts", _oldest)

    observed = run_cursor_backfill(channel_id="C1", since="2026-01-01", until=None)

    assert observed == 1
    # --until defaulted to the oldest ingested ts (upper bound of the gap).
    assert captured["latest_per_channel"] == {"C1": oldest_ts}
    assert captured["cursor_per_channel"] == {"C1": since_ts}
    state = _load_cursors(source.cursor_set_calls[-1]["value"])
    assert state["backfill"] == {"C1": since_ts}


def test_oldest_observed_ts_returns_numeric_min(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import create_engine, insert

    from opshub.cli._slack_cursor import _oldest_observed_ts  # pyright: ignore[reportPrivateUsage]
    from opshub.projections.sources import sources_table

    engine = create_engine("sqlite://")
    sources_table.metadata.create_all(engine)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    rows = [
        # C1: two messages — lexical max ("9...") is numerically smaller than
        # "1700...", so a lexical min would pick the wrong one; assert numeric.
        # Phase 24-B (ADR-0041 §(a)): external_id is the 3-token
        # ``{team_id}:{channel_id}:{ts}`` shape; the channel filter matches
        # the *middle* token.
        ("s1", "slack", "T1:C1:1700000500.000000"),
        ("s2", "slack", "T1:C1:999999999.000000"),
        ("s3", "slack", "T1:C2:1700000000.000000"),
        ("s4", "github", "T1:C1:1700000001.000000"),
        # Legacy 2-token row (pre-Phase-24 ingest, only present when the
        # operator skipped the ADR-0041 §(e) DB re-init): never matches the
        # ``%:C1:%`` middle-token pattern and is ignored even though its ts
        # would be the numeric min.
        ("s5", "slack", "C1:0.500000"),
    ]
    with engine.begin() as conn:
        for sid, connector, ext in rows:
            conn.execute(
                insert(sources_table).values(
                    id=sid,
                    connector_name=connector,
                    external_id=ext,
                    source_type="slack_message",
                    title="t",
                    observed_at=now,
                    updated_at=now,
                    body="b",
                )
            )

    # Numeric min over C1's slack rows = 999999999 (not the lexical-min path).
    assert _oldest_observed_ts(lambda: engine, channel_id="C1") == "999999999.000000"


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


def test_cursor_reset_channel_preserves_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-channel reset (Phase 23-H, #538) does NOT unbind the workspace team_id."""
    from opshub.cli._slack_cursor import run_cursor_reset
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    source = _FakeSource(
        '{"channels":{"C1":"200.000000","C2":"300.000000"},'
        '"backfill":{},"threads":{},"team_id":"T-WS1"}'
    )
    _patch_source(monkeypatch, source)

    _removed, new_value = run_cursor_reset(channels=["C1"], reset_all=False)

    state = _load_cursors(new_value)
    # team_id survives a per-channel reset (only --all cold-starts the bind).
    assert state["team_id"] == "T-WS1"
    assert state["channels"] == {"C2": "300.000000"}
