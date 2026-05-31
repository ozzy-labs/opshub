# Changelog

All notable changes to OpsHub will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.5](https://github.com/ozzy-labs/opshub/compare/v0.2.4...v0.2.5) (2026-05-31)


### Added

* **connectors/google_workspace:** oauth (ms365 pattern) + drive api metadata + rate-limit retry ([#283](https://github.com/ozzy-labs/opshub/issues/283)) ([45d0acf](https://github.com/ozzy-labs/opshub/commit/45d0acfcf33c6cd56adae83415cfabc35be0c084))
* **connectors/google_workspace:** workspace export body + provenance + content_extraction wiring ([#284](https://github.com/ozzy-labs/opshub/issues/284)) ([ec63da8](https://github.com/ozzy-labs/opshub/commit/ec63da89fe1ee966a950431b55ebaea8284edd09))
* **core,domain:** workspace export path + google workspace source_type literals ([#282](https://github.com/ozzy-labs/opshub/issues/282)) ([23dce1c](https://github.com/ozzy-labs/opshub/commit/23dce1c9b14e3b2bd62c3661bd47f1bcdbd37a09))


### Fixed

* **connectors/google_workspace:** TTL fallback full-pass + WARNING log (ADR-0010 §改訂 (g) 整合) ([#290](https://github.com/ozzy-labs/opshub/issues/290)) ([26b150e](https://github.com/ozzy-labs/opshub/commit/26b150ef317476d225051f8590615611c9752b6c))


### Documentation

* **adr,plan:** adr-0010 + 0014 + 0025 revisions + phase-13-plan (Google Workspace) ([#280](https://github.com/ozzy-labs/opshub/issues/280)) ([ea32e5d](https://github.com/ozzy-labs/opshub/commit/ea32e5d5e2354c38a6b86cbeb6716fe161560c6f))
* phase 13 audit cluster B (docs drift + phase 11 residual + phase 13+ → 14+ unification) ([#291](https://github.com/ozzy-labs/opshub/issues/291)) ([2192829](https://github.com/ozzy-labs/opshub/commit/219282933124e25ca4f016a4ba31aba561f48924))
* phase 13 closeout (docs + e2e + guard + phase-12 forecast realign) ([#285](https://github.com/ozzy-labs/opshub/issues/285)) ([026f67c](https://github.com/ozzy-labs/opshub/commit/026f67c43dd845726ff20e2782fb5cda325496fa))

## [0.2.4](https://github.com/ozzy-labs/opshub/compare/v0.2.3...v0.2.4) (2026-05-31)


### Added

* **config,mcp:** wire office settings + register teams/onedrive_drive in mcp.connector.sync ([#252](https://github.com/ozzy-labs/opshub/issues/252)) ([3ca4edf](https://github.com/ozzy-labs/opshub/commit/3ca4edf331d687f96e653a300c3885d2f1ff5d5a))
* **connectors/box_drive:** office content extraction hook ([#247](https://github.com/ozzy-labs/opshub/issues/247)) ([0d15bb7](https://github.com/ozzy-labs/opshub/commit/0d15bb7ef3f51f530c14dcddf5dd082310ca0142))
* **connectors/ms365:** outlook body deep retention ([#244](https://github.com/ozzy-labs/opshub/issues/244)) ([0da7271](https://github.com/ozzy-labs/opshub/commit/0da7271a53ca3c18d6b1e467895573b95f922ac9))
* **connectors/onedrive_drive:** new local-fs connector ([#248](https://github.com/ozzy-labs/opshub/issues/248)) ([41e113c](https://github.com/ozzy-labs/opshub/commit/41e113ce63e90c3d143a52522443218bc0a0b5de))
* **connectors/teams:** graph chat delta + user token ([#243](https://github.com/ozzy-labs/opshub/issues/243)) ([cf1739d](https://github.com/ozzy-labs/opshub/commit/cf1739d5ab9b06ce382f19e950a22a8cc0cd57bc))
* **core:** office document extraction foundation ([#245](https://github.com/ozzy-labs/opshub/issues/245)) ([2845e8b](https://github.com/ozzy-labs/opshub/commit/2845e8bf5ea6585d01467fd68395f810e63f1018))
* **mcp,skills:** phase 12 h1 foundation (adr revisions + 4 new mcp tools + existing 5 rename) ([#262](https://github.com/ozzy-labs/opshub/issues/262)) ([178c4dc](https://github.com/ozzy-labs/opshub/commit/178c4dc83808f80c1cd2406b1e57ee6cdacfb5eb))
* **mcp:** widen tool surface (brief + graph + source + propose.generate) ([#231](https://github.com/ozzy-labs/opshub/issues/231)) ([451214c](https://github.com/ozzy-labs/opshub/commit/451214c0b9909d1cc3fe4e7bff4f7e4e59aa6fb0))
* **skills,mcp:** h4 hitl write (inbox-triage + source-extract + meeting-followup) ([#266](https://github.com/ozzy-labs/opshub/issues/266)) ([19ade66](https://github.com/ozzy-labs/opshub/commit/19ade66a42c5836ade8d5ac83846bc77c4ff0c98))
* **skills:** h2 info gathering (meeting-prep + research) ([#265](https://github.com/ozzy-labs/opshub/issues/265)) ([94fdee1](https://github.com/ozzy-labs/opshub/commit/94fdee17faca840e12a9b0896b523672248187e9))
* **skills:** h5 draft (handoff-draft + announcement-draft) ([#263](https://github.com/ozzy-labs/opshub/issues/263)) ([5fe43f4](https://github.com/ozzy-labs/opshub/commit/5fe43f409c23c4b515f1a7e411a7dcca705426b6))


### Fixed

* **ci:** post-Wave-2 CI hotfix (ruff format + mypy strict) ([#246](https://github.com/ozzy-labs/opshub/issues/246)) ([1cab7fd](https://github.com/ozzy-labs/opshub/commit/1cab7fdb45dd5fa0d97a4fd6b211df9dbd42fa79))
* **docs,skills:** phase 12 audit cluster A (catalog drift) ([#270](https://github.com/ozzy-labs/opshub/issues/270)) ([31dbfde](https://github.com/ozzy-labs/opshub/commit/31dbfdee2bf2ef224ff0465fea2a80fdc118dc01))
* **tests:** remove unused type: ignore in test_skill_specs ([#268](https://github.com/ozzy-labs/opshub/issues/268)) ([96af9be](https://github.com/ozzy-labs/opshub/commit/96af9bebff68efd3625ea8a5162a5cea63faf660))


### Documentation

* **adr:** adr-0025 office doc extraction / adr-0019 + 0010 改訂 (phase 11) ([#242](https://github.com/ozzy-labs/opshub/issues/242)) ([942b485](https://github.com/ozzy-labs/opshub/commit/942b485c2bfa20b07e74a3709c27c42b86ef5af2))
* **audit:** phase 11 audit cluster C (excludes flat + claudemd status + keyring slot + e2e gap) ([#251](https://github.com/ozzy-labs/opshub/issues/251)) ([c6f687c](https://github.com/ozzy-labs/opshub/commit/c6f687cd4ad5006ebc4d9bd817d1d50ad838bf80))
* phase 11 audit cluster A (skill drift fix) ([#250](https://github.com/ozzy-labs/opshub/issues/250)) ([bd16101](https://github.com/ozzy-labs/opshub/commit/bd16101a41b94def906b1380fcbd9c63ba17427b))
* phase 11 closeout (docs + e2e + guard) ([#249](https://github.com/ozzy-labs/opshub/issues/249)) ([9e1c8ea](https://github.com/ozzy-labs/opshub/commit/9e1c8eabbf6f2da90e455e8a1bba6b39c7a475fd))
* phase 12 audit cluster C (cross-cutting drift) ([#271](https://github.com/ozzy-labs/opshub/issues/271)) ([e2b1e8e](https://github.com/ozzy-labs/opshub/commit/e2b1e8e8f3f3dffbf75a2c82f1d3bd0e77bb44ff))
* phase 12 closeout + e2e lifecycle test ([#269](https://github.com/ozzy-labs/opshub/issues/269)) ([694fe74](https://github.com/ozzy-labs/opshub/commit/694fe74823ff5520fc93018f3d7cc20d2897af0f))
* **plan:** add Phase 11 implementation plan (MS Office 深掘り) ([#240](https://github.com/ozzy-labs/opshub/issues/240)) ([fc850fd](https://github.com/ozzy-labs/opshub/commit/fc850fd04b5732e8183014521eeb28515032397c))
* **plan:** add Phase 12 implementation plan (Secretary Skills 拡張) ([#260](https://github.com/ozzy-labs/opshub/issues/260)) ([7a4ebbd](https://github.com/ozzy-labs/opshub/commit/7a4ebbd6f1e8fcdec3b857b254fca473340930ae))
* **plan:** fix source_type=calendar_event → ms365_calendar in plan ([#273](https://github.com/ozzy-labs/opshub/issues/273)) ([65ec0ad](https://github.com/ozzy-labs/opshub/commit/65ec0ad0da50c86f4d408b09685c0910b8c927cc))
* **plan:** phase 11 pre-implementation audit updates ([#241](https://github.com/ozzy-labs/opshub/issues/241)) ([f9455b2](https://github.com/ozzy-labs/opshub/commit/f9455b2f8e0868af693a3f65681cbdbe5efd80fc))
* **plan:** phase 12 pre-implementation audit corrections ([#261](https://github.com/ozzy-labs/opshub/issues/261)) ([3c1db76](https://github.com/ozzy-labs/opshub/commit/3c1db765def7526b4c9031ca2e5d64b26ce11068))

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
