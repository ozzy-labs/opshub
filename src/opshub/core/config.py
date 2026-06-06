"""Pydantic Settings base.

Phase 1 step 5 shipped the minimal root settings (config / data dirs). Step 12
extends it with three nested sections (`storage`, `workspace`, `embedding`)
that match `docs/phase-1-plan.md` §2.2 and ADR-0012.

Env vars use the ``OPSHUB_`` prefix with ``__`` as the nested delimiter, so
nested fields can be overridden via e.g. ``OPSHUB_STORAGE__DB_PATH=/tmp/x.db``
or ``OPSHUB_EMBEDDING__BACKEND=local``.

Path defaults follow the XDG Base Directory specification so that opshub
data does not leak into the user's home directory root.

Phase 17 ([ADR-0032](../../../docs/adr/0032-runtime-toml-config-loading.md), #418)
wires :class:`TomlConfigSettingsSource` into the sources tuple so the
starter ``config.toml`` written by ``opshub init`` is **actually read at
runtime**. Before #418, ``OpsHubSettings()`` only consulted env vars +
field defaults — the TOML file was silently ignored, which was the
behaviour ``docs/upgrading.md`` already described incorrectly. The TOML
file path is resolved from ``$OPSHUB_CONFIG_DIR`` (when set) or
:func:`default_config_dir` (XDG fallback). Priority order, highest →
lowest: ``init args`` > env > dotenv > **toml** > file_secret > defaults.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource
from pydantic_settings.sources import PydanticBaseSettingsSource

from opshub.core.errors import ConfigError, ValidationError
from opshub.core.time import parse_since


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


#: Per-channel ``since`` value that disables the date floor for that
#: channel, i.e. "sync this channel's full history regardless of the
#: global ``sync_since``" (Phase 20, ADR-0036 §(d)). Kept as a module
#: constant so the connector's floor resolver
#: (:func:`opshub.connectors.slack.connector._floor_to_ts`) and these
#: validators agree on the sentinel spelling.
SLACK_FULL_HISTORY_SENTINEL = "all"


#: Phase 20-C late-reply polling activity window (default 30 days). Threads
#: whose ``last_reply_ts`` is older than this window are pruned from the
#: ``threads`` axis at the end of each sync (ADR-0030 §(d) revised). Keeping
#: the constant module-local so the connector's pruning helper and the
#: settings default agree on the value.
SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW = timedelta(days=30)


#: Sentinel value for ``[connectors.slack] thread_activity_window`` that
#: disables the late-reply polling prune entirely (Phase 20-E audit
#: followup, [#478](https://github.com/ozzy-labs/opshub/issues/478)).
#: ``docs/troubleshooting.md`` §3.12 and ``docs/upgrading.md`` §Phase 20
#: documented the spelling as ``"all"`` ahead of time; the validator
#: coerces it to ``None`` so the connector's ``_prune_inactive_threads``
#: / ``_window_cutoff_ts`` helpers see a single ``None`` sentinel and
#: skip the prune wholesale. Case-insensitive (``"All"`` / ``"ALL"``
#: also accepted) so a copy-paste from a doc with title-casing still
#: works.
SLACK_THREAD_ACTIVITY_WINDOW_ALL_SENTINEL = "all"


#: Phase 20-C duration grammar for ``[connectors.slack] thread_activity_window``.
#: Accepts ``<N>d`` / ``<N>w`` (lifted from :data:`opshub.core.time._SINCE_RELATIVE_RE`)
#: so the operator surface is identical to ``sync_since``. Months / years are
#: intentionally unsupported — ``90d`` covers any practical window.
_THREAD_ACTIVITY_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([dw])\s*$")


def _coerce_thread_activity_window(value: Any) -> Any:
    """Accept ``"30d"`` / ``"4w"`` / ``"all"`` strings alongside :class:`timedelta` / ``int``.

    Pydantic's native ``timedelta`` parsing handles ISO 8601 durations
    (``"P30D"``) and integer seconds, but the operator-facing surface
    for ``[connectors.slack]`` already uses the ``"7d"`` / ``"2w"``
    grammar (``sync_since`` / per-channel ``since``). Mirroring that
    here keeps every Slack-side duration knob spelled the same way.

    Phase 20-E ([#478](https://github.com/ozzy-labs/opshub/issues/478))
    adds the ``"all"`` sentinel (case-insensitive) that maps to
    ``None`` to disable the prune entirely — the spelling
    ``docs/troubleshooting.md`` §3.12 already promises operators.
    A :class:`timedelta` / int / ``None`` passes through unchanged so
    pydantic's existing coercion paths still apply.
    """
    if isinstance(value, str):
        if value.strip().lower() == SLACK_THREAD_ACTIVITY_WINDOW_ALL_SENTINEL:
            # ``None`` is the connector-side "disable prune" sentinel.
            # Coercing the operator-facing ``"all"`` spelling here keeps
            # ``_prune_inactive_threads`` / ``_window_cutoff_ts`` aware
            # of a single shape (``timedelta | None``) — they branch on
            # ``None`` to skip prune wholesale.
            return None
        match = _THREAD_ACTIVITY_WINDOW_RE.match(value)
        if match is None:
            # Let pydantic raise its native error (e.g. ISO 8601 attempt)
            # so the operator sees one validation message rather than two.
            return value
        amount = int(match.group(1))
        unit = match.group(2)
        try:
            return timedelta(days=amount) if unit == "d" else timedelta(weeks=amount)
        except OverflowError as exc:
            raise ConfigError(
                f"[connectors.slack] thread_activity_window {value!r} is too "
                "long; pick a smaller window (e.g. '90d')."
            ) from exc
    return value


def _validate_slack_floor(value: str | None, *, field: str) -> str | None:
    """Validate a Slack date-floor value (``sync_since`` / per-channel ``since``).

    ``None`` (inherit / no floor) and the :data:`SLACK_FULL_HISTORY_SENTINEL`
    (``"all"`` — explicit full backfill) pass through untouched. Any
    other value must parse as a :func:`opshub.core.time.parse_since`
    relative duration (``90d`` / ``4w``) or ISO date (``2026-01-01``);
    a malformed value raises :class:`~opshub.core.errors.ConfigError` at
    settings-construction time (fail-fast) rather than surfacing as a
    cryptic failure mid-sync. The raw string is preserved (not the
    parsed datetime) so relative floors are re-evaluated at sync time
    (ADR-0036 §(f)).
    """
    if value is None or value == SLACK_FULL_HISTORY_SENTINEL:
        return value
    try:
        parse_since(value, field=field)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    return value


class SlackChannelSpec(BaseModel):
    """One ``[[connectors.slack.channels]]`` entry (Phase 20, ADR-0036).

    ``id`` is the Slack channel / DM id (``"C0123ABC"`` / ``"D0123ABC"``).
    ``since`` is the optional per-channel date floor: ``None`` (the
    default) inherits the connector-wide :attr:`SlackConnectorSettings.sync_since`,
    :data:`SLACK_FULL_HISTORY_SENTINEL` (``"all"``) forces a full-history
    backfill for this channel even when a global floor is set, and any
    other value (``"90d"`` / ``"2026-01-01"``) sets a channel-specific
    floor that overrides the global default (ADR-0036 §(d)/(e)).

    Operators may also write a bare channel id string in the ``channels``
    array (``channels = ["C0123ABC"]``); :meth:`SlackConnectorSettings._coerce_channel_ids`
    normalises it to ``{"id": "C0123ABC"}`` so the historical string-array
    form (and the ``opshub slack conversations --format=toml`` snippet)
    keeps working unchanged.
    """

    id: str
    since: str | None = None

    @field_validator("since")
    @classmethod
    def _check_since(cls, value: str | None) -> str | None:
        return _validate_slack_floor(value, field="[connectors.slack] channels[].since")


def _empty_channel_list() -> list[SlackChannelSpec]:
    """Typed ``default_factory`` for :attr:`SlackConnectorSettings.channels`.

    A bare ``default_factory=list`` infers ``list[Unknown]`` under pyright
    strict for a model-typed list (unlike ``list[str]``, which the builtin
    factory resolves cleanly), so we spell the factory's return type out.
    """
    return []


class SlackConnectorSettings(BaseModel):
    """Slack connector tuning (Phase 7 step A3; Phase 20 date floor).

    ``enabled = False`` is the default per Phase 7 plan §1 #2 — every
    SaaS connector is opt-in so a fresh ``uv tool install`` never tries
    to reach Slack on first run. Operators flip the flag and populate
    ``channels`` after running ``opshub slack auth set`` to
    store the OAuth access token.

    ``channels`` selects the Slack channel ids / DMs the connector will
    sync. Each entry is a :class:`SlackChannelSpec` (``id`` + optional
    per-channel ``since`` floor); a bare id string is also accepted and
    normalised to ``{"id": ...}`` so ``channels = ["C0123ABC"]`` and the
    ``opshub slack conversations --format=toml`` snippet keep working.
    Channel *names* (``#general``) are intentionally not accepted —
    channel membership / access is keyed on the id and Slack does not
    guarantee name stability. Empty list means "no channels configured"
    — the connector surfaces this as a structured warning and runs as a
    no-op (the sync command still exits 0).

    ``sync_since`` (Phase 20, ADR-0036) is the connector-wide date floor:
    ``conversations.history`` is bounded so messages older than the floor
    are never fetched, capping the cold-start / newly-added-channel
    backfill. ``None`` (the default) keeps the historical behaviour of
    backfilling the full channel history. Accepts a relative duration
    (``"90d"`` / ``"4w"``, evaluated at sync time) or an ISO date
    (``"2026-01-01"``). Per-channel :attr:`SlackChannelSpec.since`
    overrides it; ``since = "all"`` opts a single channel back into full
    backfill. The floor only ever moves the resume bound *forward* — an
    already-synced channel's cursor is authoritative, so enabling
    ``sync_since`` never re-fetches or deletes history (ADR-0036 §(g)).

    The OAuth access token lives in the OS keyring under
    ``connector:slack:token`` per ADR-0014 / ADR-0018 — it never
    appears in ``opshub.toml`` or this settings model. User Token
    (``xoxp-``) is the first-class principal; Bot Token (``xoxb-``)
    is accepted as an alternative.
    """

    enabled: bool = False
    channels: list[SlackChannelSpec] = Field(default_factory=_empty_channel_list)
    sync_since: str | None = None
    #: Phase 20-C (ADR-0030 §(d) revised): activity window for the late-reply
    #: polling phase. Threads whose ``last_reply_ts`` falls outside this
    #: window are skipped on the polling phase and pruned from the
    #: ``threads`` axis of the compound cursor at the end of each sync.
    #: Default 30d — long enough that "late but recent" replies (the
    #: workspace-norm case of a reply a few weeks after the parent post)
    #: are still picked up, short enough that the per-sync
    #: ``conversations.replies`` budget stays bounded. Operators can
    #: override via this setting or the ``--thread-activity-window`` CLI
    #: flag / ``OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW`` env
    #: var. Accepts ``"7d"`` / ``"4w"`` strings (uniform with
    #: ``sync_since``) plus pydantic's native :class:`timedelta`
    #: coercion (ISO 8601 ``"P7D"`` / integer seconds). Phase 20-E
    #: ([#478](https://github.com/ozzy-labs/opshub/issues/478)) adds
    #: the ``"all"`` (case-insensitive) sentinel that coerces to
    #: ``None`` so the late-reply polling prune is disabled entirely —
    #: the spelling ``docs/troubleshooting.md`` §3.12 promised
    #: operators ahead of the validator catching up.
    thread_activity_window: timedelta | None = SLACK_DEFAULT_THREAD_ACTIVITY_WINDOW

    @field_validator("channels", mode="before")
    @classmethod
    def _coerce_channel_ids(cls, value: Any) -> Any:
        """Accept a bare id string per entry, normalising to ``{"id": ...}``.

        Keeps the historical ``channels = ["C0123ABC", "C0456DEF"]``
        string-array form (and the ``opshub slack conversations
        --format=toml`` paste snippet, which emits exactly that shape)
        valid alongside the new ``[[connectors.slack.channels]]`` table
        form (ADR-0036 §(b)). Non-list / already-dict entries pass
        through for pydantic to validate / reject normally.
        """
        if isinstance(value, list):
            # ``cast`` narrows pyright's ``isinstance(Any, list)`` widen to
            # ``list[Unknown]``; mypy treats it as redundant (Any → list[Any])
            # so we suppress only mypy, mirroring ``_load_cursors`` in the
            # Slack connector.
            items = cast(  # type: ignore[redundant-cast]
                "list[Any]", value
            )
            return [{"id": item} if isinstance(item, str) else item for item in items]
        return value

    @field_validator("sync_since")
    @classmethod
    def _check_sync_since(cls, value: str | None) -> str | None:
        return _validate_slack_floor(value, field="[connectors.slack] sync_since")

    @field_validator("thread_activity_window", mode="before")
    @classmethod
    def _coerce_thread_activity_window(cls, value: Any) -> Any:
        return _coerce_thread_activity_window(value)

    @model_validator(mode="after")
    def _check_unique_channel_ids(self) -> SlackConnectorSettings:
        """Reject duplicate channel ids — a copy-paste accident otherwise
        silently double-syncs (and double-floors) a channel.
        """
        ids = [spec.id for spec in self.channels]
        duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
        if duplicates:
            raise ConfigError(
                f"[connectors.slack] channels has duplicate id(s): {duplicates}; "
                "each channel id must appear at most once."
            )
        return self


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
    ``opshub box auth set``.
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

    Path-based exclusion lives **only** in the shared
    ``~/.config/opshub/excludes.yaml`` ``paths:`` selector
    (ADR-0020 §(b)). The Phase 9 inline ``exclude_globs`` field was
    removed in epic #470 — ``model_config = ConfigDict(extra="forbid")``
    rejects stale TOML carrying the key so operators get a fail-fast
    :class:`~pydantic.ValidationError` rather than the silent
    "no path filter applied" degradation the dual-read shim used to
    mask. See ``docs/upgrading.md`` for the migration step.

    Phase 11 F4 (ADR-0019 §(b') + ADR-0025) adds ``content_extraction``:

    * ``content_extraction = False`` (default) — the connector retains
      Phase 9 behaviour bit-for-bit: ``stat()``-only walk, no
      ``open()`` / ``read_*`` calls anywhere, ``body=None`` on every
      :class:`~opshub.domain.events.source.SourceObserved`. Upgrading
      from Phase 9 to Phase 11 is therefore a no-op until the operator
      opts in.
    * ``content_extraction = True`` — the connector calls
      :func:`opshub.core.document_extract.extract_document` for files
      whose extension matches
      :data:`opshub.core.document_extract.SOURCE_TYPE_BY_EXTENSION`
      (``.docx`` / ``.xlsx`` / ``.pptx`` / legacy ``.doc`` / ``.xls`` /
      ``.ppt``). The extracted markdown becomes ``body`` and the
      ``source_type`` switches from ``"box_drive_file"`` to the
      Office-specific discriminator (``"word_document"`` /
      ``"excel_spreadsheet"`` / ``"powerpoint_slide_deck"``). All
      other files keep the ``"box_drive_file"`` / ``body=None`` shape.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    root_path: Path | None = None
    max_depth: int = 16
    max_files: int = 100_000
    follow_symlinks: bool = False
    content_extraction: bool = False


class OneDriveDriveConnectorSettings(BaseModel):
    """OneDrive (local-filesystem-backed) connector configuration (Phase 11 F4-b, ADR-0019 §(j)).

    The ``onedrive_drive`` connector mirrors :class:`BoxDriveConnectorSettings`
    structurally: it reads a local OneDrive desktop sync folder on the
    host filesystem rather than the Microsoft Graph API. Phase 11 F4-b
    (ADR-0019 §(j) パターン汎化) factors the local-FS contract out of
    the box_drive precedent so onedrive_drive — and any future
    ``local_drive`` family connector — share the same shape:
    ``stat()``-only scan by default, opt-in
    :func:`opshub.core.document_extract.extract_document` hook for
    Office bodies, identical structural safety caps.

    ``enabled = False`` is the default (matches every other connector
    and ADR-0019 §決定 (a)) so a fresh ``uv tool install`` never tries
    to walk an arbitrary directory on first run.

    ``root_path`` is the absolute path to the OneDrive sync folder. A
    ``None`` value (the default) delegates to
    :func:`opshub.core.platform.onedrive_drive_default_root_path` so
    WSL2 hosts pick up ``/mnt/onedrive`` and macOS hosts pick up
    ``~/OneDrive``. Linux native hosts have no default — the connector
    raises :class:`ConfigError` with a pointer to
    ``docs/onedrive-drive-setup.md`` at first sync.

    Structural safety caps mirror :class:`BoxDriveConnectorSettings`
    one-for-one so operators can move between the two without
    re-learning knobs:

    * ``max_depth = 16`` / ``max_files = 100_000`` — same blow-up
      guards.
    * ``follow_symlinks = False`` — OneDrive does not synthesise
      symlinks of its own, so any link under the root is
      operator-introduced and likely escapes the workspace.
    * ``content_extraction = False`` — ADR-0019 §(b') opt-in. When
      ``true``, ``.docx`` / ``.xlsx`` / ``.pptx`` (and legacy
      ``.doc`` / ``.xls`` / ``.ppt``) are routed through
      :func:`opshub.core.document_extract.extract_document`; everything
      else stays on the stat-only path. Default-off path keeps the
      no-``open()`` invariant intact bit-for-bit.

    Path-based exclusion lives **only** in the shared
    ``~/.config/opshub/excludes.yaml`` ``paths:`` selector
    (ADR-0020 §(b)). The Phase 11 F4-b inline ``exclude_globs`` field
    was removed in epic #470 — ``model_config = ConfigDict(extra="forbid")``
    rejects stale TOML carrying the key, symmetric with
    :class:`BoxDriveConnectorSettings`. See ``docs/upgrading.md`` for
    the migration step.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    root_path: Path | None = None
    max_depth: int = 16
    max_files: int = 100_000
    follow_symlinks: bool = False
    content_extraction: bool = False


class ExcelOfficeSettings(BaseModel):
    """Excel-specific extraction caps (Phase 11 audit Cluster B, ADR-0025 §決定 (e)).

    Per-sheet and per-workbook cell ceilings layered on top of the
    unified character cap in :class:`OfficeSettings`. ADR-0025 §決定 (e)
    pinned both numbers as the operator override path while the
    Phase 11 MVP relied on the character cap alone (markitdown's
    XlsxConverter already collapses huge sheets into compact markdown
    tables). The settings exist here so an opt-in toml override is
    available the moment the cell-level refinement lands; the
    extractor function (:func:`opshub.core.document_extract.extract_document`)
    already accepts both as parameters for API-shape stability.

    Defaults match ADR-0025 §決定 (e) exactly:

    * ``max_cells_per_sheet = 10_000`` — single-sheet cap.
    * ``max_cells_per_workbook = 50_000`` — sum across all sheets.

    Pass ``0`` to either field to disable the corresponding cap (matches
    the file-size / char-cap "0 = unlimited (非推奨)" convention in
    :class:`OfficeSettings`).
    """

    max_cells_per_sheet: int = 10_000
    max_cells_per_workbook: int = 50_000


class OfficeSettings(BaseModel):
    """Office document extraction tuning (Phase 11 audit Cluster B, ADR-0025).

    Operator-facing settings for the
    :func:`opshub.core.document_extract.extract_document` Office body
    extractor. ADR-0025 §決定 (b) pinned both caps as the two-layer
    defence against context-window explosion and storage blow-up; this
    section is the documented override path the ADR promises.

    Defaults track ADR-0025 §決定 (b) one-for-one:

    * ``max_file_size_mb = 50`` — pre-extraction file size ceiling. Box
      Drive / OneDrive Desktop documents that exceed this skip
      extraction entirely (``body=None`` + ``skip_reason="file too
      large"``) so a single 100MB-plus workbook never blocks the scan.
    * ``max_chars = 500_000`` — post-extraction markdown char cap. The
      extractor head-truncates beyond this and appends a deterministic
      ``[truncated: original=<N> chars, limit=<M>]`` notice so callers
      (recall, brief, propose) see the truncation cue without extra
      plumbing.
    * ``excel.max_cells_per_sheet = 10_000`` /
      ``excel.max_cells_per_workbook = 50_000`` — ADR-0025 §決定 (e)
      cell ceilings; surfaced for shape stability ahead of the
      cell-level refinement landing.

    Env-var override pattern follows the standard nested delimiter
    (``__``): ``OPSHUB_OFFICE__MAX_FILE_SIZE_MB=100``,
    ``OPSHUB_OFFICE__MAX_CHARS=1000000``,
    ``OPSHUB_OFFICE__EXCEL__MAX_CELLS_PER_SHEET=20000``.

    The ``[connectors.box_drive] content_extraction`` /
    ``[connectors.onedrive_drive] content_extraction`` /
    ``[connectors.google_workspace] content_extraction`` opt-in still
    gates whether the extractor is invoked at all; this section only
    tunes the per-call caps once an operator opts in (ADR-0019 §(b')
    + ADR-0025 §決定 (g) two-key composition). Phase 13 G4 (#278)
    extended the propagation surface to the Google Workspace
    connector so all three opt-in paths share the same operator
    knob.
    """

    max_file_size_mb: int = 50
    max_chars: int = 500_000
    excel: ExcelOfficeSettings = Field(default_factory=ExcelOfficeSettings)


class GoogleWorkspaceConnectorSettings(BaseModel):
    """Google Workspace connector configuration (Phase 13 Sub-issue G3, ADR-0010 §Phase 13 改訂).

    The Google Workspace connector reads Google Drive API v3
    ``changes.list`` with a single ``startPageToken`` cursor and a TTL
    expiry fallback (Drive returns 404 / 410 once a stored token
    crosses the ~30-day vendor window; the connector bootstraps a
    fresh token via ``changes.getStartPageToken`` and walks forward).
    Phase 13 plan §1 OQ5 + ADR-0010 §Phase 13 改訂 (g) pin the
    contract.

    ``enabled = False`` is the default per Phase 7 plan §1 #2 — every
    SaaS connector is opt-in so a fresh ``uv tool install`` never
    tries to reach an external API. Operators flip the flag and
    populate ``client_id`` + ``client_secret`` after registering an
    OAuth client in Google Cloud Console (Installed Application type;
    see ``docs/google-workspace-setup.md`` which lands in G5).

    ``client_id`` has no useful default — Google's OAuth endpoint
    rejects requests without a registered client — so we leave it as
    an empty string and let :class:`GoogleWorkspaceAuth` raise a
    :class:`~opshub.core.errors.ConfigError` at construction time.
    Same shape for ``client_secret``: although Google documents the
    installed-app client secret as "not actually secret" (it can be
    extracted from any distributed binary), Google's OAuth endpoint
    requires it on every code-exchange / refresh round-trip on the
    wire. The empty default keeps the typing tight (``str`` rather
    than ``str | None``) and matches the
    :class:`MS365ConnectorSettings` / :class:`BoxConnectorSettings`
    style.

    ``redirect_uri`` defaults to ``http://localhost`` per Google's
    documented installed-app convention. opshub's paste-code flow
    does not actually listen on this URI — the operator copies the
    ``?code=...`` parameter out of the URL Google redirects to. The
    URI only needs to match the value the operator registered with
    Google Cloud Console.

    ``content_extraction`` defaults to ``False`` (Phase 13 G3 ships
    metadata-only). Phase 13 G4 (#278) wires this flag to the
    connector so that
    :func:`opshub.core.document_extract.extract_workspace_export`
    is called on the bytes returned by Drive's
    ``files.export(fileId, mimeType=<MS Office mediatype>)`` for the
    three Google Workspace native source_types (``google_doc`` /
    ``google_slides`` / ``google_sheets``); non-native files (the
    catch-all ``google_workspace_file``) stay metadata-only because
    Drive returns 403 ``fileNotExportable`` for them. When ``True``,
    the connector also propagates the
    :class:`OfficeSettings` overrides (``[office] max_file_size_mb``
    / ``[office] max_chars`` / ``[office.excel] max_cells_*``) so a
    single operator override governs Box Drive / OneDrive / Google
    Workspace bodies in lockstep (ADR-0025 §決定 (g) two-key
    composition). Default ``False`` keeps the G3 metadata-only
    behaviour bit-for-bit (no ``files.export`` call, no markitdown
    cost) so upgrading from G3 → G4 is a no-op until the operator
    opts in.

    ``fallback_window_days`` controls how far back the connector scans
    when Drive rejects the stored ``startPageToken`` with 404 / 410
    (ADR-0010 §Phase 13 改訂 (g) TTL fallback). Defaults to ``30`` —
    long enough to cover a typical vacation / outage window without
    slurping years of history. Operators with longer outages set this
    higher (a temporary ``365`` for re-onboarding is the documented
    pattern). A value of ``0`` disables the full-pass scan entirely —
    the connector then bootstraps a fresh token via
    ``changes.getStartPageToken`` without backfilling the TTL gap,
    meaning any changes that occurred while the token was expired are
    lost (discouraged but allowed for operators who explicitly opt out
    of the recovery cost). Mirrors
    :class:`TeamsConnectorSettings.fallback_window_days` (Phase 11
    ADR-0010 §改訂 (c)) so the two delta-cursor connectors expose one
    operator-facing knob.

    The Refresh Token lives in the OS keyring under
    ``connector:google_workspace:refresh_token`` per ADR-0014
    §Phase 7 Validation (Phase 13 改訂 で 3 件目の rotation pin として
    追加) — it never appears in ``opshub.toml`` or this settings
    model.
    """

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost"
    content_extraction: bool = False
    fallback_window_days: int = 30


class GoogleCalendarConnectorSettings(BaseModel):
    """Google Calendar connector configuration (Phase 14 Sub-issue G4, ADR-0010 §Phase 14 改訂).

    The Google Calendar connector reads Google Calendar API v3
    ``events.list`` with a ``syncToken`` delta cursor and a 410 GONE
    TTL-fallback path (Calendar invalidates sync tokens after a
    vendor-defined window; the connector falls back to a
    ``timeMin`` / ``timeMax`` window walk and persists the freshly
    minted ``nextSyncToken`` as the new cursor). Phase 14 plan §1 OQ5
    + ADR-0010 §Phase 14 改訂 (j) pin the contract.

    ``enabled = False`` is the default per Phase 7 plan §1 #2
    convention — every SaaS connector is opt-in so a fresh
    ``uv tool install`` never tries to reach an external API.
    Operators flip the flag after configuring the shared Google
    OAuth principal under ``[connectors.google_workspace]``
    (``client_id`` + ``client_secret``; Phase 14 G2 generalised the
    OAuth helper so one principal covers Drive + Gmail + Calendar
    per Phase 14 plan §1 OQ6).

    ``calendar_id`` defaults to ``"primary"`` (the operator's primary
    calendar). Phase 14 G4 MVP fetches only the primary calendar per
    Phase 14 plan OQ13 — secondary calendar loop is a Phase 15+
    extension. Operators on a multi-calendar workflow can override
    this to a specific calendar id today but only one calendar can be
    fetched per sync until the loop ships.

    ``time_min_days`` controls the backward window edge for first-sync
    bootstrap + TTL fallback. Defaults to ``90`` (Phase 14 plan OQ11)
    — long enough to cover a typical seasonal cycle without slurping
    years of history. ``time_max_days`` controls the forward edge,
    defaulting to ``365`` so events the operator already accepted for
    the next year appear in the recall projection from day one. Both
    are inclusive day counts measured from "now"; the connector
    expands them into RFC 3339 timestamps when it calls
    ``events.list``.

    Operators with longer outages bump ``time_min_days`` temporarily
    (e.g. ``365`` for re-onboarding after a multi-month gap). Both
    fields accept ``0`` (the window collapses to a single instant —
    not useful in production but accepted so the connector does not
    fail-fast on an obvious-misconfig boundary; Calendar would reject
    the resulting call and the operator would see the error in the
    sync log).

    The Refresh Token lives in the OS keyring under
    ``connector:google_workspace:refresh_token`` per ADR-0014 §Phase 7
    Validation (one slot shared by Drive / Gmail / Calendar per Phase
    14 plan OQ6) — it never appears in ``opshub.toml`` or this
    settings model. There is intentionally no ``[connectors.google_calendar]
    client_id`` field; the Calendar connector reads the principal from
    ``[connectors.google_workspace]`` to make the "1 Google account =
    1 principal" rule visible in the config shape.
    """

    enabled: bool = False
    calendar_id: str = "primary"
    time_min_days: int = 90
    time_max_days: int = 365


class GoogleMailConnectorSettings(BaseModel):
    """Gmail connector configuration (Phase 14 Sub-issue G3, ADR-0010 §Phase 14 改訂).

    The Gmail connector reads Gmail API v1 ``users.history.list`` with
    a single ``startHistoryId`` cursor and a TTL expiry fallback
    (Gmail returns 404 ``historyNotFound`` once a stored history id
    crosses the documented ~7-day vendor window; the connector runs a
    ``users.messages.list?q=after:<epoch>`` full-pass over the
    configured ``fallback_window_days`` and then bootstraps a fresh
    id via ``users.getProfile``). Phase 14 plan §1 OQ5 + ADR-0010
    §Phase 14 改訂 (j) pin the contract — the same delta-cursor +
    TTL-fallback recipe Phase 11 Teams and Phase 13 Google Workspace
    use.

    ``enabled = False`` is the default per Phase 7 plan §1 #2 — every
    SaaS connector is opt-in so a fresh ``uv tool install`` never
    tries to reach an external API. Operators flip the flag and rely
    on the shared :class:`GoogleWorkspaceConnectorSettings`
    ``client_id`` / ``client_secret`` (Phase 14 plan §1 OQ6 + §X.1:
    one Google account = one principal, three connectors share the
    same OAuth client + refresh token + keyring slot). The Gmail
    connector deliberately does **not** carry its own ``client_id``
    / ``client_secret`` — adding them would invite operators to
    register a second OAuth client and split the principal, which
    contradicts the shared-foundation design and would break the
    "one consent grants every Google connector" contract.

    ``initial_window_days`` controls how far back the connector walks
    on first sync (when the cursor is ``None``). Defaults to ``7`` —
    long enough that the operator's recent inbox shows up in the
    assistant's first ``personal-brief`` / ``next-actions`` run but
    short enough that the first sync does not download years of
    history (a separate ``opshub source backfill`` workflow lives in
    Phase 15+ for explicit backfills). A value of ``0`` means "skip
    the initial backfill entirely" — the connector then only sees
    messages that arrive after the cursor bootstrap.

    ``fallback_window_days`` controls how far back the connector
    scans when Gmail rejects the stored ``startHistoryId`` with 404
    ``historyNotFound`` (ADR-0010 §Phase 14 改訂 (j) TTL fallback).
    Defaults to ``30`` — long enough to cover a typical vacation /
    outage window without slurping years of history. Operators with
    longer outages set this higher (a temporary ``365`` for
    re-onboarding is the documented pattern). A value of ``0``
    disables the full-pass scan entirely — the connector then
    bootstraps a fresh history id without backfilling the TTL gap,
    meaning any messages that arrived while the id was expired are
    lost (discouraged but allowed for operators who explicitly opt
    out of the recovery cost). Mirrors
    :class:`GoogleWorkspaceConnectorSettings.fallback_window_days`
    so the three delta-cursor connectors (Drive / Gmail / Calendar)
    expose one operator-facing knob.

    The Refresh Token lives in the shared OS keyring slot
    ``connector:google_workspace:refresh_token`` per ADR-0014
    §Phase 7 Validation + Phase 14 改訂 (m) — the Gmail connector
    does not own a separate token, it shares the Drive / Calendar
    principal.
    """

    enabled: bool = False
    initial_window_days: int = 7
    fallback_window_days: int = 30


class TeamsConnectorSettings(BaseModel):
    """Microsoft Teams connector configuration (Phase 11 F5).

    The Teams connector reads Microsoft Graph
    ``/me/chats/getAllMessages`` with delta-token pagination and a
    full-pass fallback when Graph invalidates the stored delta link
    (ADR-0010 §改訂 (c)).

    ``enabled = False`` is the default per the Phase 7 plan §1 #2
    convention — every SaaS connector is opt-in so a fresh
    ``uv tool install`` never tries to reach an external API.

    ``fallback_window_days`` controls how far back the connector scans
    when Graph rejects the stored delta link. Defaults to ``30`` per
    ADR-0010 §改訂 (c) — long enough to cover a typical vacation /
    outage window without slurping years of history. Operators with
    longer outages set this higher (a temporary ``365`` for
    re-onboarding is the documented pattern). A value of ``0``
    disables the fallback path entirely — the connector then surfaces
    the underlying :class:`ConnectorFailedError` instead of recovering
    (discouraged but allowed).

    The Microsoft Graph User Token lives in the OS keyring under
    ``connector:teams:token`` per ADR-0014 / ADR-0010 §改訂 (d) — it
    never appears in ``opshub.toml`` or this settings model.
    """

    enabled: bool = False
    fallback_window_days: int = 30


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
    pre-existing connectors by design. Phase 11 F5 adds
    :class:`TeamsConnectorSettings` for the Microsoft Teams chat
    connector (Graph delta query + User Token principal). Phase 11
    F4-b (ADR-0019 §(j)) adds :class:`OneDriveDriveConnectorSettings`
    for the OneDrive local-FS connector — the second entry in the
    ``local_drive`` family, structurally identical to
    :class:`BoxDriveConnectorSettings`. Phase 13 G3 (ADR-0010 §Phase 13
    改訂) adds :class:`GoogleWorkspaceConnectorSettings` for the Google
    Drive API v3 connector — OAuth Refresh Token + ``changes.list``
    cursor + (G4 で対応予定の) ``files.export``-based body extraction.

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
    onedrive_drive: OneDriveDriveConnectorSettings = Field(
        default_factory=OneDriveDriveConnectorSettings
    )
    teams: TeamsConnectorSettings = Field(default_factory=TeamsConnectorSettings)
    google_workspace: GoogleWorkspaceConnectorSettings = Field(
        default_factory=GoogleWorkspaceConnectorSettings
    )
    google_calendar: GoogleCalendarConnectorSettings = Field(
        default_factory=GoogleCalendarConnectorSettings
    )
    # Phase 14 G3 (#295) — Gmail connector. Shares the OAuth client +
    # refresh token with ``google_workspace`` (Phase 14 plan §1 OQ6 +
    # §X.1: one Google account = one principal, three connectors).
    google_mail: GoogleMailConnectorSettings = Field(default_factory=GoogleMailConnectorSettings)


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

    Phase 17 ([ADR-0032](../../../docs/adr/0032-runtime-toml-config-loading.md),
    #418) overrides :meth:`settings_customise_sources` to insert
    :class:`TomlConfigSettingsSource` into the sources tuple between
    ``dotenv_settings`` and ``file_secret_settings``. The TOML path is
    resolved at instantiation time from ``$OPSHUB_CONFIG_DIR`` (when
    set) or :func:`default_config_dir` (XDG fallback) — keying the
    lookup off an env var rather than a class attribute keeps the
    operator-facing ``OPSHUB_CONFIG_DIR=/tmp/x opshub ...`` override
    working without code changes. A missing TOML file is treated as an
    empty config (no exception), so a fresh ``uv tool install`` that
    has not yet run ``opshub init`` still produces working defaults.
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
    office: OfficeSettings = Field(default_factory=OfficeSettings)

    def __init__(self, **values: Any) -> None:
        """Construct settings; convert pydantic ValidationError to ConfigError.

        ADR-0032 §決定 (#418) — once ``config.toml`` is read at runtime,
        a typo in the operator-edited file (e.g. ``backend = "grok"``)
        produces a :class:`pydantic.ValidationError`. The CLI driver's
        existing error-rendering path already understands
        :class:`ConfigError` (single-line stderr summary, exit 1), so
        re-wrapping the pydantic exception keeps the operator-facing
        UX consistent across all three config sources (TOML / env /
        init args) without forcing every CLI command to import
        :class:`pydantic.ValidationError` separately.

        The wrap preserves ``__cause__`` so the underlying pydantic
        diagnostic chain stays accessible for ``--debug`` traces
        (ADR-0027 §決定 (c)). Pure :class:`OpsHubError` subclasses
        (e.g. the :meth:`EmbeddingSettings._check_disabled_has_no_descriptors`
        validator already raising :class:`ConfigError` directly) pass
        through untouched because they never trigger the
        :class:`pydantic.ValidationError` branch.
        """
        try:
            super().__init__(**values)
        except PydanticValidationError as exc:
            raise ConfigError(
                f"invalid OpsHub config: {exc}. Check $OPSHUB_CONFIG_DIR/config.toml, "
                "OPSHUB_* env vars, and the field constraints in "
                "src/opshub/core/config.py."
            ) from exc

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert ``TomlConfigSettingsSource`` into the sources tuple.

        Priority (highest → lowest): ``init_settings`` > ``env_settings``
        > ``dotenv_settings`` > ``toml_settings`` > ``file_secret_settings``.
        The order matches ADR-0032 §決定 — env vars (the documented CI /
        headless path) always win over the on-disk TOML, init args (used
        by ``OpsHubSettings(storage=StorageSettings(...))``-style overrides
        in tests / programmatic code) always win over env, and defaults
        are the floor.

        The TOML path is recomputed every call so ``monkeypatch.setenv(
        "OPSHUB_CONFIG_DIR", ...)`` in tests / ``$OPSHUB_CONFIG_DIR=...``
        on the CLI redirects the lookup without code changes. A
        non-existent file is **not** an error — :class:`TomlConfigSettingsSource`
        returns an empty mapping in that case, so a fresh ``uv tool install``
        that has not yet run ``opshub init`` keeps working off defaults.
        Malformed TOML (a syntax error in the file the operator hand-edited)
        raises :class:`tomllib.TOMLDecodeError` from inside the source
        constructor; the CLI driver wraps it in a :class:`ConfigError` so
        the operator gets an actionable message instead of a raw Python
        traceback.
        """
        config_dir_env = os.environ.get("OPSHUB_CONFIG_DIR")
        config_dir = Path(config_dir_env) if config_dir_env else default_config_dir()
        toml_path = config_dir / "config.toml"
        try:
            toml_settings: PydanticBaseSettingsSource = TomlConfigSettingsSource(
                settings_cls, toml_file=toml_path
            )
        except tomllib.TOMLDecodeError as exc:
            # ``TomlConfigSettingsSource`` raises ``tomllib.TOMLDecodeError``
            # synchronously from its constructor when the file is malformed.
            # Surface it as our project-level :class:`ConfigError` so the
            # CLI driver can render an actionable message instead of leaking
            # the raw stdlib traceback. Path is included so the operator
            # knows exactly which file to fix.
            raise ConfigError(
                f"failed to parse {toml_path}: {exc}. Fix the TOML syntax or "
                "delete the file and re-run `opshub init` to regenerate the starter."
            ) from exc
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            toml_settings,
            file_secret_settings,
        )

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
