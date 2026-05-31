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
- External write-back is **still forbidden** (ADR-0010 §禁止事項 7). The secretary drafts only; the operator sends.
