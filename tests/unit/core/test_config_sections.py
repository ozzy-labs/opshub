"""Tests for the storage / workspace / embedding settings sections.

These cover Phase 1 step 12 (ADR-0012). Kept in a separate file from
``test_config.py`` so the step 5 baseline tests stay untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.config import (
    AnthropicLLMSettings,
    EmbeddingSettings,
    LLMSettings,
    OpenAILLMSettings,
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
    assert embedding.dimensions is None


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
        "OPSHUB_EMBEDDING__DIMENSIONS",
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


def test_embedding_disabled_rejects_dimensions() -> None:
    with pytest.raises(ConfigError, match="dimensions"):
        EmbeddingSettings(backend="disabled", dimensions=1024)


def test_embedding_local_accepts_minimal_config() -> None:
    """Phase 4 step A5: ``backend = "local"`` alone is now valid.

    The :mod:`opshub.vectors.factory` supplies backend-specific
    defaults (model_id, dim) so config files can opt into a backend
    without forcing the operator to also pin the model id / dim.
    """
    embedding = EmbeddingSettings(backend="local")
    assert embedding.backend == "local"
    assert embedding.model_id is None
    assert embedding.dimensions is None


def test_embedding_local_with_dimensions_override() -> None:
    embedding = EmbeddingSettings(backend="local", dimensions=768)
    assert embedding.dimensions == 768


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


# ---- LLM settings (Phase 5 step A5, ADR-0015) ---------------------------


def test_llm_default_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0015 §決定 (b): a fresh install must not silently hit a billed API."""
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    monkeypatch.delenv("OPSHUB_LLM__BACKEND", raising=False)
    settings = LLMSettings()
    assert settings.backend == "disabled"


def test_llm_default_model_ids_match_adr_0015(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0015 §決定 (c): pin the cost-effective Haiku / gpt-4o-mini defaults."""
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    settings = LLMSettings()
    assert settings.anthropic.model_id == "claude-haiku-4-5-20251001"
    assert settings.anthropic.model_version == "2026-05-01"
    assert settings.openai.model_id == "gpt-4o-mini"
    assert settings.openai.model_version == "2026-05-01"


def test_opshub_settings_includes_llm_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    settings = OpsHubSettings()
    assert isinstance(settings.llm, LLMSettings)
    assert isinstance(settings.llm.anthropic, AnthropicLLMSettings)
    assert isinstance(settings.llm.openai, OpenAILLMSettings)
    assert settings.llm.backend == "disabled"


def test_llm_backend_env_shortcut_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OPSHUB_LLM_BACKEND`` is the single-underscore convenience alias.

    ADR-0015 §決定 (d) documents the env var pattern as the CI / headless
    escape hatch; operators typically reach for the flat form first.
    Pinning this here keeps the convenience alias from regressing into
    the canonical nested-only form.
    """
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    settings = OpsHubSettings()
    assert settings.llm.backend == "anthropic"


def test_llm_backend_nested_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical nested form ``OPSHUB_LLM__BACKEND`` also works.

    ``pydantic_settings`` resolves nested fields via the configured
    ``__`` delimiter regardless of the convenience alias above; keeping
    both paths covered means operators following either docs style get
    the same result.
    """
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "openai")
    settings = OpsHubSettings()
    assert settings.llm.backend == "openai"


def test_llm_backend_env_shortcut_rejects_bogus_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus ``OPSHUB_LLM_BACKEND`` must fail at config-load time.

    Letting ``grok`` etc. silently leak through to ``build_llm_client``
    would surface as a confusing "unknown llm backend" error far from
    the actual misconfiguration; we re-validate via ``LLMSettings`` so
    the failure happens at ``OpsHubSettings()`` construction instead.

    Phase 17 (ADR-0032, #418) wrapped :class:`pydantic.ValidationError`
    inside :class:`ConfigError` at :meth:`OpsHubSettings.__init__` time
    so the CLI driver renders a single-line actionable error across all
    three config sources (TOML / env / init args). The original pydantic
    exception is chained via ``__cause__`` for ``--debug`` diagnostics.
    """
    from pydantic import ValidationError as PydanticValidationError

    from opshub.core.errors import ConfigError

    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "grok")
    with pytest.raises(ConfigError) as excinfo:
        OpsHubSettings()
    # Underlying pydantic diagnostic is preserved via __cause__.
    assert isinstance(excinfo.value.__cause__, PydanticValidationError)


def test_llm_anthropic_model_id_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-backend nested env overrides use the canonical ``__`` delimiter."""
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    monkeypatch.setenv("OPSHUB_LLM__ANTHROPIC__MODEL_ID", "claude-sonnet-4-5-20251001")
    settings = OpsHubSettings()
    assert settings.llm.anthropic.model_id == "claude-sonnet-4-5-20251001"


def test_llm_default_factories_are_not_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against the mutable-default footgun, same as Phase 1 step 12
    coverage for storage / workspace / embedding."""
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    a = OpsHubSettings()
    b = OpsHubSettings()
    assert a.llm is not b.llm
    assert a.llm.anthropic is not b.llm.anthropic
    assert a.llm.openai is not b.llm.openai


def test_llm_invalid_backend_rejected() -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        LLMSettings(backend="bogus")  # type: ignore[arg-type]
