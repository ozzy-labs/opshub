"""Tests for opshub.core.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.config import (
    BoxDriveConnectorSettings,
    EmbeddingSettings,
    ExcelOfficeSettings,
    OfficeSettings,
    OpsHubSettings,
    default_config_dir,
    default_data_dir,
)
from opshub.core.errors import ConfigError


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


# ---------------------------------------------------------------------------
# Phase 17 / ADR-0032 / #418 — runtime TOML loading via
# :class:`TomlConfigSettingsSource`.
#
# The 13 cases below pin every observable behaviour of the
# ``settings_customise_sources`` override:
#
# 1-3.  Basic load / absent file / XDG default lookup.
# 4-6.  Priority order (init args > env > toml > defaults).
# 7-10. Nested sections (storage / connectors.box_drive / office[.excel] / llm)
#       round-trip from TOML into the corresponding pydantic submodel.
# 11-12. Validation path: invalid Literal values and malformed TOML both
#        surface as :class:`ConfigError` so the CLI driver renders a
#        single-line actionable error instead of leaking pydantic / tomllib
#        tracebacks.
# 13.   ``OPSHUB_CONFIG_DIR`` env override redirects the TOML lookup
#       (with the matching ``config_dir`` field default coming from XDG
#       when the env is unset).
# ---------------------------------------------------------------------------


def _write_toml(config_dir: Path, body: str) -> Path:
    """Write a ``config.toml`` body and return the resulting path."""
    config_dir.mkdir(parents=True, exist_ok=True)
    toml_path = config_dir / "config.toml"
    toml_path.write_text(body, encoding="utf-8")
    return toml_path


def _isolate_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Re-point XDG dirs into ``tmp_path`` and clear the OPSHUB env overrides.

    Returns the would-be ``default_config_dir()`` for assertions. The
    helper guarantees no cross-test bleed even when the developer has
    ``OPSHUB_CONFIG_DIR`` exported in their real shell environment.
    """
    xdg_config_home = tmp_path / "xdg_config"
    xdg_data_home = tmp_path / "xdg_data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))
    monkeypatch.delenv("OPSHUB_CONFIG_DIR", raising=False)
    return xdg_config_home / "opshub"


def test_toml_file_loaded_when_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ``config.toml`` at ``$OPSHUB_CONFIG_DIR`` is read at runtime.

    ADR-0032 §決定 (#418) — the canonical end-state contract. Before
    the override, ``OpsHubSettings()`` only consulted env + defaults
    and the TOML file was silently ignored (the bug ``docs/upgrading.md``
    inadvertently documented as the intended behaviour).
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    _write_toml(tmp_path, '[embedding]\nbackend = "local"\n')

    settings = OpsHubSettings()

    assert settings.embedding.backend == "local"


def test_toml_file_absent_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No TOML file → defaults apply; no ``FileNotFoundError``.

    Fresh ``uv tool install`` that has not yet run ``opshub init``
    must still produce working defaults — :class:`TomlConfigSettingsSource`
    returns an empty mapping when the path is absent (verified via
    ``pydantic_settings`` ≥ 2.3 semantics).
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    # No config.toml written.

    settings = OpsHubSettings()

    assert settings.embedding.backend == "disabled"
    assert settings.embedding == EmbeddingSettings()


def test_default_config_dir_is_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``OPSHUB_CONFIG_DIR`` unset, the TOML lookup uses XDG.

    Pins the XDG fallback path inside ``settings_customise_sources``:
    when the env var is missing, the source falls back to
    :func:`default_config_dir`, which itself honours ``XDG_CONFIG_HOME``.
    """
    xdg_opshub_dir = _isolate_xdg(monkeypatch, tmp_path)
    _write_toml(xdg_opshub_dir, '[embedding]\nbackend = "openai"\n')

    settings = OpsHubSettings()

    assert settings.config_dir == xdg_opshub_dir
    assert settings.embedding.backend == "openai"


def test_env_overrides_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Env > TOML — env always wins (ADR-0032 priority pin).

    Mirrors ADR-0015 §決定 (d) — env vars are the documented CI /
    headless override path, so they must override the on-disk TOML
    rather than the other way around.
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    _write_toml(tmp_path, '[embedding]\nbackend = "local"\n')
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "openai")

    settings = OpsHubSettings()

    assert settings.embedding.backend == "openai"


def test_init_args_override_env_and_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """init args > env > TOML — programmatic overrides always win.

    The ``init_settings`` source sits at the top of the sources tuple
    so callers building a settings instance directly (e.g. tests, the
    ``opshub mcp`` wiring, future programmatic embedders) always
    observe the explicit args they passed regardless of stray env or
    TOML state.
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    _write_toml(tmp_path, '[embedding]\nbackend = "local"\n')
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "openai")

    settings = OpsHubSettings(embedding=EmbeddingSettings(backend="voyage"))

    assert settings.embedding.backend == "voyage"


def test_toml_overrides_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TOML > defaults — the entire point of the override.

    Verifies the lower half of the priority order: a TOML file
    populating a non-default value actually changes ``OpsHubSettings()``
    output even when no env / init args are supplied.
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    # Clear any embedding env vars so the only signal is the TOML file.
    monkeypatch.delenv("OPSHUB_EMBEDDING__BACKEND", raising=False)
    _write_toml(tmp_path, '[embedding]\nbackend = "local"\n')

    settings = OpsHubSettings()

    # Defaults would put backend="disabled" — TOML flipping it to "local"
    # proves the source reaches the model.
    assert settings.embedding.backend == "local"
    # Other fields stay at defaults (TOML only carries one section).
    assert settings.storage.encryption is False


def test_storage_section_loaded_from_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nested ``[storage]`` section maps to :class:`StorageSettings`."""
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    custom_db = tmp_path / "custom.sqlite"
    _write_toml(
        tmp_path,
        f'[storage]\ndb_path = "{custom_db}"\nencryption = true\n',
    )

    settings = OpsHubSettings()

    assert settings.storage.db_path == custom_db
    assert settings.storage.encryption is True


def test_connectors_box_drive_section_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nested ``[connectors.box_drive]`` round-trips into the submodel."""
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    root = tmp_path / "drive"
    root.mkdir()
    _write_toml(
        tmp_path,
        f"""[connectors.box_drive]
enabled = true
root_path = "{root}"
max_depth = 32
max_files = 5000
follow_symlinks = true
content_extraction = true
""",
    )

    settings = OpsHubSettings()

    assert settings.connectors.box_drive.enabled is True
    assert settings.connectors.box_drive.root_path == root
    assert settings.connectors.box_drive.max_depth == 32
    assert settings.connectors.box_drive.max_files == 5000
    assert settings.connectors.box_drive.follow_symlinks is True
    assert settings.connectors.box_drive.content_extraction is True


def test_office_section_loaded_with_nested_excel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Double-nested ``[office.excel]`` section round-trips.

    The ``[office.excel]`` table sits two levels deep — pins that the
    TOML source unflattens correctly into :class:`OfficeSettings` /
    :class:`ExcelOfficeSettings` without needing the ``__`` env-style
    delimiter.
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    _write_toml(
        tmp_path,
        """[office]
max_file_size_mb = 100
max_chars = 1000000

[office.excel]
max_cells_per_sheet = 20000
max_cells_per_workbook = 100000
""",
    )

    settings = OpsHubSettings()

    assert settings.office.max_file_size_mb == 100
    assert settings.office.max_chars == 1_000_000
    assert settings.office.excel.max_cells_per_sheet == 20_000
    assert settings.office.excel.max_cells_per_workbook == 100_000


def test_llm_section_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nested ``[llm]`` + ``[llm.anthropic]`` sections round-trip.

    Phase 5 + Phase 17 — the ``[llm]`` backend selector and the
    per-backend ``model_id`` / ``model_version`` defaults both need to
    flow through the TOML source. Without this pin, an operator could
    set ``backend = "anthropic"`` in TOML and silently keep getting the
    "disabled" code path (pre-#418 behaviour).
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    # Clear the single-underscore convenience shortcut so the test
    # exercises the canonical nested path, not the model_validator
    # fallback.
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    _write_toml(
        tmp_path,
        """[llm]
backend = "anthropic"

[llm.anthropic]
model_id = "claude-opus-4-5-20251015"
model_version = "2026-06-01"
""",
    )

    settings = OpsHubSettings()

    assert settings.llm.backend == "anthropic"
    assert settings.llm.anthropic.model_id == "claude-opus-4-5-20251015"
    assert settings.llm.anthropic.model_version == "2026-06-01"


def test_invalid_backend_value_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bogus ``backend`` value in TOML surfaces as :class:`ConfigError`.

    The :meth:`OpsHubSettings.__init__` wrap converts pydantic's
    :class:`ValidationError` into :class:`ConfigError` so the CLI
    driver renders a single-line actionable message instead of a raw
    traceback. The original pydantic exception is chained via
    ``__cause__`` for ``--debug`` diagnostics (ADR-0027).
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    _write_toml(tmp_path, '[embedding]\nbackend = "grok"\n')

    with pytest.raises(ConfigError) as excinfo:
        OpsHubSettings()

    # The wrapped message mentions the operator-facing remediation
    # surface, not the pydantic internal field path.
    assert "config.toml" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_malformed_toml_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A syntactically broken TOML file surfaces as :class:`ConfigError`.

    ``TomlConfigSettingsSource`` raises ``tomllib.TOMLDecodeError``
    eagerly from its constructor; the override catches it and wraps it
    with a path-bearing message so the operator can find and fix the
    file. The chained ``__cause__`` retains the original tomllib
    diagnostic for ``--debug``.
    """
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(tmp_path))
    toml_path = _write_toml(tmp_path, "not = valid = toml\n")

    with pytest.raises(ConfigError) as excinfo:
        OpsHubSettings()

    assert str(toml_path) in str(excinfo.value)
    assert "opshub init" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_opshub_config_dir_env_overrides_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``$OPSHUB_CONFIG_DIR`` redirects the TOML lookup at runtime.

    Combines both observable effects:
    * the ``config_dir`` field reflects the env override (the existing
      ``OPSHUB_`` env_prefix wiring), and
    * the ``settings_customise_sources`` lookup actually reads
      ``<override>/config.toml`` (the new behaviour).
    """
    override_dir = tmp_path / "custom-config"
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(override_dir))
    _write_toml(override_dir, '[embedding]\nbackend = "openai"\n')

    settings = OpsHubSettings()

    assert settings.config_dir == override_dir
    assert settings.embedding.backend == "openai"


def test_config_dir_field_uses_xdg_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``OPSHUB_CONFIG_DIR`` unset, ``config_dir`` resolves under XDG.

    Pins the lower-priority half of #418's resolution rule: the field
    default itself falls back to :func:`default_config_dir`, which
    consults ``XDG_CONFIG_HOME``. The TOML source under the same path
    is read if present.
    """
    xdg_opshub_dir = _isolate_xdg(monkeypatch, tmp_path)
    # No TOML file — just verify the field default resolution.

    settings = OpsHubSettings()

    assert settings.config_dir == xdg_opshub_dir
