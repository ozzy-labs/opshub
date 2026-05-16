"""Tests for the storage / workspace / embedding settings sections.

These cover Phase 1 step 12 (ADR-0012). Kept in a separate file from
``test_config.py`` so the step 5 baseline tests stay untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.config import (
    EmbeddingSettings,
    OpsHubSettings,
    StorageSettings,
    WorkspaceSettings,
    default_data_dir,
)
from opshub.core.errors import ConfigError

# ---- defaults -----------------------------------------------------------


def test_storage_defaults_resolve_under_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    storage = StorageSettings()
    assert storage.db_path == default_data_dir() / "db" / "opshub.sqlite"
    assert storage.cache_dir == default_data_dir() / "cache"


def test_workspace_default_is_under_home() -> None:
    workspace = WorkspaceSettings()
    assert workspace.root == Path.home() / "opshub" / "workspace"


def test_embedding_default_is_disabled() -> None:
    embedding = EmbeddingSettings()
    assert embedding.backend == "disabled"
    assert embedding.model_id is None
    assert embedding.model_version is None
    assert embedding.api_base_url is None


def test_opshub_settings_sections_are_default_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear nested env vars so factories drive every value.
    for var in (
        "OPSHUB_STORAGE__DB_PATH",
        "OPSHUB_STORAGE__CACHE_DIR",
        "OPSHUB_WORKSPACE__ROOT",
        "OPSHUB_EMBEDDING__BACKEND",
        "OPSHUB_EMBEDDING__MODEL_ID",
        "OPSHUB_EMBEDDING__MODEL_VERSION",
        "OPSHUB_EMBEDDING__API_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = OpsHubSettings()
    assert isinstance(settings.storage, StorageSettings)
    assert isinstance(settings.workspace, WorkspaceSettings)
    assert isinstance(settings.embedding, EmbeddingSettings)
    assert settings.embedding.backend == "disabled"


def test_default_factories_are_not_shared_between_instances() -> None:
    """Guard against the classic mutable-default footgun.

    Even if Pydantic copies on assignment today, an accidental switch to a
    module-level instance would silently make every ``OpsHubSettings`` share
    one ``StorageSettings``, so we assert independence directly.
    """
    a = OpsHubSettings()
    b = OpsHubSettings()
    assert a.storage is not b.storage
    assert a.workspace is not b.workspace
    assert a.embedding is not b.embedding


# ---- env overrides ------------------------------------------------------


def test_storage_db_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", "/tmp/x.db")
    settings = OpsHubSettings()
    assert settings.storage.db_path == Path("/tmp/x.db")


def test_workspace_root_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", "/tmp/ws")
    settings = OpsHubSettings()
    assert settings.workspace.root == Path("/tmp/ws")


def test_embedding_backend_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_EMBEDDING__MODEL_ID", "bge-m3")
    monkeypatch.setenv("OPSHUB_EMBEDDING__MODEL_VERSION", "1.0")
    settings = OpsHubSettings()
    assert settings.embedding.backend == "local"
    assert settings.embedding.model_id == "bge-m3"
    assert settings.embedding.model_version == "1.0"


# ---- validator ----------------------------------------------------------


def test_embedding_disabled_rejects_model_id() -> None:
    with pytest.raises(ConfigError, match="model_id"):
        EmbeddingSettings(backend="disabled", model_id="x")


def test_embedding_disabled_rejects_model_version() -> None:
    with pytest.raises(ConfigError, match="model_version"):
        EmbeddingSettings(backend="disabled", model_version="1.0")


def test_embedding_disabled_rejects_api_base_url() -> None:
    with pytest.raises(ConfigError, match="api_base_url"):
        EmbeddingSettings(backend="disabled", api_base_url="https://example.com")


def test_embedding_local_with_descriptors_validates() -> None:
    embedding = EmbeddingSettings(
        backend="local",
        model_id="bge-m3",
        model_version="1.0",
    )
    assert embedding.backend == "local"
    assert embedding.model_id == "bge-m3"
    assert embedding.model_version == "1.0"
    assert embedding.api_base_url is None


def test_embedding_openai_with_api_base_url_validates() -> None:
    embedding = EmbeddingSettings(
        backend="openai",
        model_id="text-embedding-3-small",
        model_version="2024-10-01",
        api_base_url="https://api.openai.example/v1",
    )
    assert embedding.api_base_url == "https://api.openai.example/v1"


def test_embedding_invalid_backend_rejected() -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        EmbeddingSettings(backend="bogus")  # type: ignore[arg-type]
