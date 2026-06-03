"""Tests for ``opshub ms365 auth set / test`` (Phase 17-B, ADR-0031).

MS365 uses an interactive OAuth paste-code flow; the tests patch the
flow runner so they do not hit the real Microsoft authorisation URL.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app


def test_ms365_auth_set_dispatches_to_paste_code_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ms365 auth set`` invokes the MS365 OAuth paste-code helper."""
    calls: list[int] = []

    def _fake_run_paste_code() -> None:
        calls.append(1)

    monkeypatch.setattr(
        "opshub.cli._ms365_oauth.run_paste_code_flow",
        _fake_run_paste_code,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["ms365", "auth", "set"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [1]


def test_ms365_auth_set_with_token_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing ``--token`` surfaces a warning (the value is ignored)."""
    calls: list[int] = []

    def _fake_run_paste_code() -> None:
        calls.append(1)

    monkeypatch.setattr(
        "opshub.cli._ms365_oauth.run_paste_code_flow",
        _fake_run_paste_code,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["ms365", "auth", "set", "--token", "ignored"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "warning: --token is ignored for ms365" in result.stderr
    # The OAuth flow still ran.
    assert calls == [1]


def test_ms365_auth_test_without_client_id_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``client_id`` configured → ``ConfigError`` → exit 1 + status: failed."""

    class _FakeMS365Settings:
        client_id = ""

    class _FakeConnectors:
        ms365 = _FakeMS365Settings()

    class _FakeSettings:
        connectors = _FakeConnectors()
        config_dir = "/fake/config"

    def _fake_settings_factory(*_args: Any, **_kwargs: Any) -> _FakeSettings:
        return _FakeSettings()

    monkeypatch.setattr("opshub.core.config.OpsHubSettings", _fake_settings_factory)

    runner = CliRunner()
    result = runner.invoke(app, ["ms365", "auth", "test"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "client_id is not configured" in result.stderr


def test_ms365_auth_test_with_client_id_dispatches_to_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``client_id`` set, the test_token verifier is invoked and its dict rendered."""

    class _FakeMS365Settings:
        client_id = "ms365-client"

    class _FakeConnectors:
        ms365 = _FakeMS365Settings()

    class _FakeSettings:
        connectors = _FakeConnectors()
        config_dir = "/fake/config"

    def _fake_settings_factory(*_args: Any, **_kwargs: Any) -> _FakeSettings:
        return _FakeSettings()

    monkeypatch.setattr("opshub.core.config.OpsHubSettings", _fake_settings_factory)

    class _FakeMS365Auth:
        def __init__(self, *, client_id: str) -> None:
            self.client_id = client_id

        def test_token(self) -> dict[str, str]:
            return {"id": "user-1", "displayName": "Alice", "userPrincipalName": "alice@acme.com"}

    monkeypatch.setattr("opshub.connectors.ms365.auth.MS365Auth", _FakeMS365Auth)

    runner = CliRunner()
    result = runner.invoke(app, ["ms365", "auth", "test"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "connector: ms365" in result.stdout
    assert "status:    ok" in result.stdout
    assert "Alice" in result.stdout
    assert "alice@acme.com" in result.stdout
