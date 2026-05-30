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

#: Allowed values for ``[llm] backend``. ``"disabled"`` is the Phase 5
#: default per ADR-0015 §決定 (b) — opting into a real backend always
#: requires an explicit config change so a fresh ``uv tool install``
#: never silently hits a billed API on first run.
LLMBackend = Literal["disabled", "anthropic", "openai", "ollama"]


class StorageSettings(BaseModel):
    """SQLite / cache filesystem locations.

    Both defaults resolve under ``default_data_dir()`` so a user who relocates
    ``OPSHUB_DATA_DIR`` does *not* automatically relocate these — the section
    overrides are independent on purpose (ADR-0012 keeps storage separate from
    the XDG data dir so external mounts / encrypted volumes can be plugged in).

    ``encryption`` (Phase 10, ADR-0021) toggles whole-DB SQLCipher
    AES-256 encryption at rest. When ``True``, the engine factory opens
    the DB through the SQLCipher driver and applies ``PRAGMA key`` from
    the keyring-managed key (:mod:`opshub.core.encryption`); the
    ``encryption`` extras (``sqlcipher3-binary``) must be installed.

    The default is ``False`` so a fresh ``uv tool install`` / CI / test
    run works without the native SQLCipher binary. ADR-0021 §(b) calls
    for ``True`` once external bodies are retained (ADR-0020); operators
    enabling Full Local Content Retention flip this and run
    ``opshub init`` (or set ``OPSHUB_DB_ENCRYPTION_KEY``) so the key is
    provisioned before the DB is written.
    """

    db_path: Path = Field(default_factory=_default_db_path)
    cache_dir: Path = Field(default_factory=_default_cache_dir)
    encryption: bool = False


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
    # Phase 5 step C1: opt-in projector hook that embeds new entities
    # immediately after their originating event commits. Default MUST
    # remain ``False`` so Phase 4 behaviour is unchanged (existing
    # users rely on the CLI-driven ``opshub embeddings rebuild`` /
    # ``opshub embeddings drain`` flow). Setting ``auto = true`` only
    # takes effect when ``backend != "disabled"``: the composition
    # root in :mod:`opshub.cli._wiring` refuses to wire the hook when
    # there is no embedder to call.
    auto: bool = False

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


class AnthropicLLMSettings(BaseModel):
    """Anthropic LLM backend tuning (see ADR-0015 §決定 (c)).

    Defaults are pinned to the Phase 5 推奨 model
    (``claude-haiku-4-5-20251001``): cost-effective Haiku tier, briefing
    does not need tool_use. Operators can override either field via the
    ``opshub.toml`` ``[llm.anthropic]`` section or env vars
    (``OPSHUB_LLM__ANTHROPIC__MODEL_ID`` etc.).
    """

    model_id: str = "claude-haiku-4-5-20251001"
    model_version: str = "2026-05-01"


class OpenAILLMSettings(BaseModel):
    """OpenAI chat-completions LLM backend tuning (see ADR-0015 §決定 (c)).

    Defaults track the cost-effective ``gpt-4o-mini`` tier; operators can
    upgrade to ``gpt-4o`` / ``o1`` via the ``opshub.toml``
    ``[llm.openai]`` section or env vars
    (``OPSHUB_LLM__OPENAI__MODEL_ID`` etc.).
    """

    model_id: str = "gpt-4o-mini"
    model_version: str = "2026-05-01"


class OllamaLLMSettings(BaseModel):
    """Ollama local LLM backend tuning (see ADR-0016 §決定 (h)).

    Defaults target the Phase 6 推奨 local model (``llama3.2:3b`` — 2 GB,
    CPU-friendly) talking to the default Ollama daemon location
    (``http://localhost:11434``). No API key is needed (local daemon).

    Operators can override every field via the ``opshub.toml``
    ``[llm.ollama]`` section or env vars
    (``OPSHUB_LLM__OLLAMA__MODEL_ID`` / ``OPSHUB_LLM__OLLAMA__HOST`` /
    ``OPSHUB_LLM__OLLAMA__TIMEOUT_SECONDS``).

    ``timeout_seconds`` is exposed because local model latency varies
    wildly with hardware — a 3 B model can answer in <5 s on a recent
    Apple Silicon but easily exceeds 30 s on a CPU-only laptop. The
    Anthropic / OpenAI clients delegate timeouts to the SDK defaults
    (~600 s), so an explicit knob is only needed for the Ollama path.
    """

    model_id: str = "llama3.2:3b"
    model_version: str = "ollama"
    host: str = "http://localhost:11434"
    timeout_seconds: float = 60.0


class SlackConnectorSettings(BaseModel):
    """Slack connector tuning (Phase 7 step A3).

    ``enabled = False`` is the default per Phase 7 plan §1 #2 — every
    SaaS connector is opt-in so a fresh ``uv tool install`` never tries
    to reach Slack on first run. Operators flip the flag and populate
    ``channels`` after running ``opshub connector auth set slack`` to
    store the OAuth access token.

    ``channels`` is the list of Slack channel ids
    (``["C0123ABC", "C0456DEF"]``) the connector will sync. Channel
    *names* (``#general``) are intentionally not accepted — channel
    membership / access is keyed on the id and Slack does not
    guarantee name stability, so accepting names would force a
    per-sync ``conversations.list`` lookup that risks the Tier-2
    rate-limit budget. Empty list means "no channels configured" —
    the connector surfaces this as a structured warning and runs as
    a no-op (the sync command still exits 0; the operator sees the
    warning in the structured log).

    The OAuth access token lives in the OS keyring under
    ``connector:slack:token`` per ADR-0014 / ADR-0018 — it never
    appears in ``opshub.toml`` or this settings model. User Token
    (``xoxp-``) is the first-class principal; Bot Token (``xoxb-``)
    is accepted as an alternative.
    """

    enabled: bool = False
    channels: list[str] = Field(default_factory=list)


class MS365ConnectorSettings(BaseModel):
    """Microsoft 365 connector tuning (Phase 7 step B1).

    ``enabled = False`` is the default per Phase 7 plan §1 #2 — every
    SaaS connector is opt-in so a fresh ``uv tool install`` never tries
    to reach an external API. Operators flip the flag and populate
    ``client_id`` after registering an Azure AD app (free for personal
    Microsoft accounts; see docs/phase-7-plan.md §2.2 B1).

    ``client_id`` has no useful default — Azure AD's "common" tenant
    will reject any request without a registered application — so we
    leave it as an empty string and let :class:`MS365Auth` raise a
    :class:`~opshub.core.errors.ConfigError` with the actionable hint
    at construction time. The empty default keeps the typing tight
    (``str`` rather than ``str | None``) and matches the
    :class:`OpenAILLMSettings` / :class:`AnthropicLLMSettings` style.

    ``authority`` defaults to Microsoft's ``/common`` endpoint so both
    personal (consumer) and work / school accounts work without
    further configuration. Operators with a single Entra tenant can
    override it via ``[connectors.ms365] authority``.

    The per-endpoint ``calendar_enabled`` / ``onedrive_enabled`` /
    ``outlook_enabled`` flags (Phase 7 step B3) default to ``True`` so
    a freshly-enabled MS365 connector observes every endpoint group
    out of the box; operators who only consented to a subset of
    scopes (e.g. ``Calendars.Read`` only) flip the unused flags to
    ``False`` so :meth:`MS365Connector.sync` skips them without
    raising an authorisation error.
    """

    enabled: bool = False
    client_id: str = ""
    authority: str = "https://login.microsoftonline.com/common"
    calendar_enabled: bool = True
    onedrive_enabled: bool = True
    outlook_enabled: bool = True


class BoxConnectorSettings(BaseModel):
    """Box connector configuration (Phase 7 step C1).

    Holds the **non-sensitive** Box app metadata. The accompanying
    secrets (``client_secret`` and rotating ``refresh_token``) live in
    the OS keyring under ``connector:box:client_secret`` /
    ``connector:box:refresh_token`` per ADR-0014 — they never appear
    in ``opshub.toml``.

    Per phase-7-plan §1 #2, the connector defaults to ``enabled = false``
    so a fresh install never tries to talk to Box without an explicit
    opt-in. Operators set ``enabled = true`` after running
    ``opshub connector auth set connector:box``.
    """

    enabled: bool = False
    client_id: str = ""


class BoxDriveConnectorSettings(BaseModel):
    """Box Drive (local-filesystem-backed) connector configuration (Phase 9, ADR-0019).

    The ``box_drive`` connector reads a local Box Drive desktop client
    mount point on the host filesystem rather than the Box Platform
    API (the Phase 7 :class:`BoxConnectorSettings` covers that path).
    Operator-facing IT policies sometimes block the SaaS API entirely;
    ADR-0019 introduces this connector specifically so opshub still
    has visibility into Box content under those constraints.

    ``enabled = False`` is the default per ADR-0019 §決定 (a) — the
    connector is opt-in so a fresh ``uv tool install`` never tries to
    walk an arbitrary directory on first run.

    ``root_path`` is the absolute path to the Box Drive mount point.
    A ``None`` value (the default) delegates to
    :func:`opshub.core.platform.box_drive_default_root_path` so WSL2
    hosts pick up ``/mnt/b`` and macOS hosts pick up ``~/Box``
    automatically. Linux native hosts have no default — the connector
    raises :class:`ConfigError` with a pointer to
    ``docs/box-drive-setup.md`` at first sync.

    Structural safety caps mirror the scanner's defaults
    (:class:`opshub.connectors.box_drive.scanner.BoxDriveScanner`):

    * ``max_depth = 16`` — generous for typical Box Drive workspaces
      (rarely more than 8 deep) and tight enough that a misconfigured
      root cannot enumerate ``/`` indefinitely.
    * ``max_files = 100_000`` — escape hatch when a single scan
      exceeds the cap. Operators with very large workspaces raise this
      in ``opshub.toml``; values past ~1M should prompt a Phase 9.x
      chunked-scan discussion.
    * ``follow_symlinks = False`` — Box Drive does not synthesise
      symlinks of its own, so any link under the root is operator-made
      and likely escapes the workspace. The safe default refuses to
      follow them.
    * ``exclude_globs = []`` — fnmatch / gitignore-style patterns
      (``"**/.DS_Store"``, ``"**/secrets/**"``, ...) that the scanner
      skips. Empty list means "no exclusions".
    """

    enabled: bool = False
    root_path: Path | None = None
    max_depth: int = 16
    max_files: int = 100_000
    follow_symlinks: bool = False
    exclude_globs: list[str] = Field(default_factory=list)


class ConnectorSettings(BaseModel):
    """External SaaS / local-FS connector configuration root.

    Phase 7 introduces this section as the dedicated home for each
    connector's tuning (enable flag + OAuth metadata). Step B1 added
    the :class:`MS365ConnectorSettings` field, and step C1 adds
    :class:`BoxConnectorSettings`. Phase 9 step B2 (ADR-0019) adds
    :class:`BoxDriveConnectorSettings` for the local-filesystem-backed
    Box Drive connector — the first non-SaaS connector in opshub, so
    its shape (``root_path`` / ``max_depth`` / ``max_files`` instead
    of ``client_id`` / OAuth metadata) differs from the four
    pre-existing connectors by design.

    The section is intentionally separate from :class:`LLMSettings` /
    :class:`EmbeddingSettings` so per-connector overrides like
    ``OPSHUB_CONNECTORS__MS365__CLIENT_ID=...`` or
    ``OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH=/mnt/b`` follow the
    documented nested-env-var pattern without colliding with the
    LLM/embedding namespaces.
    """

    slack: SlackConnectorSettings = Field(default_factory=SlackConnectorSettings)
    ms365: MS365ConnectorSettings = Field(default_factory=MS365ConnectorSettings)
    box: BoxConnectorSettings = Field(default_factory=BoxConnectorSettings)
    box_drive: BoxDriveConnectorSettings = Field(default_factory=BoxDriveConnectorSettings)


class LLMSettings(BaseModel):
    """LLM backend selection (see ADR-0015 + ADR-0016 §決定 (h)).

    ``backend = "disabled"`` is the Phase 5 default per ADR-0015 §決定 (b)
    — opting into ``anthropic`` / ``openai`` / ``ollama`` always requires
    an explicit config / env change so a fresh ``uv tool install`` does
    not silently bill the operator (API backends) or fail-fast on a
    missing local daemon (Ollama) on first run.

    Per-backend nested sections (``anthropic`` / ``openai`` / ``ollama``)
    carry the model_id / model_version defaults (plus host / timeout for
    Ollama) that :mod:`opshub.llm.factory` forwards to the concrete
    client. The shape mirrors :class:`EmbeddingSettings` so operators
    keep one mental model.
    """

    backend: LLMBackend = "disabled"
    anthropic: AnthropicLLMSettings = Field(default_factory=AnthropicLLMSettings)
    openai: OpenAILLMSettings = Field(default_factory=OpenAILLMSettings)
    ollama: OllamaLLMSettings = Field(default_factory=OllamaLLMSettings)


class OpsHubSettings(BaseSettings):
    """Root settings.

    Env vars use the ``OPSHUB_`` prefix with ``__`` as the nested delimiter so
    that nested overrides such as ``OPSHUB_STORAGE__DB_PATH=...`` and
    ``OPSHUB_EMBEDDING__BACKEND=local`` work without code changes.

    The ``llm`` section additionally honours the convenience env var
    ``OPSHUB_LLM_BACKEND`` (single underscore — no nested delimiter) via
    a small ``model_validator`` below, mirroring ADR-0015 §決定 (d)'s
    "env var override is the documented CI / headless path" stance.
    Per-backend overrides still use the canonical
    ``OPSHUB_LLM__ANTHROPIC__MODEL_ID`` etc. form.
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
    llm: LLMSettings = Field(default_factory=LLMSettings)
    connectors: ConnectorSettings = Field(default_factory=ConnectorSettings)

    @model_validator(mode="after")
    def _apply_llm_backend_env_shortcut(self) -> OpsHubSettings:
        """Honour the single-underscore ``OPSHUB_LLM_BACKEND`` env shortcut.

        ``pydantic_settings`` only resolves nested fields via the
        configured delimiter (``OPSHUB_LLM__BACKEND``). Operators
        following ADR-0015's documented env var pattern often reach for
        ``OPSHUB_LLM_BACKEND`` first; we treat that as a convenience
        alias for the backend selector specifically (not the per-backend
        model_id fields, which stay nested-only to avoid ambiguity).
        """
        override = os.environ.get("OPSHUB_LLM_BACKEND")
        if override is None:
            return self
        # Re-validate through the LLMSettings model so a bogus value
        # like ``OPSHUB_LLM_BACKEND=grok`` fails at config-load time
        # instead of leaking through to ``build_llm_client``.
        self.llm = LLMSettings(
            backend=override,  # type: ignore[arg-type]
            anthropic=self.llm.anthropic,
            openai=self.llm.openai,
        )
        return self
