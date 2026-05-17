# Decisions Log (Rejected Alternatives)

> Status: Draft (in active design). Last reviewed: 2026-05-17.

設計フェーズで検討したが採用しなかった案の索引。詳細理由は対応する ADR / docs に記載。本ドキュメントは「あの議論はどこで結論が出たか」の早見表として機能する。

Phase 5 (briefing layer + Pluggable LLM + event-driven auto-embed 補助) は 2026-05-17 に完了。LLM 利用方針は §12 (ADR-0015) で closeout し、principles.md §Open Q #1 を §確定済み に移動した。

Phase 6 (action loop layer + Pluggable LLM structured output + Ollama backend + Proposal domain) も 2026-05-17 に完了。Action loop / structured output / Local LLM の方針は §13 (ADR-0016) で closeout し、ADR-0015 §決定 (a) deferred (Local LLM) を ADR-0016 §決定 (h) で closeout (Ollama 採用)。実装は PR #100 (ADR-0016) / #101 (Proposal events) / #102 (LLMClient.complete_structured Protocol 拡張) / #103 (Anthropic + OpenAI structured) / #104 (proposals projection + migration 0015) / #105 (OllamaLLMClient) / #106 (ProposalService) / #112 (`opshub propose` CLI) / closeout PR (本コミット) の 9 PR で構成。Phase 6.x 候補 (`llama.cpp` direct binding / proposal scoring / multi-step plan / `inbox_item` / `source` candidate types) と Phase 7 (Connectors Wave 2、epic #113) は principles.md §9 / phase-6-plan.md §5 / docs/phase-7-plan.md を参照。

## 1. Repository / Product 命名

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| `relay` | `opshub` | 機能が伝わらない | ADR-0008 |
| `inbox` | `opshub` | scope より狭い | ADR-0008 |
| `triage` | `opshub` | 「処理」側に寄りすぎ、保存・横断検索が読めない | ADR-0008 |
| `agenda` | `opshub` | npm `agenda` パッケージとの衝突 | ADR-0008 |
| `desk` | `opshub` | Zendesk のブランド連想 | ADR-0008 |
| `dispatch` | `opshub` | Netflix Dispatch 連想、能動的すぎ | ADR-0008 |
| `signal` | `opshub` | Signal Messenger SEO 衝突 | ADR-0008 |
| `agentic-opshub` (with prefix) | `opshub` | ゼロベース命名と不整合、長さ 14 字 | ADR-0008 |
| `Agentic OpsHub` (displayName のみ) | `OpsHub` | repo 名と displayName を乖離させると発見性低下 | ADR-0008 |

## 2. Description / Tagline 文言

| 却下表現 | 採用表現 | 理由 |
|---|---|---|
| `Pluggable connectors` | `Connectors` | `Connector` 単独で「組み合わせて使える単位」のニュアンスを内包。`Pluggable` は engineering jargon 寄り |
| `markdown store` | `single local store` | storage 形式 (markdown / SQLite / event log) を早期確定させない抽象表現 |
| `Event-sourced local workspace ...` を冒頭に置く案 | `Local-first operational memory and execution hub ...` | 実装語を先頭にすると「触る前に難解そう」と感じる読者がいる |
| `for multi-agent workflows` を主語 | `for humans and AI agents` | 単一 agent ユーザーを排除して見える |

## 3. アーキテクチャ / Storage

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Markdown を source of truth (Obsidian 流) | Markdown を workspace surface (projection) | クエリ性 / 整合性 / event-sourced との衝突 | ADR-0003 |
| CRUD ベース + 別途 audit log | Event-Sourced | 二重管理・replay 不能 | ADR-0002 |
| Hybrid (重要 entity のみ event-sourced) | 一律 event-sourced | 境界が曖昧化・複雑化 | ADR-0002 |
| Git-backed (markdown + commit を event 代わり) | SQLite append-only events | 構造化クエリに不向き、粒度制御困難 | ADR-0002 |
| CQRS + 別 DB | 単一 SQLite | over-engineering | ADR-0002 |
| `markdown 主体 + SQLite 索引` (初期検討) | Event Store + Projections + Workspace surface | より principled、Brief 採用 | ADR-0002, 0003 |
| Full body をローカル保持 | 最小化 (ID / URL / summary / metadata) | 機密 / TOS / 容量 / agent context 効率 | ADR-0005 |
| Encrypted local body 保持 | 最小化 | 機密性は担保できるが TOS / 容量問題は残る | ADR-0005 |

## 4. 言語 / スタック

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| TypeScript + drizzle + zod + better-sqlite3 + commander | Python + uv + Typer + SQLAlchemy + Pydantic | sqlite-vec Node binding 未成熟、Alembic 級 migration なし、local embedding 選択肢が狭い | ADR-0001 |
| TypeScript + Bun + libSQL + drizzle | Python | 単一バイナリ配布は魅力的だが Alembic 不在、sqlite-vec 参考実装薄い、Bun が若い | ADR-0001 |
| Rust + sqlx + clap | Python | 開発速度・AI 駆動開発相性・Phase 4 LLM/Embedding エコシステムで Python が優位 | ADR-0001 |
| Go + sqlc + cobra | Python | Pydantic 相当の validation が弱い、event-sourcing 参考実装が少ない、sqlite-vec Go binding 希少 | ADR-0001 |
| Python + Django / FastAPI | Python + Typer | Web フレームワーク不要 (CLI + 内部 SQLite) | ADR-0001 |
| Python + Click (Typer 不採用) | Python + Typer | Typer は Click 上位互換、Pydantic と統合しやすい | ADR-0001 |

## 5. Agent 連携

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| MCP-first (MVP から MCP サーバー提供) | CLI-first | context 常駐コスト・保守コスト | ADR-0006 |
| CLI + MCP を MVP から両提供 | CLI のみ | 二重実装で工数増 | ADR-0006 |
| REST API | CLI | ローカル利用に Web サーバー不要 | ADR-0006 |
| Python SDK のみ (CLI なし) | CLI | shell / cron / 他言語からの利用困難 | ADR-0006 |
| Agent に DB 直接アクセス権 | CLI / Service / Repository 経由 | audit / safety / coordination が崩れる | ADR-0004 |
| Read-only DB + write は CLI | すべて CLI | projection 構造変更時の prompt 修正コスト | ADR-0004 |
| Claude Code 単独サポート | 4 vendor 対等サポート | vendor lock-in リスク、ozzy-labs 全体方針との不整合 | ADR-0009 |
| Claude Code + 1 vendor (例: Codex CLI) | 4 vendor 対等サポート | 中途半端、追加コストわずか | ADR-0009 |

## 6. リポ / パッケージ構成

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Monorepo (uv workspace) を最初から | Single Python package | MVP overhead、境界未確定 | ADR-0007 |
| Connector のみ別 package | Single Python package | 中途半端、`optional-dependencies` で代替 | ADR-0007 |
| `docs/decisions/` | `docs/adr/` | SEO / adr-tools デフォルト | ADR-0000 |
| `agentic-` prefix を新カテゴリに適用 | 無接頭辞単語 1 語 | ゼロベース命名と不整合 | ADR-0008 |

## 7. Connector

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Connector が Task を直接生成 | inbox item 経由 + triage 必須 | 誤検知タスク、tasks テーブル膨張 | ADR-0010 |
| Connector が「重要そう」なイベントを Task 化 | inbox item 経由 + triage 必須 | 判断基準が SaaS / 用途で異なる | ADR-0010 |
| Inbox を経由しない直接 Triaged Event Stream | inbox item を projection として持つ | 未処理 / 処理済みの区別不能 | ADR-0010 |

## 8. ozzy-labs エコシステム

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Standalone リポとして運営 | エコシステムにフル参加 | bootstrap / agent 連携 / skill 配信を自前整備するコスト | ADR-0011 |
| commons は採用、skills は採用しない | skills も 4 vendor opt-in | 13 skill の自前実装コスト、ADR-0009 と矛盾 | ADR-0011 |
| commons を手動 fork | pinned + Phase 1 TODO | 継続同期の利益を失う | ADR-0011 |
| commons-python 別リポ作成 | commons 単一 | 共通部分は言語非依存、maintain コスト増 | ADR-0011 |

## 9. ツール

| 却下案 | 採用案 (暫定) | 理由 | 参照 |
|---|---|---|---|
| Taskfile | `just` | 知識 MCP の `tools/just` 整合 | ADR-0001, Open Questions |
| pre-commit (Python 標準) | lefthook | ozzy-labs 共通の lefthook 採用 | ADR-0001 |
| SQLAlchemy ORM | SQLAlchemy Core | event-sourced で aggregate を ORM map すると複雑化 | ADR-0001 |
| eventsourcing library | 自前実装 | append + reducer + 純粋関数 projector で十分 | ADR-0002 |
| 専用 graph DB | relational link table | OpsHub の規模で過剰 | Architecture 2.5 |

## 10. Description / Tagline の最終ぶれ

| 段階 | 表現 |
|---|---|
| 初版 | `Local-first operational memory and execution hub for humans and AI agents — aggregating GitHub, Slack, Microsoft 365, and Box into one markdown store.` |
| 改訂 1 (拡張性反映) | `... Pluggable connectors aggregate work signals — GitHub, Slack, Microsoft 365, Box, and more — into a single markdown store.` |
| 改訂 2 (storage 抽象化 + `Connectors` 単独化) | `... Connectors aggregate work signals — GitHub, Slack, Microsoft 365, Box, and more — into a single local store.` ← **採用** |

## 11. Embedding 戦略 (ADR-0012)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| API embedder のみ (OpenAI / Voyage 等) | Pluggable Embedder + VectorStore | local-first 違反、機密 summary の外部流出、コスト | ADR-0012 |
| Local embedder のみ (sentence-transformers 等) | Pluggable | 配布が ~500MB-2GB に肥大化、CPU 環境で初回 embed が遅い、品質の逃げ場なし | ADR-0012 |
| 抽象レイヤなし (Phase 4 で具象着手) | Phase 1 で Embedder / VectorStore Protocol 定義 | Phase 4 で `services/` / CLI / projection に破壊的改修 | ADR-0012 |
| 複数 vector store 並列 (sqlite-vec + LanceDB) | 単一 sqlite-vec | ADR-0002 単一 SQLite 原則違反、backup / replay 対象増 | ADR-0012 |
| 単一 model + version 列なし | `model_id` + `model_version` 列で増分 re-embed 可能に | モデル変更で全件 re-embed 必須、A/B 比較不能 | ADR-0012 |
| event payload も embed | summary 系のみ | event は immutable で量が多い、検索は projection で代替可能 | ADR-0012 |
| Hybrid (短期 API + 長期 local archive) | Pluggable で柔軟性 | 同一 entity が異 embedder で recall 結果不安定、切替ロジック複雑 | ADR-0012 |

## 12. LLM 利用方針 (ADR-0015)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| LLM 抽象なし (Anthropic / OpenAI SDK を BriefingService が直接呼ぶ) | Pluggable `LLMClient` Protocol + Anthropic / OpenAI 具象 | Phase 6+ の backend 追加で service に分岐が滲み、test stub 化も困難。ADR-0012 Pluggable Embedder と非対称になり ADR-0009 と衝突 | ADR-0015 |
| Local LLM (Ollama / `llama.cpp`) を MVP に含める | API backend 2 つ (Anthropic + OpenAI) のみ MVP、local は Phase 5.x | model ファイル 4-30GB で配布が壊れる、daemon 前提の cross-platform 問題、briefing 品質の validation 未実施 | ADR-0015 |
| Default backend = `anthropic` (or `openai`) | `disabled` を default | 片方 vendor 固定化で ADR-0009 と衝突、初回 install で認証エラーを踏む、Phase 4 embedding default と非一致 | ADR-0015 |
| Prompt を初手から外部ファイル化 (`~/.config/opshub/prompts/briefing.md`) | inline Python 定数 (Phase 5 MVP) | 1 flow のために loader + packaging を実装するのは scope creep、Phase 5.x で後付け可能 | ADR-0015 |
| Prompt injection 対策を system prompt の自然言語注意のみ | 明示 delimiter wrap (`<source id="...">...</source>`) + do-not-follow preamble | 「prompt で強く言えば従う」は誤り (知識 MCP), OWASP LLM01:2025 典型攻撃面 | ADR-0015 |
| Auto fallback (Anthropic → OpenAI) を MVP に含める | 自動 fallback なし、`OpsHubError` で caller に伝播 | 再現性 / cache invariant が壊れる、cost surprise、Protocol 層が複雑化 | ADR-0015 |
| API key を `[llm.<backend>] api_key_env` で config 駆動 | ADR-0014 (`core/secrets` + keyring + env override) 再利用、key 規約 `llm:<name>:api_key` | embedding (`embedder:<name>:api_key`) と規約統一、operator の mental model を 1 つに | ADR-0015 |
| `max_tokens` を Protocol で optional + default 持ち | `max_tokens` 必須引数 | caller が cost を把握できず observability 低下、backend ごとの reasonable default 差で Protocol 側の選定が lock-in | ADR-0015 |

## 13. Action loop / structured output (ADR-0016)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| Grammar / JSON-mode constrained decoding (provider 固有 `response_format` 等) | provider-native tool calling (Anthropic `tool_use` / OpenAI `tools=` / Ollama OpenAI 互換 `tools=`) | JSON-mode は availability が provider 固有で portability が劣る、tool calling は 3 backend 同一概念モデル + multi-step proposal への拡張余地 | ADR-0016 |
| Free-form text + regex 抽出 | Pydantic v2 model を SSOT、各 client が JSON schema に serialize | regex は brittle、schema validation の二重化、prompt engineering で format 強制は遵守率不安定 | ADR-0016 |
| Single-shot markdown を人間が手で apply | `(proposal_id, candidate_index)` natural key + CLI apply 経路 | markdown には index が無く idempotent apply 不可、Action loop 自動化が薄まる、`ProposalApplied` event の意義が消える | ADR-0016 |
| Auto-apply mode (`opshub propose --auto-apply` / `[llm] auto_apply = true`) | human-in-the-loop 必須、Phase 6.x 以降も禁止 | ADR-0004 (Agent Runtime Boundary) と矛盾、LLM 生成 text が prompt injection / hallucination 経由で durable state に到達、OpsHub の core value (信用できる operational memory) と直接矛盾 | ADR-0016 |
| Apply 経路で entity event を直接 append (TaskService 不経由) | 既存 TaskService / DecisionService を経由 | Phase 1-2 で確立した validation / sanitisation が bypass、validation の 2 系統化、ADR-0005 (External Content Minimization) summary 制約も bypass | ADR-0016 |
| In-place migration (Phase 6.x で v1 candidate を v2 に rewrite) | `schema_version: Literal["v1"]` literal + 両 version 読み分け | ADR-0002 event immutability 違反、event log の audit trail / replay 整合性が崩れる、Pydantic discriminated union で type-safe に表現可 | ADR-0016 |
| `llama.cpp` direct (python binding) を MVP に含める | Ollama daemon 経由のみ MVP、`llama.cpp` direct は Phase 6.x | OS-specific binary install + model file 4-30GB が ADR-0001 配布制約 (`uv tool install opshub`) を破る、Ollama で 90% covered | ADR-0016 |
