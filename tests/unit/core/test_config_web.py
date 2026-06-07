"""Tests for the ``[connectors.web]`` settings section (Phase 21-C, ADR-0037).

Pins the default shape, the ``pages`` string-array contract (table form is
NOT accepted — YAGNI, issue #507), TOML parse, env-var override, and the
``extra="forbid"`` fail-fast on stale / mistyped keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.config import OpsHubSettings, WebConnectorSettings


def test_web_defaults() -> None:
    web = WebConnectorSettings()
    assert web.enabled is False
    assert web.pages == []


def test_web_section_default_on_root_settings(tmp_path: Path) -> None:
    settings = OpsHubSettings(data_dir=tmp_path)
    assert isinstance(settings.connectors.web, WebConnectorSettings)
    assert settings.connectors.web.enabled is False
    assert settings.connectors.web.pages == []


def test_web_pages_string_array_parses() -> None:
    web = WebConnectorSettings(
        enabled=True,
        pages=["https://a.example/", "https://b.example/docs"],
    )
    assert web.enabled is True
    assert web.pages == ["https://a.example/", "https://b.example/docs"]


def test_web_default_factories_not_shared() -> None:
    """Guard against the mutable-default footgun on ``pages``."""
    a = OpsHubSettings()
    b = OpsHubSettings()
    assert a.connectors.web is not b.connectors.web
    assert a.connectors.web.pages is not b.connectors.web.pages


def test_web_config_toml_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``config.toml`` ``[connectors.web]`` section with a string array loads."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "[connectors.web]\n"
        "enabled = true\n"
        'pages = ["https://x.example/", "https://y.example/page"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))

    settings = OpsHubSettings()

    assert settings.connectors.web.enabled is True
    assert settings.connectors.web.pages == [
        "https://x.example/",
        "https://y.example/page",
    ]


def test_web_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested env-var override (``OPSHUB_CONNECTORS__WEB__*``)."""
    monkeypatch.setenv("OPSHUB_CONNECTORS__WEB__ENABLED", "true")
    settings = OpsHubSettings()
    assert settings.connectors.web.enabled is True


def test_web_rejects_unknown_key() -> None:
    """``extra="forbid"`` fails fast on a stale / mistyped key."""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        WebConnectorSettings(bogus_key="x")  # type: ignore[call-arg]


def test_web_rejects_table_form_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The table form ``[[connectors.web.pages]]`` is rejected (string-array only).

    A web page has no per-page knobs worth a table (issue #507 YAGNI), so
    ``pages`` is a plain ``list[str]``. Feeding a list of dicts (the TOML
    array-of-tables shape) must fail validation rather than silently
    coercing — pinning the string-array contract.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "[connectors.web]\n"
        "enabled = true\n\n"
        "[[connectors.web.pages]]\n"
        'url = "https://x.example/"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))

    from opshub.core.errors import ConfigError

    with pytest.raises(ConfigError):
        OpsHubSettings()
