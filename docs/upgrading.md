# Upgrading OpsHub

OpsHub uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
current `0.x` line means the public API surface may change between minor
versions until `1.0.0`; patch versions (`0.y.z`) stay backward-compatible.

## Database migrations

OpsHub stores all state in a single SQLite database (default path:
`$XDG_DATA_HOME/opshub/db/opshub.sqlite`). Schema migrations are tracked by
[Alembic](https://alembic.sqlalchemy.org/). Apply pending migrations after
upgrading the CLI:

```bash
uv tool upgrade opshub
opshub db migrate                # apply pending alembic upgrades
```

The `db migrate` command is idempotent — running it without pending migrations
is a no-op. The current head revision corresponds to the highest-numbered file
under [`src/opshub/db/migrations/versions/`](https://github.com/ozzy-labs/opshub/tree/main/src/opshub/db/migrations/versions).

## Downgrading

Alembic supports `downgrade` operations on every shipped migration. The
`opshub db migrate` CLI does not yet expose a `--to <revision>` flag in
`0.1.0`; for now, roll back via the alembic CLI directly:

```bash
ALEMBIC_CONFIG=...alembic.ini uv run alembic downgrade <revision>
```

Downgrade contracts:

- Phase 4 migration `0013_create_embeddings_vec_table` (vec0 virtual tables)
  downgrade is data-destructive for `embeddings_vec_*` tables (vec0 has no
  `RESTORE` semantics). Run `opshub embeddings rebuild` after rolling forward
  again to re-populate.
- Other migrations are reversible. See each migration's `downgrade()` for
  specifics.

## Configuration changes between versions

OpsHub's config file (`$XDG_CONFIG_HOME/opshub/config.toml`) is loaded at
runtime via [`pydantic-settings`](https://docs.pydantic.dev/latest/usage/pydantic_settings/)'s
`TomlConfigSettingsSource` ([ADR-0032](adr/0032-runtime-toml-config-loading.md), [#418](https://github.com/ozzy-labs/opshub/issues/418)).
Each `OpsHubSettings()` instantiation re-reads the file from disk and merges
its keys into the settings tree. Unknown keys are silently ignored
(`model_config.extra = "ignore"`), so new optional config fields added in a
minor version are backward-compatible — old configs continue to work and the
defaults apply.

Resolution priority (highest → lowest):

1. **init args** — values passed to `OpsHubSettings(storage=..., embedding=...)`
   directly (used by tests and programmatic wiring).
2. **env vars** — `OPSHUB_<SECTION>__<FIELD>=...` (e.g.
   `OPSHUB_EMBEDDING__BACKEND=openai`) plus the convenience aliases
   `OPSHUB_CONFIG_DIR` / `OPSHUB_DATA_DIR` / `OPSHUB_LLM_BACKEND`.
3. **dotenv** — `.env` next to the CLI invocation (pydantic-settings default).
4. **TOML file** — `$OPSHUB_CONFIG_DIR/config.toml` if `$OPSHUB_CONFIG_DIR` is
   set, otherwise `$XDG_CONFIG_HOME/opshub/config.toml`.
5. **field defaults** — every section's `Field(default_factory=...)`.

A missing TOML file is **not** an error — a fresh `uv tool install` that has
not yet run `opshub init` falls through to step 5 cleanly. Malformed TOML
(syntax error from a hand-edit) surfaces as `ConfigError` with the file path
in the message so the operator can find and fix it.

To temporarily relocate the config directory (e.g. for a CI smoke test):

```bash
OPSHUB_CONFIG_DIR=/tmp/opshub-ci opshub init
OPSHUB_CONFIG_DIR=/tmp/opshub-ci opshub task list   # same dir is consulted
```

Pre-#418 versions silently ignored `config.toml` at runtime — the file was
written by `opshub init` but only env vars + field defaults flowed into
`OpsHubSettings()`. After upgrading, any setting an operator had previously
expressed in `config.toml` (and worked around by re-exporting as an `OPSHUB_*`
env var) starts taking effect from the file directly; the env-var workaround
still wins because env > toml in the priority order above.

## Embedding model changes

Switching `[embedding] backend` (e.g. `disabled` → `openai` or `openai` →
`local`) changes the active `model_id`. Existing embeddings remain in the
database but are not used until you re-embed:

```bash
opshub embeddings rebuild                  # re-embeds everything under the new backend
opshub embeddings status                   # confirms backend + embedded counts
```

`opshub recall` and `opshub brief` will return `ConfigError` until a rebuild
under the new backend produces matching embeddings. This is intentional — it
prevents stale embeddings from leaking into recall results.

## v0.1.0 specifics

- First public release. No upgrade path from a prior version.
- DB head = `0016_create_links_table` (Phase 8).

## Phase 10: full local body retention + encryption at rest

Phase 10 introduced **full local content retention** ([ADR-0020](adr/0020-full-local-content-retention.md), supersedes ADR-0005) and **opt-in encryption at rest** ([ADR-0021](adr/0021-encryption-at-rest.md)).

### Behavioural change: bodies are now retained

Connectors (`slack` / `ms365` / `box` / `github`) now persist the full message / issue / PR / email body in `sources.body` alongside the existing summary, tagged with `provenance_origin` / `provenance_trust`. The `box_drive` FS-scan connector is exempt — it remains metadata-only (ADR-0019 §不変条件 (b) forbids `open()` on FS-backed sources).

After upgrading, run a one-time backfill for projections and embeddings to populate the new body-derived index:

```bash
opshub db migrate                          # apply 0018_add_body_provenance_to_sources + 0019_create_sources_fts
opshub projections rebuild                 # refresh sources projection from the event log (body column included)
opshub embeddings rebuild                  # re-embed using bodies instead of summaries (ADR-0012 改訂 §4)
```

Existing pre-Phase-10 rows continue to work — `body = NULL` round-trips cleanly through every query path (recall, search, brief).

### Opt-in: enable encryption at rest

Operators handling regulated / sensitive bodies should enable SQLCipher-backed encryption. Encryption is **off by default** to keep the cold-install footprint minimal.

```bash
# 1. Install the encryption extras (pulls sqlcipher3-binary + keyring).
uv tool install "ozzylabs-opshub[encryption]"   # add to your existing install

# 2. Mint the DB key in the OS keychain (recommended).
opshub init                                       # auto-mints db:encryption_key on first run
# Or, for CI / non-interactive contexts:
export OPSHUB_DB_ENCRYPTION_KEY="<long random hex>"

# 3. Flip the config flag.
# In ~/.config/opshub/config.toml:
#   [storage]
#   encryption = true

# 4. Re-create the DB encrypted (export → import; ADR-0021 §(c) — no in-place re-key in 0.x).
opshub db migrate                                # idempotent if already at head
```

If `[storage] encryption = true` is set but no key is reachable (no keyring slot, no env var), every CLI invocation fails fast with `ConfigError: no DB key` — pinned by `tests/unit/core/test_encryption.py::test_require_db_key_raises_when_absent`.

### Cross-connector ingest excludes

A new `~/.config/opshub/excludes.yaml` (ADR-0020 §(b), parsed by `src/opshub/core/excludes.py`) lets you keep specific channels / senders / repos / paths out of the body store. Excluded rows are dropped at connector-fetch time so they never enter `sources.body` in the first place. Format:

```yaml
# Top-level flat keys — the four connector identity dimensions
# (ADR-0020 §(b)). Each connector consults only the dimensions
# meaningful for it (Slack → channels + senders, GitHub → repos +
# senders, box_drive / OneDrive → paths, Teams → channels + senders).
# Nested per-connector forms (``slack: { channels: [...] }`` etc.)
# are **rejected** with ``ConfigError`` so a typo / stale doc never
# silently disables exclusion.
channels:
  - C0LEAKS
  - C09SECRET
  - "19:secret-teams-channel-id"
senders:
  - security-bot
  - legal@example.com
repos:
  - org/private-incident-log
paths:
  - "/Confidential/**"
```

The file is optional. With no `excludes.yaml`, every connector keeps its default fetch behaviour.

### Phase 10 specifics

- DB head = `0019_create_sources_fts` (Phase 10).
- New optional extras: `encryption` (SQLCipher) / `mcp` (MCP server SDK).
- New CLI subcommands: `opshub search` (FTS5 cross-connector full-text search), `opshub mcp serve` (stdio MCP server), `opshub mcp tools` (tool registry inspection).
- `opshub propose generate --reply-to <source-id>` enters reply-draft mode (ADR-0016 §決定 (i)). `propose apply` saves the draft locally — there is no write-back to SaaS (ADR-0010 §禁止事項 7).

## Phase 11: MS Office deep-dive (Teams + Outlook body + Office extraction)

Phase 11 ([ADR-0025](adr/0025-office-document-content-extraction.md) + [ADR-0019](adr/0019-local-filesystem-backed-connector.md) revision §決定 (b')+(j) + [ADR-0010](adr/0010-connector-contract.md) revision (a)/(c)/(d)) extends the body store to Word / Excel / PowerPoint documents and adds two new connectors (`teams`, `onedrive_drive`). Existing pre-Phase-11 rows continue to work — opt-in behaviour means no automatic schema change.

### Opt-in: enable Office document content extraction

`box_drive` and `onedrive_drive` connectors gain a `content_extraction` switch (default off, ADR-0019 §決定 (b')). When enabled, `.docx` / `.xlsx` / `.pptx` files on the scanned mount are extracted via markitdown ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (a)) and stored in `sources.body` with `source_type` of `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`.

```bash
# 1. Install the office extras (pulls markitdown[docx,xlsx,pptx]).
uv tool install "ozzylabs-opshub[office]"

# 2. Flip the per-connector config flag.
# In ~/.config/opshub/config.toml:
#   [connectors.box_drive]
#   content_extraction = true
# or
#   [connectors.onedrive_drive]
#   content_extraction = true

# 3. Re-scan to pick up the bodies.
opshub box_drive sync
opshub onedrive_drive sync
opshub embeddings rebuild                        # re-embed with the new body content
```

Limits ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (b)):

- Files larger than 50 MB are skipped with **internal** `ExtractResult.body=None` + a warning log; the mapper substitutes `body = summary` so the projection persists a non-empty string (epic [#470](https://github.com/ozzy-labs/opshub/issues/470) / [ADR-0010](adr/0010-connector-contract.md) §不変条件 6 / [ADR-0020](adr/0020-full-local-content-retention.md) §(d'))
- Extracted text longer than 500 000 chars is head-truncated and annotated
- Extraction failures (corrupted file, password-protected workbook, unsupported sub-format) surface as **internal** `ExtractResult.body=None` + sanitised warning; the same `body = summary` substitute applies so the projection persists a non-empty string. The metadata row is still emitted so the scan never blocks on a single bad file

Per-operator overrides live under `[office]` in `opshub.toml` (`max_file_size_mb`, `max_chars`).

### Opt-in: enable the `teams` connector

```bash
# 1. Install the connectors-teams extras.
uv tool install "ozzylabs-opshub[connectors-teams]"

# 2. Register an Azure app and grant Chat.Read (see docs/teams-setup.md).
# 3. Store the Microsoft Graph User Token.
opshub teams auth set                            # paste-once
# Or for CI / non-interactive contexts:
export OPSHUB_CONNECTOR_TEAMS_TOKEN="<access_token>"

# 4. Flip the config flag.
# In ~/.config/opshub/config.toml:
#   [connectors.teams]
#   enabled = true
#   fallback_window_days = 30                    # delta-link invalidation fallback window

# 5. Sync.
opshub teams sync
```

The Teams connector uses Microsoft Graph delta query (`/me/chats/getAllMessages`). When Graph invalidates the stored delta token (`410 Gone` / `invalidatedDeltaToken`) the fetcher automatically falls back to a `$filter=lastModifiedDateTime ge <iso>` full pass over the last `fallback_window_days` (default 30) and re-acquires a fresh delta link ([ADR-0010](adr/0010-connector-contract.md) §改訂 (c)).

Full Teams setup (Azure app, scopes, troubleshooting): [`docs/teams-setup.md`](teams-setup.md).

### Opt-in: enable the `onedrive_drive` connector

The OneDrive Desktop FS-scan path mirrors the Phase 9 `box_drive` connector ([ADR-0019](adr/0019-local-filesystem-backed-connector.md) §決定 (j) pattern generalisation).

```bash
# 1. Ensure OneDrive Desktop is installed and signed in (macOS) or bind-mounted (WSL2).
# 2. Flip the config flag.
# In ~/.config/opshub/config.toml:
#   [connectors.onedrive_drive]
#   enabled = true
#   # root_path defaults: WSL2 = /mnt/onedrive ; macOS = ~/OneDrive
#   content_extraction = true                    # optional, requires [office] extras

# 3. Sync.
opshub onedrive_drive sync
```

Full OneDrive Drive setup (mount, root_path overrides, troubleshooting): [`docs/onedrive-drive-setup.md`](onedrive-drive-setup.md).

### Behavioural change: Outlook body deep retention

The existing `ms365` connector's Outlook mapper (`opshub.connectors.ms365.mapper.map_outlook_message`) now retains the full email body alongside the summary. Existing rows with `body = NULL` continue to round-trip cleanly through every query path (the recall / search / brief paths already tolerated NULL bodies in Phase 10). Re-running `opshub ms365 sync` after upgrading will start populating bodies for newly observed mail. Bodies larger than 500K chars are head-truncated inline.

To re-embed the freshly-retained bodies:

```bash
opshub ms365 sync
opshub embeddings rebuild
```

### Phase 11 specifics

- DB head = `0019_create_sources_fts` (Phase 10) — Phase 11 ships **no** new migrations; bodies flow through the existing `sources.body` column + FTS5 index.
- New optional extras: `office` (`markitdown[docx,xlsx,pptx]` — i.e. markitdown plus the `mammoth` / `openpyxl` / `python-pptx` sub-extras for the three Office sub-formats opshub extracts) / `connectors-teams` (msal + httpx).
- New connectors: `teams` (Microsoft Graph chat delta) / `onedrive_drive` (OneDrive Desktop FS scan).
- New `source_type` discriminators: `teams_message` / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`.
- No CLI breaking changes (Phase 11 当時). `opshub teams sync` / `opshub onedrive_drive sync` are the new sync targets.
- External write-back is **still forbidden** (ADR-0010 §禁止事項 7). The assistant drafts only; the operator sends.

## Phase 12: Assistant skills expansion

Phase 12 ([ADR-0004](adr/0004-agent-runtime-boundary.md) revision §決定 (c-2) + [ADR-0022](adr/0022-mcp-server-surface.md) revision §決定 (f) + [ADR-0016](adr/0016-action-loop-and-structured-output.md) revision §決定 (l)) grows the assistant skill repertoire from **5 to 14** and widens the MCP surface with 4 new tools. **No DB schema changes**, **no breaking CLI changes**, and the **external write-back ban remains in force** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7).

### New MCP tools (4)

- **`search`** — FTS5 cross-connector full-text search ([ADR-0022](adr/0022-mcp-server-surface.md) §決定 (f), `ReadCategory.SEARCH`). Phrase-quoted by default; the CLI-only `--raw-query` flag is intentionally absent from the MCP schema so host LLMs cannot smuggle raw MATCH syntax through.
- **`propose.apply`** — HITL idempotent apply path (`WriteCategory.PROPOSE_APPLY`, `destructive=false` + `idempotent=true`). The handler catches `OpsHubError("already applied" / "already rejected")` from the underlying service and normalises it to `{ok: true, already_applied: true, applied_entity_id}` so retries never throw. The `destructive=false` carve-out is documented in [SECURITY.md](../SECURITY.md#phase-12-assistant-skills-expansion--what-changed) (every other write tool stays `destructive=true`).
- **Physical-column time filters on the existing 4 read tools** — `task.list.updated_after/before` (`tasks.updated_at`) / `inbox.list.created_after/before` (`inbox_items.created_at`) / `decision.list.recorded_after/before` (`decisions.recorded_at`) / `source.list.observed_after/before` (`sources.observed_at`). ISO 8601, half-open interval (`>= after` / `< before`). Tool-specific names (not a shared `since/until`) keep business-time vs physical-column semantics from drifting.

### New `propose generate --mode` flag

`opshub propose generate` (and the equivalent `propose.generate` MCP write tool) gain a `--mode` argument with three new values: `inbox_triage` / `source_extract` / `meeting_followup`. These dispatch to the corresponding new HITL-write skills (`inbox-triage` / `source-extract` / `meeting-followup`) and reuse the existing `propose generate → apply / reject` lifecycle. The pre-existing `reply_draft` mode (Phase 10) is unchanged; `--mode` is omitted for the default proposal path.

### Skill catalog — 5 → 14

The assistant skill catalog grows to **14 skills** = **10 read (host-LLM-autonomous)** + **4 HITL write** ([`docs/assistant-agent.md`](assistant-agent.md) is the SSOT for the responsibility map, pair structure, HITL boundary, and MCP-tool dependency matrix):

- **read (10)**: `personal-brief` (renamed from `daily-brief`) / `next-actions` / `pr-review` / `find-document` (renamed from `file-lookup`) / `meeting-prep` / `research` / `external-brief` / `decision-rationale` / `handoff-draft` / `announcement-draft` (the last two are **text-only** — no persist path, no `propose apply` route)
- **HITL write (4)**: `reply-draft` / `inbox-triage` / `source-extract` / `meeting-followup`

Two existing skills were renamed (`daily-brief` → `personal-brief`, `file-lookup` → `find-document`). The old names are not aliased — host configs that referenced them must be updated.

### Skill install on the host

Phase 16-A ([ADR-0029](adr/0029-distribute-assistant-skills-via-opshub-package.md)) confirmed **opshub package bundling + `opshub skills install`** as the canonical distribution channel for the 14 assistant skills (the **opshub repo `docs/skills/<name>/SKILL.md`** remains the SSOT, [ADR-0004 §決定 (c)](adr/0004-agent-runtime-boundary.md)). Phase 16-B ([#383](https://github.com/ozzy-labs/opshub/issues/383)) shipped the CLI — operators install the 14 skills by running `opshub skills install` after `uv tool install ozzylabs-opshub[mcp]`. Flag details (`--host` / `--scope` / `--skip-existing` / `--dry-run` / `--print-paths`) and the `opshub skills list` status command live in [`docs/assistant-agent.md`](assistant-agent.md) §8.

The pre-existing 5 skills' SKILL.md were rewritten to call MCP directly (the previous CLI fallback was dropped); the MCP server (`opshub mcp serve`, Phase 10) is now a hard dependency for the assistant skills.

### Phase 12 specifics

- **DB head unchanged** = `0019_create_sources_fts` (Phase 10). Phase 12 ships **no migrations**.
- **No new extras**. The MCP server still lives under the existing `mcp` extras.
- **No CLI breaking changes**. `--mode` is additive on `propose generate`; the previous default behaviour (no `--mode`) is preserved.
- New MCP tool surface: total **17 tools** = 12 read + 5 write (was 13 = 11 read + 2 write before Phase 12 H1; Phase 10 + Step 1 widening PR #231 baseline).
- **No DB schema changes / no event-schema changes.** Existing rows continue to round-trip cleanly through every query path.
- External write-back is **still forbidden** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7). All 4 HITL-write skills draft locally; the operator sends.

## Phase 13: Google Workspace connector

Phase 13 ([ADR-0010](adr/0010-connector-contract.md) revision §Phase 13 (e)-(h) + [ADR-0014](adr/0014-saas-token-storage.md) revision (rotation pin list 3rd entry) + [ADR-0025](adr/0025-office-document-content-extraction.md) revision (§決定 (d') + (j))) adds a new `google_workspace` connector that ingests Google Docs / Slides / Sheets via Drive API v3 + OAuth Refresh Token + Workspace export → markitdown. **No DB schema changes** (the existing `sources.body` column + FTS5 index from Phase 10 carry the new bodies), **no breaking CLI changes**, and the external write-back ban remains in force ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7).

### Opt-in: enable the `google_workspace` connector

```bash
# 1. Install the connectors-google-workspace extras (httpx).
uv tool install "ozzylabs-opshub[connectors-google-workspace]"

# 2. Register a Google Cloud OAuth client (Installed Application type)
#    and capture client_id / client_secret. Full walkthrough:
#    docs/google-workspace-setup.md.

# 3. Configure the OAuth client in opshub.toml.
# In ~/.config/opshub/config.toml:
#   [connectors.google_workspace]
#   enabled = true
#   client_id = "<your-client-id>.apps.googleusercontent.com"
#   client_secret = "<your-client-secret>"
#   redirect_uri = "http://localhost"      # default; matches the value registered in GCP
#   content_extraction = false             # leave off until you've installed [office]
#   fallback_window_days = 30              # changes.list TTL invalidation fallback window

# 4. Run the paste-code OAuth flow to obtain a refresh token.
opshub google_workspace auth set                  # opens the consent URL; paste the code back
# Or for CI / non-interactive contexts:
export OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN="<refresh-token>"

# 5. Sync.
opshub google_workspace sync
```

The connector reads Drive API v3 `changes.list` with a stored `startPageToken` cursor. When Google invalidates the stored token (`400 invalidToken` / `404 startPageToken expired` / `410 Gone`) the connector logs a warning and falls back to a `changes.getStartPageToken` bootstrap + a one-time full pass over the last `fallback_window_days` (default 30) before resuming differential mode — same shape as the Phase 11 Teams delta-link fallback ([ADR-0010](adr/0010-connector-contract.md) §改訂 (c) → §Phase 13 改訂 (g)).

The Refresh Token lives in the OS keychain under `connector:google_workspace:refresh_token` ([ADR-0014](adr/0014-saas-token-storage.md) §Phase 7 Validation, 3rd rotation pin entry). The connector follows the MS365 / Box pattern: when Google rotates the refresh token, the new value is written back to the keychain so the next process resumes cleanly. This is **separate from the Teams verbatim user-token pattern** ([ADR-0010](adr/0010-connector-contract.md) §Phase 13 改訂 (h) makes the split explicit).

### Opt-in: enable Workspace export body extraction

By default Phase 13 ships metadata-only — `content_extraction = false` keeps the connector cost-free (no `files.export` round-trip, no markitdown invocation). To pull Workspace native bodies into `sources.body`:

```bash
# 1. Install the office extras (markitdown[docx,xlsx,pptx]).
uv tool install "ozzylabs-opshub[office]"

# 2. Flip the per-connector config flag.
# In ~/.config/opshub/config.toml:
#   [connectors.google_workspace]
#   content_extraction = true

# 3. Re-sync to pick up the bodies.
opshub google_workspace sync
opshub embeddings rebuild                        # re-embed with the new body content
```

When `content_extraction = true`, the connector calls Drive API `files.export(fileId, mimeType=<MS Office mediatype>)` for the three Workspace native source_types (`google_doc` → `.docx`, `google_slides` → `.pptx`, `google_sheets` → `.xlsx`) and routes the bytes through `core/document_extract.extract_workspace_export(bytes, source_type)`. The same caps from Phase 11 apply ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (b)):

- Files larger than 50 MB are skipped with **internal** `ExtractResult.body=None` + a warning log (configurable via `[office] max_file_size_mb`); the mapper substitutes `body = summary` so the projection persists a non-empty string (epic [#470](https://github.com/ozzy-labs/opshub/issues/470) / [ADR-0010](adr/0010-connector-contract.md) §不変条件 6 / [ADR-0020](adr/0020-full-local-content-retention.md) §(d')).
- Extracted text longer than 500 000 chars is head-truncated and annotated (configurable via `[office] max_chars`).
- Export failures (Drive throttling, file permission loss, malformed export) surface as **internal** `ExtractResult.body=None` + sanitised warning; the same `body = summary` substitute applies so the projection persists a non-empty string. The metadata `SourceObserved` is still emitted so the sync never gets blocked on a single bad export.

Non-native files (the catch-all `google_workspace_file` source_type — Drive returns 403 `fileNotExportable` for them) stay metadata-only regardless of `content_extraction`.

### New source_types (Phase 13)

Four new `source_type` discriminators land in `sources` ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (d')):

| Google mimeType | `source_type` | Body extraction |
|---|---|---|
| `application/vnd.google-apps.document` | `google_doc` | docx export → markitdown |
| `application/vnd.google-apps.presentation` | `google_slides` | pptx export → markitdown |
| `application/vnd.google-apps.spreadsheet` | `google_sheets` | xlsx export → markitdown |
| anything else (Workspace folder, uploaded PDF, etc.) | `google_workspace_file` | metadata-only (no export) |

The first three discriminators flow through the same `find-document` / `search` / `recall.search` paths as Phase 11 Office bodies — assistant skills can filter on them when desired (e.g. "Google Sheets only" find-document).

### Phase 13 specifics

- **DB head unchanged** = `0019_create_sources_fts` (Phase 10). Phase 13 ships **no migrations**; bodies flow through the existing `sources.body` column + FTS5 index.
- **New optional extras**: `connectors-google-workspace` (httpx). Pair with `[office]` extras when enabling `content_extraction = true`.
- **No CLI breaking changes (Phase 13 当時)**. `opshub google_workspace auth set` and `opshub google_workspace sync` are the new sync targets — both are additive.
- **External write-back is still forbidden** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7). Drive write APIs (`files.update` / `files.create` / `files.copy` / `comments.create` / `permissions.*`) are deliberately not implemented in the connector. Drive push notifications (`files.watch`) are also forbidden — the connector polls via `changes.list` only to preserve form-A (no opshub-internal runtime).

## Phase 14: Gmail + Google Calendar connectors

Phase 14 ([ADR-0010](adr/0010-connector-contract.md) revision §Phase 14 (i)-(m) + [ADR-0014](adr/0014-saas-token-storage.md) revision (scope expansion for `connector:google_workspace:refresh_token` slot + shared auth foundation extraction)) adds two new connectors — `google_mail` (Gmail) and `google_calendar` (Google Calendar) — that **share the existing Google OAuth principal** from Phase 13. **No DB schema changes** (the existing `sources.body` column + FTS5 index from Phase 10 carry the new bodies), **no breaking CLI changes**, **no new extras** (`connectors-google-workspace` is reused for httpx), and the external write-back ban remains in force.

### Re-consent required (existing operators)

If you already configured the `google_workspace` connector in Phase 13, **your existing refresh token covers only `drive.readonly`** and will not work for the new Gmail / Calendar APIs. Phase 14 expands the shared OAuth principal to the 3-scope fixed list `drive.readonly + gmail.readonly + calendar.readonly` (1 Google account = 1 principal shared across Drive + Gmail + Calendar, [Phase 14 plan §1 OQ6](phase-14-plan.md#1-確定済み事項)). To upgrade:

1. **Re-register scopes in the Google Cloud Console.** Open your existing OAuth client → OAuth consent screen → **Add or Remove Scopes** → add `gmail.readonly` and `calendar.readonly` (`drive.readonly` is already there from Phase 13). Submit for verification if your project is in production mode. Full walkthrough: [`docs/google-workspace-setup.md`](google-workspace-setup.md) §Scopes.
2. **Re-run the paste-code flow once.** A single re-consent applies to all three connectors:

   ```bash
   opshub google_workspace auth set
   # browser opens with the new 3-scope consent screen; paste the code back
   ```

   The keyring slot stays the same (`connector:google_workspace:refresh_token`), so existing env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` continues to work — just refresh its value once.
3. **Existing `google_workspace` sync continues to work** unchanged after re-consent. The Drive endpoint still uses `drive.readonly`; the extra scopes are unused there.

If you skip re-consent, `opshub google_mail sync` and `opshub google_calendar sync` will fail with a 401 `Request had insufficient authentication scopes` error and exit cleanly without writing any events.

### Opt-in: enable the `google_mail` connector

```bash
# 1. extras はすでに Phase 13 で install 済み (connectors-google-workspace は httpx 共通)。
#    新 extras なし — Phase 14 Gmail は httpx を流用。

# 2. Configure in opshub.toml.
# In ~/.config/opshub/config.toml:
#   [connectors.google_mail]
#   enabled = true
#   fallback_window_days = 30              # users.history.list 7-day TTL invalidation fallback window
#   # initial_window_days = 7              # first-sync backfill window (cursor=None pass の遡及範囲)

# 3. Sync (re-consent 済み前提、Phase 13 google_workspace と同 principal).
opshub google_mail sync
```

The connector reads Gmail API v1 `users.messages.list` for the initial sync and `users.history.list` for delta. When Google invalidates the `startHistoryId` (HTTP 404 after the 7-day TTL elapses) the connector logs a warning (`connector.history_list.expired`) and falls back to a one-time `users.messages.list` full pass over the last `fallback_window_days` (default 30) before resuming differential mode. Same shape as the Phase 11 Teams / Phase 13 Drive fallback ([ADR-0010](adr/0010-connector-contract.md) §Phase 14 改訂 (j) generalises the pattern to all delta-cursor connectors).

Bodies are extracted **symmetrically with the Phase 11 Outlook mapper**: `text/plain` preferred → `text/html` raw kept; no markitdown; no attachment retention. Labels prepend as `[Labels: INBOX, ImportantWork]`; truncation appends `[gmail body truncated: N / M chars]`. `threadId` is kept as a field; thread aggregation projection is a Phase 15+ candidate.

### Opt-in: enable the `google_calendar` connector

```bash
# 1. extras はすでに Phase 13 で install 済み。

# 2. Configure in opshub.toml.
# In ~/.config/opshub/config.toml:
#   [connectors.google_calendar]
#   enabled = true
#   # window default は MVP では「過去 90 日 + 未来 365 日」、override 可能:
#   # time_min_days = 90                   # 過去 N 日 (syncToken 410 GONE fallback の遡及範囲も兼用)
#   # time_max_days = 365                  # 未来 N 日

# 3. Sync.
opshub google_calendar sync
```

The connector reads Calendar API v3 `events.list(syncToken=...)` for delta. When Google returns 410 GONE (`SyncTokenExpiredError`), the connector logs `connector.events_list.expired` and falls back to a `events.list(timeMin, timeMax)` window walk (`singleEvents=false` + `showDeleted=true` pinned) before resuming sync-token mode. Override events (`recurringEventId` + `originalStartTime` set) are emitted as **separate `SourceObserved` records sharing `source_type="google_calendar"`** with a body back-pointer `Override of: <master_id> (originalStart: <iso>)` — symmetric in spirit with how the MS365 calendar mapper handles master events ([Phase 14 plan §G4 / OQ3](phase-14-plan.md#1-確定済み事項)).

Summaries follow `f"{start_iso} - {end_iso} ({attendees_count} attendees)"`; attendee email list / description / location are appended to the body; RRULE is kept as a field. Instance expansion (master + RRULE → individual instances) is a Phase 15+ projection-layer candidate (ms365 + google simultaneously).

### Phase 14 specifics

- **DB head unchanged** = `0019_create_sources_fts` (Phase 10). Phase 14 ships **no migrations**.
- **No new extras** — both connectors reuse `connectors-google-workspace` (httpx).
- **No CLI breaking changes (Phase 14 当時)**. `opshub google_mail sync` and `opshub google_calendar sync` are additive. The auth CLI is unchanged: `opshub google_workspace auth set` now provisions the refresh token for all three connectors at once.
- **Mapper symmetry is mechanically verified** by `tests/unit/connectors/test_mapper_symmetry.py` (6 cases for Gmail ↔ Outlook + 4 cases for google_calendar ↔ ms365_calendar). If you fork the mapper for vendor-specific tweaks, run this pin test to confirm the divergence is intentional.
- **External write-back is still forbidden** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7). Gmail `send` API and Calendar `events.insert` / `events.patch` / `events.delete` are deliberately not implemented. Push notifications (`users.watch` for Gmail / Calendar `events.watch`) are also forbidden — both connectors poll only ([ADR-0010](adr/0010-connector-contract.md) §Phase 14 改訂 (i)).
- **No new ADRs** — Phase 14 continues the single-revision trajectory (Phase 11 = 1 new + 2 revisions → Phase 12 = 0 new + 3 revisions → Phase 13 = 0 new + 3 revisions → **Phase 14 = 0 new + 2 revisions**).

### New source_types (Phase 14)

Two new `source_type` discriminators land in `sources` ([ADR-0010](adr/0010-connector-contract.md) §Phase 14 改訂 (l)):

| Source | `source_type` | Body extraction |
|---|---|---|
| Gmail message | `gmail_message` | text/plain preferred → text/html raw kept; no markitdown; no attachment retention; `[Labels: ...]` prepend; `[gmail body truncated]` tag; threadId field |
| Google Calendar event (master or override) | `google_calendar` | summary = `start_iso - end_iso (N attendees)`; attendee email list / description / location in body; RRULE field; override emitted as separate record with `Override of: <master_id>` back-pointer in body |

Both flow through the existing `find-document` / `search` / `recall.search` / `meeting-prep` / `meeting-followup` / `personal-brief` / `next-actions` paths — no skill-side changes required.

- Full setup (GCP project, OAuth consent screen, scopes, troubleshooting): [`docs/google-workspace-setup.md`](google-workspace-setup.md).

## Phase 14: Gmail + Google Calendar (G2 — shared Google OAuth foundation, scope expansion)

Phase 14 ([ADR-0010](adr/0010-connector-contract.md) §Phase 14 改訂 (i)-(m) + [ADR-0014](adr/0014-saas-token-storage.md) §Phase 7 Validation slot scope expansion) extends the Phase 13 Google connector to **Gmail + Google Calendar** (delivered across G2 / G3 / G4 / G5). **G2 (this section)** lands the shared OAuth foundation: the OAuth helper previously at `opshub.connectors.google_workspace.auth` moves to `opshub.connectors.google_auth.auth` and the requested OAuth scope widens from `drive.readonly` alone to **`drive.readonly + gmail.readonly + calendar.readonly`** so a single paste-code round grants every Google connector access ([Phase 14 plan §1 OQ6](https://github.com/ozzy-labs/opshub/blob/main/docs/phase-14-plan.md#1-確定済み事項)).

### Re-consent is required after upgrading

Because the requested OAuth scope set grows, Google invalidates the existing refresh token on first refresh after the upgrade (Google's installed-app OAuth flow surfaces this as `invalid_grant` from `oauth2.googleapis.com/token`). Re-run the paste-code flow once to consent to all three scopes in a single round:

```bash
# 1. Update the GCP OAuth consent screen.
#    APIs & Services -> OAuth consent screen -> Add or Remove Scopes
#    add (alongside drive.readonly):
#      https://www.googleapis.com/auth/gmail.readonly
#      https://www.googleapis.com/auth/calendar.readonly
#    (Full walkthrough: docs/google-workspace-setup.md §1 (c)).

# 2. Re-run the paste-code OAuth flow. The CLI now opens a consent URL
#    that requests all three scopes at once; the operator approves the
#    new combined consent screen and pastes the redirect URL back.
opshub google_workspace auth set

# 3. Next sync picks up the rotated refresh token automatically.
opshub google_workspace sync
```

The keyring slot string (`connector:google_workspace:refresh_token`) and the env-var override name (`OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN`) are **unchanged** — Phase 14 G2 only moves the Python module path and widens the scope set. Operators using the env-var override should re-paste their refresh token after re-running the consent flow (the previous token will no longer satisfy `gmail.readonly` / `calendar.readonly` requests once Gmail / Calendar connectors land in G3 / G4).

### What does **not** change in G2

- DB head, schema, migrations: **unchanged**.
- `[connectors-google-workspace]` extras: **unchanged** (Phase 14 plan §Alternatives §9 — Gmail / Calendar reuse the same extras instead of introducing `[connectors-google-mail]` / `[connectors-google-calendar]`).
- `opshub google_workspace sync` (Drive) keeps working bit-for-bit — the connector now imports its auth helper from the shared module, but the contract (cursor, mapper, settings) is invariant.
- ADR-0010 §禁止事項 7 (no external write-back): **still in force** for Gmail / Calendar too — G2 only ships read scopes, and Phase 14 G3 / G4 will land Gmail / Calendar read connectors. Gmail send / Calendar write are not part of Phase 14.

### Phase 14 G2 specifics

- New module path: `opshub.connectors.google_auth.auth` (shared by Drive / Gmail / Calendar). The Phase 13 `opshub.connectors.google_workspace.auth` import path is **removed** (opshub is pre-userbase — no compatibility shim).
- New OAuth scope set: 3 fixed scopes (per Phase 14 plan §X.1 — per-connector subset declarations are not supported; disable connectors via `[connectors.<name>] enabled = false` instead).
- Re-consent is one-time per operator; once the refresh token reflects the new scope set, every subsequent `sync` resumes automatically.
- Gmail / Calendar connectors land in **Phase 14 G3 / G4** — G2 alone exposes no new CLI subcommand or `source_type`; the G3 / G4 sections of this document will cover those on merge.

## Phase 15: search quality — FTS5 Japanese tokenizer (trigram) + short-query LIKE fallback

Phase 15 ([ADR-0028](adr/0028-fts5-japanese-tokenizer.md), epic [#338](https://github.com/ozzy-labs/opshub/issues/338)) fixes the long-standing 0-hit behaviour of `opshub search` on Japanese natural-text queries. Phase 10's original FTS5 tokenizer (`unicode61 remove_diacritics 2`) treated Japanese run-on text as a single long token, so queries like `boxの権限` / `進捗記入` / `CDKの` could only match via prefix `*` + `--raw`. Phase 15 swaps the tokenizer to FTS5's built-in `trigram` (SQLite 3.34+, no new dependency) and adds a 1-2 character LIKE fallback so `依頼` / `PR` / `Q4` no longer silently return 0 hits.

### Apply the new migration

The change ships as migration `0028_rebuild_sources_fts_trigram`, which drops the three `sources_fts` sync triggers, drops the FTS5 virtual table, recreates it with `tokenize='trigram'`, back-fills from `sources.body`, and re-attaches the triggers in the same shape Phase 10 created them. Migration `0019_create_sources_fts` itself stays immutable (Phase 1 onward 規範); operators converge by running the new revision:

```bash
uv tool upgrade opshub
opshub db migrate                # applies 0028_rebuild_sources_fts_trigram
```

The back-fill is automatic — `opshub embeddings rebuild` is **not** required (Phase 15 does not change the embedding model or `sources.body` itself, only the derived FTS index). Encrypted databases (`[storage] encryption = true`, [ADR-0021](adr/0021-encryption-at-rest.md)) migrate transparently through SQLCipher.

### Behavioural change: Japanese queries hit by substring

After applying migration 0028 and upgrading the CLI, `opshub search` behaves as follows:

| Query example | Before Phase 15 | After Phase 15 |
|---|---|---|
| `opshub search "boxの権限"` | 0 hits (tokenizer treats `boxの権限きれてそう` as one token) | hits (trigram indexes 3-char substrings) |
| `opshub search "進捗記入"` | 0 hits | hits |
| `opshub search "依頼"` (2 chars) | 0 hits | hits via LIKE fallback (full scan, `LOWER(body) LIKE LOWER(?)`) |
| `opshub search "PR"` (2 chars) | hits only if `PR` is an isolated token | hits via LIKE fallback |
| `opshub search "DailyMeeting"` | hits | hits (unchanged, trigram path) |
| `opshub search "box* AND 権限*" --raw` | hits | hits (unchanged, `--raw` preserves FTS5 syntax) |

The fallback is bypassed under `--raw` so operators who opt into FTS5 boolean / prefix / phrase syntax keep full control — `--raw` queries with 1-2 char inputs still return 0 hits because trigram does not index them. See [`docs/troubleshooting.md`](troubleshooting.md) §3.6 for the diagnostic recipe.

### Downgrade is supported

Migration 0028's `downgrade()` recreates `sources_fts` with the original `unicode61 remove_diacritics 2` tokenizer and re-back-fills from `sources.body`, so operators can roll the tokenizer choice back without losing the `sources` projection itself:

```bash
ALEMBIC_CONFIG=...alembic.ini uv run alembic downgrade 0019_create_sources_fts
```

Downgrading reverts the Japanese-substring improvement; the SearchService LIKE fallback still runs for 1-2 char queries (it lives in `src/opshub/services/search_service.py`, not in the DB), so short queries continue to work even after a downgrade.

### `opshub search --help` clarifies `--raw` scope

The `--raw` flag help text now positions raw mode as a power-user contract for FTS5 boolean / prefix / phrase syntax (`box* AND 権限*`), and notes that the LIKE fallback is disabled under `--raw`. The default mode is the recommended path for Japanese natural-text queries.

### MCP `search` tool contract is unchanged

The Phase 12 H1 MCP `search` tool ([ADR-0022](adr/0022-mcp-server-surface.md) §決定 (f)) keeps `raw_query` hard-coded `false` — assistant skills (`find-document` / `personal-brief` / `next-actions` / `meeting-prep` / `research` / etc.) call into SearchService transparently and inherit the trigram + LIKE fallback improvements without any skill-side change.

### Phase 15 specifics

- **DB head** = `0028_rebuild_sources_fts_trigram` (Phase 15). Phase 15 ships one migration.
- **No new extras** — trigram is a built-in FTS5 tokenizer shipped with SQLite ≥ 3.34 (the mise toolchain ships SQLite ≥ 3.38).
- **No CLI breaking changes** — `opshub search` argument shape is unchanged; only the `--raw` help text wording is updated.
- **No new ADRs other than ADR-0028** — Phase 15 = 1 new + 0 revisions (Phase 11 = 1 new + 2 revisions → Phase 12 = 0 + 3 → Phase 13 = 0 + 3 → Phase 14 = 0 + 2 → **Phase 15 = 1 + 0**).
- **MCP `search` tool contract (ADR-0022 §決定 (f)) is unchanged**.
- Full plan and rejected alternatives: [`docs/phase-15-plan.md`](phase-15-plan.md). Troubleshooting recipe for Japanese queries that still appear to miss: [`docs/troubleshooting.md`](troubleshooting.md) §3.6.

## Phase 17: CLI surface 再編 (noun-first / per-noun group) — **BREAKING CHANGE**

Phase 17 ([ADR-0031](adr/0031-cli-command-surface-organization.md), epic [#409](https://github.com/ozzy-labs/opshub/issues/409)) reorganises the CLI from the legacy 3-level `opshub connector <verb> <name>` layout into per-noun 2-level groups (`opshub <connector> <verb>`). The legacy `opshub connector` group is **fully removed** with no backward-compat alias (opshub is pre-userbase; ADR-0031 §決定 (7)).

### Migration table

| Legacy (≤ v0.2.x) | New (≥ v0.3.0) |
|---|---|
| `opshub connector list` | `opshub connectors` |
| `opshub connector sync slack` | `opshub slack sync` |
| `opshub connector sync github` | `opshub github sync` |
| `opshub connector sync ms365` | `opshub ms365 sync` |
| `opshub connector sync box` | `opshub box sync` |
| `opshub connector sync teams` | `opshub teams sync` |
| `opshub connector sync google_workspace` | `opshub google_workspace sync` |
| `opshub connector sync google_mail` | `opshub google_mail sync` |
| `opshub connector sync google_calendar` | `opshub google_calendar sync` |
| `opshub connector sync box_drive` | `opshub box_drive sync` |
| `opshub connector sync onedrive_drive` | `opshub onedrive_drive sync` |
| `opshub connector slack conversations [...]` | `opshub slack conversations [...]` |
| `opshub connector auth set slack` (or `connector:slack`) | `opshub slack auth set` |
| `opshub connector auth set github` | `opshub github auth set` |
| `opshub connector auth set connector:ms365` | `opshub ms365 auth set` |
| `opshub connector auth set connector:box` | `opshub box auth set` |
| `opshub connector auth set connector:teams` | `opshub teams auth set` |
| `opshub connector auth set google_workspace` | `opshub google_workspace auth set` |
| `opshub connector auth set embedder:openai` | `opshub embedder auth set openai` |
| `opshub connector auth set embedder:voyage` | `opshub embedder auth set voyage` |
| `opshub connector auth set llm:anthropic` | `opshub llm auth set anthropic` |
| `opshub connector auth set llm:openai` | `opshub llm auth set openai` |
| `opshub connector auth test github` | `opshub github auth test` |
| `opshub connector auth test slack` (or `connector:slack`) | `opshub slack auth test` |
| `opshub connector auth test connector:ms365` | `opshub ms365 auth test` |
| `opshub connector auth test connector:box` | `opshub box auth test` |
| `opshub connector auth test google_workspace` | `opshub google_workspace auth test` |
| `opshub connector auth set connector:box_drive` (no-op reject) | *(not registered — Typer exits 2 with "No such command")* |
| `opshub connector auth set connector:onedrive_drive` (no-op reject) | *(not registered — Typer exits 2 with "No such command")* |

### What is unchanged

- **Keyring slot names** (e.g. `connector:slack:token`, `connector:google_workspace:refresh_token`, `embedder:openai:api_key`, `llm:anthropic:api_key`) are **unchanged** — only the CLI command surface moves. Operators do **not** need to re-run `auth set` after the upgrade; the stored tokens remain valid.
- **Environment variable overrides** (`OPSHUB_CONNECTOR_SLACK_TOKEN`, `OPSHUB_CONNECTOR_GITHUB_PAT`, `OPSHUB_CONNECTOR_TEAMS_TOKEN`, `OPSHUB_DB_ENCRYPTION_KEY`, etc.) are **unchanged**. Existing CI / cron configurations continue to work.
- **stdout / stderr output shapes** keep the same markers: `synced <name>: N item(s) observed` (stdout, success) / `sync failed: <Type>...` (stderr, failure). Scripts grepping the `synced ` / `sync failed:` prefixes keep working. (Phase 23-B [#532] later appended the sanitised message body to the failure line — `sync failed: <Type>: <sanitised-msg>` — but the `sync failed:` prefix is preserved, so prefix-anchored grep is unaffected; see §Phase 23-B below.)
- **MCP tool surface** ([ADR-0022](adr/0022-mcp-server-surface.md)) is **unchanged** — MCP tool names (`connector.sync`, `recall.search`, etc.) are independent of the CLI group naming.
- **`Connector` Protocol** ([ADR-0010](adr/0010-connector-contract.md)) and the `connectors/<name>/` package layout (`auth.py` + `fetcher.py` + `mapper.py` + `connector.py`) are **unchanged**.

### Migration tips

**cron / launchd entries**: rewrite every line that invokes `opshub connector sync <name>`. The new path is shorter:

```cron
# Before
0 */2 * * * opshub connector sync slack

# After
0 */2 * * * opshub slack sync
```

**Shell aliases / functions**: if you wrapped the legacy CLI in shell helpers, audit them. For example:

```sh
# Before
sync_all() {
  opshub connector sync slack
  opshub connector sync github
  opshub connector sync ms365
}

# After
sync_all() {
  opshub slack sync
  opshub github sync
  opshub ms365 sync
}
```

**Recovery scripts**: error-handling scripts that grep the failure summary by its `sync failed:` prefix keep working — the prefix is preserved. (Phase 23-B [#532] appends the sanitised message body after the type name, `sync failed: <Type>: <sanitised-msg>`; scripts anchored on the `sync failed:` prefix are unaffected. Scripts that matched the *exact* `sync failed: <Type>` line with no trailing text should switch to prefix matching.) Only the command invocation changes for the Phase 17 reorg itself.

### Phase 17 specifics

- **No DB migration**. Phase 17 is CLI-surface-only.
- **No new extras**. Existing `[connectors-*]` / `[office]` / `[secrets]` / `[encryption]` extras gates are unchanged.
- **BREAKING CHANGE → major version bump**. The Phase 17-B PR's commit message carries `feat!:` + `BREAKING CHANGE:` footer so `release-please` flips the next release to a major version. No deprecation alias is shipped (ADR-0031 §決定 (7)).
- **One new ADR + zero revisions** — Phase 17 = 1 new + 0 revisions (Phase 11 = 1 new + 2 revisions → Phase 12 = 0 + 3 → Phase 13 = 0 + 3 → Phase 14 = 0 + 2 → Phase 15 = 1 + 0 → **Phase 17 = 1 + 0**, Phase 16 was non-ADR-touching skill distribution work).
- Full rationale and rejected alternatives: [ADR-0031](adr/0031-cli-command-surface-organization.md). Phase 17-A delivered the ADR (PR [#412](https://github.com/ozzy-labs/opshub/pull/412)); Phase 17-B delivers the implementation + tests + this docs section.

## Phase 18-B: Slack mention / DM demand digest projection

Phase 18-B ([ADR-0033](adr/0033-slack-mention-demand-digest.md), epic [#426](https://github.com/ozzy-labs/opshub/issues/426)) materialises a per-channel × per-demand-kind digest of the most recent Slack message that demands the operator's attention (`<@self>` mention or DM). No new fetch / connector / mapper / event is added — the new `slack_demand_digest` projection consumes the existing `SourceObserved` event stream emitted by the Phase 7 Slack connector (ADR-0033 §決定 (a), ADR-0002 event-sourced architecture is preserved). The Phase 18-C follow-up will surface this read model to assistant skills via a new MCP `slack.demand.list` tool.

### Apply the new migration and rebuild

The schema lands as migration `0029_create_slack_demand_digest`. Existing operators converge by running the new revision and a one-shot full projection rebuild so historical Slack events populate the digest table:

```bash
uv tool upgrade opshub
opshub db migrate                  # applies 0029_create_slack_demand_digest
opshub projections rebuild         # replays the event log into every projection,
                                   # including slack_demand_digest
```

The rebuild is idempotent and replay-order-independent (`last_demand_ts < excluded.last_demand_ts` guard on the upsert). Subsequent `opshub slack sync` runs incrementally update the digest as new Slack events arrive — no recurring rebuild is required.

### Self user id resolution

The projection needs the operator's Slack `U...` id to spot the `<@self_user_id>` literal in message bodies. Resolution order (first non-empty wins):

1. The Slack `auth.test()` cache — succeeds for any operator who has run `opshub slack auth set` + `opshub slack auth test` previously.
2. The `OPSHUB_SLACK_SELF_USER_ID` environment variable — useful for CI / headless docker / WSL2 sandboxes where the keyring is not reachable but the operator already knows their Slack id.

If neither resolves, the projection logs a single warning and skips mention detection for the remainder of the rebuild; DM detection (which only needs the channel id prefix) still works. See [`docs/troubleshooting.md`](troubleshooting.md) §3.11 for the diagnostic recipe.

### Debug CLI: `opshub slack mentions list`

A read-only CLI exposes the digest projection for operator inspection during rollout / incidents:

```bash
opshub slack mentions list                       # default (all types, all kinds, limit 50)
opshub slack mentions list --types im,mpim       # DM + MPIM only
opshub slack mentions list --demand-kind mention # mention rows only
opshub slack mentions list --format json | jq .  # JSON output (full row schema)
```

The first-class skill surface (`next-actions` / `personal-brief` / `inbox-triage`) lands in Phase 18-C as the MCP `slack.demand.list` tool ([ADR-0033 §決定 (c)](adr/0033-slack-mention-demand-digest.md)); the CLI is debug-only.

### Phase 18-B specifics

- **DB head** = `0029_create_slack_demand_digest` (Phase 18-B). Phase 18-B ships one migration.
- **No new extras** — the projection lives entirely on the existing `[connectors-slack]` foundation (slack-sdk for the optional `auth.test()` self-id resolution) and stdlib SQLite. Operators who have already configured Slack need no additional install steps.
- **No CLI breaking changes** — `opshub slack mentions list` is a new sub-group under the existing Phase 17 noun-first `opshub slack` group ([ADR-0031](adr/0031-cli-command-surface-organization.md) §決定 (4)).
- **No new ADR — implements [ADR-0033](adr/0033-slack-mention-demand-digest.md)** (delivered in Phase 18-A docs-only PR [#431](https://github.com/ozzy-labs/opshub/pull/431)).
- **Forward-compat enum headroom** — the `demand_kind` CHECK constraint admits `{"mention", "dm", "mpim"}` (3-value SSOT per ADR-0033 §決定 (b) §不変条件 #2). Phase 18-B writes `mention` and `dm` only; group-DM `<@self>` mentions land in the mention row. A Phase 19+ MPIM-specific refinement can start writing `mpim` rows without a schema migration.

## Phase 18-C: `slack.demand.list` MCP tool + assistant skill wiring

Phase 18-C ([ADR-0033 §決定 (c)](adr/0033-slack-mention-demand-digest.md), epic [#426](https://github.com/ozzy-labs/opshub/issues/426)) exposes the Phase 18-B `slack_demand_digest` projection to the assistant skills (`next-actions` / `personal-brief` / `inbox-triage`) via a new MCP read tool `slack.demand.list`. **No DB schema changes** (the projection schema landed in Phase 18-B migration `0029_create_slack_demand_digest`), **no new CLI surface** (the Phase 18-B `opshub slack mentions list` debug CLI stays in place), and **no new Slack scope requirements** for existing operators.

### First-time setup after upgrading

If you already ran the Phase 18-B steps (`opshub db migrate` + `opshub projections rebuild`), no additional commands are required for Phase 18-C — the new MCP tool ships with the package and is wired automatically. Verify it appears in the registry:

```bash
opshub mcp tools | grep slack.demand.list
# → read   slack.demand.list             List Slack demand digest rows
```

If you have not yet bootstrapped the projection, the Phase 18-B steps still apply:

```bash
uv tool upgrade opshub
opshub db migrate                  # applies 0029_create_slack_demand_digest (Phase 18-B)
opshub projections rebuild         # replays the event log into slack_demand_digest
```

### New MCP tool: `slack.demand.list` (read-only)

| field | shape |
| --- | --- |
| `types` | `list[str]`, optional, default = all four. Members ∈ `{im, mpim, private, public}`. Maps 1:1 to `slack_demand_digest.channel_type`. |
| `demand_kinds` | `list[str]`, optional, default = all three. Members ∈ `{mention, dm, mpim}`. Maps 1:1 to `slack_demand_digest.demand_kind`. |
| `since_ts` | `float`, optional. Slack epoch lower bound on `slack_demand_digest.last_demand_ts` (rows strictly older are excluded). |
| `limit` | `int`, optional, default 50, max 200. ADR-0022 §(d) page cap. |
| `order` | `"last_demand_desc"` (fixed for Phase 18; reserved for forward-compat). |

Output: `{items: SlackDemandItem[], total: N, truncated: bool, next_offset: int|null}` where `SlackDemandItem` mirrors the projection columns (`channel_id` / `channel_type` / `channel_name` / `demand_kind` / `last_demand_ts` / `last_demand_user_id` / `last_demand_excerpt` / `last_demand_permalink` / `last_source_id`).

The tool is **read-only** (`readOnlyHint=true, destructiveHint=false, openWorldHint=false, idempotent=true`) and hits local SQLite only — no Slack API round-trip happens at MCP call time (the projection is rebuilt offline from already-stored `SourceObserved` events).

### Assistant skill wiring

The three skills below now reference `slack.demand.list` in their SKILL.md SSOT (the `opshub skills install` install path picks up the new bodies automatically):

- **`next-actions`** — Adds a "Slack で読むべき" section to the priority ranking. DM rows are surfaced at the top, mention rows are tiered by `channel_name` + `last_demand_ts` freshness.
- **`personal-brief`** — Adds "Slack の demand 信号" to the period summary. Filters by `since_ts` (the period lower bound translated to Slack epoch float) so only mentions / DMs inside the brief window appear.
- **`inbox-triage`** — Uses `slack.demand.list` as an *auxiliary* priority signal alongside the existing `inbox.list` triage queue. Slack mention / DM rows themselves remain in `sources`, not `inbox_items` (ADR-0033 §採用しなかった代替 §2 — no inbox row duplication).

If you ran `opshub skills install` (or `opshub init` with the default install behaviour) before Phase 18-C, re-run `opshub skills install` after upgrading to pick up the new SKILL.md bodies:

```bash
uv tool upgrade opshub                 # ships the new SKILL.md SSOT in src/opshub/_skills/
opshub skills install                  # overwrite ~/.claude/skills/ + ~/.agents/skills/ with the new bodies
# or, for repo-scoped installs:
opshub skills install --scope project
```

### Phase 18-C specifics

- **No DB migration.** DB head stays at `0029_create_slack_demand_digest` (Phase 18-B).
- **No new extras.** The MCP server continues to use the existing `[mcp]` extras; the new tool reuses the standing engine.
- **No new Slack scope.** Existing Slack operators already have the data (the Phase 18-B projection is purely derived from `SourceObserved` events the Phase 7 connector already emits).
- **No CLI breaking changes.** The Phase 18-B debug CLI (`opshub slack mentions list`) stays in place; `slack.demand.list` is the MCP surface twin, not a CLI replacement.
- **MCP tool surface after Phase 18-C: 13 read + 5 write** (was 12 read + 5 write after Phase 12 H1; the single new read tool `slack.demand.list` brought the read count to 13). Phase 21-D later adds the write tool `browser.fetch`, taking the surface to **19 tools = 13 read + 6 write** (see [Phase 21](#phase-21-browser-read-layer-playwright--web-connector--mcp-browserfetch) below).
- **No new ADR — implements [ADR-0033 §決定 (c)](adr/0033-slack-mention-demand-digest.md) + adds the §(f) 補遺 cross-reference to [ADR-0022](adr/0022-mcp-server-surface.md)** (delivered in Phase 18-A docs-only PR [#431](https://github.com/ozzy-labs/opshub/pull/431) — the MCP read tool addition is a documented extension under the existing 5 不変条件).

## Phase 19: `opshub slack conversations` engagement axis default — **BREAKING CHANGE**

Phase 19 ([ADR-0034](adr/0034-slack-engagement-axis.md), epic [#438](https://github.com/ozzy-labs/opshub/issues/438)) changes the default semantics of `opshub slack conversations --since <when>`. The `--since` filter now indexes "channels you wrote in" (engagement axis) instead of "channels with any-author activity" (Phase 17 [#374](https://github.com/ozzy-labs/opshub/issues/374) any-axis). The legacy any-author behaviour is preserved verbatim under the new opt-in flag `--activity=any` — opshub is pre-userbase, so [ADR-0034](adr/0034-slack-engagement-axis.md) ships **no compat shim** (per the standing pre-userbase posture).

### What changes

- `opshub slack conversations --since <when>` (no `--activity` flag) → engagement axis (`--activity=mine`). Uses **one** `search.messages` call with `query="from:<@<self>>"` and builds an in-memory `{channel_id: max_self_post_ts}` index that filters the listing. Channels you never wrote in within the window are dropped.
- The table renderer flips the activity column header from `LAST_ACTIVITY` to `LAST_POST` (column width unchanged at 13).
- The TOML comment label flips from `last YYYY-MM-DD` to `last post YYYY-MM-DD`.
- The JSON output now emits **one** of the two axis fields per row, never both: `last_self_post_ts` for the engagement axis, `last_activity_ts` for the any-author axis. The other field is dropped (the no-`--since` invocation still drops both).
- The spinner description switches from `listing conversations + activity` to `listing conversations + engagement` on the engagement axis.
- A one-shot stderr advisory (`notice: search.messages may lag by minutes; use --activity=any for live activity.`) surfaces before the engagement-axis index fetch — `search.messages` is full-text-index-backed and has a documented several-minute indexing lag. The advisory is unaffected by `-q` / `OPSHUB_LOG_LEVEL` (see [ADR-0034](adr/0034-slack-engagement-axis.md) §(i)); pass `--activity=any` to suppress it entirely.
- New scope requirement: `search:read` on a **User Token** (`xoxp-`). Bot Tokens (`xoxb-`) cannot satisfy `search:read` — the discovery command surfaces an explicit `ConfigError` and recommends `--activity=any` when a Bot Token is detected on the engagement path.
- New invalid combination: `--all + --activity=mine` (default or explicit) **with** `--since` is rejected with `ConfigError` exit 1. Rationale: `search.messages` only indexes channels where the principal is a member, so the intersection of "workspace-wide listing" and "engagement axis" collapses to the joined-only listing. Use `--activity=any` for workspace-wide any-author activity, or drop `--all` to restore the engagement axis.
- New debug observability: when the engagement index contains channel ids that the listing does not (Slack Connect / archived / type-filtered rows), `opshub --debug` logs a `slack.conversations.engagement_index_orphan` event with the orphan count. No operator warning — the asymmetry is structural, not a misconfiguration.

### Restoring the Phase 17 behaviour

```bash
opshub slack conversations --since 30d --activity=any   # legacy #374 path (any-author, conversations.history per-row, *:history scopes)
```

This is the documented escape hatch for operators who cannot grant `search:read` (workspace policy, audit committee delay), need broadcast / announcement-only channels in the output, or want live activity without the `search.messages` indexing lag.

### JSON consumer impact (Phase 19)

If a downstream script reads `last_activity_ts` from `opshub slack conversations --format=json --since <when>`, it now has to handle the field switching depending on `--activity`:

| `--activity` | populated field | absent field |
| --- | --- | --- |
| `mine` (default) | `last_self_post_ts` | `last_activity_ts` |
| `any` | `last_activity_ts` | `last_self_post_ts` |
| (no `--since`) | (both absent) | (both absent) |

Either pin `--activity=any` to keep the field name stable, or update the consumer to read whichever of the two fields is present.

### Phase 19 specifics

- **No DB migration.** Phase 19 is CLI / connector / docs surface only.
- **No new extras.** The change reuses `[connectors-slack]` (slack-sdk) for the `search.messages` call.
- **BREAKING CHANGE → minor version bump while pre-1.0.** `opshub` is in the `0.x` line where any minor can change the public surface (SemVer §4). Phase 19-B's Conventional Commit prefix is `feat(slack):` (not `feat!:`) because the breaking change rule for `release-please` only forces a major bump on `0.x` when the operator opts in explicitly. Operators reading this section before upgrading are the intended trigger for the docs-side awareness.
- **One new ADR + zero revisions** — Phase 19 = 1 new + 0 revisions (Phase 11 = 1 new + 2 revisions → Phase 12 = 0 + 3 → Phase 13 = 0 + 3 → Phase 14 = 0 + 2 → Phase 15 = 1 + 0 → Phase 17 = 1 + 0 → Phase 18 was 0 new + ADR-0033 forward-pinned across A/B/C → **Phase 19 = 1 new + 0 revisions**).
- Full rationale and rejected alternatives: [ADR-0034](adr/0034-slack-engagement-axis.md). Phase 19-A delivered the ADR ([#440](https://github.com/ozzy-labs/opshub/issues/440)); Phase 19-B delivers the implementation + tests + this docs section.

## Phase 19-D: `opshub slack conversations` sort axis consolidation — **BREAKING CHANGE**

Phase 19-D ([ADR-0035](adr/0035-slack-sort-axis-consolidation.md), epic [#448](https://github.com/ozzy-labs/opshub/issues/448)) supersedes the Phase 19-B `--activity={mine|any}` CLI surface with a unified `--sort=name|last_self_post|last_activity` flag, flips the default `--format` from `table` to `toml`, and pins the default sort to `name` (independent of `--since`). The engagement-axis signal source (search.messages-backed self-post index), Bot Token rejection, `search:read` requirement, field disjointness invariant, and `--all` × engagement-axis non-coexistence are **all inherited from ADR-0034 unchanged**; only the CLI vocabulary is reshuffled. opshub is pre-userbase, so ADR-0035 ships **no compat shim** (per the standing pre-userbase posture).

### What changes (breaking)

1. **`--activity` flag fully removed.** Use `--sort` instead. Mapping:

   | Old (pre-19-D) | New (19-D) |
   | --- | --- |
   | `--activity=mine --since <X>` | `--sort=last_self_post --since <X>` |
   | `--activity=any --since <X>` | `--sort=last_activity --since <X>` |
   | `--since <X>` (no `--activity`) | `--since <X>` (no `--sort`) — engagement axis stays the implicit default per ADR-0035 §(d), behaviour identical |

2. **Default `--format` is now `toml`.** The primary use case is pasting the output into `[connectors.slack] channels` in `opshub.toml`; the default switch removes the `--format toml` tax. Pass `--format=table` to reproduce the pre-19-D rendering (eyeball / debug / script-compatible).

3. **`--since` no longer flips the within-bucket sort.** Pre-19-D: passing `--since` flipped the sort from `display_name` ascending to activity-ts descending. Post-19-D: default sort is `name` regardless of `--since`; pass `--sort=last_self_post` (engagement axis ts) or `--sort=last_activity` (any-author axis ts) when you want the ts-descending listing.

4. **Implicit `--since 90d` cutoff when `--sort=last_self_post|last_activity` is used without `--since`.** A one-shot stderr notice surfaces (`notice: --sort=<sort> defaulted to --since 90d to cap probe cost; pass --since explicitly to override.`) so the choice is observable. Pass `--since` explicitly to override (e.g. `--since 365d` for a year-wide probe).

5. **`--sort=name + --since` keeps the engagement-axis `search:read` requirement.** ADR-0035 §(d): when `--sort` is not specified and `--since` is set, the adapter takes the engagement axis as its implicit default. Bot Token users and `search:read`-less User Token users will see `ConfigError` / `ConnectorFailedError` exactly as on the explicit `--sort=last_self_post` path. Workaround: pass `--sort=last_activity` explicitly to fall back to the any-author probe (requires `*:history` scopes instead).

### Restoring the pre-19-D `--activity=mine` / `--activity=any` behaviour

```bash
opshub slack conversations --sort=last_self_post --since 30d   # engagement axis (was --activity=mine --since 30d)
opshub slack conversations --sort=last_activity --since 30d    # any-author axis (was --activity=any --since 30d)
opshub slack conversations --since 30d                          # engagement-axis implicit default (was --since 30d with no --activity)
opshub slack conversations --format=table                       # pre-19-D default table rendering
```

### Indexing-lag notice rename

The Phase 19-B indexing-lag advisory is rewritten to match the new flag vocabulary:

```text
notice: search.messages may lag by minutes; use --sort=last_activity for live activity.
```

The behaviour is unchanged — the notice still surfaces once per call on the engagement-axis path, still ignores `-q` / `OPSHUB_LOG_LEVEL`, and is still suppressed by switching to `--sort=last_activity` ([ADR-0034](adr/0034-slack-engagement-axis.md) §(i) inherited by [ADR-0035](adr/0035-slack-sort-axis-consolidation.md) §(f)).

### JSON consumer impact (Phase 19-D)

The field-switching rule from Phase 19-B is inherited verbatim — only the flag name changes:

| invocation | populated ts field | absent ts field |
| --- | --- | --- |
| `--sort=last_self_post --since <X>` (or `--sort=name + --since <X>`) | `last_self_post_ts` | `last_activity_ts` |
| `--sort=last_activity --since <X>` | `last_activity_ts` | `last_self_post_ts` |
| `--sort=name` (no `--since`) | (both absent) | (both absent) |
| `--sort=last_self_post` (no `--since`, implicit 90d) | `last_self_post_ts` | `last_activity_ts` |
| `--sort=last_activity` (no `--since`, implicit 90d) | `last_activity_ts` | `last_self_post_ts` |

Pin `--sort=last_activity` to keep `last_activity_ts` stable, or update the consumer to read whichever of the two fields is present.

### `--all` × engagement-axis combination matrix (inherited from ADR-0034 §(h), extended)

| `--all` | `--sort` | `--since` | result |
| --- | --- | --- | --- |
| no | any | any | OK |
| yes | `name` | unset | OK (no probe runs) |
| yes | `name` | set | **rejected** (engagement-axis implicit default) |
| yes | `last_self_post` | any | **rejected** (engagement-axis explicit) |
| yes | `last_activity` | any | OK (any-axis is workspace-wide safe) |

Rejection message: `Error: --all is incompatible with engagement-axis sort (--sort=last_self_post or --sort=name + --since; search.messages indexes only self-member channels); use --sort=last_activity for workspace-wide activity.` (exit 1).

### Phase 19-D specifics

- **No DB migration.** Phase 19-D is CLI / connector / docs surface only.
- **No new extras.** The change reuses `[connectors-slack]` (slack-sdk).
- **BREAKING CHANGE in Conventional Commits.** The implementation PR uses the `feat(slack)!:` prefix so `release-please` cuts a CHANGELOG `BREAKING CHANGE:` entry. Pre-1.0 SemVer keeps the version bump in the minor lane (`0.3.x → 0.4.0`).
- **One new ADR (ADR-0035) + one partial supersede (ADR-0034 §(b) §(g) §(h) §(i) §不変条件 2 — CLI surface only).** The decision body of ADR-0034 (engagement axis source, Bot Token rejection, no silent fallback, field disjointness, `--all` × engagement-axis non-coexistence, indexing-lag notice) carries forward unchanged; only the CLI flag spelling is rewritten.
- Full rationale and rejected alternatives: [ADR-0035](adr/0035-slack-sort-axis-consolidation.md). Phase 19-D-1 delivered the ADR + supersede notice ([#451](https://github.com/ozzy-labs/opshub/pull/451)); Phase 19-D-2 delivers the implementation + tests + this docs section ([#450](https://github.com/ozzy-labs/opshub/issues/450)).

## Phase 20: `opshub slack sync` date floor (`sync_since` + per-channel `since`)

Phase 20 ([ADR-0036](adr/0036-slack-sync-date-floor.md)) adds an opt-in **date
floor** to `opshub slack sync` so the cold-start / newly-added-channel backfill
can be capped instead of walking the whole channel history. This is an
**additive, non-breaking** change — existing configs keep working untouched.

### Opt-in: cap the backfill

In `~/.config/opshub/config.toml`:

```toml
[connectors.slack]
enabled = true
sync_since = "90d"          # global floor: don't fetch messages older than 90 days
                            # (relative "90d"/"4w" evaluated at sync time, or ISO "2026-01-01")

# Bare-string channel ids still work (unchanged):
# channels = ["C_GENERAL", "C_RANDOM"]

# ...or use the table form for per-channel overrides:
[[connectors.slack.channels]]
id = "C_GENERAL"            # inherits the global sync_since (90d)

[[connectors.slack.channels]]
id = "C_INCIDENTS"
since = "all"               # opt this channel back into full-history backfill

[[connectors.slack.channels]]
id = "C_ARCHIVE"
since = "2026-01-01"        # channel-specific floor (overrides the global default)
```

The env override accepts either shape: `OPSHUB_CONNECTORS__SLACK__CHANNELS='["C1"]'`
(legacy string array) or `'[{"id":"C1","since":"30d"}]'` (table form as JSON).

### Behavioural notes

- **Default is unchanged.** With no `sync_since` and no per-channel `since`, the
  connector backfills the full channel history exactly as before.
- **The floor only moves the resume bound forward.** The per-channel cursor is
  authoritative (`oldest = max(cursor, floor)`), so enabling `sync_since` on an
  already-synced workspace **never re-fetches or deletes** history.
- **Lowering the floor does not retroactively backfill.** Because the cursor
  wins, dropping `sync_since` from `90d` to `365d` (or to an earlier ISO date)
  will *not* pull the older history back in. To re-fetch older messages, reset
  the Slack cursor (`opshub projections rebuild`) so the next sync starts from
  the new floor.
- **Relative floors advance over time.** `"90d"` is evaluated at each sync run;
  use an ISO date (`"2026-01-01"`) if you need an absolute lower bound.
- **`opshub slack conversations --format=toml`** still emits the
  `channels = ["C..."]` snippet, which remains valid to paste under
  `[connectors.slack]`.

### Phase 20 specifics

- **No DB migration.** Phase 20 is config / connector / docs surface only.
- **No new extras.** The change reuses `[connectors-slack]` (slack-sdk).
- **No breaking change.** `channels` accepts both the historical string array and
  the new table form, so the Conventional Commit lands as `feat(slack):` (no `!`)
  and `release-please` bumps the minor lane.
- **One new ADR ([ADR-0036](adr/0036-slack-sync-date-floor.md)).** Full rationale,
  cursor-authoritative semantics, and rejected alternatives live there.

## Phase 20 (Slack thread reply ingestion): `conversations.replies` ingest + 2-axis compound cursor + `thread_activity_window`

Phase 20 ([ADR-0030](adr/0030-slack-thread-reply-ingestion.md) revised + landed,
epic [#465](https://github.com/ozzy-labs/opshub/issues/465)) extends
`opshub slack sync` to ingest **thread replies (late replies included) as
message-level `slack_message` rows** — symmetric with Gmail (`gmail_message`)
and Outlook (`ms365_outlook`). Parents and replies share the same source type,
and `thread_ts` is retained verbatim on `SourceObserved.raw["thread_ts"]`. The
`sources` projection schema is unchanged.

This is mostly behavioural — the only operator-visible action is a one-shot
`opshub slack cursor reset --all` to drop the pre-Phase-20-B flat-dict cursor
envelope and cold-start on the new 2-axis compound schema (Phase 23-A,
[#531](https://github.com/ozzy-labs/opshub/issues/531) corrected the original
`opshub projections rebuild` guidance, which was a dead-end for the flat dict).

### Behavioural change: thread replies are now ingested

Before Phase 20-A ([#466](https://github.com/ozzy-labs/opshub/issues/466) → PR
[#474](https://github.com/ozzy-labs/opshub/pull/474)), `opshub slack sync` only
called `conversations.history` and never walked into `conversations.replies`, so
any discussion happening inside a thread was structurally dropped (see ADR-0030
§Context for the failure modes per assistant skill).

After Phase 20:

1. **Phase 1 — channel history + snapshot replies.** Each
   `conversations.history` parent whose payload carries `latest_reply` triggers
   a single `conversations.replies(channel, ts=thread_ts)` call. `messages[0]`
   is the parent itself and is dropped (the
   `external_id = f"{channel_id}:{ts}"` UNIQUE constraint would also reject it
   idempotently). Children are yielded as additional `slack_message` rows. The
   cursor element on each reply yield is the **parent's `ts`** (not the reply
   ts) so reply timestamps do not skip past the gap between parents.
2. **Phase 2 — late-reply polling.** Known threads stored on the new `threads`
   axis of the cursor are re-polled with
   `conversations.replies(channel, ts=thread_ts, oldest=threads_cursor,
   inclusive=False)`; only late replies (those that arrived after the previous
   sync) are yielded. The `threads` axis is then advanced per yielded reply.
3. **Activity-window prune.** Threads whose `threads` cursor is older than
   `thread_activity_window` are skipped on Phase 2 and pruned from the cursor
   at the end of a successful sync (mid-iteration crashes preserve the entry so
   resume re-tries are safe).

### Apply the cursor schema migration (one-shot)

Phase 20-B ([#467](https://github.com/ozzy-labs/opshub/issues/467) → PR
[#473](https://github.com/ozzy-labs/opshub/pull/473)) reshapes the JSON value
stored in the existing `connector_cursors.cursor_value` TEXT column from the
legacy flat dict to a 2-axis envelope:

```json
{
  "channels": {"C012345": "1717000000.000100"},
  "threads":  {"C012345:1717000000.000100": "1717100000.000500"}
}
```

`opshub` is pre-userbase, so ADR-0030 ships **no silent migration**. The first
`opshub slack sync` against a pre-Phase-20-B database exits with:

```text
Error: Slack cursor envelope is pre-Phase-20-B (flat dict). Run
`opshub slack cursor reset --all` to drop it and cold-start on the
{"channels": ..., "threads": ...} compound schema. opshub is pre-userbase and
ships no silent migration (per ADR-0030 §不変条件 #4).
```

Recovery is one command (Phase 23-A,
[#531](https://github.com/ozzy-labs/opshub/issues/531)):

```bash
opshub slack cursor reset --all
# Hard-drops the cursor (without parsing the legacy value) and persists the
# empty compound. Unlike `opshub projections rebuild` — which replays the
# flat-dict `ConnectorSyncCompleted` payload verbatim and so loops back to the
# same error — this records an empty-compound `ConnectorSyncCompleted`, so even
# a later rebuild stays on the compound schema.

opshub slack sync
# Every channel cold-starts from its floor (`sync_since` / per-channel `since`,
# or all history if unset). The legacy `ts` could not be lifted 1:1 (it was the
# per-channel max including never-observed thread replies, which would break the
# Phase 20-C replies-fetch), so we cold-start rather than coerce.
```

> ⚠️ The old `opshub projections rebuild` recovery was a **dead-end** for the
> flat dict: rebuild restores `connector_cursors` by replaying the
> `ConnectorSyncCompleted` event, whose payload IS the flat dict, so the same
> `ConfigError` recurs. `opshub slack cursor reset --channel` / `backfill` also
> parse the cursor first and hit the same error — only `reset --all` hard-drops.

No Alembic migration is required — only the JSON value in the existing TEXT
column changes shape.

### Opt-in: tune `thread_activity_window`

The Phase 2 polling phase only walks threads whose `threads` cursor is within
`thread_activity_window` (default `"30d"`). Operators can widen or narrow the
window from `opshub.toml`, the CLI, or an env override:

```toml
[connectors.slack]
enabled = true
thread_activity_window = "60d"   # double the default window
# thread_activity_window = "all" # disable pruning entirely (rate-limit risk)
```

```bash
opshub slack sync --thread-activity-window 14d
OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW=14d opshub slack sync
```

The value goes through the shared `opshub.core.time.parse_since` helper
(Phase 20 [ADR-0036](adr/0036-slack-sync-date-floor.md) introduced this), so
relative durations (`"30d"` / `"4w"`) and ISO absolute dates (`"2026-05-01"`)
both work. `"all"` disables pruning (threads stay on the cursor forever and are
polled every sync).

Narrowing the window reduces `conversations.replies` budget pressure but
introduces a **cold-thread reactivation limitation**: replies posted to a thread
after its `threads` entry has been pruned will not be ingested. The
`channels` cursor is monotonic on parent `ts`, so Phase 1 will not re-fetch the
parent either. If you need to bring an old thread back into ingest, reset the
channel cursor with `opshub slack cursor reset --channel <id>` (Phase 22-E,
[ADR-0038](adr/0038-slack-sync-gap-backfill.md) §(f)); `opshub projections
rebuild` does **not** reset the cursor (it replays `ConnectorSyncCompleted` and
restores the same value).

### Behavioural notes (Phase 20 thread reply)

- **Symmetric with Gmail / Outlook.** Parents and replies share
  `source_type = slack_message`; the assistant skills (`find-document`,
  `research`, `reply-draft`, `recall.search`, `next-actions`,
  `personal-brief`, `meeting-followup`, `inbox-triage`) transparently benefit
  because they consume `sources.body` without branching on whether a source is
  a parent or a child. No SKILL.md change ships in Phase 20-D.
- **`conversations.replies` joins the rate-limit retry pool.** The new
  `_call_replies` is the 4th call site sharing
  `opshub.connectors.slack._retry.retry_on_rate_limit` (3 attempts, `Retry-After`
  honoured, fallback `1s / 2s / 4s` exponential). Bumping any of the
  per-Tier-3 budgets is a one-line change in `_retry.py` (ADR-0030 §不変条件 #6).
- **`excludes` is honoured before the `conversations.replies` call.** If a
  parent matches `[connectors.slack] excludes.channels` or `excludes.senders`,
  the additional `conversations.replies` call is skipped entirely (Phase 20-A
  API-budget guard, PR [#474](https://github.com/ozzy-labs/opshub/pull/474)).
- **Activity-window prune is independent of [ADR-0036](adr/0036-slack-sync-date-floor.md)
  `sync_since`.** `sync_since` bounds Phase 1's `conversations.history` floor;
  `thread_activity_window` bounds Phase 2's polling. They are orthogonal —
  `sync_since` can be unset while `thread_activity_window = "30d"` (or vice
  versa).
- **No new MCP tool / no new `source_type`.** `sources` projection, MCP surface,
  and `SourceObserved` event schema are all unchanged. ADR-0030 §不変条件 #1 #2
  #3 are reaffirmed.

### Phase 20 (thread reply) specifics

- **No DB migration.** Phase 20 thread-reply work is `cursor_value` JSON shape
  only — the Alembic head is untouched.
- **No new extras.** The change reuses `[connectors-slack]` (slack-sdk).
- **No breaking change for assistant skills.** `find-document` /
  `reply-draft` / `recall.search` transparently see thread reply rows.
- **One revised ADR, zero new ADRs.** [ADR-0030](adr/0030-slack-thread-reply-ingestion.md)
  was re-published as **Accepted + Landed (revised)** with §(d) rewritten from
  "late reply scope 外" to "2-axis compound cursor + Phase 2 polling + activity
  window prune".
- **Sub-PRs landed across Phase 20-A through 20-D.**
  - 20-A — fetcher + `thread_ts` field
    ([#466](https://github.com/ozzy-labs/opshub/issues/466) →
    PR [#474](https://github.com/ozzy-labs/opshub/pull/474))
  - 20-B — cursor compound schema
    ([#467](https://github.com/ozzy-labs/opshub/issues/467) →
    PR [#473](https://github.com/ozzy-labs/opshub/pull/473))
  - 20-C — late-reply polling + activity window
    ([#468](https://github.com/ozzy-labs/opshub/issues/468) →
    PR [#476](https://github.com/ozzy-labs/opshub/pull/476))
  - 20-D — ADR §(d) revise + docs sync
    ([#469](https://github.com/ozzy-labs/opshub/issues/469))
- **Troubleshooting:** [`docs/troubleshooting.md`](troubleshooting.md) §3.12
  walks through `--thread-activity-window` tuning, cold-thread reactivation,
  `conversations.replies` rate-limit handling, and recovery from the legacy
  cursor shape.

## Pre-userbase compat shim cleanup (epic #470)

Epic [#470](https://github.com/ozzy-labs/opshub/issues/470) is the
pre-userbase compat shim sweep: under
[ADR-0011 §設計判断のスタンス](adr/0011-ozzy-labs-ecosystem-adoption.md)
(no installed user base → no deprecation period, no dual-read), the four
shims accumulated through Phase 1-19 are removed in a single batch instead
of being unwound incrementally. Each sub-issue is a self-contained
`refactor!:` commit (`release-please` bumps the minor lane on the `0.x`
line — see top of this document for the SemVer posture) and lands its own
operator note. Operators who only ran defaults pre-#470 only need the
operator action in §`sources.body NOT NULL`; the other three sub-issues
either require no operator action or only a `opshub.toml` edit.

The four sub-issues (in landing order) are:

| Sub-issue | What it drops | Required operator action |
|---|---|---|
| [§Drop inline `exclude_globs`](#drop-inline-exclude_globs-pr-483--pr-484) | `BoxDriveConnectorSettings.exclude_globs` / `OneDriveDriveConnectorSettings.exclude_globs` Pydantic fields + `ExcludeRules.merged_with_paths` + `BoxDriveScanner._is_excluded` duplicate match | Required if you still use the inline TOML key — move patterns to `~/.config/opshub/excludes.yaml` `paths:` |
| [§Drop `expand_graph` param + make `LinkService` required](#drop-expand_graph-param--make-linkservice-required-pr-485) | `expand_graph: bool = False` kwarg on `BriefingService.generate` / `ProposalService.generate` / `ProposalService.generate_reply_draft` + `link_service: LinkService = None` constructor default + CLI `--expand-graph` flag + MCP schema property | Not required for default callers; required for any script that explicitly passed `expand_graph=...` / `--expand-graph` |
| [§Drop per-phase event union aliases](#drop-per-phase-event-union-aliases-pr-482) | `Phase2Event` / `Phase3Event` / `Phase4Event` / `Phase5Event` / `Phase6Event` / `Phase8Event` re-exports from `opshub.domain.events.__init__` | Not required (internal API only — `AllEvent` decoded every row already) |
| [§`sources.body NOT NULL`](#sourcesbody-not-null-pr-486) | `SourceObserved.body: str \| None = None` → `str = Field(min_length=1)` + `sources.body` `nullable=True` → `nullable=False` + migration `0030_enforce_sources_body_not_null` | **Required** — drop DB, run `opshub init`, reset cursors, re-sync each enabled connector, rebuild embeddings + FTS |

### Drop inline `exclude_globs` (PR [#483](https://github.com/ozzy-labs/opshub/pull/483) + PR [#484](https://github.com/ozzy-labs/opshub/pull/484))

ADR-0020 §(b) closeout. Removes the Phase 9 / Phase 11 F4-b inline `exclude_globs` field
from `BoxDriveConnectorSettings` and `OneDriveDriveConnectorSettings`. Path-based
exclusion now has a single SSOT: the `paths:` selector inside
`~/.config/opshub/excludes.yaml`
([ADR-0020 §(b)](adr/0020-full-local-content-retention.md)). The dual-read
shim that used to merge both lists at sync time is gone, and the two settings
models declare `model_config = ConfigDict(extra="forbid")` so a stale TOML
key surfaces as a fail-fast `ValidationError` instead of the silent "no path
filter applied" degradation the old merger could mask.

> **Required operator action (perform before the next sync).** If your
> `opshub.toml` still carries `[connectors.box_drive] exclude_globs = [...]`
> or `[connectors.onedrive_drive] exclude_globs = [...]`, **move the
> patterns into `~/.config/opshub/excludes.yaml` `paths:`** and delete the
> TOML key. Leaving the inline key in place means the next
> `opshub box_drive sync` / `opshub onedrive_drive sync` (or any CLI that
> instantiates `OpsHubSettings`) raises `ValidationError`: `extra fields
> not permitted` and exits non-zero.

Before (Phase 9 / Phase 11 F4-b shape):

```toml
# ~/.config/opshub/opshub.toml
[connectors.box_drive]
enabled = true
exclude_globs = ["**/.DS_Store", "**/~$*", "**/secrets/**"]

[connectors.onedrive_drive]
enabled = true
exclude_globs = ["**/.DS_Store", "**/~$*"]
```

After (post-#470 shape — inline key removed, patterns moved to the shared
`excludes.yaml`):

```toml
# ~/.config/opshub/opshub.toml
[connectors.box_drive]
enabled = true

[connectors.onedrive_drive]
enabled = true
```

```yaml
# ~/.config/opshub/excludes.yaml
paths:
  - "**/.DS_Store"
  - "**/~$*"
  - "**/secrets/**"
```

The shared `paths:` selector was already honoured by both connectors
pre-#470 (ADR-0020 §(b) introduced it in Phase 10), so the migration is a
pure copy — no semantic change beyond losing the silently-ignored inline
path. Patterns continue to use fnmatch / gitignore-style syntax matched
against the POSIX-form `rel_path`; `**/` is treated as optional so a single
pattern catches both nested and top-level files.

#### Why this shape

- `excludes.yaml` is the cross-connector SSOT for ingest exclusion
  (`channels` / `senders` / `repos` / `paths`); keeping path filtering inline
  in two connectors left operators with two places to audit and a dual-read
  merge that could not be expressed in one settings query.
- The Phase 11 audit Cluster B `_is_excluded` duplicate match logic on
  `BoxDriveScanner` was wholly redundant with `ExcludeRules.excludes_path`.
  Both scanners now delegate to the value object, so the four-selector
  exclusion logic lives in exactly one module
  (`src/opshub/core/excludes.py`).
- `extra="forbid"` on the two settings models is scoped to the touched
  connectors. A repo-wide `extra="forbid"` rollout for every `OpsHubSettings`
  child is intentionally deferred to a follow-up issue; this epic only
  removes the two shims it had to remove.

#### Specifics

- **No DB migration.** Cleanup is config / schema / docs surface only.
- **No new extras.** Existing `[connectors-box-drive]` / `[connectors-onedrive-drive]`
  dependency closures are unchanged.
- **Breaking config change.** The Conventional Commit lands as `refactor!:`
  and `release-please` bumps the minor lane (`0.x` line — see top of this
  document for the SemVer posture). Operators who never used the inline
  `exclude_globs` key see no behavioural difference.
- **One ADR.** [ADR-0020 §(b)](adr/0020-full-local-content-retention.md)
  was re-published with the "future cleanup" comment removed and the
  Implementation status flipped to `landed`.

### Drop `expand_graph` param + make `LinkService` required (PR [#485](https://github.com/ozzy-labs/opshub/pull/485))

ADR-0017 §決定 (f) closeout. Drops the
`expand_graph: bool = False` keyword from
`BriefingService.generate` / `ProposalService.generate` /
`ProposalService.generate_reply_draft` and promotes
`link_service: LinkService` to a required keyword on both service
constructors. Graph 1-hop expansion is now the unconditional Phase
8+ behaviour (ADR-0017 §決定 (e)+(f)).

ADR-0017 §決定 (f) was originally landed (Phase 8 D2) with the
`expand_graph: bool = False` default as a backward-compat shim so
the Phase 5/6 prompt-snapshot tests would keep passing. In
practice, all 6 graph-aware assistant skill SSOTs
(`docs/skills/{reply-draft,inbox-triage,meeting-followup,
source-extract,research,external-brief}/SKILL.md`) explicitly
passed `expand_graph: true`, and `cli/_wiring.py` always supplied
a `LinkService` — the `expand_graph=True + link_service=None`
`ConfigError` dead-branch never fired in production.
Sub-issue #472 drops the entire opt-in surface:

- **Service `generate` / `generate_reply_draft`** no longer accept
  an `expand_graph: bool` keyword; graph 1-hop expansion runs
  unconditionally.
- **`ProposalService` / `BriefingService` constructors** require a
  `link_service: LinkService` keyword (no `None` default). Passing
  `link_service=None` raises `TypeError` at construction time.
- **`ConfigError("expand_graph=True requires LinkService ...")`**
  is removed; the dead-branch no longer exists.
- **MCP `briefing.generate` / `proposal.generate` tool schemas**
  drop the `expand_graph` property. With `additionalProperties:
  false` already pinned, an agent host calling `{"expand_graph":
  ...}` now fails schema validation (caught by
  `tests/unit/mcp/test_registry_policy.py::test_brief_and_propose_generate_drop_expand_graph_property`).
- **CLI `opshub brief --expand-graph` / `opshub propose generate
  --expand-graph` flags** are removed. Typer reports unknown
  option and exits 2 (pinned in
  `test_brief_rejects_legacy_expand_graph_flag` /
  `test_generate_rejects_legacy_expand_graph_flag`).
- **6 SKILL.md SSOTs** drop the `expand_graph: true` argument line
  and replace it with a short note that graph expansion is the
  default. `uv run opshub skills install --scope project`
  regenerates `.claude/skills/` + `.agents/skills/` mirrors;
  the `skills-sync-check` lefthook hook pins drift.

Operator impact:

1. Any caller passing `expand_graph=...` / `--expand-graph` /
   `{"expand_graph": ...}` will **fail fast** at the boundary
   (unknown option / additional property not allowed / `TypeError`).
   Pre-userbase posture → no compat shim, no deprecation period.
2. Graph expansion now runs on every `brief` / `propose generate` /
   `propose generate --reply-to` invocation. LLM prompt size + cost
   increase up to ~20-30% versus the Phase 5/6 baseline (max
   `original_recall_hits × 3` neighbour entities per call, capped
   by `_GRAPH_EXPAND_PER_HIT_LIMIT = 3`). Cost is controlled via the
   existing `max_tokens` knob (ADR-0015 §決定 (h) — caller
   responsibility); there is no `--no-expand-graph` opt-out.
3. ADR-0017 §決定 (f) was rewritten to record the param-drop
   landing; Phase 8 plan §2.4 D2 remains as a historical record of
   the opt-in shape that has now been retired.

### Drop per-phase event union aliases (PR [#482](https://github.com/ozzy-labs/opshub/pull/482))

ADR-0002 §Decision 5 closeout. Deletes the 6 per-phase
discriminated-union aliases — `Phase2Event` / `Phase3Event` /
`Phase4Event` / `Phase5Event` / `Phase6Event` / `Phase8Event` (Phase 7
was projection-only and never had a `Phase7Event`) — from
`src/opshub/domain/events/__init__.py` and consolidates on the flat
`AllEvent` union. The aliases were historical bookkeeping; the
persistence layer (`SqlAlchemyEventStore._decode`) always reached for
`AllEvent` and never touched the per-phase unions.

- **Operator action: none.** Event store rows (`events` table) decode
  byte-for-byte identically — `AllEvent` already enumerated every event
  type listed in any per-phase alias, so no `opshub projections rebuild`
  is required. Existing SQLite databases continue to load.
- **Internal API change.** The aliases also disappear from `__all__`,
  and `from opshub.domain.events import Phase5Event` (etc.) now raises
  `ImportError`. Test fixtures / migration scripts that referenced them
  switch to `TypeAdapter(AllEvent)` or the individual event classes
  (`from opshub.domain.events import BriefingRequested`). The
  phase-scoped grouping never carried production semantics, so this is
  purely a renaming exercise.
- **External callers (pre-userbase, expected to be none):** any script
  that did `from opshub.domain.events import PhaseNEvent` must switch to
  `AllEvent` or the concrete event classes. There is no transitional
  re-export; the aliases are gone in one step.
- **One ADR.** [ADR-0002 §Decision 5](adr/0002-event-sourced-architecture.md)
  records the `AllEvent`-only consolidation and the rationale for
  removing the per-phase aliases.

### `sources.body NOT NULL` (PR [#486](https://github.com/ozzy-labs/opshub/pull/486))

[ADR-0020 §(d')](adr/0020-full-local-content-retention.md) promotes
`SourceObserved.body` from `str | None = None` to `str =
Field(min_length=1)` and `sources.body` from `nullable=True` to
`nullable=False` (migration
`0030_enforce_sources_body_not_null`). The previous `body=NULL` shim —
used by stat-only / metadata-only connectors (`box_drive`
`content_extraction=False`, Google Workspace `google_workspace_file`
catch-all, MS365 OneDrive metadata, Box web-API events, GitHub
notifications, ...) — is replaced by **`body = summary`** at the mapper
layer ([ADR-0010 §不変条件 metadata-only rule](adr/0010-connector-contract.md)).
The `body` ↔ `summary` semantic distinction (preview vs. full text /
search target) is preserved: stat-only paths emit identical values for
both, the body-extracting paths (Office documents, Slack messages,
Outlook bodies, Calendar descriptions, Gmail bodies) keep them
distinct.

#### ⚠️ **Required operator action**

Migration `0030` drops every existing `sources` row whose `body IS
NULL` (or empty) as part of the rebuild. The pre-userbase posture
tolerates the round-trip: the next connector sync regenerates those
rows with the new `body = summary` contract. Operators **MUST**:

1. **Drop the existing event store + projections** (`rm -f
   ~/.opshub/opshub.sqlite*` or whatever path your `[storage] db_path`
   resolves to). The migration itself only purges projection rows but
   the cleanest reset is to start fresh — historic `body=None` events
   would deserialise into `ValidationError` against the new schema
   anyway.
2. **Run `opshub init`** to re-mint the DB at head (which now includes
   migration 0030 — the `sources` table comes up with `body NOT NULL`
   from the start).
3. **Reset every connector's cursor** so the next sync re-fetches
   every source from scratch and the mapper substitution lands the
   non-empty body. With the DB dropped, cursors are already reset; on
   a partial cleanup, run:

   ```bash
   sqlite3 ~/.opshub/opshub.sqlite \
     "DELETE FROM connector_cursors WHERE connector_name IN \
        ('github','slack','box','box_drive','ms365','teams','onedrive_drive', \
         'google_workspace','google_mail','google_calendar')"
   ```

4. **Re-sync each enabled connector** for full re-population:

   ```bash
   opshub github sync           # GitHub issues / PRs / notifications
   opshub slack sync            # Slack messages
   opshub box sync              # Box web-API events
   opshub box-drive sync        # Box Drive FS scan (body = path summary)
   opshub ms365 sync            # Outlook / OneDrive metadata / Calendar
   opshub teams sync            # Teams chat messages
   opshub onedrive-drive sync   # OneDrive FS scan (body = path summary)
   opshub google-workspace sync # Drive (Docs / Slides / Sheets + catch-all)
   opshub google-mail sync      # Gmail messages
   opshub google-calendar sync  # Calendar events
   ```

   Only run the commands for connectors you have configured in
   `opshub.toml`; the others are no-ops if disabled.

5. **Rebuild embeddings + FTS** so the new body content is indexed:

   ```bash
   opshub embeddings rebuild --purge   # re-embed against the new body surface
   opshub projections rebuild          # idempotent; the migration already
                                       # rebuilt sources, but this also
                                       # refreshes downstream projections
   ```

After these steps `SELECT COUNT(*) FROM sources WHERE body IS NULL OR
length(body) = 0` returns 0 and the embed / search surface uniformly
reads `sources.body` (no more `COALESCE(body, summary)` fallback).

#### Why this is safe

- **No installed user base.** Opshub is single-user, pre-1.0; there are
  no third-party deployments to migrate gracefully
  ([ADR-0011](adr/0011-ozzy-labs-ecosystem-adoption.md)).
- **Append-only event log.** The legacy `body=None` events are gone
  from the projection, but the event log is the SSOT — they are
  preserved (and ignored on rebuild since `SourceObserved.body` now
  rejects `None`). Operators who want to keep historic events for
  audit can keep the old DB on disk; the new one is what serves
  queries.
- **Idempotent connectors.** Each `<connector> sync` upserts on the
  natural key `(connector_name, external_id)`, so re-syncing
  reproduces the same projection state with the new body contract.

## Phase 21: Browser read layer (Playwright) + web connector + MCP `browser.fetch`

Phase 21 ([epic #504](https://github.com/ozzy-labs/opshub/issues/504), [ADR-0037](adr/0037-browser-read-layer-playwright.md) new + [ADR-0010](adr/0010-connector-contract.md) §Phase 21 revision (n)/(o) + [ADR-0022](adr/0022-mcp-server-surface.md) §決定 (g) revision) adds a Playwright-backed **browser read layer**: an 11th connector (`web`) that renders operator-listed URLs and a new MCP write tool (`browser.fetch`) for ad-hoc page reads. **No DB migration, no breaking CLI changes** — the additions are purely additive (a new opt-in connector, a new opt-in extras group, a new MCP tool). The PRs: 21-A [#510](https://github.com/ozzy-labs/opshub/pull/510) (ADRs), 21-B [#511](https://github.com/ozzy-labs/opshub/pull/511) (browser core + `[browser]` config + `browser` extras + CI Chromium), 21-C [#513](https://github.com/ozzy-labs/opshub/pull/513) (web connector + `opshub web sync`), 21-D [#512](https://github.com/ozzy-labs/opshub/pull/512) (MCP `browser.fetch`), 21-E (this docs closeout).

### New extras: `browser` (Playwright)

The browser layer is gated behind a new optional-dependency group so operators who never read Web pages pay nothing:

```bash
uv sync --extra browser          # pulls playwright>=1.50
uv run playwright install chromium   # one-time Chromium binary provisioning (operator step, ADR-0037 §決定 (g))
```

`uv tool install` users add it the same way:

```bash
uv tool install "ozzylabs-opshub[browser]"
playwright install chromium
```

The `playwright` import is lazy inside `opshub.browser.core.fetch_page`, so the cold-start guard (`opshub --help` ≤ 300 ms) is unaffected when the extra is absent. When Chromium is missing, the `web` connector and `browser.fetch` fail with a `ConfigError` that names `playwright install chromium` (ADR-0037 §決定 (g)) rather than a raw Playwright "executable doesn't exist" trace.

### New config: `[browser]` (shared infrastructure)

Browser tuning lives at the **settings root** (`[browser]` / `OPSHUB_BROWSER__*`), not under any connector, because both the `web` connector and the `browser.fetch` MCP tool drive the same browser core (ADR-0037 §決定 (c)):

```toml
[browser]
headless = true        # run Chromium without a GUI (default; flip to false only when debugging a render)
# channel = "chrome"   # use a branded system Chromium instead of the bundled one; default = bundled
timeout = 30000        # per-navigation ceiling in milliseconds (Playwright unit); 0 disables (discouraged)
# cdp_endpoint = "http://localhost:9222"  # attach to an operator-launched Chrome (connect_over_cdp escape hatch); default = launch our own persistent context
```

Env override examples: `OPSHUB_BROWSER__HEADLESS=false`, `OPSHUB_BROWSER__TIMEOUT=60000`. The Chromium profile uses an **opshub-dedicated user-data-dir** under the data dir (`default_data_dir() / "browser"`); your everyday Chrome profile is never touched (ADR-0037 §決定 (c)).

### New config: `[connectors.web]` (the web connector)

The `web` connector is opt-in like every connector (default `enabled = false`). Configure it with a **plain list of URL strings**:

```toml
[connectors.web]
enabled = true
pages = [
  "https://example.com/status",
  "https://docs.internal.example/runbook",
]
```

The table form `[[connectors.web.pages]]` is deliberately **not** accepted — a Web page has no per-page knobs worth a table (no `since` window, no auth principal), so the string array is the whole contract. Stale / mistyped keys are rejected with a fail-fast `ValidationError` (`model_config = ConfigDict(extra="forbid")`).

Then provision Chromium once and sync:

```bash
playwright install chromium
opshub web sync          # renders each listed URL, persists changed pages as web_page sources
```

`opshub web sync` has **no `auth` sub-app** (public pages need no auth; `opshub web auth set` falls through to Typer's `No such command 'auth'` exit-2, the ADR-0031 §決定 (6) box_drive precedent). Progress renders on a TTY (ADR-0026); override with `--progress` / `--no-progress` / `OPSHUB_PROGRESS`.

### Behavioural notes (Phase 21 web connector)

- **Operator-listed URLs only — not a crawler.** The connector fetches exactly the URLs in `[connectors.web] pages` and never follows `<a href>` links found on them, nor walks a sitemap (ADR-0010 §Phase 21 revision (n)). Link-following / sitemap crawling is structurally absent (no code path) to keep form-A (no proactive runtime) integrity.
- **Change detection by fingerprint, not delta API.** Web pages have no "what changed since last time" API, so the connector reuses the [ADR-0019](adr/0019-local-filesystem-backed-connector.md) §決定 (d) `sources.fingerprint` pattern (the same one `box_drive` / `onedrive_drive` use): each URL's extracted body is SHA-256 hashed and compared against the prior `sources.fingerprint`. An **unchanged page emits no event**, so re-running `opshub web sync` is idempotent. No new column or event field is added — the existing `sources.fingerprint TEXT NULL` (migration 0017) and `SourceObserved.fingerprint` field are reused.
- **A single dead URL is logged at WARN and skipped**; the other pages still sync (navigation error / timeout → `BrowserFetchError`, a `ConnectorFailedError` subclass, handled by the existing per-page fail-safe).
- **Bodies are extracted from the rendered DOM** (`page.inner_text("body")` after `wait_until="load"`), head-truncated at 500K chars with the ADR-0025 marker (`opshub.core.text_limits.truncate_with_marker`, shared with Office extraction). This is the Playwright-adoption payoff: JS-rendered SPA / dynamic-page bodies that `requests` + HTML parsing cannot reach land in `sources.body`, searchable from FTS5 / recall / the assistant skills.
- **External write-back stays forbidden** (ADR-0010 §禁止事項 7). The browser layer is **read-only** — it carries no `page.click` / `page.fill` / `page.set_input_files` / form-submit code path. Operations (click / fill / submit) are deferred to a later phase that must first settle HITL approval granularity, Web-page-sourced prompt-injection defence, and the ADR-0010 write-back ban整理 (ADR-0037 §決定 (f)).

### New MCP tool: `browser.fetch` (write-category, HITL)

Phase 21-D adds one new MCP **write** tool, taking the surface from 18 to **19 tools (13 read + 6 write)**:

| Field | Value |
| --- | --- |
| name | `browser.fetch` |
| category | `WriteCategory.BROWSER_FETCH` |
| annotation | `readOnlyHint=false`, `destructiveHint=true`, `idempotentHint=false`, `openWorldHint=true` (same pattern as `connector.sync`) |
| input | `url` (required; `http` / `https` only — `file` / `data` / `ftp` / `javascript` are rejected by the handler) |
| output | 200-char DOM-text snippet (secret-redacted) + `<title>` + `text_chars` (full length) + `truncated` (browser core 500K cap hit) + `persisted: false` |

It is classified **write-category even though it returns data** because it **egresses the public network** (ADR-0037 §決定 (e), ADR-0022 §決定 (g)). The "read tool = local SQLite only" invariant reserves the network-egress bucket for the same HITL treatment as `connector.sync`: the call leaves a trace in the remote's rate-limit / audit log, and the fetched body is an indirect-prompt-injection carrier — so a host should confirm with the operator before invoking. **Nothing is persisted** — durable ingestion is the `web` connector's job. The async MCP handler bridges to the synchronous browser core via `asyncio.to_thread` (Playwright's sync API cannot run inside the asyncio loop, ADR-0037 §決定 (h)).

No assistant skill calls `browser.fetch` as a primary path yet; the `research`-skill wiring is deferred to the operations phase together with the click / fill / submit work (ADR-0037 §Non-goals). The 14-skill catalog and every `SKILL.md` are unchanged in Phase 21 (`test_skills_install_only_writes_14_assistant_skills` stays green).

### Phase 21 specifics

- **No DB migration.** No new columns / events — the web connector reuses `sources.fingerprint` (migration 0017) and `SourceObserved.fingerprint`.
- **New extras `browser`** (`playwright>=1.50`) + one-time `playwright install chromium` operator step.
- **New config sections** `[browser]` (shared) and `[connectors.web]` (web connector). Both opt-in; absent config means the layer never runs.
- **MCP surface 18 → 19 tools = 13 read + 6 write** (the new write tool `browser.fetch`).
- **CI** runs the `browser` integration tests (`@pytest.mark.browser`) against a localhost `http.server` fixture with a real Chromium provisioned by `playwright install chromium` — no external network egress from CI (epic #504 test plan).

## Inbox idempotency (issue #522): one inbox row per `source_ref`

The `inbox` projection now **deduplicates `ItemEnqueued` by `source_ref`**. `SourceService.observe` appends a fresh `ItemEnqueued` event (new ULID) on *every* re-observation of an external item (ADR-0002 §"every observation is a new event"); previously each event inserted a new inbox row, so a cursor rewind / reset / backfill (epic #516) or a fingerprint-driven re-observation (`box_drive` / `web`) could multiply inbox rows for one source — the issue #339 cascade, re-instantiated.

### Behavioural change (all connectors)

A re-observed or edited source **no longer re-surfaces in the inbox**. The reducer now does `INSERT ... ON CONFLICT(source_ref) DO NOTHING` against a new partial unique index `uq_inbox_items_source_ref` (`WHERE source_ref IS NOT NULL`):

- **First-observation wins.** The first inbox row for a `source_ref` survives; later observations are dropped. Because `iter_all` replays in `(recorded_at, id)` order, the surviving row is deterministic across rebuilds.
- **Triaged items stay resolved.** Re-observing a source whose inbox item was already triaged (`to_task` / `decision` / `discard`) does **not** re-open it. A "re-triage this edited source" workflow, if ever wanted, is a separate signal — not a side effect of dedup.
- **`source_ref IS NULL` is exempt.** Manual / source-less enqueues (`opshub inbox add` etc.) carry no natural key, fall outside the partial index, and keep their historical one-row-per-event behaviour.

### Migration `0031_add_inbox_items_source_ref_unique_index`

`opshub db migrate` creates the partial unique index. A pre-existing database may already hold duplicate `source_ref` rows minted before this revision; the migration **deletes the duplicates first**, keeping the smallest `id` per `source_ref` (ULID-monotonic ≈ first observation) so the index can be created.

The kept row is a best-effort proxy. For the canonical `(recorded_at, id)` first-wins row, follow up with:

```bash
opshub projections rebuild
```

The event log is untouched (every historic `ItemEnqueued` is preserved); the rebuild replays through the new `DO NOTHING` reducer to land the deterministic result. `downgrade` drops the index only — deleted duplicate rows are not restored (rebuild re-materialises them).

## Phase 22: Slack sync gap backfill (floor 引き下げ時の自動取り直し)

[ADR-0038](adr/0038-slack-sync-gap-backfill.md)（epic [#516](https://github.com/ozzy-labs/opshub/issues/516)）で、`opshub slack sync` の date floor ([ADR-0036](adr/0036-slack-sync-date-floor.md)) を**引き下げたとき、新たに広がった過去区間だけを自動で取り直す** gap backfill を追加した。

### 何が変わったか

- **`[connectors.slack] sync_since`（または per-channel `since`）を下げると、次回 sync が `(新floor, 旧low-water]` の窓だけを取得**して追いつく。forward の既取得区間には触れないので inbox は膨張しない。これは **feature 着地後に sync された channel** で自動的に効く。
- compound cursor が 2 軸 → **3 軸**に拡張された: `{"channels": ..., "backfill": ..., "threads": ...}`。新しい `backfill` 軸が per-channel の low-water mark（取得済み最古 ts 境界）を保持する。**旧 2 軸 cursor は無改修で読める**（`backfill` 欠損は空として許容）。
- **相対 floor (`"90d"`) は誤発火しない**（sync ごとに ts が前進するため）。発火するのは**絶対 floor の引き下げ / `sync_since` の縮小 / floor の撤去**のときだけ。

### 重要: `opshub projections rebuild` は cursor をリセットしない

旧ドキュメント（ADR-0036 §Consequences 等）は「古い履歴を取り直すには `opshub projections rebuild`」と案内していたが、これは**誤りだった**。rebuild は event log を保持したまま projection を流し直し、`connector_cursors` は `ConnectorSyncCompleted` を replay して cursor を**同じ最新値に復元する**ため、cursor はリセットされず再 backfill も起きない。Phase 22 で正しい手段を提供した（下記）。

### 新コマンド `opshub slack cursor`

```bash
opshub slack cursor show                                   # 現在の 3 軸 cursor を pretty-print (--format=json も可)
opshub slack cursor reset --channel C1,C2                  # 指定 channel を cold-start 化 (working reset; --all で全 channel)
opshub slack cursor backfill --channel C1 --since 30d      # (since, low-water] を取り直し (low-water 既知なら --until 省略可)
opshub slack cursor backfill --channel C1 --since 2026-01-01 --until 2026-03-01  # 明示窓
```

- **`reset`** は破壊的（cold-start 再取得で既取得分を再観測し得る）。確認プロンプトあり（`--yes` で skip）。
- **`backfill`** はサージカル（指定窓だけ取得、overlap なし）。**pre-feature channel（本機能着地より前に sync 済み = `backfill` 軸に low-water 未記録）の救済主経路**。pre-feature channel は過去 floor が復元不能なため auto backfill されず、`--until` に当時の floor を明示して取り直す。

### per-invocation opt-out

- `[connectors.slack] backfill_on_floor_lower = false`（default `true`）で auto backfill を恒久無効化。
- `opshub slack sync --no-backfill` で当該 run だけ抑止（floor は forward fetch の bound としては効く）。

### Phase 22 specifics

- **No DB migration.** cursor schema 拡張は `connector_cursors.cursor_value`（TEXT）内の JSON で完結（additive、新 column / event なし）。
- **MCP surface 不変**（19 tools）。skill catalog 不変（14、`test_skills_install_only_writes_14_assistant_skills` green）。
- **関連**: inbox `ItemEnqueued` の `source_ref` 冪等化（[#522](https://github.com/ozzy-labs/opshub/issues/522)、旧 #339 class）は hardening dependency として別 issue 化。着地すれば `reset` / pre-feature 経路の残留 overlap も無害化される。

## Phase 23-B: `opshub <connector> sync` 失敗 stderr にサニタイズ済み本文をデフォルト表示 ([#532](https://github.com/ozzy-labs/opshub/issues/532))

sync 失敗時の stderr サマリが、これまで `OPSHUB_DEBUG=1`（`--debug`）でしか出なかった**サニタイズ済みの例外本文**をデフォルトで表示するようになった。sync は scope / token / rate limit / legacy cursor など最もエラーに当たる経路で、connector 側が回復手順（scope catalogue URL / `run opshub slack auth set` / 代替軸誘導など）を本文に書いているのに、default では型名だけ（`sync failed: ConfigError`）しか手に入らなかった問題を是正する。

### 変更点

- **Before**: `sync failed: <Type>`（型名のみ）。本文は `OPSHUB_DEBUG=1` 限定。
- **After**: `sync failed: <Type>: <sanitised-msg>`（型名 + サニタイズ済み本文）をデフォルト stderr に表示。`--debug` が追加するのはサニタイズ済み traceback だけ。

### 不変条件（変わらないもの）

- **`sync failed:` prefix は維持**。prefix-anchored な grep スクリプトは引き続き機能する。
- **secret redaction は全 verbosity で維持**。本文は `core/sanitise.sanitise_error_message` を通すので、`sk-` / `ghp_` / `github_pat_` / `xox*-` / `AKIA` / `AIza` / JWT / `Bearer` 等のトークン形状は marker に置換される（ADR-0027 R4）。本文は元々 `--debug` 経路で同じサニタイズを経て出ていたため、**secret 新規露出はゼロ**。
- **event log の `error_message` は型名のみ**（`ConnectorSyncFailed.error_message`、R3 不変条件）。stderr を昇格しても event log の token surface は広がらない。
- 成功時の `synced <name>: N item(s) observed` は **stdout** 据え置き。

これで sync と `opshub slack conversations`（`Error: <full text>`）のエラー表示が対称になった。

### Phase 23-B specifics

- **No DB migration.** stderr 表示の昇格のみ。schema / event 不変。
- **MCP surface 不変**（19 tools）。skill catalog 不変（14）。
- **関連**: [ADR-0027](adr/0027-observability-and-troubleshooting-logging.md)（observability / redaction）, [#320](https://github.com/ozzy-labs/opshub/issues/320) (R2/R3/R4)。
