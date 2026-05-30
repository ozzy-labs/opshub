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
slack:
  channels: ["C0LEAKS", "C09SECRET"]
  senders: ["security-bot"]
github:
  repos: ["org/private-incident-log"]
ms365:
  senders: ["legal@example.com"]
box:
  paths: ["/Confidential/**"]
```

The file is optional. With no `excludes.yaml`, every connector keeps its default fetch behaviour.

### Phase 10 specifics

- DB head = `0019_create_sources_fts` (Phase 10).
- New optional extras: `encryption` (SQLCipher) / `mcp` (MCP server SDK).
- New CLI subcommands: `opshub search` (FTS5 cross-connector full-text search), `opshub mcp serve` (stdio MCP server), `opshub mcp tools` (tool registry inspection).
- `opshub propose generate --reply-to <source-id>` enters reply-draft mode (ADR-0016 §決定 (i)). `propose apply` saves the draft locally — there is no write-back to SaaS (ADR-0010 §禁止事項 7).
