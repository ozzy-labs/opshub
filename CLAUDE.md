# CLAUDE.md

共通方針は AGENTS.md を参照（Phase 1-14 完了状態の記載含む）。以下は Claude Code 固有の設定。

## 基本ルール

- ユーザーへの確認には `AskUserQuestion` を使用する

## Available Skills

スキル本体は [`ozzy-labs/skills`](https://github.com/ozzy-labs/skills) に置き、`@ozzylabs/skills` の Renovate preset 経由で `.claude/skills/` に配布される（[ADR-0016](https://github.com/ozzy-labs/handbook/blob/main/adr/0016-create-skills-repo.md)）。

### 開発作業用スキル

- `/implement` — Issue または指示をもとに、ブランチ作成・実装
- `/lint` — 全リンターを自動修正付きで実行
- `/test` — ビルド・テスト・型チェックを実行
- `/commit` — 変更をステージし、Conventional Commits でコミット
- `/pr` — 変更を push し、PR を作成・更新
- `/review` — コード変更や PR をレビュー
- `/ship` — lint・コミット・PR 作成を一括実行
- `/drive` — implement + ship + review loop（Issue から merge-ready な PR まで自律駆動）

### 秘書エージェント Skills (Phase 10 で 5 Skill 開始、Phase 12 で 14 Skill 体制に拡張、opshub MCP 経由)

opshub は形A (ADR-0004) に基づき秘書 **14 Skill** を提供する。SKILL.md の SSOT は `docs/skills/<name>/SKILL.md` に置く (Phase 12 H1 で opshub を SSOT に確定、ADR-0004 §決定 (c))。配信機構 (`ozzy-labs/skills` CI + Renovate preset) は引き続き Phase 15+ に defer されているため、Phase 14 までは host 側に手動 install (詳細は [`docs/secretary-agent.md`](docs/secretary-agent.md) §8)。発火条件は自然文 (skill description) で表現されており、自分で叩く必要はない。

**read 自律 OK (10 件)**:

- `personal-brief` — 「今日のまとめ」「今週どうなってる」「先月の振り返り」「最近どうなってる」「状況教えて」
- `next-actions` — 「次に何やる?」「やること教えて」「今週やること」「優先度高いのは?」
- `pr-review` — 「PR #N レビューして」「この差分どう?」
- `find-document` — 「Box にあったあの資料」「<キーワード>含むファイル」 (Phase 12 H1 で `search` FTS5 MCP tool 直接利用)
- `meeting-prep` (Phase 12 H2) — 「来週の会議準備」「明日のミーティング前確認」
- `research` (Phase 12 H2) — 「<X> について調べて」「<トピック> 網羅的に教えて」
- `external-brief` (Phase 12 H3) — 「上司向け週次報告」「クライアント向け進捗まとめ」 (pair = personal-brief)
- `decision-rationale` (Phase 12 H3) — 「あの決定はなぜ」「X を選んだ理由」
- `handoff-draft` (Phase 12 H5) — 「引き継ぎ書作って」「handoff 書く」 (text-only、persist なし)
- `announcement-draft` (Phase 12 H5) — 「リリース告知文書いて」「announcement 作って」 (text-only、persist なし)

**HITL write (4 件)** (外送信なし、apply は HITL):

- `reply-draft` — 「これに返信案考えて」「下書き作って」 (idempotent apply)
- `inbox-triage` (Phase 12 H4) — 「受信箱整理して」「inbox 仕分けて」 (pair = source-extract)
- `source-extract` (Phase 12 H4) — 「この資料から task 抽出」「<source_id> から候補を」
- `meeting-followup` (Phase 12 H4) — 「会議後の action items」「議事録から task 抽出」 (pair = meeting-prep)

詳細 (責務マップ / pair structure / MCP tool 依存マップ / HITL boundary) は [`docs/secretary-agent.md`](docs/secretary-agent.md) を参照。MCP セットアップは [`docs/mcp-setup.md`](docs/mcp-setup.md)。

## Skills の共通ルール

- スキル完了時のネクストアクション提案には `AskUserQuestion` を使用する
- ネクストアクションはユーザーの確認なく実行しない
