# 0017. Knowledge Graph

- Status: Accepted (revised 2026-06-07 for epic #470 — drop `expand_graph` opt-in flag)
- Date: 2026-05-17 (initial); 2026-05-30 (Phase 10 §決定 (b) revision: `reply_draft_replies_to` / `referenced_in_reply_draft` link_type 追加); 2026-06-07 (epic #470 §決定 (f) revision: `--expand-graph` opt-in 撤廃 + graph 1-hop 拡張を無条件 default に統一)
- Deciders: ozzy

## Context

Phase 1-7 で OpsHub は 6 種類の主要 entity (`tasks` / `decisions` / `inbox_items` / `sources` / `briefings` / `proposals`) を operational memory として蓄積する基盤を完成させた。これらは単体としては projection で索引化されており CLI から quick lookup できるが、**entity 間の関係 (link)** は現在 event log を JOIN しないと辿れない。例えば「この decision はどの briefing を経て作られたか」「この task はどの proposal の apply で生まれたか」「この source は何回 task や decision から参照されたか」のような **provenance query** は、現状 SQL を直接書くか開発者が手で event log を grep する必要がある。これは agent から見ると「memory はあるが、memory 間のつながりが見えない」状態であり、operational memory tool としての中核機能が欠けている。

第二の論点は **既存 event payload に既に存在する cross-entity reference を materialise する経路がない** ことである。Phase 5 で `BriefingGenerated.source_refs: list[SourceRef]` を導入し、briefing 生成時に「どの entity を context として使ったか」を event に記録するようにした。Phase 6 で `ProposalApplied.applied_entity_type` + `ProposalApplied.applied_entity_id` を導入し、proposal apply で生成された task / decision の ID を event に記録するようにした。Phase 6 で `ProposalRequested.briefing_id: str | None` を導入し、proposal が特定の briefing から生成されたことを記録するようにした。これらの payload は cross-entity reference そのものだが、どの projection もまだこれを materialise しておらず、活用するには event log query が必要なままである。

第三の論点は **Phase 3 で placeholder のまま残された `SourceReferenced` event の closeout** である。Phase 3 では connector 経由で取り込んだ source の本文中に「この source は task #123 / decision X に言及している」という参照を発見した場合に発行する event として `SourceReferenced` を予約していたが、消費する projector が存在しないまま Phase 3-7 が完了した。Phase 8 でこの event を第一級の link 抽出経路として projector に組み込み、placeholder を closeout する (connector-side の **自動抽出ロジック** = GitHub Issue body の `#123` parse / Slack message URL parse 等は Phase 8.x に持ち越し、本 phase は consumer 側の実装と manual `link add` の経路を確立する)。

第四の論点は **Knowledge graph によって provenance query の loop が閉じる** ことである。`opshub graph trace <task-id>` で「この task は proposal P から生まれた → proposal P は briefing B から生成された → briefing B は source S1, S2, decision D を context として使った」という chain を即座に出せる。さらに `opshub brief "<topic>" --expand-graph` / `opshub propose generate "<topic>" --expand-graph` を opt-in で実行すると、RecallService の hit を graph 1-hop 拡張して LLM context を広げ、Phase 5/6 の機能を **LLM stack を変えずに** 品質向上できる。`--expand-graph` は default off にすることで Phase 5/6 の既存テストと cost profile を保護する。

第五の論点は **principles.md の Open Question 残置状況** である。Phase 7 完了時点で残った Open Question は §5 (Multi-machine sync) のみで、Phase 8 はこれを closeout しない (Phase 9 候補)。Phase 8 (Knowledge graph) 自体は単一 machine 内の graph 構築に集中し、graph data の sync (links table が follower に複製されるのか / follower 側で events から再 derive するのか) は Phase 9 ADR で別途判断する。本 ADR ではその選択肢を Open Questions に flag するに留める。

第六の論点は **schema 設計の単純化判断** である。link を表現する方法として「link_type ごとに別 table を切る」(`applied_links` / `briefing_links` / `reference_links` / `manual_links`) vs 「単一 `links` table に link_type 列で多態化する」の 2 案を検討した結果、本 ADR では **単一 denormalized `links` table + 2 つの index で bidirectional traversal** を採択する。理由: (i) schema 量を 1 table に抑えて migration / backup / replay の対象を最小化する (ADR-0002 single-source-of-truth と整合)、(ii) `links_from_idx (from_entity_type, from_entity_id)` と `links_to_idx (to_entity_type, to_entity_id)` の 2 index で bidirectional traversal が WHERE 1 つで済む、(iii) link_type ごとの filter は WHERE clause で十分高速で、table 選択は不要、(iv) per-link_type table 案は UNION 経由の bidirectional query になり SQL が複雑化する。

## Decision

OpsHub に **Knowledge graph layer** を追加する。`links` projection を新設し、4 種類の自動抽出経路 (`ProposalApplied` / `BriefingGenerated.source_refs` / `ProposalRequested.briefing_id` / `SourceReferenced`) と manual `opshub link add` で graph を構築する。`LinkService` で `related` / `trace` / `expand` の 3 種の traversal API を提供し、`opshub link` / `opshub graph` CLI で operator が触れるようにする。`--expand-graph` flag で Phase 5/6 の briefing / propose を graph 拡張版に opt-in できるようにする。以下に 8 つの決定を pin する。

### (a) `links` projection schema = 単一 denormalized table + 2 index

`links` projection は以下の 1 table で構成する (Phase 8 step A2 で migration 0016 を作成して登録)。

```sql
CREATE TABLE links (
  id TEXT PRIMARY KEY,                            -- ULID
  from_entity_type TEXT NOT NULL,                 -- "task" | "decision" | "inbox_item" | "source" | "briefing" | "proposal" | ...
  from_entity_id TEXT NOT NULL,                   -- ULID of the entity
  to_entity_type TEXT NOT NULL,
  to_entity_id TEXT NOT NULL,
  link_type TEXT NOT NULL,                        -- see (b)
  created_at TEXT NOT NULL,                       -- ISO 8601
  source_event_id TEXT,                           -- nullable: ULID of the event that emitted/derived this link
  metadata TEXT,                                  -- nullable: JSON blob for link-type specific extras
  UNIQUE (from_entity_type, from_entity_id, to_entity_type, to_entity_id, link_type)
);

CREATE INDEX links_from_idx ON links (from_entity_type, from_entity_id);
CREATE INDEX links_to_idx ON links (to_entity_type, to_entity_id);
```

要点:

- **Natural key** = `(from_entity_type, from_entity_id, to_entity_type, to_entity_id, link_type)`。`UNIQUE` 制約で重複を防ぎ、projector は `INSERT OR REPLACE` (もしくは equivalent UPSERT) semantics で apply する。同 event を 2 回 apply しても 1 行のまま (`projections rebuild` で historical event を流しても idempotent)
- **2 INDEX**: `links_from_idx (from_entity_type, from_entity_id)` で outgoing traversal、`links_to_idx (to_entity_type, to_entity_id)` で incoming traversal。bidirectional query は両 index を OR で叩く 1 文で済む
- `source_event_id` は nullable。自動抽出 link は元 event の id を記録 (replay debug 用)、manual link は `LinkCreated` event の id を記録
- `metadata` は JSON blob で link-type 固有の追加情報を保持 (例: `referenced_in_briefing` の link で source_ref の `score` を残す、Phase 8.x で拡張余地)

per-link_type 別 table 案は **§Alternatives Considered 1** で却下している (schema bloat / 重複 index / UNION query 複雑化)。

### (b) `link_type` 初期 enum (Phase 8 MVP = 5 種類、Phase 10 で 7 種類に拡張)

Phase 8 MVP では以下の 5 種類の `link_type` を pin する。自動抽出 path はこの enum に限定し、enum 外の link_type は **manual path でのみ** warning 付きで許容する (operator が任意の関係を表現できる余地は残しつつ、auto-extracted link と区別可能にするため)。

| link_type | 発行経路 | 意味 |
|---|---|---|
| `applied_to` | `ProposalApplied` 自動抽出 | `proposal:<id>` → `<applied_entity_type>:<applied_entity_id>` (apply で生成された entity への link) |
| `referenced_in_briefing` | `BriefingGenerated.source_refs` 自動抽出 | `briefing:<id>` → `<referenced_entity_type>:<referenced_entity_id>` (briefing が context として参照した entity) |
| `generated_from_briefing` | `ProposalRequested.briefing_id` 自動抽出 (briefing_id が non-None の場合) | `proposal:<id>` → `briefing:<briefing_id>` (proposal を生成する元になった briefing) |
| `references` | `SourceReferenced` 自動抽出 | `source:<id>` → `<referenced_entity_type>:<referenced_entity_id>` (source 本文が言及している entity) |
| `manual` | `LinkCreated` (`opshub link add`) | operator が明示的に張った link (任意の entity 間) |

manual path の `link_type` は free-form 文字列 (`--type` の値は自由) を許容するが、上記 5 種類以外を使うと CLI が `warning: link_type "<value>" is not in the recommended enum; auto-extracted links use [applied_to, referenced_in_briefing, generated_from_briefing, references, manual]` を出す。これは operator が任意の link semantics を導入できる余地を残しつつ、auto-extracted link と区別可能にする目的。

> **Phase 10 改訂 (2026-05-30、Sub-issue E)**: ADR-0016 改訂 (§決定 (i)) で追加される `reply_draft` candidate kind の provenance を表現するため、以下の **2 link_type** を enum に昇格する。これにより auto-extracted enum は 7 種類になる。新 event は派生せず、§決定 (c) の **pure derived state projector パターン**を踏襲する (`ProposalApplied` 経路と同様、既存 event payload から派生)。

| link_type (Phase 10 追加) | 発行経路 | 意味 |
|---|---|---|
| `reply_draft_replies_to` | `ProposalApplied` 自動抽出 (`applied_entity_type = "reply_draft"` の場合) + Phase 10 reply_draft candidate の `reply_to_source_id` を `metadata` 経由で源として確定 | `proposal:<id>` → `source:<reply_to_source_id>` (返信下書きが返信先 source を参照) |
| `referenced_in_reply_draft` | `BriefingGenerated.source_refs` 拡張パターンと同じく、reply_draft の生成プロンプトで `<context_source>` (ADR-0017 §決定 (f) `--expand-graph` 経由) として注入された entity 群を materialise | `proposal:<id>` → `<referenced_entity_type>:<referenced_entity_id>` (reply_draft 生成 context として参照した entity) |

要点:

- **新 event 非発行**: 上記 2 link_type は **`ProposalApplied` (reply_draft 候補が apply された場合) や `ProposalGenerated` (reply_draft 候補が生成された時点で proposal payload に reply_to_source_id が含まれる)** から派生する純粋な derived state で、新 event family を導入しない。ADR-0017 §決定 (c) と完全に同じパターン (§Phase 8 b/c で確立した pattern を踏襲)
- **`LinksProjector` の dispatch 表に 2 path 追加**: `ProposalApplied` 経路で `applied_entity_type == "reply_draft"` の場合 (ADR-0016 §決定 (i) の `reply_to_source_id` を candidate payload から復元) と `ProposalGenerated` 経路で reply_draft candidate が含まれる場合 (`<context_source>` 由来の link)。両 path は §決定 (a) の natural-key UPSERT で `projections rebuild` 冪等
- **既存 5 link_type の semantics は不変**: `applied_to` は task / decision / reply_draft の 3 entity 種に拡張されるが、link_type 文字列としては従来通り (新 entity 種類が増えるだけ)
- **manual path での自由度**: ADR-0017 §決定 (b) で manual link が free-form 文字列を許容する原則は不変。`reply_draft_replies_to` / `referenced_in_reply_draft` を manual で打つと auto-extracted と同じ enum 内なので warning は出ない (operator が `opshub link add` で意図的に reply_draft の provenance を手書きするのは想定される運用)

### (c) 自動抽出 projector は新 event を発行しない (pure derived state)

`LinksExtractor` (Phase 8 B2 で実装する `LinksProjector` 内の logic) は **既存の event を読んで `links` row を書く純粋な derived-state projector** とする。新 event (例: `LinkCreated`) を派生発行しない。

具体的な dispatch 表 (Phase 8 B2 で実装):

| 入力 event | 派生する link row |
|---|---|
| `ProposalApplied(proposal_id, applied_entity_type, applied_entity_id, ...)` | `(proposal:<proposal_id>, <applied_entity_type>:<applied_entity_id>, applied_to)` |
| `BriefingGenerated(briefing_id, source_refs=[(entity_type, entity_id, ...), ...])` | source_refs 各 entry につき `(briefing:<briefing_id>, <entity_type>:<entity_id>, referenced_in_briefing)` |
| `ProposalRequested(proposal_id, briefing_id=<non-None>)` | `(proposal:<proposal_id>, briefing:<briefing_id>, generated_from_briefing)` |
| `SourceReferenced(source_id, referenced_entity_type, referenced_entity_id, ...)` | `(source:<source_id>, <referenced_entity_type>:<referenced_entity_id>, references)` |

採用理由:

- **ADR-0002 (Event-Sourced Architecture) 整合**: event log は immutable な single source of truth。derived state は projection で表現する。auto-extraction projector が新 event を派生発行すると「同じ事実が event log に 2 回記録される」状況になり、replay / audit / 「いつ何が起きたか」の trace が二重化されて整合性が壊れる
- **idempotent rebuild が単純**: 同じ event を 2 回 apply しても natural-key UPSERT で 1 行に collapse する。`projections rebuild` で全 historical event を流せば links table が完全再構築される
- **manual path と切り分け**: operator が `link add` で張った link は `LinkCreated` event として log に残り、auto-extracted link との出自を区別できる (前者は events table に entry あり、後者は events table に対応 entry なし、links table の `source_event_id` で trace 可能)

### (d) Manual link CRUD は `LinkCreated` / `LinkDeleted` event 経由

operator が `opshub link add` / `opshub link remove` で link を CRUD する際は、**必ず新 event を発行**して event log に記録する (ADR-0002 単一経路原則)。

- `opshub link add <from> <to> --type <link_type>` → `LinkCreated(link_id, from_entity_type, from_entity_id, to_entity_type, to_entity_id, link_type, source_event_id=None, metadata=...)` を append
- `opshub link remove <link-id> [--reason "..."]` → `LinkDeleted(link_id, deleted_by, reason?)` を append
- `LinksProjector` が両 event を消費し、`links` 行を INSERT / DELETE する

自動抽出 link は既存 event から derive されるため新 event を発行しないが (§決定 (c))、manual link は **operator が起こした state mutation** であり、prior event から derive できない。ADR-0002 の「state mutation は必ず event 経由」原則に従い、event log への append が必須。

`LinkDeleted.reason` は free-form 文字列だが、`core.sanitise.sanitise_error_message` を経由してから event に書き込む (ADR-0015 §決定 (g) の secret leakage 防御を全 Failed 系 event family に適用する慣行と整合)。

### (e) Traversal depth limits + cycle detection 必須

`LinkService` の 3 traversal API は depth 上限と cycle detection を **必須実装** とする。

| API | default depth | max depth | direction | 用途 |
|---|---|---|---|---|
| `related(entity)` | 1-hop fixed | 1 | both (outgoing + incoming) | 「この entity に直接つながる link」一覧 |
| `trace(entity, depth=N)` | 3 | 10 | backward 中心 (incoming) | provenance query (「この task はどう作られたか」) |
| `expand(entity, depth=N)` | 2 | 5 | bidirectional | LLM context 拡張 / graph subset 取得 |

要件:

- **cycle detection**: 全 traversal で visited set (`set[tuple[str, str]]` of `(entity_type, entity_id)`) を持ち、訪問済 node の再訪を skip する。A → B → A のような cycle で `expand(A, depth=2)` が無限ループしないことを test で pin
- **depth 上限超過**: 各 API は呼び出し時に `depth > max_depth` を検査し、超過時は `ConfigError(f"depth {depth} exceeds max {max_depth}")` を即時 raise する (fail-fast)
- **trace の direction 規約**: provenance query では「結果 → 原因」を遡るため backward (incoming edge を follow) を priority とする
- **expand の direction 規約**: graph context 拡張では両方向の neighbour を集めるため bidirectional

depth の default 値 (related 1 / trace 3 / expand 2) は Phase 8 B1-D2 の test fixture と operator 期待値 (cost / 出力サイズ) に基づく初期値で、Phase 8.x 以降に operator feedback を見て調整する余地を残す (本 ADR §Open Questions §1 参照)。

### (f) Graph 拡張は無条件 (epic #470 で `--expand-graph` opt-in 廃止)

`opshub brief "<topic>"` / `opshub propose generate "<topic>"` は graph 1-hop 拡張を **常時実行** する。Phase 8 D2 で導入された `--expand-graph` opt-in flag (CLI + MCP arg + Service `expand_graph: bool = False` param) は epic #470 (pre-userbase compat shim cleanup) で **完全削除** された。挙動は旧 `--expand-graph` 指定時 (RecallService の hit 各々について `LinkService.related(entity, link_types=["referenced_in_briefing", "references", "applied_to"], limit=3)` を呼び、link 先 entity を追加 source として LLM prompt に含める。dedupe は `(entity_type, entity_id)` で実施、original recall hits が priority) と等価。

採用理由 (epic #470):

- **アシスタント 14 Skill は全 graph 連携 skill で `expand_graph: true` を明示渡ししていた** — `reply-draft` / `inbox-triage` / `meeting-followup` / `source-extract` / `research` / `external-brief` の 6 SSOT がいずれも true 固定だったため、opt-out 経路は実質的に使われていなかった
- **wiring (`cli/_wiring.py`) は常時 `LinkService` を渡していた** — `link_service=None` ケースは production 経路に存在せず、`expand_graph=True + link_service=None` の dead-branch `ConfigError` を抱えていた
- **pre-userbase posture**: ADR-0011 §設計判断のスタンス により、improvement 量より「あるべき設計」を優先する。「Phase 5/6 既存 snapshot 保護のための backward-compat default」は pre-userbase で意味がない
- **cost control は `max_tokens` (ADR-0015 §決定 (h)) で十分**: graph 拡張で追加された source も同じ token budget 制約下で truncate される。operator が明示 opt-out したい場合の代替経路 (`--no-expand-graph`) は提供しない (param 削除と矛盾しないため)

旧文 (Phase 8 D2 時点) は「Phase 5/6 backward compat / cost monitoring / 品質 validation の段階性」を opt-in default off の根拠としていたが、いずれも pre-userbase posture (ADR-0011) で機能していなかったため epic #470 で撤回した。Phase 8 plan §2.4 D2 の `--expand-graph` flag spec は本決定で履歴情報として残置 (operator-facing spec は本 ADR §決定 (f) が SSOT)。

### (g) Connector-side automatic `SourceReferenced` 発行は Phase 8.x 持ち越し

Phase 8 MVP では **既存 event から link を derive する 4 経路 + manual `link add`** のみ実装する。connector-side で source 本文を parse して自動的に `SourceReferenced` event を発行する経路 (例: GitHub Issue body 中の `#task-id` を parse / Slack message URL を parse / MS365 Calendar attendee → entity / Box file metadata → entity) は **Phase 8.x で別 PR / 別 plan** で扱う。

採用理由:

- **scope 絞り込み**: connector-side の自動抽出は connector ごとに body parsing logic が必要で、4 connector × parsing rule の test 整備で 1 phase 分の作業量に膨らむ
- **manual baseline 優先**: 自動抽出 logic を validate する前に manual `link add` で human が想定する link 構造を運用上 pin し、その上で connector-side 抽出が manual と一致するかを検証する方が安全。誤抽出 link で `links` table が汚染されると graph context 拡張の品質も劣化する
- **既存 4 経路で chain は閉じる**: `ProposalApplied` / `BriefingGenerated` / `ProposalRequested` / `SourceReferenced` (manual `link add` で source → entity link を入れる経路) の 4 経路で「task → proposal → briefing → source」の主要 chain は trace 可能。connector-side は incremental value

### (h) `LinkDeleted` semantics = Hard delete

`LinkDeleted(link_id, deleted_by, reason?)` event を消費した `LinksProjector` は、対応する `links` 行を **物理削除 (DELETE FROM links WHERE id = ?)** する。Soft delete (`links` table に `deleted_at` 列を追加) は採用しない。

採用理由:

- **event log で trace は既に保全**: 「いつ誰が delete したか」「なぜ delete したか」は `LinkDeleted` event として events table に immutable に残る (ADR-0002)。`links` projection 側に重複して deleted state を保つ必要がない
- **「deleted but still queryable」 use case が薄い**: operator が「過去に存在したが今は無い link を見たい」と思った場合は events table を query すれば足りる (`SELECT ... FROM events WHERE event_type IN ('LinkCreated', 'LinkDeleted')`)。projection は「現在の graph」のみを表現する責務
- **`projections rebuild` が単純**: hard delete なら rebuild 時に LinkCreated → LinkDeleted の順で event を流せば最終状態 = 削除済 (空) となる。soft delete だと `deleted_at` を「rebuild 時に LinkDeleted event の timestamp で埋める」logic が必要で、rebuild の冪等性検証が複雑化
- **schema 単純**: 列 1 つ + WHERE clause の `... AND deleted_at IS NULL` 慣行が全 traversal query に出現するのを避ける

将来「削除済 link を一覧したい」 use case が顕在化した場合は Phase 8.x で soft delete を別途検討する (本 ADR §Open Questions §3 参照、再導入には migration が必要)。

## Consequences

### Positive

1. **provenance query が即答可能になる** — `opshub graph trace <task-id>` で「この task は proposal P → briefing B → sources S1, S2 から作られた」の chain を 1 コマンドで取得できる。これまで event log を SQL で JOIN する必要があった作業が CLI 化される
2. **既存 event payload の cross-entity reference が初めて活用される** — Phase 5 `source_refs` / Phase 6 `applied_entity_id` / Phase 6 `briefing_id` は event payload に既に存在していたが、materialise する projection が無かった。Phase 8 でこれらが links projection 経由で索引化される
3. **Phase 3 `SourceReferenced` placeholder が closeout される** — 3 phase 越しに consumer の無い event だった `SourceReferenced` が `LinksProjector` で消費され、第一級の link 抽出経路として正式に運用に乗る
4. **graph 1-hop 拡張は無条件 default として LLM stack 変更なしに Phase 5/6 を強化** — epic #470 で `expand_graph` opt-in flag を撤廃した結果、`BriefingService` / `ProposalService.generate` / `ProposalService.generate_reply_draft` は graph 1-hop 拡張を常時走らせて LLM prompt に拡張 context を載せる。Pluggable Embedder / LLMClient は一切変更しない (旧文 = Phase 8 D2 時点の opt-in flag 経路は §決定 (f) の epic #470 改訂で履歴情報として残置)
5. **schema 単純** — 1 table + 2 index + 1 UNIQUE 制約で完結。migration / backup / replay の対象が増えない (per-link_type 別 table 案だと 5+ table)
6. **idempotent rebuild が保証される** — 自動抽出 projector が新 event を発行しない pure derived state pattern + natural-key UPSERT で、`projections rebuild` で historical event を全部流しても links table が完全再構築される
7. **event-sourced trace が保全される** — auto-extracted link は元 event の id を `source_event_id` 列で trace 可能、manual link は `LinkCreated` event を log に残す。hard delete でも `LinkDeleted` event で「いつ誰が delete したか」が events table に残る

### Negative / Trade-offs

1. **`links` projection は denormalized で write amplification がある** — 1 event が複数 link を派生する場合 (例: `BriefingGenerated.source_refs` が 5 entry なら 5 link row) があり、event log 量に比べて links table 量が膨らむ可能性
   - 緩和: Phase 8 E1 で operator-scale (1000 task / 100 briefing / 50 proposal) で links table 行数を実測する。問題が顕在化したら Phase 8.x で limit / pagination を `related` / `expand` に追加
2. **graph 1-hop 拡張は無条件 baseline で、cost 増 (最大 `original_hits × 3` 個の neighbour entity 追加) は caller の `max_tokens` 設定で受け止める** — epic #470 で opt-in flag を廃止したため、cost monitoring は ADR-0015 §決定 (h) の `max_tokens` 経由の caller responsibility に統合される (`--no-expand-graph` opt-out 経路は提供しない、ADR-0011 pre-userbase スタンス)
   - 緩和: `_GRAPH_EXPAND_PER_HIT_LIMIT = 3` で neighbour 数を bound、`max_tokens` で truncate される (ADR-0015 §決定 (h))。CLI help から `--expand-graph` 関連文言は撤去され、cost 制御は `--max-tokens` / `[brief] max_tokens` 経由の単一経路に集約
3. **`link_type` enum を 5 種類で pin したことで Phase 8.x の追加には ADR 更新が必要** — 新しい auto-extraction 経路 (例: connector-side automatic SourceReferenced) を加えたいときに、新 link_type を enum に足す → 本 ADR を update する手続きが要る
   - 緩和: manual path は free-form 文字列を許容するので、operator は新しい link_type を運用上 trial 可能 (enum 外 warning 付き)。trial で価値が validate されたら enum に昇格させる 2 段階運用 (Phase 8.x で実施)
4. **hard delete で「削除済 link の audit」は events table query が必要** — `links` projection には現在の graph しか残らないため、「過去存在したが現在は削除された link」を一覧するには events table の `LinkCreated` / `LinkDeleted` を JOIN する SQL を書く必要がある
   - 緩和: §決定 (h) で示したとおり、現状の use case では event log query で十分。`opshub link list --include-deleted` 相当の CLI は §Open Questions §3 で deferred
5. **cycle detection / depth limit のテスト負荷** — traversal API 3 種類 × cycle / depth-limit / direction filter / link_type filter の組み合わせで test ケース数が増える
   - 緩和: Phase 8 C1/C2 で基本 fixture + cycle / max depth 専用 test を C 直下にまとめる。E1 closeout で integration end-to-end を追加

## Validation

Phase 8 sub-issue A-E の実装で本 ADR の決定 (a)-(h) は以下のとおり pin した (Phase 8 E1 closeout 時点で全て pinned):

- **(a) `links` projection schema** — `src/opshub/projections/links.py` の `links_table` 定義 (Phase 8 A2 で追加 + migration 0016 で create)。schema (PK / 列 / nullable) は `tests/unit/projections/test_links_skeleton.py` で metadata inspection 経由で pin。`alembic upgrade head` で table + UNIQUE + 2 INDEX が作成されることは `tests/integration/test_phase8_migrations.py` (`test_alembic_upgrade_creates_links_table` / `test_alembic_creates_natural_key_unique_constraint` / `test_alembic_creates_bidirectional_traversal_indexes`) で再現
- **(b) `link_type` 初期 enum** — `src/opshub/projections/links.py` の `LINK_TYPES_MVP: frozenset[str]` 定数で 5 種類 (`applied_to` / `referenced_in_briefing` / `generated_from_briefing` / `references` / `manual`) を pin。auto-extraction projector の出力が全てこの enum に collapse することは `tests/unit/projections/test_links_extractor.py` の 6 dispatch path test で pin。manual path の任意文字列許容は `tests/unit/cli/test_link.py` で pin
- **(c) Auto-extraction projector が新 event を発行しない** — `LinksProjector.apply` が EventStore に対して `append` を呼ばないことは `tests/unit/projections/test_links_extractor.py::test_apply_unrelated_event_is_noop` (+ 6 dispatch path test 全て connection 直接操作で pure derived state を pin)。grep ベースの contract: `src/opshub/projections/links.py` は `EventStore` import / `.append(` 呼び出しを一切持たない (純粋 SQLAlchemy `connection.execute` のみ)
- **(d) Manual link CRUD via `LinkCreated` / `LinkDeleted`** — `LinkService.create_link()` / `LinkService.delete_link()` が `LinkCreated` / `LinkDeleted` event を 1 件ずつ append することを `tests/unit/services/links/test_link_service_writer.py` で pin。CLI 経路での event 発行は `tests/unit/cli/test_link.py` で `events` テーブルを SELECT して `link.created` / `link.deleted` の存在を pin。`LinkDeleted.reason` が `core.sanitise.sanitise_error_message` 経由になることも同 file で API-key-shaped reason に対する mask を pin
- **(e) Traversal depth limits + cycle detection** — 以下で pin:
  - `tests/unit/services/links/test_link_service_basic.py`: `related(entity)` の 1-hop outgoing / incoming / bidirectional 返却 + `link_types` filter、`trace(entity, depth=11)` が `ConfigError` を raise、`trace` の cycle 検出 (`visited` set による branch 終端 + 閉じる edge を path に含めて返却)
  - `tests/unit/services/links/test_link_service_expand.py`: `expand(entity, depth=6)` / `expand(entity, depth=-1)` が `ConfigError` を raise、A → B → A graph で `expand(A, depth=2)` が無限ループしないこと (visited set による cycle break)、bidirectional 拡張 + dedup-by-id、`find_link_id` lookup helper
- **(f) Graph 1-hop 拡張は無条件 baseline (epic #470)** — `expand_graph` opt-in flag を撤廃した後、surviving test 群が以下の不変条件を pin する:
  - `tests/unit/services/test_briefing_service.py:594 test_generate_expands_via_link_service` / `tests/unit/services/test_proposal_service.py:944 test_generate_expands_via_link_service` — `BriefingService.generate` / `ProposalService.generate` が `LinkService.related` を呼び、graph 拡張 entity が LLM prompt に追加されること (Mock で pin)
  - `tests/unit/services/test_briefing_service.py:771 test_construct_without_link_service_raises_type_error` / `tests/unit/services/test_proposal_service.py:1090 test_construct_without_link_service_raises_type_error` — `BriefingService` / `ProposalService` 構築時に `link_service=None` を渡すと `TypeError` (constructor 必須化、dead-branch `ConfigError` 完全除去)
  - `tests/unit/mcp/test_registry_policy.py:176 test_brief_and_propose_generate_drop_expand_graph_property` — MCP `briefing.generate` / `proposal.generate` tool schema から `expand_graph` property が落ち、`additionalProperties: false` の効果で agent host が `{"expand_graph": ...}` を渡すと schema validation で reject されること
  - `tests/unit/cli/test_brief.py:409 test_brief_rejects_legacy_expand_graph_flag` / `tests/unit/cli/test_propose.py:407 test_generate_rejects_legacy_expand_graph_flag` — CLI `opshub brief --expand-graph` / `opshub propose generate --expand-graph` が Typer の unknown option として exit 2 で reject されること

  `tests/integration/test_phase5_lifecycle.py` / `test_phase6_lifecycle.py` は epic #470 後の baseline (graph 拡張 default) に再 snapshot 済で、Phase 5/6 経路でも graph 1-hop 拡張が常時走ることが integration レベルで pin される (旧「`expand_graph=False` で graph 拡張なし」の test は #485 で削除済)
- **(g) Connector-side automatic `SourceReferenced` 発行は Phase 8.x 持ち越し** — Phase 8 MVP の test には connector-side auto extraction の test を **追加しない** ことが pin (test の不在自体が決定の reflection)。Phase 8.x で別 ADR / 別 PR で追加
- **(h) `LinkDeleted` = Hard delete** — `LinksProjector` が `LinkDeleted` event を消費後、`links` table に対応 row が **存在しないこと** を `tests/unit/projections/test_links_extractor.py::test_link_deleted_removes_row_by_id` で pin (SELECT で empty result を assert)。`links` table の schema に `deleted_at` column が存在しないことは `tests/unit/projections/test_links_skeleton.py` の column inspection test で pin

end-to-end の整合確認は以下 4 ファイルで pin した (Phase 8 E1 closeout 時点):

- `tests/integration/test_phase8_lifecycle.py` — `opshub task create` → `opshub embeddings rebuild` → `opshub brief` → `opshub propose generate --from-briefing` → `opshub propose apply` → `opshub projections rebuild` で 3 種類の自動 link (`referenced_in_briefing` / `generated_from_briefing` / `applied_to`) を materialise → `opshub graph trace` (incoming chain) + `opshub graph expand` (bidirectional chain) で chain reachability を確認
- `tests/integration/test_phase8_manual_link_lifecycle.py` — manual `link add` → `link list` (`--from` / `--type` filter) → `link remove` (`--reason`) → 同 id への重複 `link remove` no-op の round-trip。projection 行と `link.created` / `link.deleted` event の 1:1 対応 + no-op delete 時も audit event が append されることを pin
- `tests/integration/test_phase8_expand_graph_lifecycle.py` — task → source の manual `references` link を seed し、無条件 baseline (graph 拡張 default、epic #470 改訂後) の brief / propose 経路で source body が常に含まれること + graph-expanded source が dedup されること (= 1 回のみ出現) を LLM stub の captured prompt 経由で pin。brief / propose 経路を対称な 2 test で pin (旧「`--expand-graph` 指定/未指定の対比」shape は #485 で baseline 一本に collapse 済)
- `tests/integration/test_phase8_rebuild_idempotency.py` — 6 種類の link-emitting event (4 自動抽出 + `LinkCreated` + `LinkDeleted`) を seed して `rebuild_all` で完全再構築、2 回 rebuild してもバイト一致 (`_stable_link_id` の決定性 + UPSERT 冪等性) を pin

## Known Limitations / Phase 8.x

本 ADR の決定で **MVP 範囲外** として明示的に残した項目と、Phase 8.x で追加検討すべき制約:

1. **Connector-side automatic `SourceReferenced` 発行不在** — §決定 (g) のとおり Phase 8 MVP は manual + 既存 event consumption のみ。GitHub Issue body の `#task-id` parse / Slack message URL parse / MS365 attendee → entity / Box file metadata → entity は Phase 8.x で connector ごとに mapper を拡張
2. **Graph visualisation web UI 不在** — Phase 8 は CLI + DOT (`--format dot`) 出力のみ。HTML / SVG renderer (`opshub graph serve` で HTTP server) は Phase 8.x
3. **`links` の soft delete 不在** — §決定 (h) のとおり hard delete を採択。「削除済 link を一覧する」CLI subcommand は events table query で代替可能だが、専用 CLI (`opshub link list --include-deleted` 相当) は Phase 8.x で use case が顕在化したら検討
4. **link_type ごとの quota / rate 制限不在** — 1 entity に 1000 link が張られた場合の `related` / `expand` の出力サイズ制御は `limit` 引数のみで実装。`link_type` ごとに weight を持って ranking する仕組みは Phase 8.x
5. **graph 経由の semantic recall (vector + graph hybrid) 不在** — `--expand-graph` は recall hit を graph 拡張するのみで、graph traversal 結果を vector 空間にも投影して二次 recall する hybrid 検索は Phase 8.x

## Open Questions

Phase 8 内で確定しない (Phase 8.x / 9 持ち越し):

1. **Connector-side automatic `SourceReferenced` 発行の per-connector parsing rule** — GitHub Issue body の `#123` parse は markdown / plain text 両対応か、`org/repo#123` cross-repo notation を扱うか / Slack message の URL parse は permalink のみ or 全 URL か / MS365 Calendar attendee → email match で entity 引きをどう実装するか / Box file metadata の何を見るか。Phase 8.x で connector ごとに mapper 設計
2. **`--expand-graph` の品質 validation** — graph 拡張で briefing / propose の品質 (operator 採用率 / hallucination 率 / latency) が実測で改善するか。Phase 8.x の運用 metric で評価し、結果次第で default on / off の議論
3. **`opshub graph serve` HTTP server + SVG / interactive visualisation** — CLI + DOT 出力では大規模 graph の俯瞰が困難。Phase 8.x で web UI / 別 ADR
4. **`opshub link list --include-deleted` 相当の CLI** — 「過去存在したが現在は削除された link」を CLI で 1 コマンドで出したい use case が出てきたら追加検討 (現状は events table 直接 query で代替)
5. **Multi-machine sync と `links` projection の関係** — principles.md §Open Q #5 (Multi-machine sync) は Phase 9 候補。Phase 9 で「`links` table が follower に複製されるのか / follower 側で events から再 derive するのか」を別 ADR で判断する必要がある (event log の sync で `links` を再 derive できるため後者が自然だが、large graph での initial rebuild コストとのトレードオフ)。本 ADR は §決定 (c) で「auto extraction は pure derived state」を pin したため、後者の選択肢が技術的に成立することは既に保証される
6. **link_type の version 管理 / migration policy** — link_type の semantic を変えたい (例: `references` を `references_via_body` / `references_via_metadata` に分割) 場合の migration policy。`link_type` 列に version suffix を入れるか、新 link_type を introduce して旧 link_type を deprecate するか。Phase 8.x で前例が出たら判断
7. **`metadata` JSON 列の schema 標準化** — Phase 8 MVP では `metadata` を free-form JSON blob とするが、link_type ごとに `metadata` の schema を Pydantic model で pin するか / link_type ごとに必要な fields を docs で標準化するか。Phase 8.x で 2-3 link_type の metadata 使用例が出たら検討

## Alternatives Considered

### 1. Multiple per-link_type tables (`applied_links` / `briefing_links` / `reference_links` / `manual_links`)

link_type ごとに専用 table を切り、link_type 列を不要にする (table 名で識別)。

却下理由:

- schema 量が増える (5 link_type で 5 table)。migration / backup / replay の対象が膨らむ (ADR-0002 single-source-of-truth と緊張)
- 各 table で同じ from/to index を重複して持つ必要があり (10 index)、disk / write amplification が増える
- bidirectional 全方向 traversal が UNION query になり SQL が複雑化 (`SELECT ... FROM applied_links UNION ALL SELECT ... FROM briefing_links UNION ALL ...`)
- 新 link_type 追加に毎回 migration が必要 (manual path の free-form link_type を吸収できない、§決定 (b) の柔軟性が失われる)
- 単一 table + WHERE clause filter の方が SQL が単純で query plan も 1 index lookup で済む

### 2. In-place migration of historical Candidate v1 → v2 when schema changes

links 自体の話ではないが、proposal Candidate schema が v1 → v2 に進化したときに過去 event を rewrite する案 (graph に影響する payload field を変更する場合)。

却下理由:

- ADR-0002 (Event-Sourced Architecture) の event immutability 原則違反。過去 event を rewrite すると audit / replay 整合性が崩れる
- ADR-0016 §決定 (f) で既に「`schema_version` literal + 両 version 読み分け、in-place migration なし」が pin されている。Phase 8 でも同じ pattern を `link_type` enum 拡張で踏襲する

### 3. Graph database (Neo4j embedded / SQLite graph extension)

専用 graph DB を採用する。Cypher のような graph query 言語が使え、traversal は宣言的に書ける。

却下理由:

- ADR-0001 (Python Stack) の配布制約: Neo4j embedded / SQLite graph extension は OS-specific binary を要求し、`uv tool install opshub` の core size 制約 (~10-50MB) を破る
- ADR-0002 (Event-Sourced Architecture) の「SQLite を単一 storage」原則違反。backup / replay 対象が増える
- Phase 8 MVP の graph 規模 (1000 task / 100 briefing / 50 proposal 程度) では SQLite + 2 index で十分な性能が出る前例 (Phase 4 sqlite-vec embedding と同じ判断)
- 将来 graph 規模が膨らんで SQLite limit に到達したら別 ADR で再判断する余地は残す

### 4. Graph を JSON column として各 entity row に materialize する

各 entity table (`tasks` / `decisions` / `briefings` / `proposals` / `sources` / `inbox_items`) に `outgoing_links: JSON` / `incoming_links: JSON` 列を追加し、entity row 取得時に link も同時に得られるようにする。

却下理由:

- state が分散して duplicate になる (`tasks.outgoing_links` + `decisions.incoming_links` で同じ link を 2 重に保持)
- bidirectional query (「from = X OR to = X」) が 1 SQL で書けず、6 entity table を UNION する必要が出る
- link 追加 / 削除で entity table を 2 つ (from 側 + to 側) UPDATE する必要があり、UPSERT semantics が複雑化
- 単一 `links` table 案より write amplification が高く、index も entity 数だけ重複する

### 5. Connector-side automatic extraction を Phase 8 MVP に含める

Phase 8 MVP scope に GitHub / Slack / MS365 / Box の body parse → `SourceReferenced` 自動発行 logic を含める。

却下理由:

- connector ごとに body parsing logic + test fixture が必要で、4 connector × parsing rule で 1 phase 分の作業量に膨らむ
- 自動抽出 logic を validate する前に運用上 link の構造を pin する manual baseline が無い状態で reactive に extraction rule を書くと、誤抽出 link で links table が汚染される
- 既存 4 経路 (`ProposalApplied` / `BriefingGenerated` / `ProposalRequested` / `SourceReferenced` の manual path 経由) で task → proposal → briefing → source の主要 chain は trace 可能。connector-side は incremental value で Phase 8.x で別 PR

### 6. Auto-extraction projector が `LinkCreated` event を派生発行する

`LinksExtractor` が既存 event を読んで `LinkCreated` event を自動派生発行し、Manual `link add` も `LinkCreated` event を発行する。`LinksProjector` は `LinkCreated` / `LinkDeleted` のみを消費すれば足りる (events 経路の統一)。

却下理由:

- event log に「同じ事実が 2 回記録される」状況になる: 元 event (例: `ProposalApplied`) + 派生 event (`LinkCreated`)。replay / audit / 「いつ何が起きたか」の trace が二重化されて、何が原因で何が結果か曖昧化
- ADR-0002 (Event-Sourced Architecture) の single source of truth 原則違反。events は state mutation の唯一の log であり、derived state は projection で表現するべき
- replay 時に「派生 event を再発行するか / skip するか」の判断が projector に必要になり、idempotency 保証が複雑化
- manual `link add` (operator が起こした state mutation) と auto-extracted link (event から derive) を event log で区別できなくなり、出自 trace が失われる

`LinksProjector` が 6 event family (`ProposalApplied` / `BriefingGenerated` / `ProposalRequested` / `SourceReferenced` / `LinkCreated` / `LinkDeleted`) を dispatch する複雑性は受け入れる (§決定 (c))。

## 関連

- [Principles 2 (Event-Sourced)](../principles.md) — auto-extraction projector が pure derived state である根拠
- [Principles 8 (Replayability)](../principles.md) — `projections rebuild` で historical event から links graph が完全再構築される根拠
- [Principles 9 (Phased Delivery)](../principles.md) — Phase 8 (Knowledge graph) の位置付け、§5 Multi-machine sync は Phase 9 候補で本 ADR では closeout しない
- [Architecture §2.10 Knowledge graph layer](../architecture.md) — Phase 8 E1 で追記済
- [ADR-0001: Python Stack](0001-python-stack.md) — SQLite 単一 storage 制約、Neo4j embedded を却下する根拠
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — auto extraction が新 event を発行しない pure derived state である根拠、`LinkCreated` / `LinkDeleted` を manual path で必須化する根拠、hard delete でも event log で trace が残る根拠
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — `links` projection は ID + link_type のみ持ち、body を含まない (External Content Minimization と整合)
- [ADR-0010: Connector Contract](0010-connector-contract.md) — connector-side `SourceReferenced` 自動発行を Phase 8.x に持ち越す根拠 (connector contract に body parse を加える場合の影響を別 phase で評価)
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md) — `--expand-graph` で graph 拡張 entity を LLM prompt に含める設計の前提 (RecallService が semantic recall の結果を返す経路)
- [ADR-0015: LLM Usage Strategy](0015-llm-usage-strategy.md) — `LinkDeleted.reason` の sanitise 経路 + `--expand-graph` が LLM 経路を変えない (Pluggable LLMClient signature 不変) ことの根拠
- [ADR-0016: Action Loop and Structured Output](0016-action-loop-and-structured-output.md) — `ProposalApplied.applied_entity_id` を link 派生元として使う前提、`schema_version` literal + 両 version 読み分けの pattern を本 ADR でも踏襲する根拠
- [Phase 8 Plan §1 (確定済み事項) + §2.1 (sub-issue A)](../phase-8-plan.md)
