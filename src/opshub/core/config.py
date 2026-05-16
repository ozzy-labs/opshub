"""Pydantic Settings base.

Phase 1 step 5 ships the minimal root settings (config / data dirs) so that
later steps can extend it. Concrete sections (`[storage]`, `[workspace]`,
`[embedding]`) land in step 12 (see docs/phase-1-plan.md §2.2).

Path defaults follow the XDG Base Directory specification so that opshub
data does not leak into the user's home directory root.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class OpsHubSettings(BaseSettings):
    """Root settings. Section-specific subclasses extend this in step 12.

    Env vars use the ``OPSHUB_`` prefix with ``__`` as the nested delimiter so
    that future ``OPSHUB_STORAGE__DB_PATH=...`` style overrides work without
    code changes.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPSHUB_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    config_dir: Path = Field(default_factory=default_config_dir)
    data_dir: Path = Field(default_factory=default_data_dir)
