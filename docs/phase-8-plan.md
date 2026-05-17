# Phase 8 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-17. Scope: Knowledge graph layer = `links` projection 本実装 + event-driven link extraction (Phase 3 `SourceReferenced` 消費 + Phase 5 `BriefingGenerated.source_refs` materialise + Phase 6 `ProposalApplied`/`ProposalRequested.briefing_id` materialise + manual `opshub link add`) + `LinkService` traversal + `opshub graph` / `opshub link` CLI + briefing/propose の `--expand-graph` integration。Multi-machine sync (principles.md §Open Q #5) / `inbox_item`/`source` candidate types (Phase 6.x) / `llama.cpp` direct binding / Additional connectors は Phase 8.x / 9 で別途。

Phase 8 の目的は **Knowledge graph layer** を Phase 1-7 の foundation 上に追加すること。Phase 1-7 で複数 entity (`tasks` / `decisions` / `inbox_items` / `sources` / `briefings` / `proposals`) が存在するが、entity 間の link は現在 event log の JOIN でしか辿れない。Phase 8 で `links` projection を導入し、4 種類の自動 link 抽出経路 (`ProposalApplied` / `BriefingGenerated.source_refs` / `ProposalRequested.briefing_id` / `SourceReferenced`) と manual `opshub link add` で graph を構築する。

これにより agent が "この task はどの proposal から作られたか" / "この decision を参照している source は" / "この briefing が使った source は" のような provenance queries を即答可能になる。さらに `opshub brief "<topic>" --expand-graph` / `opshub propose generate "<topic>" --expand-graph` で RecallService の hit を graph 1-hop 拡張して LLM context を広げ、Phase 5/6 の機能を新 LLM 機能なしで品質向上させる。

Phase 3 で placeholder のままだった `SourceReferenced` event を本 phase で第一級に格上げし、connector 経由で取り込まれた source 内の `#task-id` 参照などを link として materialize する経路を確立する (connector-side の自動抽出は Phase 8.x、本 phase は manual + Phase 3 既存の placeholder 経路の closeout)。

Multi-machine sync (principles.md §Open Q #5) は Phase 9 に持ち越し。本 phase は単一 machine 内の graph 構築に集中。

## 1. 着手前に解消する TODO

Phase 7 完了時点で Phase 8 着手前に解消が必要な事項は **なし**。Phase 1-7 で確立した実装契約 (uow_factory / `EventStore.append(event, conn)` / `Projector.apply(event, conn)` / `projections/registry.all_projections()` SSOT / `AllEvent` discriminated union / `cli/* import` whitelist / atomic failing-projector test / `core/secrets` + ADR-0014 token storage / Pluggable backend Protocol freeze + factory pattern / `core/sanitise.sanitise_error_message` / `<source>` delimiter wrap + html.escape / Connector framework + 4 connectors) は Phase 8 も全て継承する。

**確定済み事項** (Phase 8 着手前に確定):

1. **Scope の絞り込み** — Phase 8 MVP = ADR-0017 (Knowledge graph) + `links` projection + 4 種類の自動 link 抽出 + manual `link add/remove/list` CLI + `graph related/trace/expand` CLI + `brief`/`propose` の `--expand-graph` integration + closeout。Multi-machine sync / connector-side automatic SourceReferenced extraction / `inbox_item`/`source` proposal candidates / `llama.cpp` direct binding / Additional connectors は Phase 8.x / 9 で別 plan
2. **`links` projection 構造** — 単一 table `(id PK, from_entity_type, from_entity_id, to_entity_type, to_entity_id, link_type, created_at, source_event_id?, metadata JSON?)`。`(from_entity_type, from_entity_id, to_entity_type, to_entity_id, link_type)` を natural key として UPSERT (`INSERT OR REPLACE`) で重複防止。両方向 traversal 用に `(from_*)` と `(to_*)` の 2 つの INDEX を追加
3. **`link_type` 初期 enum** (Phase 8 MVP):
   - `applied_to` — `ProposalApplied` 経由 (`proposal:<id>` → `task:<id>` or `decision:<id>`)
   - `referenced_in_briefing` — `BriefingGenerated.source_refs` 経由 (`briefing:<id>` → `<entity_type>:<id>` per source_ref)
   - `generated_from_briefing` — `ProposalRequested.briefing_id` 経由 (`proposal:<id>` → `briefing:<id>`)
   - `references` — `SourceReferenced` event 経由 (`source:<id>` → `<referenced_entity_type>:<id>`)
   - `manual` — `opshub link add` 経由 (任意 → 任意)
4. **自動抽出 vs 手動 link の event-sourced 契約** — 自動抽出 (`LinksExtractor` projector) は新 event を発行せず、既存 event を読んで `links` 行を生成する純粋 projection (ADR-0002 derived state pattern)。Manual `opshub link add` / `opshub link remove` は新 event `LinkCreated` / `LinkDeleted` を発行 (event-sourced 単一経路、ADR-0002)
5. **`Phase8Event` discriminated union** — `LinkCreated` / `LinkDeleted` の 2 event。`AllEvent` を `... | Phase7Event | Phase8Event` (Phase 7 で `Phase7Event` 追加なら) or `... | Phase6Event | Phase8Event` (Phase 7 が新 event 追加していなければ) に拡張。Phase 7 の AllEvent 拡張状況を本 plan 確定後に再確認 — Phase 7 は projection 拡張のみで新 event 追加なしの場合は `Phase8Event` は Phase 6 の次
6. **Graph traversal の depth 上限** — `LinkService.expand(entity, depth)` の `depth` は default 2、max 5 (cycle detection + 性能保護)。`trace(entity)` (backward direction priority) の depth は default 3、max 10
7. **`SourceReferenced` の Phase 3 placeholder closeout** — Phase 3 で `SourceReferenced(source_id, referenced_entity_type, referenced_entity_id, referenced_by?)` event を定義済 (要確認、未定義なら Phase 8 で定義) だが projector 未消費。Phase 8 B2 で `LinksExtractor` が消費し `links` row を生成。Connector-side の自動 `SourceReferenced` 発行 (GitHub Issue body の `#task-id` parse 等) は Phase 8.x で別 PR
8. **`--expand-graph` flag の挙動** (Phase 8 D2):
   - `opshub brief "<topic>" --expand-graph` — RecallService で得た hit 各々について `LinkService.related(entity, link_types=["referenced_in_briefing", "references", "applied_to"], max_per_entity=3)` を呼び、link 先 entity を additional source として LLM prompt に含める。重複は `(entity_type, entity_id)` で dedupe
   - `opshub propose generate "<topic>" --expand-graph` — 同じく
   - `--expand-graph` 未指定時は既存挙動を変えない (backward-compat)
9. **CLI 名前空間設計**:
   - `opshub link <subcommand>` — link CRUD (`add` / `remove` / `list`)
   - `opshub graph <subcommand>` — graph queries (`related` / `trace` / `expand`)
   - 別 namespace に分ける理由: CRUD と query で typer App を分けると help 表示が読みやすい
10. **Bidirectional traversal** — `LinkService.related(entity)` は default で「from = entity OR to = entity」両方を返す。一方向のみ欲しい場合は `direction=Literal["outgoing", "incoming", "both"]` で絞れる。`trace` は backward (incoming) 中心で provenance を辿る、`expand` は両方向

## 1.1 Prep PR (Phase 1-7) で確立した実装契約 (Phase 8 全 PR が継承)

- 新規 service は `uow_factory: Callable[[], ContextManager[Connection]] | None = None` を constructor で受け、event append + projection apply を 1 transaction にまとめる (PR #26 契約)
- 新規 projection は `projections/<entity>.py` で Table を `opshub.db.schema.metadata` に登録 + `projections/registry.all_projections()` に追記
- 新規 event family は `Phase8Event` discriminated union を作り、`AllEvent` を `... | Phase8Event` に拡張 (PR B1 で実施)
- 新規 CLI subcommand module は module-level import を `__future__` / `typer` / `typing` / `pathlib` に限定する (M6 cold-start guard が CI で検出)
- 新規 service は失敗 projector の atomicity test を 1 件追加
- 新規 projection は rebuild の冪等性テストを 1 件追加 — `LinksExtractor` 特に重要 (既存 event を re-apply して同じ links が生成されることを pin)
- `links` projection の row は `(from_entity_type, from_entity_id, to_entity_type, to_entity_id, link_type)` で natural-key UPSERT。同じ link の 2 回目 apply は no-op
- 自動抽出 projector (`LinksExtractor`) は新 event を **発行しない** (純粋 derived state、ADR-0002)。Manual CLI のみ `LinkCreated` / `LinkDeleted` を発行
- Phase 1 / 5 で frozen な Protocol (`Embedder` / `VectorStore` / `LLMClient`) は本 phase で一切変更しない
- `BriefingService` / `ProposalService` の既存 signature は変更しない。`generate(..., expand_graph: bool = False)` を default False で追加 (backward-compat)

## 2. Phase 8 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。

### 2.1 Sub-issue A: Foundation (2 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| A1 | `docs(adr): adr-0017 knowledge graph` | `docs/adr/0017-knowledge-graph.md` 新設。Status: Accepted。決定 7 件: (a) `links` projection schema (table 構造 + natural key + index)、(b) `link_type` 初期 enum (`applied_to` / `referenced_in_briefing` / `generated_from_briefing` / `references` / `manual`)、(c) 自動抽出 projector は新 event を発行しない (pure derived state)、(d) Manual link は `LinkCreated` / `LinkDeleted` event を発行 (event-sourced 単一経路)、(e) Traversal depth 上限 (related=1-hop / trace default 3 max 10 / expand default 2 max 5、cycle detection 必須)、(f) `--expand-graph` flag default off (backward-compat)、(g) Connector-side automatic `SourceReferenced` 発行は Phase 8.x 持ち越し。decisions-log.md entry。`SourceReferenced` event の Phase 3 status を確認し、本 ADR §Context で言及 | A |
| A2 | `feat(projections): links projection + migration 0016` | `src/opshub/projections/links.py` 新設。`links_table` (id PK / from_entity_type / from_entity_id / to_entity_type / to_entity_id / link_type / created_at / source_event_id nullable / metadata JSON nullable) を `metadata` に登録 + `registry.all_projections()` に追記。Migration `0016_create_links_table.py` (revision `0016`, down_revision = 直前 = 0015)。`links_from_idx` (from_*) + `links_to_idx` (to_*) の 2 INDEX を作成。`LinksProjector` skeleton (まだ extraction logic は実装せず、apply 経路は no-op で event を素通り)。冪等性 test + atomic failing-projector test 1 件追加 | A |

### 2.2 Sub-issue B: Event-driven link extraction (2 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| B1 | `feat(domain): link events + sourcereferenced closeout` | `src/opshub/domain/events/link.py` 新設。`LinkCreated(link_id, from_entity_type, from_entity_id, to_entity_type, to_entity_id, link_type, source_event_id?, metadata?)` + `LinkDeleted(link_id, deleted_by, reason?)` の 2 event。`Phase8Event` discriminated union 新規、`AllEvent` を `... | Phase8Event` に拡張。**Phase 3 の `SourceReferenced` placeholder を本 phase で第一級に格上げ**: Phase 3 で定義済なら本 PR で field 確認 + docstring 更新 (`Phase8Event` には入れず Phase 3 event family のまま、ただし projector 消費は本 phase で初実装)、未定義なら本 PR で `SourceReferenced(source_id, referenced_entity_type, referenced_entity_id, referenced_by?)` を Phase 3 event family として遡及的に追加。`LinkDeleted.reason` は `core.sanitise.sanitise_error_message` 経由 (ADR-0015 §決定 (g) 準拠) | B |
| B2 | `feat(projections): LinksExtractor projector` | `src/opshub/projections/links.py` の `LinksProjector` に extraction logic を実装。dispatch 表で 5 種類の event を `links` row に変換: (1) `ProposalApplied` → `(proposal:<id>, applied_entity_type:<id>, applied_to)` / (2) `BriefingGenerated` → `source_refs` 各 entry につき `(briefing:<id>, <entity_type>:<id>, referenced_in_briefing)` / (3) `ProposalRequested(briefing_id)` → briefing_id が non-None なら `(proposal:<id>, briefing:<briefing_id>, generated_from_briefing)` / (4) `SourceReferenced` → `(source:<id>, <referenced_entity_type>:<id>, references)` / (5) `LinkCreated` → 直接 link 行を INSERT / (6) `LinkDeleted` → 該当 link 行を DELETE。UPSERT semantics (natural-key 重複は no-op)。`tests/unit/projections/test_links_extractor.py` で 6 経路 × idempotent test。`projections rebuild` 経由で historical event から links が正しく再構築されることも pin | B |

### 2.3 Sub-issue C: LinkService traversal (2 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| C1 | `feat(services): LinkService related + trace` | `src/opshub/services/links/__init__.py` + `src/opshub/services/links/service.py` 新設。`LinkService(engine)`。`related(entity_type, entity_id, *, direction: Literal["outgoing","incoming","both"]="both", link_types: list[str] | None = None, limit: int = 100) -> list[Link]`: 1-hop。`trace(entity_type, entity_id, *, depth: int = 3, max_depth: int = 10, link_types: list[str] | None = None) -> list[LinkPath]`: backward (incoming) 中心の recursive traversal、`LinkPath` は `[(entity_type, entity_id, link_type), ...]` の chain。Cycle detection は visited set。test は migrated SQLite + seed links で 1-hop / multi-hop / cycle / depth-limit を pin | C |
| C2 | `feat(services): LinkService expand + bidirectional` | `LinkService.expand(entity_type, entity_id, *, depth: int = 2, max_depth: int = 5, link_types: list[str] | None = None) -> GraphSubset`: bidirectional N-hop。`GraphSubset` は nodes + edges を持つ dataclass (CLI render 用)。Cycle detection + visited tracking。depth 上限超過は `ConfigError`。test に large graph + cycle + depth-limit pin。`LinkService.find_link_id(...)` (manual `link remove` で使う lookup helper) も同 PR で追加 | C |

### 2.4 Sub-issue D: CLI + brief/propose integration (2 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| D1 | `feat(cli): link + graph commands` | `src/opshub/cli/link.py` 新設: `opshub link add <from-type>:<from-id> <to-type>:<to-id> --type <link-type> [--metadata k=v ...]` (emits `LinkCreated`) / `opshub link remove <link-id> [--reason "..."]` (emits `LinkDeleted`) / `opshub link list [--from <entity>] [--to <entity>] [--type <link-type>] [--limit N] [--format md\|json]`。`src/opshub/cli/graph.py` 新設: `opshub graph related <entity-type>:<entity-id> [--direction outgoing\|incoming\|both] [--type <link-type>] [--limit N] [--format md\|json\|dot]` / `opshub graph trace <entity-type>:<entity-id> [--depth N] [--format md\|json\|dot]` / `opshub graph expand <entity-type>:<entity-id> [--depth N] [--format md\|json\|dot]`。`--format dot` で Graphviz DOT 出力 (operator が `dot -Tpng` でレンダ可能、Phase 8.x で SVG 出力 helper を検討)。M6 cold-start guard 順守 (module-level import は whitelist のみ) | D |
| D2 | `feat(services): briefing/propose --expand-graph` | `BriefingService.generate(..., expand_graph: bool = False)` + `ProposalService.generate(..., expand_graph: bool = False)` に option 追加。`expand_graph=True` 時、RecallService の hit 各々について `LinkService.related(entity, link_types=["referenced_in_briefing","references","applied_to"], limit=3)` を呼び、link 先 entity を追加 source として LLM prompt に含める。dedupe by `(entity_type, entity_id)`、original recall hits が優先。`opshub brief "<topic>" --expand-graph` / `opshub propose generate "<topic>" --expand-graph` CLI flag。default False で backward-compat。`tests/unit/services/test_briefing_service.py` + `test_proposal_service.py` に `--expand-graph=True` で graph 拡張が prompt に効くことを pin | D |

### 2.5 Sub-issue E: Phase 8 closeout (1 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| E1 | `test: phase 8 end-to-end + docs` | `tests/integration/test_phase8_lifecycle.py`: end-to-end (`opshub task add` → `opshub embeddings rebuild` → `opshub brief` → `opshub propose generate --from-briefing <id>` → `opshub propose apply` で task が作成される → `opshub graph trace <new-task-id>` で proposal → briefing → sources の chain が出る、までの自動 link 抽出 chain を mock 経由で検証)。`tests/integration/test_phase8_manual_link_lifecycle.py`: manual `link add` / `link remove` の round-trip + 冪等性 + `links` projection への反映。`tests/integration/test_phase8_expand_graph_lifecycle.py`: `--expand-graph` で graph 拡張された context が LLM prompt に乗ることを pin。`projections rebuild` の冪等性 (historical events を re-apply して links が完全再構築) を `tests/integration/test_phase8_rebuild_idempotency.py` で pin。docs: README に `opshub link` + `opshub graph` 追記 + `brief --expand-graph` / `propose --expand-graph` 言及。AGENTS.md / CLAUDE.md / docs/principles.md (§9 Phase 8 = ✅ Complete、§Open Q 残置 = §5 Multi-machine sync のみ) / docs/architecture.md §2.10 (新規) Knowledge graph layer 追記 / docs/repository-structure.md (`[P8]` annotation) / docs/decisions-log.md (Phase 8 entry) / ADR-0017 Validation 追記 (test ファイルへの reference)。Phase 3 `SourceReferenced` の placeholder closeout を docs で明示 | E |

= 合計 **9 PR** (A 2 + B 2 + C 2 + D 2 + E 1)。

**Wave 構成** (DAG):

```text
Wave 1: A1 ADR-0017 → 1 並列 (sequential foundation)
Wave 2: A2 links projection + B1 link events + SourceReferenced closeout → 2 並列 (A1 依存)
Wave 3: B2 LinksExtractor + C1 LinkService related/trace → 2 並列 (A2 + B1 依存)
Wave 4: C2 LinkService expand + D1 link/graph CLI → 2 並列 (C1 依存)
Wave 5: D2 brief/propose --expand-graph → 1 (C2 依存)
Wave 6: E1 closeout → 1 (全 sub-issue 依存)
```

= 6 wave。Phase 6 と同サイズ (9 PR / 6 wave)。Wave 2-4 で 2 並列、Wave 1 / 5 / 6 は sequential。

## 3. 各 Sub-issue の Definition of Done

### Sub-issue A — Foundation

- [ ] ADR-0017 Accepted + decisions-log.md entry
- [ ] `links` projection が migration 0016 で作成、`alembic upgrade head` で apply 可能
- [ ] 2 INDEX (`links_from_idx` + `links_to_idx`) が作成される
- [ ] `LinksProjector` skeleton が apply 経路で no-op (まだ extraction logic なし)、冪等性 test 通過
- [ ] failing-projector atomicity test pass

### Sub-issue B — Event-driven link extraction

- [ ] `Phase8Event` 追加 + `AllEvent` 拡張が mypy + pyright clean
- [ ] `LinkCreated` / `LinkDeleted` event の round-trip test
- [ ] Phase 3 `SourceReferenced` の field 確認 + docstring 更新済 (or 新規追加)
- [ ] `LinksExtractor` が 6 経路 (ProposalApplied / BriefingGenerated / ProposalRequested.briefing_id / SourceReferenced / LinkCreated / LinkDeleted) を正しく `links` row に変換
- [ ] UPSERT semantics: 同 natural-key の 2 回目 apply は no-op (test pin)
- [ ] `projections rebuild` 経由で historical events から完全な links graph が再構築される (idempotent rebuild test)
- [ ] `LinkDeleted.reason` が `core.sanitise.sanitise_error_message` 経由

### Sub-issue C — LinkService traversal

- [ ] `LinkService.related(entity, direction="both")` が outgoing + incoming の link 両方を返す
- [ ] `LinkService.related(entity, link_types=[...])` で filter 可能
- [ ] `LinkService.trace(entity, depth=N)` が backward chain を返す
- [ ] `LinkService.expand(entity, depth=N)` が bidirectional graph subset を返す
- [ ] cycle detection: A → B → A の graph で `expand(A, depth=2)` が無限ループしない
- [ ] depth 上限超過で `ConfigError`
- [ ] `LinkService.find_link_id(...)` lookup helper

### Sub-issue D — CLI + integration

- [ ] `opshub link add <from> <to> --type <type>` で `LinkCreated` event + projection row
- [ ] `opshub link remove <link-id>` で `LinkDeleted` event + row 削除 (実 row は残し state=deleted の方式 or 物理削除のいずれかは A1 ADR で確定)
- [ ] `opshub link list [--from/--to/--type]` filter 動作
- [ ] `opshub graph related/trace/expand` の 3 subcommand
- [ ] `--format md/json/dot` の 3 形式
- [ ] M6 cold-start guard 順守 (`cli/link.py` / `cli/graph.py` の module-level import が whitelist 範囲)
- [ ] `opshub brief "<topic>" --expand-graph` / `opshub propose generate "<topic>" --expand-graph` 動作
- [ ] `--expand-graph` で graph 拡張 entity が LLM prompt に追加される (test で pin)
- [ ] `--expand-graph` 未指定時は既存挙動 unchanged (Phase 5/6 test 全通過、backward-compat)

### Sub-issue E — Phase 8 closeout

- [ ] `test_phase8_lifecycle.py` が自動 link 抽出 chain を mock e2e で検証
- [ ] `test_phase8_manual_link_lifecycle.py` で manual CRUD round-trip
- [ ] `test_phase8_expand_graph_lifecycle.py` で `--expand-graph` の prompt 効果 pin
- [ ] `test_phase8_rebuild_idempotency.py` で `projections rebuild` の冪等性 pin
- [ ] README に `opshub link` + `opshub graph` 追記
- [ ] AGENTS.md / CLAUDE.md / principles.md (§9 Phase 8 ✅ Complete、§Open Q 残置 = §5 Multi-machine sync のみ) / architecture.md (§2.10 新規 Knowledge graph) / repository-structure.md (`[P8]`) / decisions-log.md (Phase 8 entry)
- [ ] ADR-0017 Validation 追記
- [ ] Phase 3 `SourceReferenced` placeholder closeout を docs で明示

## 4. Open Questions

Phase 8 着手時点で未確定、本 plan 内で確定すべきもの:

1. **`LinkDeleted` semantics** — Soft delete (link row に `deleted_at` 列追加) vs Hard delete (DELETE FROM links)。本 plan では **Hard delete + LinkDeleted event を log に残す** を default。event-sourced trace は events table で保持されるので projection 側で重複する必要なし。A1 ADR で正式採択
2. **Phase 3 `SourceReferenced` の現状** — 未定義 / placeholder / 部分実装 のいずれか。B1 着手前に `src/opshub/domain/events/` を grep して確認、未定義なら B1 で新規定義 (Phase 3 event family `Phase3Event` に追加する形)。確認手順を B1 spec に明記
3. **Manual link の `link_type` 制約** — operator が任意の文字列を `--type` で指定可能にするか、ADR-0017 で確定した enum (`applied_to` / `referenced_in_briefing` / `generated_from_briefing` / `references` / `manual`) のみ許可するか。本 plan では **任意文字列許可** (`--type` value validation は free-form、ただし enum 外を使うと auto-extracted link と区別できない可能性ありを CLI help に明記) を default。A1 ADR で確定

Phase 8 内では確定しなくてよい (Phase 8.x / 9 持ち越し):

1. **Connector-side automatic `SourceReferenced` 発行** — GitHub Issue body の `#123` parse / Slack message の URL parse 等は Phase 8.x。本 phase は manual + projector consumption の closeout のみ
2. **Graph visualisation web UI** — Phase 8 は CLI + DOT 出力のみ。HTML/SVG renderer は Phase 8.x
3. **Multi-machine sync** — principles.md §Open Q #5、Phase 9 候補
4. **`inbox_item` / `source` proposal candidate types** — Phase 6 MVP gap、Phase 8.x
5. **`llama.cpp` direct backend** — Phase 8.x
6. **Common OAuth helper refactor** — Phase 7 後、Phase 8.x
7. **Connector observability + cost layer** — Phase 8.x
8. **Additional connectors** (Notion / Linear / Discord / Jira / Confluence) — Phase 8.x
9. **Briefing cache + narrow scope** — Phase 5.x 名残

## 5. Phase 8.x / 9 outlook

Phase 8 完了直後の候補:

- **Multi-machine sync** (principles.md §Open Q #5 closeout、Phase 9 候補): litestream / Turso / event-sourced export-import + ADR-0018
- **Connector-side automatic `SourceReferenced` 発行** (GitHub / Slack / MS365 / Box の body parse → link 自動抽出): Phase 8.x で各 connector の mapper を拡張
- **Graph visualisation web UI** (`opshub graph serve` で HTTP server + SVG renderer): Phase 8.x
- **`inbox_item` / `source` proposal candidate types** (Phase 6 MVP gap): Phase 8.x
- **`llama.cpp` direct backend** (Ollama 不要): Phase 8.x
- **Connector observability + cost layer**: Phase 8.x
- **Common OAuth helper refactor** (Phase 7 後): Phase 8.x
- **Additional connectors** (Notion / Linear / Discord / Jira / Confluence): Phase 8.x
- **Briefing cache + narrow scope**: Phase 5.x 名残

Phase 8.x / 9 着手時に連動して見直すべき docs: principles.md §1 (Local-first、graph data の export 必要性) / §6 (External Content Min、graph context の LLM 露出範囲) / ADR-0002 (Event-Sourced、`LinksExtractor` の derived-state pattern の整合確認) / ADR-0010 (Connector Contract、connector-side SourceReferenced 抽出方針) / ADR-0017 (本 phase で新設、E1 で Validation 追記)。
