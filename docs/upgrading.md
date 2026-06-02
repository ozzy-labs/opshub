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

OpsHub's config file (`$XDG_CONFIG_HOME/opshub/config.toml`) is loaded via
[`pydantic-settings`](https://docs.pydantic.dev/latest/usage/pydantic_settings/),
which silently ignores unknown keys. New optional config fields added in a
minor version are backward-compatible — old configs continue to work and the
defaults apply.

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

# 2. Store the DB key in the OS keychain (recommended).
opshub connector auth set db:encryption_key      # interactive paste-once flow
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
opshub connector sync box_drive
opshub connector sync onedrive_drive
opshub embeddings rebuild                        # re-embed with the new body content
```

Limits ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (b)):

- Files larger than 50 MB are skipped with `body=None` + a warning log
- Extracted text longer than 500 000 chars is head-truncated and annotated
- Extraction failures (corrupted file, password-protected workbook, unsupported sub-format) surface as `body=None` + sanitised warning; the metadata row is still emitted so the scan never blocks on a single bad file

Per-operator overrides live under `[office]` in `opshub.toml` (`max_file_size_mb`, `max_chars`).

### Opt-in: enable the `teams` connector

```bash
# 1. Install the connectors-teams extras.
uv tool install "ozzylabs-opshub[connectors-teams]"

# 2. Register an Azure app and grant Chat.Read (see docs/teams-setup.md).
# 3. Store the Microsoft Graph User Token.
opshub connector auth set connector:teams        # paste-once
# Or for CI / non-interactive contexts:
export OPSHUB_CONNECTOR_TEAMS_TOKEN="<access_token>"

# 4. Flip the config flag.
# In ~/.config/opshub/config.toml:
#   [connectors.teams]
#   enabled = true
#   fallback_window_days = 30                    # delta-link invalidation fallback window

# 5. Sync.
opshub connector sync teams
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
opshub connector sync onedrive_drive
```

Full OneDrive Drive setup (mount, root_path overrides, troubleshooting): [`docs/onedrive-drive-setup.md`](onedrive-drive-setup.md).

### Behavioural change: Outlook body deep retention

The existing `ms365` connector's Outlook mapper (`opshub.connectors.ms365.mapper.map_outlook_message`) now retains the full email body alongside the summary. Existing rows with `body = NULL` continue to round-trip cleanly through every query path (the recall / search / brief paths already tolerated NULL bodies in Phase 10). Re-running `opshub connector sync ms365` after upgrading will start populating bodies for newly observed mail. Bodies larger than 500K chars are head-truncated inline.

To re-embed the freshly-retained bodies:

```bash
opshub connector sync ms365
opshub embeddings rebuild
```

### Phase 11 specifics

- DB head = `0019_create_sources_fts` (Phase 10) — Phase 11 ships **no** new migrations; bodies flow through the existing `sources.body` column + FTS5 index.
- New optional extras: `office` (`markitdown[docx,xlsx,pptx]` — i.e. markitdown plus the `mammoth` / `openpyxl` / `python-pptx` sub-extras for the three Office sub-formats opshub extracts) / `connectors-teams` (msal + httpx).
- New connectors: `teams` (Microsoft Graph chat delta) / `onedrive_drive` (OneDrive Desktop FS scan).
- New `source_type` discriminators: `teams_message` / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`.
- No CLI breaking changes. `opshub connector sync teams` / `... onedrive_drive` are the new sync targets.
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
opshub connector auth set google_workspace        # opens the consent URL; paste the code back
# Or for CI / non-interactive contexts:
export OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN="<refresh-token>"

# 5. Sync.
opshub connector sync google_workspace
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
opshub connector sync google_workspace
opshub embeddings rebuild                        # re-embed with the new body content
```

When `content_extraction = true`, the connector calls Drive API `files.export(fileId, mimeType=<MS Office mediatype>)` for the three Workspace native source_types (`google_doc` → `.docx`, `google_slides` → `.pptx`, `google_sheets` → `.xlsx`) and routes the bytes through `core/document_extract.extract_workspace_export(bytes, source_type)`. The same caps from Phase 11 apply ([ADR-0025](adr/0025-office-document-content-extraction.md) §決定 (b)):

- Files larger than 50 MB are skipped with `body=None` + a warning log (configurable via `[office] max_file_size_mb`).
- Extracted text longer than 500 000 chars is head-truncated and annotated (configurable via `[office] max_chars`).
- Export failures (Drive throttling, file permission loss, malformed export) surface as `body=None` + sanitised warning; the metadata `SourceObserved` is still emitted so the sync never gets blocked on a single bad export.

Non-native files (the catch-all `google_workspace_file` source_type — Drive returns 403 `fileNotExportable` for them) stay metadata-only regardless of `content_extraction`.

### New source_types

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
- **No CLI breaking changes**. `opshub connector auth set google_workspace` (positional form, not `connector:google_workspace`) and `opshub connector sync google_workspace` are the new sync targets — both are additive.
- **External write-back is still forbidden** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7). Drive write APIs (`files.update` / `files.create` / `files.copy` / `comments.create` / `permissions.*`) are deliberately not implemented in the connector. Drive push notifications (`files.watch`) are also forbidden — the connector polls via `changes.list` only to preserve form-A (no opshub-internal runtime).

## Phase 14: Gmail + Google Calendar connectors

Phase 14 ([ADR-0010](adr/0010-connector-contract.md) revision §Phase 14 (i)-(m) + [ADR-0014](adr/0014-saas-token-storage.md) revision (scope expansion for `connector:google_workspace:refresh_token` slot + shared auth foundation extraction)) adds two new connectors — `google_mail` (Gmail) and `google_calendar` (Google Calendar) — that **share the existing Google OAuth principal** from Phase 13. **No DB schema changes** (the existing `sources.body` column + FTS5 index from Phase 10 carry the new bodies), **no breaking CLI changes**, **no new extras** (`connectors-google-workspace` is reused for httpx), and the external write-back ban remains in force.

### Re-consent required (existing operators)

If you already configured the `google_workspace` connector in Phase 13, **your existing refresh token covers only `drive.readonly`** and will not work for the new Gmail / Calendar APIs. Phase 14 expands the shared OAuth principal to the 3-scope fixed list `drive.readonly + gmail.readonly + calendar.readonly` (1 Google account = 1 principal shared across Drive + Gmail + Calendar, [Phase 14 plan §1 OQ6](phase-14-plan.md#1-確定済み事項)). To upgrade:

1. **Re-register scopes in the Google Cloud Console.** Open your existing OAuth client → OAuth consent screen → **Add or Remove Scopes** → add `gmail.readonly` and `calendar.readonly` (`drive.readonly` is already there from Phase 13). Submit for verification if your project is in production mode. Full walkthrough: [`docs/google-workspace-setup.md`](google-workspace-setup.md) §Scopes.
2. **Re-run the paste-code flow once.** A single re-consent applies to all three connectors:
   ```bash
   opshub connector auth set google_workspace
   # browser opens with the new 3-scope consent screen; paste the code back
   ```
   The keyring slot stays the same (`connector:google_workspace:refresh_token`), so existing env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` continues to work — just refresh its value once.
3. **Existing `google_workspace` sync continues to work** unchanged after re-consent. The Drive endpoint still uses `drive.readonly`; the extra scopes are unused there.

If you skip re-consent, `opshub connector sync google_mail` and `opshub connector sync google_calendar` will fail with a 401 `Request had insufficient authentication scopes` error and exit cleanly without writing any events.

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
opshub connector sync google_mail
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
opshub connector sync google_calendar
```

The connector reads Calendar API v3 `events.list(syncToken=...)` for delta. When Google returns 410 GONE (`SyncTokenExpiredError`), the connector logs `connector.events_list.expired` and falls back to a `events.list(timeMin, timeMax)` window walk (`singleEvents=false` + `showDeleted=true` pinned) before resuming sync-token mode. Override events (`recurringEventId` + `originalStartTime` set) are emitted as **separate `SourceObserved` records sharing `source_type="google_calendar"`** with a body back-pointer `Override of: <master_id> (originalStart: <iso>)` — symmetric in spirit with how the MS365 calendar mapper handles master events ([Phase 14 plan §G4 / OQ3](phase-14-plan.md#1-確定済み事項)).

Summaries follow `f"{start_iso} - {end_iso} ({attendees_count} attendees)"`; attendee email list / description / location are appended to the body; RRULE is kept as a field. Instance expansion (master + RRULE → individual instances) is a Phase 15+ projection-layer candidate (ms365 + google simultaneously).

### Phase 14 specifics

- **DB head unchanged** = `0019_create_sources_fts` (Phase 10). Phase 14 ships **no migrations**.
- **No new extras** — both connectors reuse `connectors-google-workspace` (httpx).
- **No CLI breaking changes**. `opshub connector sync google_mail` and `opshub connector sync google_calendar` are additive. The auth CLI is unchanged: `opshub connector auth set google_workspace` now provisions the refresh token for all three connectors at once.
- **Mapper symmetry is mechanically verified** by `tests/unit/connectors/test_mapper_symmetry.py` (6 cases for Gmail ↔ Outlook + 4 cases for google_calendar ↔ ms365_calendar). If you fork the mapper for vendor-specific tweaks, run this pin test to confirm the divergence is intentional.
- **External write-back is still forbidden** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7). Gmail `send` API and Calendar `events.insert` / `events.patch` / `events.delete` are deliberately not implemented. Push notifications (`users.watch` for Gmail / Calendar `events.watch`) are also forbidden — both connectors poll only ([ADR-0010](adr/0010-connector-contract.md) §Phase 14 改訂 (i)).
- **No new ADRs** — Phase 14 continues the single-revision trajectory (Phase 11 = 1 new + 2 revisions → Phase 12 = 0 new + 3 revisions → Phase 13 = 0 new + 3 revisions → **Phase 14 = 0 new + 2 revisions**).

### New source_types

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
opshub connector auth set google_workspace

# 3. Next sync picks up the rotated refresh token automatically.
opshub connector sync google_workspace
```

The keyring slot string (`connector:google_workspace:refresh_token`) and the env-var override name (`OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN`) are **unchanged** — Phase 14 G2 only moves the Python module path and widens the scope set. Operators using the env-var override should re-paste their refresh token after re-running the consent flow (the previous token will no longer satisfy `gmail.readonly` / `calendar.readonly` requests once Gmail / Calendar connectors land in G3 / G4).

### What does **not** change in G2

- DB head, schema, migrations: **unchanged**.
- `[connectors-google-workspace]` extras: **unchanged** (Phase 14 plan §Alternatives §9 — Gmail / Calendar reuse the same extras instead of introducing `[connectors-google-mail]` / `[connectors-google-calendar]`).
- `opshub connector sync google_workspace` (Drive) keeps working bit-for-bit — the connector now imports its auth helper from the shared module, but the contract (cursor, mapper, settings) is invariant.
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
