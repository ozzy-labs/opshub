"""Tests for opshub.core.config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from opshub.core.config import (
    BoxDriveConnectorSettings,
    EmbeddingSettings,
    ExcelOfficeSettings,
    OfficeSettings,
    OneDriveDriveConnectorSettings,
    OpsHubSettings,
    SlackChannelSpec,
    SlackConnectorSettings,
    SlackWorkspaceSettings,
    default_config_dir,
    default_data_dir,
    resolve_slack_thread_activity_window,
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
    ``max_files=100_000`` / ``follow_symlinks=False`` mirror the
    scanner constructor defaults so operators get the same behaviour
    whether they construct the scanner directly or go through
    ``opshub connector sync``.

    Path-based exclusion is no longer a per-connector inline field
    (epic #470 cleanup of ADR-0020 §(b)) — see
    :func:`test_box_drive_connector_settings_rejects_inline_exclude_globs`
    for the fail-fast pin.
    """
    cfg = BoxDriveConnectorSettings()

    assert cfg.enabled is False
    assert cfg.root_path is None
    assert cfg.max_depth == 16
    assert cfg.max_files == 100_000
    assert cfg.follow_symlinks is False


def test_box_drive_connector_settings_rejects_inline_exclude_globs() -> None:
    """Epic #470 (ADR-0020 §(b) cleanup): inline ``exclude_globs`` is rejected.

    ``BoxDriveConnectorSettings`` declares
    ``model_config = ConfigDict(extra="forbid")`` so a stale TOML
    carrying the Phase 9 / pre-cleanup ``exclude_globs = [...]`` key
    surfaces as a :class:`pydantic.ValidationError` rather than the
    silent "no path filter applied" degradation the dual-read shim
    used to mask. Operators must migrate to
    ``~/.config/opshub/excludes.yaml`` ``paths:`` (see
    ``docs/upgrading.md``).
    """
    with pytest.raises(ValidationError, match="exclude_globs"):
        BoxDriveConnectorSettings.model_validate({"exclude_globs": ["**/.DS_Store"]})


def test_onedrive_drive_connector_settings_rejects_inline_exclude_globs() -> None:
    """Symmetric pin for OneDrive: stale inline ``exclude_globs`` is rejected.

    The Phase 11 F4-b inline ``[connectors.onedrive_drive] exclude_globs``
    was removed by the same epic #470 cleanup. Pinning the
    ``ValidationError`` here guards against the OneDrive settings
    drifting back to a permissive ``extra="ignore"`` shape (which
    would silently swallow the key while the Box Drive sibling kept
    failing fast).
    """
    with pytest.raises(ValidationError, match="exclude_globs"):
        OneDriveDriveConnectorSettings.model_validate({"exclude_globs": ["**/.DS_Store"]})


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


# ------------------- Slack workspaces + date floor (Phase 20 ADR-0036 / Phase 24-C ADR-0041)


def _ws(payload: object) -> SlackConnectorSettings:
    """Build a single-workspace ``SlackConnectorSettings`` from a channels payload."""
    return SlackConnectorSettings.model_validate({"workspaces": {"acme": {"channels": payload}}})


def test_slack_workspaces_table_parses() -> None:
    """The Phase 24-C ``[connectors.slack.workspaces.<alias>]`` table form parses."""
    settings = SlackConnectorSettings.model_validate(
        {
            "workspaces": {
                "acme": {"channels": ["C1", "C2"], "sync_since": "30d"},
                "oss": {"channels": ["C9"]},
            },
            "sync_since": "90d",
        }
    )
    assert sorted(settings.workspaces) == ["acme", "oss"]
    assert [c.id for c in settings.workspaces["acme"].channels] == ["C1", "C2"]
    assert settings.workspaces["acme"].sync_since == "30d"
    assert settings.workspaces["oss"].sync_since is None
    assert settings.sync_since == "90d"


def test_slack_workspace_alias_with_hyphen_rejected() -> None:
    """ADR-0041 §(a): ``-`` collides with ``_`` in the keyring env name."""
    with pytest.raises(ConfigError, match=r"my-ws.*\^\[a-z0-9\]\[a-z0-9_\]\*\$"):
        SlackConnectorSettings.model_validate({"workspaces": {"my-ws": {"channels": ["C1"]}}})


def test_slack_workspace_alias_uppercase_rejected() -> None:
    with pytest.raises(ConfigError, match="invalid"):
        SlackConnectorSettings.model_validate({"workspaces": {"Acme": {"channels": ["C1"]}}})


def test_slack_workspace_alias_underscore_and_digits_accepted() -> None:
    settings = SlackConnectorSettings.model_validate(
        {"workspaces": {"acme_dev2": {"channels": ["C1"]}}}
    )
    assert list(settings.workspaces) == ["acme_dev2"]


def test_slack_flat_channels_rejected_with_rewrite_hint() -> None:
    """The pre-Phase-24 flat ``channels`` key fails loud with a rewrite example."""
    with pytest.raises(ConfigError, match=r"\[connectors\.slack\.workspaces\.main\]") as exc:
        SlackConnectorSettings.model_validate({"channels": ["C1"]})
    message = str(exc.value)
    assert "no longer accepts a flat `channels` key" in message
    assert "opshub slack auth set --workspace" in message


def test_slack_flat_channels_rejected_even_when_empty() -> None:
    """An empty flat ``channels = []`` (the pre-24 starter config) also rejects."""
    with pytest.raises(ConfigError, match="multi-workspace"):
        SlackConnectorSettings.model_validate({"channels": []})


def test_slack_channels_string_array_coerced_to_specs() -> None:
    """The historical ``channels = ["C1", "C2"]`` string-array form keeps working.

    This is the shape ``opshub slack conversations --format=toml`` emits,
    so it must stay valid alongside the table form (ADR-0036 §(b)), now
    nested under the workspace table (ADR-0041 §(c)).
    """
    settings = _ws(["C1", "C2"])
    channels = settings.workspaces["acme"].channels
    assert [c.id for c in channels] == ["C1", "C2"]
    assert all(c.since is None for c in channels)


def test_slack_channels_table_form_with_since() -> None:
    settings = _ws([{"id": "C1", "since": "30d"}, {"id": "C2", "since": "all"}])
    channels = settings.workspaces["acme"].channels
    assert channels[0].id == "C1"
    assert channels[0].since == "30d"
    assert channels[1].since == "all"


def test_slack_channels_mixed_string_and_table() -> None:
    settings = _ws(["C1", {"id": "C2", "since": "2026-01-01"}])
    channels = settings.workspaces["acme"].channels
    assert [c.id for c in channels] == ["C1", "C2"]
    assert channels[0].since is None
    assert channels[1].since == "2026-01-01"


def test_slack_sync_since_accepts_relative_and_iso_and_all() -> None:
    assert SlackConnectorSettings(sync_since="90d").sync_since == "90d"
    assert SlackConnectorSettings(sync_since="2026-01-01").sync_since == "2026-01-01"
    assert SlackConnectorSettings(sync_since="all").sync_since == "all"
    assert SlackConnectorSettings().sync_since is None


def test_slack_sync_since_invalid_raises_config_error() -> None:
    with pytest.raises(ConfigError, match=r"\[connectors\.slack\] sync_since"):
        SlackConnectorSettings(sync_since="not-a-date")


def test_slack_workspace_sync_since_validated() -> None:
    """The per-workspace floor override shares the floor grammar."""
    assert SlackWorkspaceSettings.model_validate({"sync_since": "30d"}).sync_since == "30d"
    with pytest.raises(ConfigError, match=r"workspaces\.<alias>\] sync_since"):
        SlackWorkspaceSettings.model_validate({"sync_since": "not-a-date"})


def test_slack_channel_since_invalid_raises_config_error() -> None:
    with pytest.raises(ConfigError, match=r"workspaces\.<alias>\] channels"):
        _ws([{"id": "C1", "since": "5h"}])


def test_slack_channel_since_keeps_raw_string_for_runtime_eval() -> None:
    """Relative floors are stored as the raw string (re-evaluated at sync time)."""
    spec = SlackChannelSpec(id="C1", since="90d")
    assert spec.since == "90d"  # not frozen to a parsed datetime


def test_slack_duplicate_channel_ids_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicate id"):
        _ws(["C1", "C1"])


def test_slack_duplicate_channel_id_across_workspaces_accepted() -> None:
    """Channel ids may collide *across* workspaces (the namespaces are disjoint)."""
    settings = SlackConnectorSettings.model_validate(
        {"workspaces": {"acme": {"channels": ["C1"]}, "oss": {"channels": ["C1"]}}}
    )
    assert [c.id for c in settings.workspaces["acme"].channels] == ["C1"]
    assert [c.id for c in settings.workspaces["oss"].channels] == ["C1"]


def test_slack_thread_activity_window_workspace_override_two_step() -> None:
    """ADR-0041 §(c): workspace window wins; ``None`` inherits; ``all`` disables."""
    from datetime import timedelta

    connector = SlackConnectorSettings.model_validate(
        {
            "workspaces": {
                "acme": {"channels": ["C1"], "thread_activity_window": "7d"},
                "oss": {"channels": ["C2"]},
                "club": {"channels": ["C3"], "thread_activity_window": "all"},
            },
            "thread_activity_window": "30d",
        }
    )
    assert resolve_slack_thread_activity_window(
        connector.workspaces["acme"], connector
    ) == timedelta(days=7)
    assert resolve_slack_thread_activity_window(
        connector.workspaces["oss"], connector
    ) == timedelta(days=30)
    assert resolve_slack_thread_activity_window(connector.workspaces["club"], connector) is None


def test_slack_workspace_thread_activity_window_invalid_rejected() -> None:
    with pytest.raises(ConfigError, match="thread_activity_window"):
        SlackWorkspaceSettings.model_validate({"thread_activity_window": "5h"})


def test_slack_backfill_on_floor_lower_defaults_true() -> None:
    """Phase 22-D (ADR-0038): auto gap-backfill is opt-out, default on."""
    assert SlackConnectorSettings().backfill_on_floor_lower is True


def test_slack_backfill_on_floor_lower_can_be_disabled() -> None:
    """``backfill_on_floor_lower=false`` suppresses the auto gap-backfill."""
    assert SlackConnectorSettings(backfill_on_floor_lower=False).backfill_on_floor_lower is False
    # The env-var path (the ``--no-backfill`` CLI shim target) coerces the
    # string ``"false"`` to the bool ``False`` like any pydantic bool field.
    assert (
        SlackConnectorSettings.model_validate(
            {"backfill_on_floor_lower": "false"}
        ).backfill_on_floor_lower
        is False
    )


def test_slack_channels_env_override_json_table_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The table form round-trips through the JSON env override (nest form)."""
    _isolate_xdg(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS",
        '[{"id": "C1", "since": "30d"}]',
    )
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__SYNC_SINCE", "90d")

    settings = OpsHubSettings()

    workspace = settings.connectors.slack.workspaces["acme"]
    assert [c.id for c in workspace.channels] == ["C1"]
    assert workspace.channels[0].since == "30d"
    assert settings.connectors.slack.sync_since == "90d"


def test_slack_env_alias_is_lowercased_from_env_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0041 §(c): the env nest segment lands as the lowercase alias.

    pydantic-settings folds env keys case-insensitively, so
    ``...__WORKSPACES__ACME__...`` must resolve to the ``acme`` alias —
    if the upper-case segment leaked through as the literal table key, the
    alias-grammar validator would reject it.
    """
    _isolate_xdg(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS", "C1")

    settings = OpsHubSettings()

    assert list(settings.connectors.slack.workspaces) == ["acme"]


def test_slack_channels_env_override_json_string_array(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The legacy string-array env form still works (integration suite relies on it)."""
    _isolate_xdg(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS", '["C1"]')

    settings = OpsHubSettings()

    workspace = settings.connectors.slack.workspaces["acme"]
    assert [c.id for c in workspace.channels] == ["C1"]
    assert workspace.channels[0].since is None


def test_slack_channels_env_override_comma_separated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 23-E (#535): the env override accepts a bare comma list.

    ``NoDecode`` stops pydantic-settings from JSON-forcing the env value,
    so the natural ``C1,C2`` form is the primary input — no JSON quoting.
    Phase 24-C verifies it still holds under the workspaces nest
    (ADR-0041 §(c) open question, resolved here).
    """
    _isolate_xdg(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS", "C1,C2,C3")

    settings = OpsHubSettings()

    workspace = settings.connectors.slack.workspaces["acme"]
    assert [c.id for c in workspace.channels] == ["C1", "C2", "C3"]
    assert all(c.since is None for c in workspace.channels)


def test_slack_channels_env_comma_form_trims_whitespace_and_empties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spaces around commas and a trailing comma do not synthesise blank ids."""
    _isolate_xdg(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__WORKSPACES__ACME__CHANNELS", " C1 , C2 ,,")

    settings = OpsHubSettings()

    assert [c.id for c in settings.connectors.slack.workspaces["acme"].channels] == ["C1", "C2"]


def test_slack_flat_channels_env_var_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-24 flat env var fails loud too (not silently ignored)."""
    _isolate_xdg(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_CONNECTORS__SLACK__CHANNELS", "C1,C2")

    with pytest.raises(ConfigError, match="multi-workspace"):
        OpsHubSettings()


def test_slack_channels_comma_string_via_model_validate() -> None:
    """A bare comma string is accepted by the field validator directly too."""
    settings = SlackWorkspaceSettings.model_validate({"channels": "C1,C2"})
    assert [c.id for c in settings.channels] == ["C1", "C2"]
