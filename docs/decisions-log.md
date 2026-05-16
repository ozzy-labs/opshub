# Decisions Log (Rejected Alternatives)

> Status: Draft (in active design). Last reviewed: 2026-05-16.

設計フェーズで検討したが採用しなかった案の索引。詳細理由は対応する ADR / docs に記載。本ドキュメントは「あの議論はどこで結論が出たか」の早見表として機能する。

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
| 改訂 2 (storage 抽象化 + `Connectors` 単独化) | `... Connectors aggregate work signals — GitHub, Slack, Microsoft 365, Box, and more — into a single local store.` ← **採用**

## 11. Embedding 戦略 (ADR-0012)

| 却下案 | 採用案 | 理由 | 参照 |
|---|---|---|---|
| API embedder のみ (OpenAI / Voyage 等) | Pluggable Embedder + VectorStore | local-first 違反、機密 summary の外部流出、コスト | ADR-0012 |
| Local embedder のみ (sentence-transformers 等) | Pluggable | 配布が ~500MB-2GB に肥大化、CPU 環境で初回 embed が遅い、品質の逃げ場なし | ADR-0012 |
| 抽象レイヤなし (Phase 4 で具象着手) | Phase 1 で Embedder / VectorStore Protocol 定義 | Phase 4 で `services/` / CLI / projection に破壊的改修 | ADR-0012 |
| 複数 vector store 並列 (sqlite-vec + LanceDB) | 単一 sqlite-vec | ADR-0002 単一 SQLite 原則違反、backup / replay 対象増 | ADR-0012 |
| 単一 model + version 列なし | `model_id` + `model_version` 列で増分 re-embed 可能に | モデル変更で全件 re-embed 必須、A/B 比較不能 | ADR-0012 |
| event payload も embed | summary 系のみ | event は immutable で量が多い、検索は projection で代替可能 | ADR-0012 |
| Hybrid (短期 API + 長期 local archive) | Pluggable で柔軟性 | 同一 entity が異 embedder で recall 結果不安定、切替ロジック複雑 | ADR-0012 | |
