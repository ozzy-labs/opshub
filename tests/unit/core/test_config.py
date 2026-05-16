"""Tests for opshub.core.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.config import (
    OpsHubSettings,
    default_config_dir,
    default_data_dir,
)


def test_defaults_resolve_under_xdg_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert default_config_dir() == Path.home() / ".config" / "opshub"
    assert default_data_dir() == Path.home() / ".local" / "share" / "opshub"


def test_defaults_respect_xdg_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert default_config_dir() == tmp_path / "cfg" / "opshub"
    assert default_data_dir() == tmp_path / "data" / "opshub"


def test_settings_env_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(tmp_path / "y"))
    settings = OpsHubSettings()
    assert settings.config_dir == tmp_path / "x"
    assert settings.data_dir == tmp_path / "y"


def test_settings_default_paths_are_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSHUB_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPSHUB_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    settings = OpsHubSettings()
    assert settings.config_dir == default_config_dir()
    assert settings.data_dir == default_data_dir()
