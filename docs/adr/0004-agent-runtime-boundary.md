# 0004. Agent Runtime Boundary

- Status: Accepted (revised 2026-05-30 for Phase 10 form-A absorption)
- Date: 2026-05-16 (initial); 2026-05-30 (Phase 10 §(e) form-A revision: MCP as authorised write path, opshub holds no agent runtime, Agent Skills distributed via `ozzy-labs/skills` preset)
- Deciders: ozzy

## Context

OpsHub は Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI を同時並行で利用することを前提とする。複数 agent が共有 Operational Memory に対して同時に操作するため、以下が必要となる。

1. **auditability** — 誰 (どの agent) がいつ何を変更したか追跡可能
2. **replayability** — agent の操作を後から再現可能
3. **safety** — silent destructive operation を防ぐ
4. **validation** — 不正な状態遷移 (例: Completed → Active) を弾く
5. **coordination** — 複数 agent の同時変更を競合解決
6. **rebuildability** — 任意の時点の Operational Memory を再構築可能

agent が直接 DB を書き換えると、上記のすべてが壊れる。具体的には:

- SQL UPDATE が agent run log を経由しない (audit 欠落)
- markdown 直編集が event を生成しない (replay 不能)
- 複数 agent の同時 UPDATE で lock なしの race condition が発生
- 不正 schema の event 列が混入し projection rebuild が失敗

### Phase 10 で追加された論点 (2026-05-30 改訂理由)

Phase 10 (秘書エージェント・プラットフォーム化、epic #203) は opshub を「人間 → 秘書エージェント (②) → opshub コマンド (①)」の三層モデルへ再定義する (Phase 10 plan §1 #2 / #3 / #3b)。この改訂で本 ADR の境界に 3 つの論点が追加される。

第一に、**②秘書エージェントの runtime をどこに置くか**。並列調査 (Phase 10 plan §10) で確認したとおり、エージェント runtime レイヤ (LangGraph / OpenAI Agents SDK / Claude Agent SDK / Microsoft Agent Framework / CrewAI / AgentScope) は資本と本番実績で固まりつつあり、後発が勝つ領域ではない。一方 MCP は Linux Foundation 配下に移管され事実上の標準となっており、調査した全フレームワークが MCP クライアントである。したがって opshub は **「runtime を持たず、MCP サーバ (口) と Agent Skills (手順書) のみ提供する」= 形A** を採る。頭脳 (LLM 推論ループ) は Claude Code 等の外部エージェントホストが担い、opshub 自身はエージェント runtime を実装しない。

第二に、**MCP を「agent が書き込んでよい認可経路」として CLI / Application service と並列に位置づける**必要がある。本 ADR の旧版 (2026-05-16) は agent の書き込み経路として CLI / Application service / Repository / JSON patch proposal を列挙していたが、これは Phase 1-9 の CLI-first MVP (ADR-0006) の枠で「MCP は明確な需要が出るまで延期」と判断した時点の整理である。Phase 10 で MCP サーバ面が ADR-0022 で確定し、形A の中核経路として位置づけられた以上、本 ADR でも MCP を agent の正規の書き込み経路として明示する必要がある。なお ADR-0022 §決定 (c) は MCP tool を read 系 (自律 OK) と write 系 (人確認推奨) に分離する **policy-as-data** を確定済みで、本 ADR の auditability / safety 不変条件は MCP 経路でも `read/write annotation` と `agent_runs` event 記録によって保たれる。

第三に、**Agent Skills の配布をどこに置くか** (Phase 10 plan §8 Open Q #4)。秘書 5 Skill (daily-brief / next-actions / reply-draft / pr-review / file-lookup) は SKILL.md 標準 (Anthropic Agent Skills 形式) で書かれ、外部ホスト (Claude Code 等) の `.claude/skills/` 配下に置かれる資産である。opshub 本体に同梱すると skill のライフサイクル (ホスト側の skill loader / 配布タイミング / 複数ホスト間の同期) が ①コアと結合し、ozzy-labs エコシステム共通の skill 配布機構 (handbook ADR-0016 `ozzy-labs/skills` preset 経路) と二重化する。したがって本 ADR では Agent Skills を **`ozzy-labs/skills` preset 配布** と確定する。opshub 本体リポでは skill の **仕様・リファレンス・catalog** (例: `docs/secretary-agent.md` および将来の `docs/skills/`) のみを保持し、SKILL.md の実体は `ozzy-labs/skills` 側に置く。

## Decision

Agent は Operational DB / Workspace を **直接変更しない**。すべての書き込みは以下を経由する。

1. **`opshub` CLI** — 標準経路。`opshub task create` / `opshub event append` / `opshub note save` 等
2. **MCP server (`opshub mcp serve`)** — agent host 用標準経路 (Phase 10 で追加、ADR-0022)。stdio transport で agent host (Claude Code 等) が subprocess として spawn し、`recall.search` / `task.create` / `propose.apply` 等の tool 経由で①コア service を叩く。read / write は `annotations.readOnlyHint` / `destructiveHint` で分離し、write は agent host 側で人確認を促す (ADR-0022 §決定 (c))
3. **Application service** — Python から直接呼ぶ場合の経路 (内部利用のみ)
4. **Repository** — service 内部で DB アクセスを集約する層
5. **JSON patch proposal** — agent が提案 → 人間が承認 (Phase 6 で `opshub propose` として確立、ADR-0016)

agent が以下を行うことは禁止する。

- 直接 `sqlite3` で `events` / projection tables を書き換える
- `generated/` 配下の markdown を直接編集する
- application service / CLI / MCP を介さず `events` ファイルや `db.sqlite` を操作する
- audit log / agent_runs テーブルへの書き込みを bypass する

CI / lefthook で次を検出する。

- agent が `sqlite3` / DB クライアントを Bash 経由で呼んでいないか (settings.json の permissions で deny)
- `generated/` への直接 write が発生していないか (`workspace doctor` で検出)
- `agent_runs` を経由しない event が存在しないか (Phase 2+ で実装)

### (a) 形A: opshub は MCP + Agent Skills のみ、runtime を持たない (Phase 10 で追加)

opshub は **エージェント runtime を持たない**。具体的には:

- LLM 推論ループ (think → act → observe の反復) を opshub 内に実装しない
- ReAct / Plan-and-Execute / Reflexion / LangGraph state machine 等の agent loop runner を opshub package に含めない
- 常駐 daemon としての agent process (always-on agent) を起動しない (能動性は Phase 10 plan §1 #5 のとおり当面持たない)

opshub が提供する agent-facing surface は次の 2 つに限定する。

1. **MCP サーバ面 (口)** — `opshub mcp serve` (stdio、ADR-0022)。外部ホストの agent runtime が ①コアを叩く経路
2. **Agent Skills (手順書)** — SKILL.md 標準 (Anthropic Agent Skills 形式)。`ozzy-labs/skills` 経由で配布され、外部ホストの `.claude/skills/` に置かれる薄い手順書。本文 ≤5k tokens、L3 = MCP tool / CLI 呼び出し (progressive disclosure、context 消費ゼロ)

頭脳 (LLM 推論ループ) は **外部エージェントホスト** (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI / 将来追加されるホスト) が担う。opshub は「外部ホストが ①コアを叩くための口と手順書」のみを提供する。

これにより:

- **②→① boundary が明確化** — ②外部ホストは①を MCP 経由でのみ叩く (Python module を直接 import しない)。secretary agent の常駐コード・能動コード・状態持ちコードが ①コア (`src/opshub/`) に混入しないことが構造的に保証される
- **opshub のスコープが固定** — 「operational memory + connector + service + CLI + MCP + skill spec」に限定され、runtime 選定の vendor lock-in を回避 (ADR-0009 multi-agent neutrality と整合)
- **runtime 競争に巻き込まれない** — LangGraph 等の runtime レイヤは資本投下と本番実績で固まる領域であり、opshub は競合せず補完的に位置づける (Phase 10 plan §10)
- **形A は能動性 (Phase 10 plan §1 #5) と緊張しない** — 将来能動機能を追加する場合も、トリガと常駐は OS cron / systemd timer / launchd / Win タスク / 外部 sidecar に外出しし、生成ロジックと派生 artifact は ①コア内に置き、承認は既存 inbox / proposals 経路を使う。常駐 daemon を ①コアに持たない原則は維持される

### (b) MCP を agent の正規の書き込み経路として並列追加 (Phase 10 で追加)

MCP サーバ (`opshub mcp serve`、ADR-0022) を agent の正規の書き込み経路として CLI / Application service と並列に位置づける。本 ADR §決定の書き込み経路一覧 (上述 1〜5) に MCP を 2 番目として明示済み。

MCP 経路でも以下の不変条件は保たれる:

- **auditability** — MCP tool 呼び出しは structlog の JSON ログに OpenTelemetry GenAI naming (`execute_tool`、ADR-0022 §決定 (e)) で出力される。durable state を変える write tool は ①コアの既存 service (TaskService / DecisionService / ProposalService 等) を経由し、`agent_runs` + `events` への append は service 内で行われる
- **safety** — read / write は `annotations.readOnlyHint` / `destructiveHint` で宣言的に分離され、write tool は agent host 側で人確認 (HITL) を促す (ADR-0022 §決定 (c))。tool poisoning 攻撃面 (auto-approve 成功率 84% vs HITL <5%) を構造的に縮減
- **validation** — MCP tool の入出力 schema は Pydantic v2 model を SSOT として provider-native の MCP tool schema に wrap される (ADR-0016 §決定 (b) と同パターン)。schema 違反は ①コア service の validation 層で弾かれる
- **認証情報の境界** — SaaS トークンは MCP tool 引数で受け取らず opshub 内部で keyring (ADR-0014) から取得、戻り値からも redact (ADR-0022 §決定 (b))。Token Passthrough は禁止

旧版 (2026-05-16) の Alternatives §3 「MCP サーバー経由でツール公開 (CLI でなく)」は MVP 段階での delay 判断であり、Phase 10 で ADR-0022 とともに採用された。本 ADR の現在版では MCP は CLI と並列の正規経路であり、Alternatives §3 は historical context として残す (下記 Alternatives §3 を改訂)。

### (c) Agent Skills は `ozzy-labs/skills` preset 配布 (Phase 10 で追加)

秘書 Agent Skills (Phase 10 Sub-issue D の 5 skill: daily-brief / next-actions / reply-draft / pr-review / file-lookup) は **opshub 本体に同梱しない**。`ozzy-labs/skills` リポに SKILL.md の実体を置き、handbook ADR-0016 の `@ozzylabs/skills` Renovate preset 経由で外部ホストの `.claude/skills/` 配下に配布する。

opshub 本体リポでは以下のみを保持する:

- **仕様・catalog** — `docs/secretary-agent.md` (秘書 5 skill の一覧・使い方・MCP tool マッピング・できること/できないこと)
- **skill security scan ロジック** — `tools/skill_scan.py` 等の検出 module (QwenPaw 由来 4 カテゴリ: プロンプトインジェクション / コマンドインジェクション / ハードコード鍵 / データ持ち出し + frontmatter の隠しユニコード / 「ignore previous instructions」類のパターン検出)。`ozzy-labs/skills` の CI lint に組み込む想定で、本リポでは scan ロジックの実装と本リポ内の skill 仕様への適用テストまでを scope とする
- **MCP セットアップ** — `docs/mcp-setup.md` (外部ホストから MCP 経由で opshub を使う手順)

これにより:

- **skill の lifecycle が ①コアと分離** — skill 改訂サイクル (頻繁) と opshub release サイクル (semver) を独立に回せる
- **ozzy-labs エコシステム共通機構と整合** — handbook ADR-0016 の skill 配布機構 (`@ozzylabs/skills` preset) を再利用、配布経路を二重化しない
- **複数ホスト間の skill 同期が単純** — 各ホストは preset 更新で skill を pull、opshub 本体の install / update とは独立
- **opshub 本体の install image が肥大化しない** — `uv tool install opshub` の payload は ①コアのみで、skill ファイルは含まない

skill の人格設定・常駐実装・状態持ちコードは opshub に持ち込まない (本 ADR §決定 (a) 形A の延長)。これらは外部ホスト側 (Claude Code の `~/.claude/` や Codex CLI の equivalent) の責務である。

## Consequences

### Positive

1. **完全な audit log** — すべての変更が `agent_runs` + `events` に記録される (CLI / MCP / Application service 経路すべてで同一 invariant)
2. **safety** — 不正な状態遷移を service 層で弾ける。MCP 経路でも read/write 分離 (ADR-0022) + HITL で同等の安全性
3. **lock 制御** — service 内で lock 取得 → 操作 → 解放を統一できる (CLI / MCP 経路で共通)
4. **複数 agent の共存** — 同じ service 層を共有するため、競合は service 層に閉じる。MCP 経由でも CLI 経由でも同一 service が呼ばれる
5. **テスト容易性** — service interface のテストで agent 経路 (CLI / MCP) を網羅できる
6. **形A による境界の明確化** (Phase 10 で追加) — ②外部ホストの runtime コードが ①コアに混入しないため、core / agent layer の責務分離が構造的に保たれる
7. **vendor lock-in 回避** (Phase 10 で追加) — runtime は外部ホスト (Claude Code / Codex CLI 等) が担うため、opshub は特定 runtime に縛られない。新しいホストが MCP クライアントとして実装される限り opshub は対応不要

### Negative / Trade-offs

1. **mediation latency** — 直接 SQL 比で 10-100 ms オーダーの overhead。MCP 経路では subprocess spawn + stdio framing で更に数十 ms 追加
2. **CLI / MCP surface 設計コスト** — agent が必要なすべての操作を CLI / MCP に表現する必要がある。Phase 10 で MCP 面を新設したことで二重 surface の保守コストが発生 (緩和: 両者とも ①コア service を呼ぶ薄い wrapper で、service が SSOT)
3. **agent への教育** — `AGENTS.md` / `CLAUDE.md` / `docs/secretary-agent.md` で boundary を繰り返し説明する必要
4. **緊急時の hack difficulty** — DB 直接修復は禁止扱いだが、データ破損時の救済手順 (`opshub admin repair`) を別途用意する必要
5. **skill 配布の外部依存** (Phase 10 で追加) — Agent Skills は `ozzy-labs/skills` 経路に依存し、opshub 本体だけでは秘書層が完結しない。緩和: 配布機構は handbook ADR-0016 で確立済みで、opshub 本体は skill spec を `docs/secretary-agent.md` 等で保持するため runtime / skill が無くても ①コアは独立動作する
6. **runtime 自由度を捨てる** (Phase 10 で追加) — opshub が runtime を持たない選択は、将来「特化型 runtime を opshub に内蔵したい」要望が出たときに ADR 改訂が要る。pre-userbase での再評価コストは小さい (1.0 / 実ユーザー surface 時に改めて議論)

## 軽減策

1. **`AGENTS.md` / `CLAUDE.md` で明示** — 「agent は `opshub` CLI / MCP 経由でのみ DB / markdown を変更する」を冒頭に書く
2. **`.claude/settings.json` で `Bash(sqlite3:*)` / `Bash(rm:* generated/*)` を deny** — permission レベルで防ぐ
3. **`opshub agent session start` を強制** — agent が work を始める前に必ず session を開かせる (Phase 2 で確立)
4. **`opshub admin repair`** — 緊急時の DB 直操作を、それ自体が CLI コマンドとして提供することで境界を維持
5. **MCP tool annotation の活用** (Phase 10 で追加) — `annotations.readOnlyHint` / `destructiveHint` を全 write tool に付与し、agent host が auto-approve 判断する手がかりを宣言的に提供 (ADR-0022 §決定 (c))
6. **skill security scan の CI 組み込み** (Phase 10 で追加) — `ozzy-labs/skills` 側 CI と opshub 本体側の skill 仕様テストで二段検査し、悪意ある skill が外部ホストの `.claude/skills/` に到達する経路を抑制 (Sub-issue D)

## Alternatives Considered

### 1. Agent に DB 直接アクセス権を与える

却下理由: audit / safety / coordination のすべてが崩れる。multi-agent 前提では論外。

### 2. Read-only DB アクセス + write は CLI

却下理由: read を直接許すと、agent が projection の中身を context に詰め込みやすく便利。ただし projection の構造変更時に agent prompt の修正コストが伴い、結局 CLI 経由 (例: `opshub task list --json`) の方が安定。

### 3. MCP サーバー経由でツール公開 (CLI でなく) — historical, Phase 10 で採用に転換

旧版 (2026-05-16) では「MVP では CLI 経路で十分。MCP は context 常駐コストとサーバー保守コストがあるため、明確な需要が出るまで延期」として却下していた (ADR-0006 CLI-first MVP)。

Phase 10 (2026-05-30) で **採用に転換**。秘書エージェント・プラットフォーム化により MCP は agent host の業界標準クライアント機構となり、CLI と MCP は「同じ ①コア service を叩く二つの口」として並列に位置づけられる。「MCP は context 常駐コスト」は外部ホスト側の skill 機構 (progressive disclosure、SKILL.md 標準) で緩和され、「サーバー保守コスト」は stdio transport (ADR-0022 §決定 (a)) で常駐 listener を持たない設計により最小化される。

→ [ADR-0006: CLI-first MVP, defer MCP](0006-cli-first-mvp.md) (CLI-first の原則は維持、MCP は並列追加で CLI を置換しない)
→ [ADR-0022: MCP Server Surface](0022-mcp-server-surface.md) (本 ADR §決定 (b) の根拠)

### 4. ファイルロック + 楽観的並行制御で直接アクセスを許可

却下理由: 競合解決はできても audit / replayability が保たれない。event-sourced 設計と整合しない。

### 5. 自前エージェント runtime を opshub に内蔵 (形B / 形C) — Phase 10 で却下

Phase 10 設計セッション (2026-05-30) で検討した代替案:

- **形B**: opshub に LangGraph / Anthropic Claude Agent SDK / OpenAI Agents SDK 等の runtime を内蔵し、`opshub agent run "<task>"` のようなコマンドで秘書を起動する。
- **形C**: opshub 独自の軽量 agent runtime (think → act → observe ループ) を実装する。

却下理由:

1. **runtime レイヤは資本投下と本番実績で固まる領域** — LangGraph / OpenAI Agents SDK / Claude Agent SDK / Microsoft Agent Framework は数億ドル規模の投資と本番採用実績を持ち、後発の小規模実装が機能・安定性で勝つ見込みは薄い (Phase 10 plan §10 並列調査)
2. **vendor lock-in** — 形B で特定 runtime を採用すると、その runtime の API 変更・廃止・license 変更に opshub が引きずられる。ADR-0009 multi-agent neutrality と矛盾
3. **複数ホストとの二重化** — 外部ホスト (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI) はすべて自前の runtime を持ち、利用者がそちらで秘書を起動する。opshub に runtime を載せると同じユーザーが二つの runtime (ホスト側 + opshub 側) を抱える
4. **scope creep** — runtime 実装には prompt 管理・tool registry・retry / backoff・streaming・error handling・cancellation・concurrent run 管理が必要で、operational memory という本来の責務と乖離する
5. **MCP が標準クライアント機構として収束済み** — 調査した全 runtime が MCP クライアントを実装しており、opshub は MCP サーバ面を出すだけで全ホストから利用可能になる (形A)

→ 形A (本 ADR §決定 (a)) を採用

### 6. Agent Skills を opshub 本体に同梱 — Phase 10 で却下

検討した代替: opshub の `share/skills/` 等に秘書 5 skill (SKILL.md) を同梱し、`opshub skills install` で外部ホストの `.claude/skills/` に copy する経路を opshub が提供する。

却下理由:

1. **skill lifecycle と ①コア lifecycle が乖離** — skill (SKILL.md の文言・トリガ条件・MCP tool マッピング) は頻繁に改訂される一方、opshub 本体は semver で安定化させる必要があり、同梱すると skill 改訂のたびに opshub release を切る or skill を main branch で前進させる選択を強いられる
2. **ozzy-labs エコシステムの skill 配布機構と二重化** — handbook ADR-0016 で `@ozzylabs/skills` Renovate preset 経路が確立済みで、`drive` / `implement` / `lint` 等の既存 skill はそちら経由で配布されている。秘書 skill だけ別経路にする合理性がない
3. **複数ホスト間の skill 同期コスト** — operator が複数マシン・複数ホストで opshub を使うとき、同梱方式だと各環境で `opshub skills install` を再実行する必要が出る。preset 方式なら Renovate が自動 PR を出し続ける
4. **opshub 本体の install image 肥大化** — `uv tool install opshub` の payload に SKILL.md (本文 + references/) を含めると CLI 起動オーバヘッドに影響しうる (M6 cold-start guard、ADR-0006)

→ `ozzy-labs/skills` preset 配布 (本 ADR §決定 (c)) を採用、opshub 本体は skill 仕様・catalog・security scan のみ保持

## 関連

- [Principles 4 (Agent Runtime Boundary)](../principles.md)
- [Architecture 2.8 (Agent Runtime Boundary)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0006: CLI-first MVP, defer MCP](0006-cli-first-mvp.md) (MCP は Phase 10 で並列追加、CLI は廃止しない)
- [ADR-0009: Multi-Agent Neutrality](0009-multi-agent-neutrality.md) (形A は 4 vendor 中立性の延長)
- [ADR-0022: MCP Server Surface](0022-mcp-server-surface.md) (本 ADR §決定 (b) の根拠)
- handbook ADR-0016 (skills repo `ozzy-labs/skills` 配布機構、本 ADR §決定 (c) の根拠)
- [Phase 10 Plan §1 / §3-D / §8 Open Q #4](../phase-10-plan.md)
