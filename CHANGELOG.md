# Changelog

All notable changes to OpsHub will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.3](https://github.com/ozzy-labs/opshub/compare/v0.2.2...v0.2.3) (2026-05-30)


### Added

* **connectors:** integrate excludes in ms365/box + body/provenance test coverage ([#226](https://github.com/ozzy-labs/opshub/issues/226)) ([a6e09ae](https://github.com/ozzy-labs/opshub/commit/a6e09aee2502d1bb3ee6b422cca7e44182f447b9))
* **mcp:** error-path redact + token shape expansion + pagination + test hardening ([#223](https://github.com/ozzy-labs/opshub/issues/223)) ([838501b](https://github.com/ozzy-labs/opshub/commit/838501bdba770ecd8ad65b62efe2302f6695bdda))
* **mcp:** stdio mcp server + tool schema ([#215](https://github.com/ozzy-labs/opshub/issues/215)) ([afc4288](https://github.com/ozzy-labs/opshub/commit/afc428876522e03985eb1dadd29b1158c0e541a3))
* **propose:** add reply-draft candidate kind ([#217](https://github.com/ozzy-labs/opshub/issues/217)) ([585faee](https://github.com/ozzy-labs/opshub/commit/585faee606061413eec5c467c6f5b81191ab734e))
* **search:** body-based embedding + sqlite fts5 + search command ([#214](https://github.com/ozzy-labs/opshub/issues/214)) ([0f2d624](https://github.com/ozzy-labs/opshub/commit/0f2d624e1f4fa57b6f22320a966a1f67197d2d28))
* **skills:** add secretary agent skill specs and security scan ([#219](https://github.com/ozzy-labs/opshub/issues/219)) ([dbef400](https://github.com/ozzy-labs/opshub/commit/dbef4007c812bfdf1ff08df8cbb30fc8d67589dc))
* store full content locally with at-rest encryption and excludes ([#211](https://github.com/ozzy-labs/opshub/issues/211)) ([47cf539](https://github.com/ozzy-labs/opshub/commit/47cf53965b1e5996462e047d9293e292325d811b))


### Fixed

* **ci:** mypy strict (types stubs + decorator ignores) after phase 10 audit ([#227](https://github.com/ozzy-labs/opshub/issues/227)) ([a8460c1](https://github.com/ozzy-labs/opshub/commit/a8460c1043c37df5496d19ce8a40c0bc8a936a80))
* **connectors/github:** defer httpx import so slack sync works without github extra ([#200](https://github.com/ozzy-labs/opshub/issues/200)) ([c00c0c7](https://github.com/ozzy-labs/opshub/commit/c00c0c7940ab82085a488bb5304bd4f6ddb1f9fb)), closes [#198](https://github.com/ozzy-labs/opshub/issues/198)
* **skills,adr:** align skill MCP calls with schema + clarify triage as generate-time hint ([#228](https://github.com/ozzy-labs/opshub/issues/228)) ([0a5bb38](https://github.com/ozzy-labs/opshub/commit/0a5bb386399aba42fdc41f982127a6b2efd41540))
* **tests:** rebuild secret-shaped test fixtures from parts to silence scanners ([#230](https://github.com/ozzy-labs/opshub/issues/230)) ([b40cd29](https://github.com/ozzy-labs/opshub/commit/b40cd2907038a9b4b05177f68a622f66a47dfeca))


### Documentation

* **adr:** adr-0012 revise embed target to body (phase 10) ([#212](https://github.com/ozzy-labs/opshub/issues/212)) ([d6d2528](https://github.com/ozzy-labs/opshub/commit/d6d252889b859a30ec33a3afc906f0dd20d021d4))
* **adr:** adr-0020 full local content retention / adr-0021 encryption at rest ([#210](https://github.com/ozzy-labs/opshub/issues/210)) ([18d6a8e](https://github.com/ozzy-labs/opshub/commit/18d6a8ed8df47f480a74277b2e3145410ba72a52))
* **adr:** adr-0022 mcp server surface ([#213](https://github.com/ozzy-labs/opshub/issues/213)) ([e26d2b0](https://github.com/ozzy-labs/opshub/commit/e26d2b03ebb915d3ce51d435425b07ac7a153722))
* **adr:** revise adr-0004 for phase 10 form-a absorption ([#218](https://github.com/ozzy-labs/opshub/issues/218)) ([5edfca5](https://github.com/ozzy-labs/opshub/commit/5edfca5121602075b647e3a685b36d5de0371eab))
* **adr:** revise adr-0016/0017/0010 for phase 10 reply-draft ([#216](https://github.com/ozzy-labs/opshub/issues/216)) ([8f05a6f](https://github.com/ozzy-labs/opshub/commit/8f05a6f4d0a1b62293f6c369b5ae487f07f31b09))
* align skill / README / secretary docs with actual CLI surface ([#222](https://github.com/ozzy-labs/opshub/issues/222)) ([874acf2](https://github.com/ozzy-labs/opshub/commit/874acf232be3c0f3e21401aa77899605486adc5c))
* phase 10 audit alignment (adr + principles + architecture + extras cleanup) ([#225](https://github.com/ozzy-labs/opshub/issues/225)) ([1086791](https://github.com/ozzy-labs/opshub/commit/1086791a38f0749eba2fd9e8a2c3b844ef32e36a))
* **plan:** add Phase 10 secretary-agent platform plan ([#202](https://github.com/ozzy-labs/opshub/issues/202)) ([c64d2d4](https://github.com/ozzy-labs/opshub/commit/c64d2d49a666c62ee78696ef908d55417135e038))

## [0.2.2](https://github.com/ozzy-labs/opshub/compare/v0.2.1...v0.2.2) (2026-05-22)


### Added

* **cli:** box_drive sync + phase 9 closeout ([#194](https://github.com/ozzy-labs/opshub/issues/194)) ([3d97643](https://github.com/ozzy-labs/opshub/commit/3d976431e27a14da7a4d3811044e396d2ddf6f77))
* **connectors/box_drive:** mapper + connector + settings ([#193](https://github.com/ozzy-labs/opshub/issues/193)) ([90d3f49](https://github.com/ozzy-labs/opshub/commit/90d3f49d680a2bd6d16aef5ad749c1f084b344a2))
* **core:** platform detection + box_drive scanner ([#191](https://github.com/ozzy-labs/opshub/issues/191)) ([ca4e1da](https://github.com/ozzy-labs/opshub/commit/ca4e1dadb722077712634feec57a6c4437e50103))
* **sources:** add fingerprint column + migration 0017 ([#192](https://github.com/ozzy-labs/opshub/issues/192)) ([97929ed](https://github.com/ozzy-labs/opshub/commit/97929ed090fc8b0e6a4bd5e880b6fd677af26010))


### Documentation

* **adr:** adr-0019 local-filesystem-backed connector ([#190](https://github.com/ozzy-labs/opshub/issues/190)) ([8ff9c1a](https://github.com/ozzy-labs/opshub/commit/8ff9c1a416e686971f51709830c7b0429394e534))
* **plan:** align phase 9 plan A1 row to actual adr-0019 ([#195](https://github.com/ozzy-labs/opshub/issues/195)) ([da353ef](https://github.com/ozzy-labs/opshub/commit/da353efc5e04e21db169cf0f260a005164167e8c))
* **plan:** phase 9 plan ([#188](https://github.com/ozzy-labs/opshub/issues/188)) ([613a2f3](https://github.com/ozzy-labs/opshub/commit/613a2f3dcfcbb3d41ea4fdd1f65768230b1f4e0f))

## [0.2.1](https://github.com/ozzy-labs/opshub/compare/v0.2.0...v0.2.1) (2026-05-19)


### Fixed

* **release-please:** refresh uv.lock and auto-sync on future release PRs ([#181](https://github.com/ozzy-labs/opshub/issues/181)) ([e2d7544](https://github.com/ozzy-labs/opshub/commit/e2d7544b4d744603093291662fa2727f6f2ee1a2))
* **tests:** assert __version__ matches SemVer shape instead of hardcoded value ([#182](https://github.com/ozzy-labs/opshub/issues/182)) ([6d0e696](https://github.com/ozzy-labs/opshub/commit/6d0e69667b5c834383c5ff48b8c3a717afdacb1a))


### Documentation

* **release-runbook:** correct primary workflow name for PyPI Trusted Publisher ([#179](https://github.com/ozzy-labs/opshub/issues/179)) ([c1cfc3e](https://github.com/ozzy-labs/opshub/commit/c1cfc3ef9826a61c53b187067d130792af8634d6))

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
