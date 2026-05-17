"""Pydantic Settings base.

Phase 1 step 5 shipped the minimal root settings (config / data dirs). Step 12
extends it with three nested sections (`storage`, `workspace`, `embedding`)
that match `docs/phase-1-plan.md` §2.2 and ADR-0012.

Env vars use the ``OPSHUB_`` prefix with ``__`` as the nested delimiter, so
nested fields can be overridden via e.g. ``OPSHUB_STORAGE__DB_PATH=/tmp/x.db``
or ``OPSHUB_EMBEDDING__BACKEND=local``.

Path defaults follow the XDG Base Directory specification so that opshub
data does not leak into the user's home directory root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opshub.core.errors import ConfigError


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def default_config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/opshub`` (or ``~/.config/opshub``)."""
    return _xdg_config_home() / "opshub"


def default_data_dir() -> Path:
    """Return ``$XDG_DATA_HOME/opshub`` (or ``~/.local/share/opshub``)."""
    return _xdg_data_home() / "opshub"


def _default_db_path() -> Path:
    return default_data_dir() / "db" / "opshub.sqlite"


def _default_cache_dir() -> Path:
    return default_data_dir() / "cache"


def _default_workspace_root() -> Path:
    return Path.home() / "opshub" / "workspace"


EmbeddingBackend = Literal["disabled", "local", "openai", "voyage"]


class StorageSettings(BaseModel):
    """SQLite / cache filesystem locations.

    Both defaults resolve under ``default_data_dir()`` so a user who relocates
    ``OPSHUB_DATA_DIR`` does *not* automatically relocate these — the section
    overrides are independent on purpose (ADR-0012 keeps storage separate from
    the XDG data dir so external mounts / encrypted volumes can be plugged in).
    """

    db_path: Path = Field(default_factory=_default_db_path)
    cache_dir: Path = Field(default_factory=_default_cache_dir)


class WorkspaceSettings(BaseModel):
    """Per-user workspace tree (cloned repos, scratch files, etc.)."""

    root: Path = Field(default_factory=_default_workspace_root)


class EmbeddingSettings(BaseModel):
    """Embedding backend selection (see ADR-0012).

    All descriptor fields are optional. When ``backend`` selects a real
    embedder (``"local"`` / ``"openai"`` / ``"voyage"``),
    :func:`opshub.vectors.factory.build_embedder` substitutes
    backend-specific defaults for any field left as ``None`` (e.g.
    ``backend = "local"`` with no other keys means
    bge-m3 / 1024-dim). The Phase 4 step A5 refinement adds
    ``dimensions`` so callers can override the default vector size
    without forking a custom embedder.

    Disabled-state invariant: when ``backend = "disabled"``, every
    descriptor must stay ``None``. Setting them alongside ``disabled``
    is silently misleading (the values are never read), so we reject
    the combination at validation time — ADR-0012 §3 calls this out
    explicitly as the kind of config drift to fail loud on.
    """

    backend: EmbeddingBackend = "disabled"
    model_id: str | None = None
    model_version: str | None = None
    api_base_url: str | None = None
    dimensions: int | None = None

    @model_validator(mode="after")
    def _check_disabled_has_no_descriptors(self) -> EmbeddingSettings:
        if self.backend == "disabled":
            extras = {
                "model_id": self.model_id,
                "model_version": self.model_version,
                "api_base_url": self.api_base_url,
                "dimensions": self.dimensions,
            }
            populated = sorted(name for name, value in extras.items() if value is not None)
            if populated:
                raise ConfigError(
                    "embedding.backend='disabled' forbids "
                    f"{', '.join(populated)}; clear these fields or pick a real backend",
                )
        return self


class OpsHubSettings(BaseSettings):
    """Root settings.

    Env vars use the ``OPSHUB_`` prefix with ``__`` as the nested delimiter so
    that nested overrides such as ``OPSHUB_STORAGE__DB_PATH=...`` and
    ``OPSHUB_EMBEDDING__BACKEND=local`` work without code changes.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPSHUB_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    config_dir: Path = Field(default_factory=default_config_dir)
    data_dir: Path = Field(default_factory=default_data_dir)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
