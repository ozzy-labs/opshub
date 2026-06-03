"""Tests for ``opshub google_workspace auth set / test`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app


def test_google_workspace_auth_set_dispatches_to_paste_code_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``google_workspace auth set`` invokes the Google OAuth paste-code helper."""
    calls: list[int] = []

    def _fake_run_paste_code() -> None:
        calls.append(1)

    monkeypatch.setattr(
        "opshub.cli._google_workspace_oauth.run_paste_code_flow",
        _fake_run_paste_code,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "auth", "set"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [1]


def test_google_workspace_auth_set_with_token_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing ``--token`` surfaces a warning (the value is ignored)."""
    calls: list[int] = []

    def _fake_run_paste_code() -> None:
        calls.append(1)

    monkeypatch.setattr(
        "opshub.cli._google_workspace_oauth.run_paste_code_flow",
        _fake_run_paste_code,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["google_workspace", "auth", "set", "--token", "ignored"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "warning: --token is ignored for google_workspace" in result.stderr
    assert calls == [1]


def test_google_workspace_auth_test_without_client_id_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``client_id`` → ``ConfigError`` → exit 1 + status: failed."""

    class _FakeGWS:
        client_id = ""
        client_secret = "secret"
        redirect_uri = "http://localhost"

    class _FakeConnectors:
        google_workspace = _FakeGWS()

    class _FakeSettings:
        connectors = _FakeConnectors()
        config_dir = "/fake/config"

    def _fake_factory(*_args: Any, **_kwargs: Any) -> _FakeSettings:
        return _FakeSettings()

    monkeypatch.setattr("opshub.core.config.OpsHubSettings", _fake_factory)

    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "auth", "test"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "client_id is not configured" in result.stderr


def test_google_workspace_auth_test_without_client_secret_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``client_secret`` → ``ConfigError`` → exit 1."""

    class _FakeGWS:
        client_id = "gws-client"
        client_secret = ""
        redirect_uri = "http://localhost"

    class _FakeConnectors:
        google_workspace = _FakeGWS()

    class _FakeSettings:
        connectors = _FakeConnectors()
        config_dir = "/fake/config"

    def _fake_factory(*_args: Any, **_kwargs: Any) -> _FakeSettings:
        return _FakeSettings()

    monkeypatch.setattr("opshub.core.config.OpsHubSettings", _fake_factory)

    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "auth", "test"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "client_secret is not configured" in result.stderr


def test_google_workspace_auth_test_with_credentials_dispatches_to_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both client_id + client_secret set, the test_token verifier is invoked."""

    class _FakeGWS:
        client_id = "gws-client"
        client_secret = "gws-secret"
        redirect_uri = "http://localhost"

    class _FakeConnectors:
        google_workspace = _FakeGWS()

    class _FakeSettings:
        connectors = _FakeConnectors()
        config_dir = "/fake/config"

    def _fake_factory(*_args: Any, **_kwargs: Any) -> _FakeSettings:
        return _FakeSettings()

    monkeypatch.setattr("opshub.core.config.OpsHubSettings", _fake_factory)

    class _FakeGoogleWorkspaceAuth:
        def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str) -> None:
            self.client_id = client_id
            self.client_secret = client_secret
            self.redirect_uri = redirect_uri

        def test_token(self) -> dict[str, str]:
            return {"user": "alice@acme.com", "rootFolderId": "root-1"}

    monkeypatch.setattr(
        "opshub.connectors.google_auth.auth.GoogleWorkspaceAuth",
        _FakeGoogleWorkspaceAuth,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "auth", "test"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "connector: google_workspace" in result.stdout
    assert "status:    ok" in result.stdout
    assert "alice@acme.com" in result.stdout
