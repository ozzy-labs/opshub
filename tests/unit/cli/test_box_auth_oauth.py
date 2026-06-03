"""Tests for ``opshub box auth set / test`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app


def test_box_auth_set_dispatches_to_paste_code_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """``box auth set`` invokes the Box OAuth paste-code helper."""
    calls: list[int] = []

    def _fake_run_paste_code() -> None:
        calls.append(1)

    monkeypatch.setattr(
        "opshub.cli._box_oauth.run_paste_code_flow",
        _fake_run_paste_code,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box", "auth", "set"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [1]


def test_box_auth_set_with_token_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing ``--token`` surfaces a warning (the value is ignored)."""
    calls: list[int] = []

    def _fake_run_paste_code() -> None:
        calls.append(1)

    monkeypatch.setattr(
        "opshub.cli._box_oauth.run_paste_code_flow",
        _fake_run_paste_code,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box", "auth", "set", "--token", "ignored"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "warning: --token is ignored for box" in result.stderr
    assert calls == [1]


def test_box_auth_test_without_client_id_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``client_id`` → ``ConfigError`` → exit 1 + status: failed."""

    class _FakeBoxSettings:
        client_id = ""

    class _FakeConnectors:
        box = _FakeBoxSettings()

    class _FakeSettings:
        connectors = _FakeConnectors()
        config_dir = "/fake/config"

    def _fake_settings_factory(*_args: Any, **_kwargs: Any) -> _FakeSettings:
        return _FakeSettings()

    monkeypatch.setattr("opshub.core.config.OpsHubSettings", _fake_settings_factory)

    runner = CliRunner()
    result = runner.invoke(app, ["box", "auth", "test"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "client_id is not configured" in result.stderr


def test_box_auth_test_with_client_id_dispatches_to_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``client_id`` set, the test_token verifier is invoked."""

    class _FakeBoxSettings:
        client_id = "box-client"

    class _FakeConnectors:
        box = _FakeBoxSettings()

    class _FakeSettings:
        connectors = _FakeConnectors()
        config_dir = "/fake/config"

    def _fake_settings_factory(*_args: Any, **_kwargs: Any) -> _FakeSettings:
        return _FakeSettings()

    monkeypatch.setattr("opshub.core.config.OpsHubSettings", _fake_settings_factory)

    class _FakeBoxAuth:
        def __init__(self, *, client_id: str) -> None:
            self.client_id = client_id

        def test_token(self) -> dict[str, str]:
            return {"id": "U1", "login": "alice@acme.com", "name": "Alice"}

    monkeypatch.setattr("opshub.connectors.box.auth.BoxAuth", _FakeBoxAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["box", "auth", "test"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "connector: box" in result.stdout
    assert "status:    ok" in result.stdout
