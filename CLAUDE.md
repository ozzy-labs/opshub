# CLAUDE.md

共通方針は AGENTS.md を参照（Phase 1-11 完了状態の記載含む）。以下は Claude Code 固有の設定。

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

### 秘書エージェント Skills (Phase 10、opshub MCP 経由)

opshub は形A (ADR-0004) に基づき秘書 5 Skill を提供する。SKILL.md の SSOT は `docs/skills/<name>/SKILL.md` に置き、配布は `ozzy-labs/skills` リポを経由する。発火条件は自然文 (skill description) で表現されており、自分で叩く必要はない。

- `daily-brief` — 「今日のまとめ」「最近どうなってる」「状況教えて」
- `next-actions` — 「次に何やる?」「やること教えて」「優先度高いのは?」
- `reply-draft` — 「これに返信案考えて」「下書き作って」 (外送信なし、apply は HITL)
- `pr-review` — 「PR #N レビューして」「この差分どう?」
- `file-lookup` — 「Box にあったあの資料」「<キーワード>含むファイル」

詳細は [`docs/secretary-agent.md`](docs/secretary-agent.md) を参照。MCP セットアップは [`docs/mcp-setup.md`](docs/mcp-setup.md)。

## Skills の共通ルール

- スキル完了時のネクストアクション提案には `AskUserQuestion` を使用する
- ネクストアクションはユーザーの確認なく実行しない
