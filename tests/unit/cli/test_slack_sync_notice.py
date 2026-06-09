"""Tests for the Phase 23-E (#535) no-op ``opshub slack sync`` notice.

The connector logs a *structured* warning when ``channels`` is empty, but
that is invisible on a TTY — a sync that exits 0 with "0 items observed"
looks like a working setup. ``opshub.cli.slack._emit_slack_sync_notice``
prints a plain-text ``notice:`` line on stderr naming the reason and the
fix for the two configured-no-op states (disabled connector / empty
channels), suppressed by ``-q`` / ``--quiet``.

These tests exercise the helper directly (no DB / network) plus the
log-level gate.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

import opshub.core.config as opshub_config
from opshub.cli import slack as slack_cli
from opshub.core.config import (
    ConnectorSettings,
    OpsHubSettings,
    SlackConnectorSettings,
)


@pytest.fixture(autouse=True)
def _info_level_root_logger() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Pin the root logger at INFO so the notice gate is deterministic.

    The Phase 23-E notice prints only when the effective level is
    ``<= INFO``; tests that want the suppressed case raise it to WARNING
    themselves. We save / restore so cross-test state does not leak.
    """
    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.setLevel(saved)


def _settings(*, enabled: bool, channels: list[str]) -> OpsHubSettings:
    slack = SlackConnectorSettings(enabled=enabled, channels=channels)  # type: ignore[arg-type]
    return OpsHubSettings(connectors=ConnectorSettings(slack=slack))


def test_disabled_with_channels_warns_but_is_not_noop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``enabled = false`` is informational: with channels the sync runs.

    The CLI driver treats ``enabled`` as informational (the connector
    module docstring), so a disabled connector with channels actually
    syncs. We surface the off-state heads-up but must NOT mark it a no-op
    (that would wrongly suppress the post-sync hint and imply 0 items).
    """
    monkeypatch.setattr(
        opshub_config, "OpsHubSettings", lambda: _settings(enabled=False, channels=["C1"])
    )

    is_noop = slack_cli._emit_slack_sync_notice()  # pyright: ignore[reportPrivateUsage]

    assert is_noop is False
    err = capsys.readouterr().err
    assert "notice:" in err
    assert "enabled = false" in err
    assert "informational" in err
    assert "docs/slack-setup.md" in err


def test_empty_channels_is_noop_regardless_of_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty channels is the genuine no-op even when enabled = false."""
    monkeypatch.setattr(
        opshub_config, "OpsHubSettings", lambda: _settings(enabled=False, channels=[])
    )

    is_noop = slack_cli._emit_slack_sync_notice()  # pyright: ignore[reportPrivateUsage]

    assert is_noop is True
    err = capsys.readouterr().err
    assert "channels is empty" in err


def test_notice_fires_when_channels_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        opshub_config, "OpsHubSettings", lambda: _settings(enabled=True, channels=[])
    )

    is_noop = slack_cli._emit_slack_sync_notice()  # pyright: ignore[reportPrivateUsage]

    assert is_noop is True
    err = capsys.readouterr().err
    assert "notice:" in err
    assert "channels is empty" in err
    assert "opshub slack conversations --format=toml" in err
    assert "docs/slack-setup.md" in err


def test_notice_silent_when_properly_configured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        opshub_config, "OpsHubSettings", lambda: _settings(enabled=True, channels=["C1"])
    )

    is_noop = slack_cli._emit_slack_sync_notice()  # pyright: ignore[reportPrivateUsage]

    assert is_noop is False
    assert capsys.readouterr().err == ""


def test_notice_suppressed_by_quiet_level(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-q`` (root level WARNING) suppresses the notice text...

    ...but the return value still reflects the no-op state so the caller
    can skip the post-sync "next" hint regardless of verbosity.
    """
    logging.getLogger().setLevel(logging.WARNING)
    monkeypatch.setattr(
        opshub_config, "OpsHubSettings", lambda: _settings(enabled=True, channels=[])
    )

    is_noop = slack_cli._emit_slack_sync_notice()  # pyright: ignore[reportPrivateUsage]

    assert is_noop is True
    assert capsys.readouterr().err == ""


def test_notice_level_gate() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    assert slack_cli._notice_level_allows() is True  # pyright: ignore[reportPrivateUsage]
    root.setLevel(logging.DEBUG)
    assert slack_cli._notice_level_allows() is True  # pyright: ignore[reportPrivateUsage]
    root.setLevel(logging.WARNING)
    assert slack_cli._notice_level_allows() is False  # pyright: ignore[reportPrivateUsage]
