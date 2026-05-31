"""Tests for opshub.core.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.config import (
    BoxDriveConnectorSettings,
    ExcelOfficeSettings,
    OfficeSettings,
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


# ---------------------------------------------------------------------------
# BoxDriveConnectorSettings (Phase 9, ADR-0019)
# ---------------------------------------------------------------------------


def test_box_drive_connector_settings_defaults() -> None:
    """Phase 9 step B2 defaults pin the opt-in / safety-cap posture (ADR-0019).

    ``enabled=False`` ensures a fresh install never walks an
    arbitrary directory on first run. ``root_path=None`` defers to
    :func:`opshub.core.platform.box_drive_default_root_path` so
    WSL2 / macOS work out of the box. ``max_depth=16`` /
    ``max_files=100_000`` / ``follow_symlinks=False`` /
    ``exclude_globs=[]`` mirror the scanner constructor defaults so
    operators get the same behaviour whether they construct the
    scanner directly or go through ``opshub connector sync``.
    """
    cfg = BoxDriveConnectorSettings()

    assert cfg.enabled is False
    assert cfg.root_path is None
    assert cfg.max_depth == 16
    assert cfg.max_files == 100_000
    assert cfg.follow_symlinks is False
    assert cfg.exclude_globs == []


def test_box_drive_connector_settings_env_var_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nested env-var pattern ``OPSHUB_CONNECTORS__BOX_DRIVE__<FIELD>`` works.

    The Phase 7 connectors use the same pattern; pinning it here
    guards against a regression that breaks the ``__`` delimiter for
    the box_drive section specifically (e.g. an accidental rename).
    """
    root = tmp_path / "drive-root"
    root.mkdir()
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__ENABLED", "true")
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH", str(root))
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__MAX_DEPTH", "32")
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__MAX_FILES", "5000")
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__FOLLOW_SYMLINKS", "true")

    settings = OpsHubSettings()

    assert settings.connectors.box_drive.enabled is True
    assert settings.connectors.box_drive.root_path == root
    assert settings.connectors.box_drive.max_depth == 32
    assert settings.connectors.box_drive.max_files == 5000
    assert settings.connectors.box_drive.follow_symlinks is True


def test_box_drive_connector_settings_attaches_to_root_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OpsHubSettings.connectors.box_drive`` exists with default values.

    A regression that drops the field from :class:`ConnectorSettings`
    would surface as an ``AttributeError`` on the access below,
    rather than as a less obvious "the box_drive sync silently uses
    different defaults than the docs say".
    """
    # Clear env to make sure we're testing the field default, not
    # an env-var leak.
    for name in (
        "OPSHUB_CONNECTORS__BOX_DRIVE__ENABLED",
        "OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH",
        "OPSHUB_CONNECTORS__BOX_DRIVE__MAX_DEPTH",
        "OPSHUB_CONNECTORS__BOX_DRIVE__MAX_FILES",
        "OPSHUB_CONNECTORS__BOX_DRIVE__FOLLOW_SYMLINKS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = OpsHubSettings()

    assert isinstance(settings.connectors.box_drive, BoxDriveConnectorSettings)
    assert settings.connectors.box_drive.enabled is False
    assert settings.connectors.box_drive.root_path is None


# ---------------------------------------------------------------------------
# OfficeSettings (Phase 11 audit Cluster B, ADR-0025)
# ---------------------------------------------------------------------------


def test_office_settings_defaults_match_adr_0025() -> None:
    """ADR-0025 §決定 (b) + (e) pin the operator-facing defaults.

    The numbers below are the documented contract: 50 MB pre-extraction
    cap, 500K chars post-extraction cap, 10K cells per sheet, 50K cells
    per workbook. A regression here would silently change the size of
    the extracted ``sources.body`` payloads operators see, so the
    explicit assertions are the cheapest guard.
    """
    cfg = OfficeSettings()

    assert cfg.max_file_size_mb == 50
    assert cfg.max_chars == 500_000
    assert isinstance(cfg.excel, ExcelOfficeSettings)
    assert cfg.excel.max_cells_per_sheet == 10_000
    assert cfg.excel.max_cells_per_workbook == 50_000


def test_office_settings_attaches_to_root_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OpsHubSettings.office`` exists with default values.

    A regression that drops the field from :class:`OpsHubSettings`
    would surface as an ``AttributeError`` on the access below, rather
    than as a less obvious "the box_drive scanner silently keeps using
    the ADR defaults even when the operator set
    ``[office] max_file_size_mb = 100``".
    """
    for name in (
        "OPSHUB_OFFICE__MAX_FILE_SIZE_MB",
        "OPSHUB_OFFICE__MAX_CHARS",
        "OPSHUB_OFFICE__EXCEL__MAX_CELLS_PER_SHEET",
        "OPSHUB_OFFICE__EXCEL__MAX_CELLS_PER_WORKBOOK",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = OpsHubSettings()

    assert isinstance(settings.office, OfficeSettings)
    assert settings.office.max_file_size_mb == 50
    assert settings.office.max_chars == 500_000
    assert settings.office.excel.max_cells_per_sheet == 10_000
    assert settings.office.excel.max_cells_per_workbook == 50_000


def test_office_settings_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested env-var pattern ``OPSHUB_OFFICE__<FIELD>`` works.

    The Phase 11 audit Cluster B H3 fix wires
    ``opshub.toml [office]`` overrides through to the box_drive /
    onedrive_drive scanners; the corresponding env-var path
    (``OPSHUB_OFFICE__MAX_FILE_SIZE_MB=100`` etc.) must hit the same
    settings field. Pinning all four knobs here guards against an
    accidental rename or a missing ``Field`` declaration.
    """
    monkeypatch.setenv("OPSHUB_OFFICE__MAX_FILE_SIZE_MB", "100")
    monkeypatch.setenv("OPSHUB_OFFICE__MAX_CHARS", "1000000")
    monkeypatch.setenv("OPSHUB_OFFICE__EXCEL__MAX_CELLS_PER_SHEET", "20000")
    monkeypatch.setenv("OPSHUB_OFFICE__EXCEL__MAX_CELLS_PER_WORKBOOK", "100000")

    settings = OpsHubSettings()

    assert settings.office.max_file_size_mb == 100
    assert settings.office.max_chars == 1_000_000
    assert settings.office.excel.max_cells_per_sheet == 20_000
    assert settings.office.excel.max_cells_per_workbook == 100_000


def test_office_settings_default_factories_are_not_shared() -> None:
    """Mutable-default footgun guard, same shape as the other sections.

    ``OfficeSettings`` carries a nested ``ExcelOfficeSettings`` so the
    independence assertion has to drill one level deeper than the
    earlier sections' equivalent.
    """
    a = OpsHubSettings()
    b = OpsHubSettings()
    assert a.office is not b.office
    assert a.office.excel is not b.office.excel
