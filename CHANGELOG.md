# Changelog

All notable changes to OpsHub will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0](https://github.com/ozzy-labs/opshub/compare/v0.1.1...v0.2.0) (2026-05-18)


### ⚠ BREAKING CHANGES

* **connectors/slack:** make User Token the first-class principal ([#167](https://github.com/ozzy-labs/opshub/issues/167))

### Added

* **cli:** add generic `opshub connector auth test <name>` subcommand ([#176](https://github.com/ozzy-labs/opshub/issues/176)) ([d4eb986](https://github.com/ozzy-labs/opshub/commit/d4eb9866f7792d296dd788e47e17edfe52bfe1ef))
* **connectors/slack:** include scope-extension hint in missing_scope error message ([#172](https://github.com/ozzy-labs/opshub/issues/172)) ([7338ae3](https://github.com/ozzy-labs/opshub/commit/7338ae3baa61e2b861abe7e72ed17c3cbb7ad29c)), closes [#169](https://github.com/ozzy-labs/opshub/issues/169)
* **connectors/slack:** make User Token the first-class principal ([#167](https://github.com/ozzy-labs/opshub/issues/167)) ([10a01ac](https://github.com/ozzy-labs/opshub/commit/10a01acac55014d279584dd9dce742b9792bcfd8))


### Changed

* **connectors/slack:** make principal detection defensive against bot_id falsy values ([#171](https://github.com/ozzy-labs/opshub/issues/171)) ([9dfe8bb](https://github.com/ozzy-labs/opshub/commit/9dfe8bb339591453fb0e07a9abac55427c98dc4d)), closes [#168](https://github.com/ozzy-labs/opshub/issues/168)
* **connectors:** align auth-test polish (httpx DI, dispatch table, config pointers) ([#177](https://github.com/ozzy-labs/opshub/issues/177)) ([aa05fc6](https://github.com/ozzy-labs/opshub/commit/aa05fc674689d4c18a515ae0fdc947b1e2b3809f))

## [0.1.1](https://github.com/ozzy-labs/opshub/compare/v0.1.0...v0.1.1) (2026-05-17)


### Documentation

* **readme:** split into README.md (en) + README.ja.md (ja) with cross-links ([#161](https://github.com/ozzy-labs/opshub/issues/161)) ([30cd55c](https://github.com/ozzy-labs/opshub/commit/30cd55c4016253bd5d8513a6af819f00f71f0e63))

## [0.1.0] - 2026-05-17

Initial public release. OpsHub is a local-first operational memory + execution
hub for humans and AI agents.

**Distribution**: v0.1.0 ships on PyPI under the distribution name
**`ozzylabs-opshub`** (PEP 423 `<owner>-<package>` form because PyPI has no
namespace concept and the bare `opshub` name was unavailable). The CLI
command remains `opshub`. Install via `uv tool install ozzylabs-opshub` or
directly from a tag at `git+https://github.com/ozzy-labs/opshub.git@v0.1.0`.
See [ADR-0001 §Updates](docs/adr/0001-python-stack.md#updates) for naming
rationale.

### Added

#### Phase 1 — Foundation (event store + CLI + markdown)

- Event-sourced architecture (ADR-0002): all state changes via `events` table,
  projections rebuildable
- Core projections: `tasks` / `events`
- CLI: `opshub init` / `opshub task add|list|update|complete|delete` /
  `opshub db migrate` / `opshub projections rebuild`
- Markdown generation for workspace surface (ADR-0003)
- Pluggable Embedder + VectorStore Protocols frozen for Phase 4 (ADR-0012)

#### Phase 2 — Coordination

- Projections: `inbox_items` / `decisions` / `work_sessions` / `agent_runs` /
  `locks` / `handoffs`
- CLI: `opshub inbox` / `opshub decision` / `opshub session` /
  `opshub agent run` / `opshub handoff` / `opshub lock`
- Lock granularity (ADR-0013): `task:<id>` / `project:<id>` / `global:` 3-tier
  scope

#### Phase 3 — Connector framework + Workspace ingest

- Connector framework (ADR-0010 contract): `Connector` Protocol +
  `connector_cursors` projection
- GitHub connector (issues + PRs + notifications)
- SaaS token storage (ADR-0014): `core/secrets` + keyring + env var override
- CLI: `opshub connector` (auth set / sync / list) + `opshub workspace`
  (ingest / generate)
- Projections: `sources` / `connector_cursors` / `ingested_files`

#### Phase 4 — Semantic recall

- Pluggable Embedder backends: local (bge-m3) / OpenAI / Voyage
- sqlite-vec VectorStore (3 backend-specific vec0 tables via migration 0013)
- `EmbeddingService` (CLI-driven rebuild) + `RecallService` + `DuplicateService`
- CLI: `opshub embeddings` (rebuild / status / find-duplicates) +
  `opshub recall`
- `VectorStore.recall_by_rowid` Protocol extension (follow-up: cost-effective
  duplicate detection)

#### Phase 5 — Briefing

- LLM usage strategy (ADR-0015): Pluggable LLM Protocol + default `disabled` +
  paste-code OAuth
- Pluggable LLM backends: Anthropic (claude-haiku-4-5) + OpenAI (gpt-4o-mini)
- `BriefingService` + `briefings` projection (migration 0014)
- Prompt injection mitigation: `<source>` delimiter wrap + `html.escape`
  (follow-up)
- CLI: `opshub brief` + `opshub embeddings drain`
- Event-driven auto-embed (opt-in `[embedding] auto = true`)

#### Phase 6 — Action loop

- Action loop / structured output (ADR-0016): tool_use / function calling per
  backend
- `LLMClient.complete_structured` Protocol extension
- Pluggable LLM backend: Ollama (local; closes ADR-0015 Local LLM deferred)
- `ProposalService` + `proposals` projection (migration 0015)
- CLI: `opshub propose` (generate / list / apply / reject)
- Human-in-the-loop enforced: no auto-apply mode (ADR-0004 alignment)

#### Phase 7 — Connectors Wave 2

- Slack connector (channels messages + permalink)
- Microsoft 365 connector (Calendar + OneDrive + Outlook via OAuth paste-code)
- Box connector (events API via OAuth paste-code)
- `connector:slack` CLI alias for backward-compat with `slack` (follow-up)

#### Phase 8 — Knowledge graph

- Knowledge graph (ADR-0017): `links` projection (migration 0016) + 4
  auto-extraction paths
- Manual link CRUD: `LinkCreated` / `LinkDeleted` events
- `LinkService` (related / trace / expand) with cycle detection + depth limits
- CLI: `opshub link` (add / remove / list) + `opshub graph`
  (related / trace / expand)
- `--expand-graph` integration on `opshub brief` and `opshub propose generate`

### Security

- API tokens stored via OS keyring (ADR-0014), env var override (`OPSHUB_*`)
- All event error_messages sanitised via
  `core.sanitise.sanitise_error_message` (sk-... / ghp_... / Bearer ...
  redacted)
- LLM prompt injection mitigation: external content delimiter-wrapped +
  HTML-escaped before LLM submission
- ADR-0005 External Content Min: source body never persisted (summary ≤ 200
  chars cap, enforced per connector)

### Performance

- Cold-start budget: `opshub --help` ≤ 300ms (measured ~140ms)
- Tests: 1533 passing + 9 skipped (extras-gated for optional ML / connector
  SDKs)

### Fixed

- **`opshub init` blocker on default install** — Phase 4 migration 0013
  unconditionally creates `embeddings_vec_*` virtual tables via `sqlite-vec`.
  Previously gated by the `[vector]` extras, so `uv tool install opshub` (the
  documented Quickstart) hit `OperationalError: no such module: vec0` and left
  the DB half-applied. Promoted `sqlite-vec` to base dependency (~500 KB wheel,
  within ADR-0001 distribution budget). `[vector]` extras remains as a
  `numpy`-only alias for backward compat — existing `uv sync --extra vector`
  invocations continue to work. See ADR-0001 §Updates for the rationale.

### Architecture

- 17 ADRs accepted (0000-0017). See `docs/adr/`.
- Event-sourced single source of truth (ADR-0002). All projections derivable
  from event log via `opshub projections rebuild`.
- Single Python package (ADR-0007). ML / LLM / connector SDKs in extras
  (ADR-0001 distribution constraint).
- Multi-Agent Neutrality (ADR-0009): Pluggable Protocols for Embedder /
  VectorStore / LLMClient + 3 backends each.

[0.1.0]: https://github.com/ozzy-labs/opshub/releases/tag/v0.1.0
