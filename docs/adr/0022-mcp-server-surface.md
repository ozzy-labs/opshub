# 0022. MCP Server Surface

- Status: Accepted (revised 2026-06-07 for Phase 21-D: write-category `browser.fetch` の追加根拠を §決定 (g) で pin、surface 18 → 19 tools)
- Date: 2026-05-30 (initial); 2026-05-31 (Phase 12 H1 改訂: + `search` FTS5 + `propose.apply` HITL idempotent + 4 read tools 物理列ベース時間フィルタ + MCP 引数名 → projection 物理列 写像表追加); 2026-06-03 (Phase 18 補遺: 新 read tool `slack.demand.list` の追加根拠を §決定 (f) 末尾に追記。ADR-0033 で詳細を pin、5 不変条件は継承、本 ADR の write tool 集合 / annotation pattern は不変); 2026-06-07 (Phase 21-D 改訂: 新 write tool `browser.fetch` を §決定 (g) で pin。外部ネットワーク egress = write 分類で「read tool = ローカル DB のみ」不変条件を維持、5 不変条件は継承、surface 18 → 19 tools = 13 read + 6 write)
- Deciders: ozzy

## Context

Phase 10 (アシスタントエージェント・プラットフォーム化、epic #203) は opshub を「人間 → アシスタントエージェント → opshub コマンド」の三層モデルへ再定義する (Phase 10 plan §1 #2 / #3 / #3b)。CLI は残るが、アシスタントエージェント (Claude Code 等の外部ホスト = ②) が ①コア (events / projection / connectors / recall / propose / brief / graph) を叩く経路を別立てで設計する必要がある。ADR-0004 (Agent Runtime Boundary) は **形A** を確定済み — opshub は MCP サーバ (口) と Agent Skills (手順書) のみ提供し、頭脳 (LLM 推論ループ) は外部ホストが担う。本 ADR は形A の「MCP サーバ面」を確定し、Sub-issue D (Agent Skills) と Sub-issue E (返信下書き) が依存する agent-facing contract を pin する。

MCP (Model Context Protocol、Anthropic 2024) は agent ↔ tool 間の標準プロトコルで、stdio / Streamable HTTP / SSE 等の transport を仕様化している。opshub が MCP サーバとして①コアを露出する設計では、以下の論点が交差する。

第一の論点は **transport の選択** である。MCP spec は単一マシン上の local server 用途に stdio を、ネットワーク越しの remote server 用途に Streamable HTTP / SSE を規定している。HTTP transport を有効にすると **confused deputy (agent が opshub 経由で意図しない上流操作)**・**SSRF**・**session hijack** (Phase 10 plan §1 #2 OWASP ASI06 関連の調査結果) が攻撃面に乗る。stdio はソケットを開かないため、これらの攻撃が「ネットワーク経路がそもそも存在しない」ことで構造的に non-applicable になる。

第二の論点は **認証情報の境界** である。opshub は Slack / Box / GitHub / MS365 の SaaS トークンを ADR-0014 の keyring 経路で保持している。MCP tool の引数として SaaS トークンを受け取る (token passthrough) 設計は、トークンが LLM の context window・MCP tool 呼び出しログ・agent host の transcript に流れ込み、prompt injection や transcript 流出で漏洩する経路を作る。Anthropic の MCP security best practices (2025) も token passthrough を anti-pattern として明示している。

第三の論点は **read / write tool の分離と自律範囲** (Phase 10 plan §8 Open Q #3b) である。アシスタントエージェントが読み取り系 tool (recall / search / brief / task list) を自律実行するのは UX の根幹だが、書き込み系 tool (task create / inbox add / propose apply / connector sync) を auto-approve すると tool poisoning 攻撃で durable state が書き換えられる。tool poisoning 研究 (Invariant Labs 2025) は auto-approve 経路での攻撃成功率 84% に対し、human-in-the-loop 経路では <5% という非対称を示している。CLI と同等操作を tool 化する際に、この read / write 境界を tool schema レベルで宣言的に表現する必要がある。

第四の論点は **context 効率** である。MCP tool が full body / full payload を返すと agent context window が枯渇し、二次的に data exfiltration の攻撃面も拡大する (返した全文がそのまま transcript 経由で外部に流れうる)。recall (ADR-0012) と brief (ADR-0015) は既に要約・関連抽出で返す設計だが、MCP tool 全般の戻り値設計原則として明文化する必要がある。

第五の論点は **観測性と OTel naming** である。Phase 1-9 で確立した event-sourced log は ①コア内部の不変条件を保証するが、MCP tool 呼び出し (= ②から①への boundary 横断) は event log には載らない agent-facing operation である。OpenTelemetry GenAI Semantic Conventions (2024-2025) は `execute_tool` 等の span name を AI agent / tool ecosystem 共通で規定しており、将来 opt-in exporter で OTel に流す場合に naming を spec 準拠で揃えておくと、特殊ロガーを作らずに済む。

これらを踏まえ、本 ADR は MCP サーバを **stdio 一択・token passthrough 禁止・read/write 分離 (policy-as-data)・context 効率・OTel naming 準拠** の 5 不変条件で設計する。

## Decision

opshub に **MCP サーバ面**を新設する。新規 module `opshub.mcp` を置き、CLI と並列の agent-facing surface として ①コアの service / projection を露出する。設計を以下に pin する。

### (a) Transport は stdio 一択、ネットワーク listen 禁止

MCP サーバは **stdio transport のみ**を実装する。`opshub mcp serve` (本 ADR の C2 PR で追加) を起動すると stdin/stdout で MCP プロトコルを話し、agent host (Claude Code 等) が subprocess として spawn する。HTTP / SSE / WebSocket server は **実装しない** (不変条件)。

これにより:

- **confused deputy / SSRF / session hijack が構造的に non-applicable** — ソケットを開かないため、これらの remote attack 面が存在しない。
- **MCP spec の local-server 推奨と一致** — opshub は単一 operator・単一マシン上の operational memory (ADR-0002 / ADR-0003) なので、remote server 用途の HTTP transport は要件にない。
- **配布の単純化** — `opshub mcp serve` は OS daemon / network listener を作らず、agent host の lifecycle に従って起動・停止する (cron / systemd 等の常駐機構が不要、ADR-0004 と整合)。

remote multi-host 用途は Phase 10 では scope 外。将来の multi-machine sync (principles.md §Open Q #5) で改めて HTTP transport の要否を議論する場合も、その時点で別 ADR を立てる。

### (b) Token Passthrough 禁止 (認証情報非露出境界)

SaaS トークン (Slack / Box / GitHub / MS365 等) は MCP tool の **引数として受け取らない**。tool 引数 schema にトークン関連 field を一切定義しない (不変条件)。

opshub 内部では `opshub.core.secrets` (ADR-0014 keyring 経路) からトークンを取得し、connector 経路で SaaS を呼ぶ。MCP tool は「どの connector を sync するか」「どの source を取り込むか」だけを agent から受け取り、認証情報は ①コアの境界の内側に閉じる。

戻り値からも認証情報を **redact** する。具体的には:

- tool の output schema にトークン・access key・API key 等の field を含めない。
- connector error / debug log を tool 戻り値に流す場合、`OPSHUB_<provider>_TOKEN` / `Authorization: Bearer ...` 等のパターンを redact する (Phase 1 で `core/sanitise.py` が確立した redaction 経路を再利用)。

これにより MCP 面に SaaS トークンが露出しない (Phase 10 plan §C2 DoD 「認証情報を tool に露出しない境界」)。Anthropic MCP security best practices および MCP spec の "Token Passthrough is forbidden" 規定と一致する。

### (c) Read / Write tool 分離 = scope minimization (policy-as-data)

MCP tool を **read 系**と **write 系** (durable state を変える系) に明示的に分離する。境界は tool name の namespace と、tool annotation (MCP spec の `annotations.readOnlyHint` / `destructiveHint`) で **policy-as-data** として表現する。

#### Read 系 (自律実行 OK、agent が確認なしで叩いてよい)

- `recall.search` — ADR-0012 hybrid recall。
- `search.body` — Sub-issue B の本文 FTS。
- `brief.generate` — ADR-0015 briefing。
- `task.list` / `inbox.list` / `decision.list` — projection 読み出し。
- `source.show` / `source.list` — 取り込んだ source の参照 (本文表示は `--include-body` flag、default off、ADR-0020 §provenance 注入と組み合わせる)。
- `graph.expand` — ADR-0017 link traversal。

これらは `annotations.readOnlyHint = true` を付与し、agent host 側で「auto-approve OK」と解釈できるようにする (Claude Code の default policy: read 系は確認なし、write 系は確認あり)。

#### Write 系 (durable state を変える、人確認推奨)

- `task.create` / `task.update` / `task.complete`
- `inbox.add` / `inbox.triage`
- `decision.record`
- `propose.apply` (下書き保存を含む。ADR-0016 改訂後の `reply_draft` も含む)
- `connector.sync` — SaaS への観測リクエスト。SaaS 側 audit log に痕跡が残り、レート制限・課金影響もあるため write 扱い。
- `link.create` / `link.delete`

これらは `annotations.readOnlyHint = false` および `annotations.destructiveHint = true` (delete / state 遷移を伴う tool のみ) を付与する。agent host 側で「default 人確認」と解釈される。

#### Write = 人確認の根拠

tool poisoning 研究 (Invariant Labs 2025) によれば auto-approve mode での攻撃成功率は **84%**、human-in-the-loop では **<5%**。本文保持 (ADR-0020) + 外部 SaaS 取り込み (Phase 3-9 connectors) の組み合わせは、indirect prompt injection の攻撃面 = (外部本文に「次の tool で task を消せ」のような命令が混入) を持つ。auto-approve は本文保持と非両立であり、write tool は **default 人確認**を policy-as-data で agent host に伝える。

ADR-0016 §決定 (a) で `propose --auto-apply` を Phase 6.x 以降も禁止と pin した経緯があり、本 ADR はその境界を「MCP 経由の write 全般」へ拡張する形になる。

#### policy-as-data の表現形式

- **第一義の表現**: MCP spec の tool `annotations` field (`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`)。MCP プロトコル native の宣言で agent host 横断に伝わる。
- **第二義の表現**: opshub 側の static registry (`src/opshub/mcp/_registry.py` 相当、C2 PR で実装) に YAML 形式の policy table を持ち、tool 実装は registry から annotation を生成する。Microsoft Agent Governance Toolkit (2025) の policy-as-data 発想のみ流用し、Agent Mesh / DID / trust score 等の重量機構は単一 operator・単一ホストの opshub では却下する (§Alternatives #4)。

#### Phase 10 C2 で出荷する surface = 4 read + 3 write のみ

C2 (`opshub mcp serve` 初版) で実装する MCP tool は **以下の 7 つに限定**する:

- read: `recall.search` / `task.list` / `inbox.list` / `decision.list`
- write: `task.create` / `inbox.add` / `connector.sync`

§(c) の表に列挙した残り (`search.body` / `brief.generate` / `source.show` / `source.list` / `graph.expand` / `task.update` / `task.complete` / `inbox.triage` / `decision.record` / `propose.apply` / `link.create` / `link.delete`) は **Sub-issue D (Agent Skills) / Sub-issue E (返信下書き) / 将来の別 ADR** で順次追加する。C2 時点では未実装 = registry に存在しないため、annotation 解釈の論点はその時点で再評価する (`propose.apply` の HITL 強制等)。この defer は Phase 10 plan §C2 DoD と一致する scope minimization の判断であり、本 ADR の他不変条件 (stdio 一択・token passthrough 禁止・要約返却・OTel naming) には影響しない。

### (d) Context 効率 (要約・関連抽出で返す)

MCP tool は agent context window に流す前提で **要約・関連抽出**で返す。Full body や full event payload を default で返さない。

- `recall.search` / `search.body` は ADR-0012 hybrid recall の top-N + snippet (本文の関連断片) を返す。`include_full_body` flag は **default false**、true 指定時のみ full body を返す (本文持ち回しは agent host の明示的選択)。
- `brief.generate` は ADR-0015 briefing の markdown 出力を返す。
- `source.show` は metadata + summary を default、full body は明示的 flag。
- 戻り値 schema に `truncated: bool` / `next_offset: int` 等の pagination hint を設け、agent が必要に応じて follow-up tool 呼び出しで深掘りできる構造にする。

二利益: (1) LLM context 圧縮で agent 動作の安定性、(2) data exfiltration 面縮小 (返した内容が transcript 経由で外部に流れる経路を持つ agent host で、流れる本文量を構造的に削減)。

### (e) OpenTelemetry GenAI naming 準拠の event 記録

MCP tool 呼び出し (= ②から①への boundary 横断) を **OpenTelemetry GenAI Semantic Conventions** の naming で記録する。

- Tool 実行 span name: `execute_tool` (OTel GenAI spec 準拠)。
- Span attributes: `gen_ai.tool.name` / `gen_ai.tool.call.id` / `gen_ai.operation.name = "execute_tool"`。
- Event log: opshub 本体の event log (`events` table) には MCP 呼び出しを **載せない** (event log は ①コアの不変条件 = durable state 遷移を記録する場で、MCP boundary trace は別カテゴリ)。代わりに structlog (`opshub.core.logging`) に JSON 形式で出力する。

**フル計装はしない、opt-in exporter のみ**。opshub 本体は OTel SDK を core dependency にしない (ADR-0001 配布制約)。OTel exporter は `[project.optional-dependencies]` の `mcp-otel` extras (本 ADR の C2 PR では未実装、将来の opt-in 経路として予約) に隔離する。

これにより、将来 OTel collector に流したい operator は `opshub[mcp-otel]` を install して env var で endpoint を指定すれば動く形を予約しつつ、default では structlog の JSON ログのみで完結する。

### (f) Phase 12 H1 surface 拡張 (2026-05-31 改訂)

Phase 12 H1 (`docs/phase-12-plan.md` §3 H1-b) で MCP surface を 7 (Phase 10 C2) + 8 (Step 1 widening PR #231) → **+ 4 追加** に広げる。すべて既存の 5 不変条件 (stdio 一択 / token passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) は維持する。

#### (f-1) `search` (新規 read tool、`ReadCategory.SEARCH`)

- 既存 `opshub.services.search_service.SearchService.search` を MCP に露出。FTS5 `sources_fts` 仮想表に MATCH を投げ、`sources` projection に join 戻して title / url / summary を返す
- **phrase quote default** で、CLI `opshub search --raw-query` の `raw_query` flag は **MCP schema から除外**する。host LLM が free-form token streams を投げても FTS5 syntax 文字 (括弧 / コロン等) のエスケープ不要 (CLI 経由の操作者は power-user 想定で生 syntax を許容、MCP 経由の host LLM は安全側に倒す)
- annotation: `readOnlyHint=true, destructiveHint=false, idempotent=true, openWorldHint=false` (`recall.search` と同パターン、SQLite ローカルのみ)
- Tier 1 skill `find-document` が直接呼ぶ surface

#### (f-2) `propose.apply` (新規 HITL write tool、`WriteCategory.PROPOSE_APPLY`)

- 既存 `opshub.services.proposals.ProposalService.apply` を MCP に露出。`(proposal_id, candidate_index)` を受け取り、`TaskCreated` / `DecisionRecorded` / `ReplyDraftApplied` 系の durable state 遷移 + `ProposalApplied` event を発行
- annotation: **`readOnlyHint=false, destructiveHint=false, idempotent=true, openWorldHint=false`**。`destructive=false` は本 ADR 史上初の write 例外。**handler 層で `OpsHubError("already applied/rejected")` を catch → `{ok:true, already_applied:true, applied_entity_type, applied_entity_id}` に正規化**することで idempotent semantics を成立させる。具体的には event log を `aggregate_id=proposal_id` + `event_type=ProposalApplied` で scan して該当 `candidate_index` の `applied_entity_*` を取り出し、初回 apply と同一 payload を返す
- `read_only=false` は維持 — 初回 apply は durable event を append するため、host が `readOnlyHint=false` で人確認を出す既存の境界は変えない
- `already rejected` および unrelated `OpsHubError` (not found / index 範囲外) は正規化せず MCP `isError` として上位に伝播
- Tier 1 skill `reply-draft` (および Phase 12 H4 で追加される `inbox-triage` / `source-extract` / `meeting-followup`) が直接呼ぶ surface

#### (f-3) 既存 4 read tools の入力 schema 拡張 (physical-column ベース時間フィルタ)

`task.list` / `inbox.list` / `decision.list` / `source.list` の入力 schema に **物理列ベースの独立した時間フィルタ argument** を追加する。tool 別に名前を分けることで、operator (host LLM) が「どの projection の物理列を絞っているか」を意識せざるをえなくする。これは「business 時間 vs 物理列」の混線（例: task の `completed_at` を projection が持たないのに `completed_after` を作ると mismatch する）を構造的に避ける。

| MCP tool | argument 名 | 対応 projection 物理列 | 区間 |
|---|---|---|---|
| `task.list` | `updated_after` / `updated_before` | `tasks.updated_at` | 半開 (`>= after` / `< before`) |
| `inbox.list` | `created_after` / `created_before` | `inbox_items.created_at` | 半開 |
| `decision.list` | `recorded_after` / `recorded_before` | `decisions.recorded_at` | 半開 |
| `source.list` | `observed_after` / `observed_before` | `sources.observed_at` | 半開 |

- 値: ISO 8601 `date-time` string (`Z` suffix 受け付け)。optional、両方とも省略すれば従来通り全件返す
- annotation 変化なし — フィルタ拡張は schema 拡張のみで、`readOnlyHint=true` などのポリシーは Phase 10 C2 のまま
- `decision.list` は ADR-0002 (event-sourced immutability) で immutable のため `recorded_at` のみ。`task.list` の "completed_after" のような business 概念列は projection に存在しないため意図的に非露出 (operator が必要なら `state=completed` + `updated_after` を組み合わせる、`docs/skills/personal-brief/SKILL.md` がその擬似コードを示す)

#### Phase 12 H1 後の surface 一覧 (合計 17 = 12 read + 5 write)

read (12): `recall.search` / `task.list` / `inbox.list` / `decision.list` / `brief` / `graph.related` / `graph.trace` / `graph.expand` / `source.list` / `source.get` / `embeddings.find_duplicates` / **`search`** (Phase 12 H1)

write (5): `task.create` / `inbox.add` / `connector.sync` / `propose.generate` / **`propose.apply`** (Phase 12 H1) — `propose.generate` と `propose.apply` を 1 pair として読むと 4 write 系列

#### Phase 12 H1 で導入される invariant

- **`destructive=false` write の policy guard**: `tests/unit/mcp/test_registry_policy` で `_NON_DESTRUCTIVE_WRITES = frozenset({"propose.apply"})` 集合を明示的に carve-out し、それ以外の write は `destructive=true` を強制継続。将来 `destructive=false` write を増やすには本 ADR §(f-2) 改訂と guard 集合への追加の両方が必須
- **`search` schema に `raw_query` 不在**: `test_search_does_not_expose_raw_query` が input_schema の properties を点検。CLI の `--raw-query` は power-user 経路として残るが MCP 面には出さない
- **時間フィルタの物理列写像**: `test_list_tools_expose_physical_column_time_filters` が tool → (`*_after`, `*_before`) の対応表を pin。drift は即時 fail

#### Phase 18 補遺 (`slack.demand.list` の追加根拠)

[ADR-0033: Slack Mention / DM Demand Digest](0033-slack-mention-demand-digest.md) で
新 read tool `slack.demand.list` を本 ADR の MCP surface に追加することを pin した
(Phase 18 [#426](https://github.com/ozzy-labs/opshub/issues/426))。本 ADR の 5 不変条件
(stdio 一択 / token passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) は
すべて継承する。

- 新 read tool として `ReadCategory` 系列に追加 — annotation は `readOnlyHint=true,
  destructiveHint=false, idempotent=true, openWorldHint=false` (`recall.search` /
  `search` と同パターン、SQLite ローカル `slack_demand_digest` projection 読み取り
  のみ)
- 入力 `types` argument は `slack_demand_digest.demand_kind` 物理列 (`{"mention",
  "dm", "mpim"}`) に直接写像する (本 ADR §決定 (f-3) 物理列ベース時間フィルタと同方針
  = MCP 引数を projection 物理列に最短で写像する原則の延長)
- 入力 `since` argument は `slack_demand_digest.last_demand_ts` 物理列に写像 (半開区間)
- 実装 PR は Phase 18-C ([#430](https://github.com/ozzy-labs/opshub/issues/430)) で
  `src/opshub/mcp/_registry.py` に登録し、register policy guard test を更新する

### (g) Phase 21-D surface 拡張 — `browser.fetch` (write-category、2026-06-07 改訂)

Phase 21 ([epic #504](https://github.com/ozzy-labs/opshub/issues/504)、[ADR-0037: Browser Read Layer via Playwright](0037-browser-read-layer-playwright.md)) で Playwright ベースのブラウザ読み取り層を新設したのに合わせ、アシスタントエージェントが ad-hoc に Web ページを読む手段として新 tool `browser.fetch` を本 ADR の MCP surface に追加する (Phase 21-D [#508](https://github.com/ozzy-labs/opshub/issues/508))。本 ADR の 5 不変条件 (stdio 一択 / token passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) はすべて継承する。

#### (g-1) `browser.fetch` (新規 write tool、`WriteCategory.BROWSER_FETCH`)

- `url` を受け取り、headless Chromium で render した後の DOM text + `<title>` を返す。**persist なし** = ad-hoc read (durable な `SourceObserved` 取り込みは Phase 21-C `web` connector の責務、本 tool は event log / projection に何も書かない)
- annotation: **`readOnlyHint=false, destructiveHint=true, idempotentHint=false, openWorldHint=true`** (`connector.sync` と同パターン)
- **write 分類の根拠 = 「read tool = ローカル DB のみ」不変条件の維持**: 本 tool は data を返すだけで local state を変えないが、**外部ネットワークに egress する** (ADR-0037 §決定 (e))。read tool 群はすべて SQLite ローカルのみを叩く (`open_world=false`) という既存の境界を崩さないため、ネットワークに出る tool は `connector.sync` と同じ write bucket に置く。根拠は二つ: (1) remote 側の rate limit / audit log に痕跡が残る観測可能な副作用 (`connector.sync` の write 分類と同論理)、(2) 取得した Web 本文が indirect prompt injection の運搬路になりうるため、auto-approve せず host に人確認させる (§決定 (c) tool poisoning 84% vs <5%)
- `destructive=true` は `connector.sync` と同じ「観測可能な上流副作用」の意味であり、local data を削除する意味ではない (`propose.apply` の `destructive=false` carve-out には**該当しない** = `_NON_DESTRUCTIVE_WRITES` に追加しない)
- 入力 schema は `url` のみ (required)。token は受けない (§決定 (b))。scheme は `http` / `https` のみ許容し、`file` / `data` / `ftp` / `javascript` 等は handler 層で `OpsHubError` 拒否する (headless browser を local file 読み出しの踏み台にしない安全ゲート)。`headless` / `timeout` 等の per-call override は出さず `[browser]` settings から解決する (MCP 面を最小に保つ)
- 戻り値は §決定 (d) の context 効率に従い text を snippet truncate (200 char、read tool 群と同 cap) して返し、`text_chars` (render 後の全長) / `truncated` (browser core の 500K cap hit) / `persisted: false` を hint として添える。full body は MCP 境界を越えない (必要時は `web` connector + `source.get`)
- async handler は **`asyncio.to_thread` 経由で sync browser core (`opshub.browser.core.fetch_page`) を呼ぶ** (Playwright sync API は asyncio loop 内で直接呼べず raise する、ADR-0037 §決定 (h))。出力は §決定 (b) の redaction net (`opshub.mcp._redact.redact_secrets`) を通る (取得 Web 本文に混入した token shape を scrub)
- 実装 PR は Phase 21-D ([#508](https://github.com/ozzy-labs/opshub/issues/508)) で `src/opshub/mcp/_registry.py` に登録し、registry policy guard test を 18 → 19 tools に更新する

#### Phase 21-D 後の surface 一覧 (合計 19 = 13 read + 6 write)

read (13): `recall.search` / `task.list` / `inbox.list` / `decision.list` / `brief` / `graph.related` / `graph.trace` / `graph.expand` / `source.list` / `source.get` / `embeddings.find_duplicates` / `search` / `slack.demand.list`

write (6): `task.create` / `inbox.add` / `connector.sync` / `propose.generate` / `propose.apply` / **`browser.fetch`** (Phase 21-D)

#### Phase 21-D で導入される invariant

- **`browser.fetch` = open-world write の policy guard**: `tests/unit/mcp/test_registry_policy` の `test_browser_fetch_is_open_world_write` が `readOnlyHint=false` + `destructiveHint=true` + `openWorldHint=true` を pin し、かつ `_NON_DESTRUCTIVE_WRITES` carve-out に**含まれない**ことを assert する。`browser.fetch` を read tool に reclassify する regression (host が network fetch を auto-approve してしまう) は即時 fail
- **surface count pin**: `test_registry_surface_is_nineteen_tools` が 19 tools = 13 read + 6 write を pin。tool の追加 / 削除は count split で fail
- **scheme 安全ゲート**: handler の unit test (`tests/unit/mcp/test_browser_fetch_handler`) が `file` / `data` / `javascript` / host-less URL を `OpsHubError` 拒否することを pin

## Consequences

### Positive

1. **構造的なネットワーク攻撃面ゼロ** — stdio 一択により confused deputy / SSRF / session hijack が non-applicable。Phase 10 で本文保持に転換するタイミングで攻撃面を拡大しない。
2. **認証情報の境界が明確** — SaaS トークンが MCP 面に露出せず、prompt injection / transcript 流出経路から構造的に外れる。
3. **read 自律 / write 人確認の宣言性** — agent host が tool annotation だけで policy を解釈でき、opshub 側で「どの operation を auto-approve するか」の判断を agent host に押し出さない (ADR-0004 形A の境界 = ②が①の policy を解釈する、と整合)。
4. **context 圧縮による安定性** — agent 動作が full body 持ち回りで不安定化せず、`include_full_body` flag による明示的選択にできる。
5. **OTel 準拠で観測性が拡張可能** — 将来 OTel collector に流す場合も特殊 naming を作らずに済み、AI agent ecosystem の他 tool と相互運用できる。
6. **形A の口の確定** — Sub-issue D (Agent Skills) と Sub-issue E (返信下書き) が依存する agent-facing contract が明確になり、後段の実装が並列化可能。

### Negative / Trade-offs

1. **multi-host 用途は将来検討** — stdio 限定により、別マシンの agent host から remote opshub を叩く用途は本 ADR では実現しない。
   - 緩和: principles.md §Open Q #5 (multi-machine sync) と一緒に将来別 ADR で議論する。pre-userbase の現段階では multi-host 要件が存在しない。
2. **agent host の policy 解釈に依存** — read / write 境界を annotation で宣言しても、agent host が annotation を honor しなければ意味がない。
   - 緩和: opshub 側でも write tool は呼び出し時に「本当に実行してよいか」のテキスト確認を返す経路を後段で予約 (Sub-issue D で host-side skill 設計と組み合わせる)。当面は Claude Code 等の主要 agent host が annotation を honor する前提に立つ。
3. **`opshub mcp serve` の lifecycle が agent host 依存** — subprocess として spawn される設計上、agent host が終了すると MCP サーバも落ちる (常駐しない)。
   - これは ADR-0004 形A の「opshub 自身は runtime を持たない」と整合した挙動であり、Negative ではなく設計通り。記録のため明記。
4. **MCP SDK の依存追加** — Python MCP SDK (`mcp` package、Anthropic 公式) を extras に追加する。
   - 緩和: `[project.optional-dependencies]` の `mcp` extras に隔離 (ADR-0001 配布制約)。core install は影響なし。
5. **CLI と MCP の二重メンテ** — 同じ ①コア操作が CLI command と MCP tool の 2 経路で露出するため、新 command 追加時に両方に追加する手間。
   - 緩和: 両者は ①コア (service / projection) を共通呼び出しするため、新 command の thin layer 追加のみ。MCP tool registry を SSOT 化して CLI と並列に走らせる (実装は C2 PR で確定)。

## Alternatives Considered

### 1. HTTP / Streamable HTTP transport も並列にサポート

却下理由:

- ネットワーク攻撃面 (confused deputy / SSRF / session hijack) が乗り、本文保持 (ADR-0020) で増えた機密データの露出 risk と非両立。
- opshub は単一 operator・単一マシン (ADR-0002 / ADR-0003) で multi-host 要件が現時点で存在しない。
- 将来 multi-machine sync (principles.md §Open Q #5) が要件化された時点で別 ADR を立てれば足り、Phase 10 で先取りすると stdio に閉じれば不要だった攻撃面緩和コードを抱え込む。

### 2. Token Passthrough を許容 (tool 引数で SaaS トークンを受ける)

却下理由:

- トークンが LLM context window / MCP tool 呼び出しログ / agent host transcript に流れ込み、prompt injection / transcript 流出経路で漏洩する。
- Anthropic MCP security best practices および MCP spec の "Token Passthrough is forbidden" 規定に反する。
- opshub は ADR-0014 で既に keyring 経由のトークン管理を確立済みで、MCP 面で別経路を作る必要がない。

### 3. read / write tool 区別なし (全 tool を同一 namespace + 同一 annotation で expose)

却下理由:

- tool poisoning auto-approve 攻撃成功率 84% vs human-in-the-loop <5% の非対称が反映されず、本文保持 (ADR-0020) で拡大した indirect prompt injection 面が durable state 改変に直結する。
- agent host が「どの tool を確認なしで叩いてよいか」を判断する手がかりがなくなり、安全側に倒すと全 tool が確認必須 = UX 崩壊、緩い側に倒すと全 tool auto-approve = 攻撃面最大化。
- 宣言的 read/write 境界 (annotation) で agent host に判断を委ねる設計が、Phase 10 形A (頭脳は外部ホスト) と整合する。

### 4. Microsoft Agent Governance Toolkit 全体 / Agent Mesh / DID / trust score を導入

却下理由:

- opshub は **単一 operator・単一マシン**の operational memory (ADR-0002 / ADR-0003) で、multi-agent mesh / 分散 trust 機構の前提が成立しない。
- Agent Mesh / DID は agent 間で trust を協調する仕組みで、単一ホストでは過剰機構 (ADR-0001 配布制約とも非両立)。
- policy-as-data の発想 (annotation で宣言する) のみ流用し、重量機構は将来 multi-host 化したタイミングで再評価する。

### 5. Tool poisoning 緩和を agent host 側に全任せ (opshub は何もしない)

却下理由:

- annotation を honor しない agent host で write tool が auto-approve され、durable state が破壊される経路を opshub 側で一切防げない。
- 「形A = 頭脳は外部」だが、「①コアの境界を ①コア側で守る」のは ADR-0004 で確立済みの責務 (CLI / service 層の validation と同じ)。
- policy-as-data を opshub 側で宣言し、agent host が honor しなかった場合の被害を後段で最小化する経路 (本文 redaction / confirmation prompt / dry-run 経路) を予約する設計が安全。

### 6. MCP tool を CLI command 1:1 で機械生成 (低レベル粒度)

却下理由:

- CLI は人間が叩く前提で sub-verb / flag が細かく、agent が組み合わせを学習する負荷が高い。
- アシスタントユースケース (「今日やること」「返信案」) と粒度がずれ、複数 tool を agent が逐次呼ぶオーバーヘッドが大きい。
- 採用: **読み取り系は CLI 同等の粒度 (recall / search / brief) + アシスタントユースケース粒度の compound tool は Sub-issue D の Agent Skills で表現**する二段構え。MCP tool は ①コア operation を直接露出し、ユースケース粒度の組み立ては Skills の手順書で行う (Phase 10 plan §C / §D 整合)。

### 7. MCP tool 呼び出しを opshub event log に append

却下理由:

- event log は ①コアの durable state 遷移を記録する SSOT (ADR-0002) で、②から①への boundary trace を混ぜると event semantics が二重化する (durable state 遷移 + agent activity log)。
- replay 時に MCP 呼び出し event を「実行する」のか「skip する」のかが曖昧になり、event-sourced replayability が崩れる。
- 採用: structlog の JSON ログに OTel GenAI naming で出力し、event log は durable state 遷移のみに保つ。OTel exporter は opt-in extras で将来予約。

## 関連

- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md) — 形A の確定元。本 ADR は ADR-0004 が指す「opshub が提供する MCP サーバ面」の中身を pin する。
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — Token Passthrough 禁止 (§(b)) の前提となる keyring 経路。
- [ADR-0016: Action Loop and Structured Output](0016-action-loop-and-structured-output.md) — `propose --auto-apply` 禁止の経緯。本 ADR の write = 人確認境界 (§(c)) はその拡張。
- [ADR-0020: Full Local Content Retention](0020-full-local-content-retention.md) — 本文保持で拡大した indirect prompt injection 面が write 人確認の根拠 (§(c)) の一つ。
- [ADR-0021: Encryption at Rest](0021-encryption-at-rest.md) — 保存時暗号化と組み合わせ、MCP 面での本文露出を context 効率 (§(d)) で構造的に縮小する。
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — MCP 呼び出しを event log に載せない (§Alternatives #7) 根拠。
- [ADR-0001: Python Stack](0001-python-stack.md) — `mcp` extras / `mcp-otel` extras を core dependency にしない配布制約。
- [Phase 10 Plan §3 Sub-issue C / §4-C / §8 Open Q #3 / #3b](../phase-10-plan.md) — 本 ADR が確定する論点の起票元。
- [ADR-0031: CLI Command Surface Organization](0031-cli-command-surface-organization.md) — CLI 表面 (top-level group の組織方針) は ADR-0031 で確定。CLI と MCP は **並列の agent-facing surface** で、本 ADR が pin する MCP tool surface (stdio / token passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) は CLI 表面再編とは独立で **不変** (MCP tool 名 `recall.search` / `task.create` / `connector.sync` 等は ADR-0031 の CLI 再編に伴って変更しない)。
- [ADR-0033: Slack Mention / DM Demand Digest](0033-slack-mention-demand-digest.md) — Phase 18 で本 ADR の MCP surface に新 read tool `slack.demand.list` を追加する根拠。本 ADR §決定 (f) 補遺 (Phase 18) で詳細を pin。5 不変条件は継承、新規 invariant の追加なし (write tool 集合に変化なし、annotation pattern は既存 read tool と同)。
- [ADR-0037: Browser Read Layer via Playwright](0037-browser-read-layer-playwright.md) — Phase 21 で追加した `browser.fetch` は外部ネットワークに egress するため **write-category** (HITL per call、`connector.sync` と同整理、本 ADR §決定 (c))。「read tool = ローカル DB のみ」不変条件は維持される。正式 surface 追加 (tool 数 18 → 19) + registry policy pin test 更新は Phase 21-D (#508) で本 ADR §決定 (g) として着地した。
