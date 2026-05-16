# 0009. Multi-Agent Neutrality

- Status: Proposed
- Date: 2026-05-16
- Deciders: ozzy

## Context

OpsHub のユーザーは複数の AI コーディングエージェントを並行契約・並行利用している。

- Claude Max 5x
- Gemini AI Pro
- Codex Plus
- GitHub Copilot Pro

これらの CLI:

- Claude Code
- Codex CLI
- Gemini CLI
- GitHub Copilot CLI

それぞれに以下の差異がある。

1. 設定ファイル: `.claude/` / `.codex/` / `.gemini/` / `.github/`
2. Skill 配信先: `.claude/skills/` / `.agents/skills/` / `.gemini/settings.json` / `.github/copilot-instructions.md`
3. Prompt 配信形態: ファイル / snippet / settings 統合
4. Permission モデル: settings.json / config.toml / hooks
5. MCP サーバー登録方法: `.mcp.json` / `.gemini/settings.json` の `mcpServers` / `.codex/config.toml` / `.copilot/mcp-config.json`

単一 vendor 前提で OpsHub を設計すると、(a) 他 vendor の機能更新を取り込めず、(b) プラン枠の柔軟運用ができず、(c) vendor 戦略変更時のロックイン被害が大きくなる。

## Decision

OpsHub は **vendor-neutral multi-agent runtime** として設計する。具体ルール:

1. **agent 接続経路は CLI に統一** — `opshub` CLI が唯一の write 経路。各 agent は Bash 経由で同じ CLI を呼ぶ (ADR-0006 と整合)
2. **Skill / Prompt は vendor-neutral テキストで書く** — `prompts/triage.md` のような単一ファイルが基本。vendor 固有 metadata は frontmatter に分離
3. **Skill 配信は ozzy-labs/skills の adapter 出力に乗る** — `.commons/sync.yaml` の `skills_adapters: [claude-code, codex-cli, gemini-cli, copilot]` 4 件をすべて opt-in (ADR-0011 と整合)
4. **agent 固有設定ファイル**: `.claude/` / `.codex/` / `.gemini/` / `.github/` を **すべて維持** し、内容が分かれる場合は commons の sync 機構に委ねる
5. **OpsHub の docs / コミュニケーションでも 4 vendor を対等に列挙** — Claude Code → Codex CLI → Gemini CLI → Copilot CLI の順序 (README / AGENTS.md 等)
6. **Vendor 専用最適化を避ける** — 「Claude Code でしか動かない skill」「Codex CLI 専用 prompt」のような分岐を docs / 仕様 / コードに残さない

## Consequences

### Positive

1. **vendor 依存リスクの最小化** — 1 つの CLI が政策・価格・機能を変えてもダメージが限定的
2. **プラン枠の柔軟運用** — レート枠が逼迫した CLI を他 CLI で代替できる (CLAUDE.md の `user_plan_budget` 方針と整合)
3. **agent エコシステム成熟への対応** — 新 vendor (例: 将来の Anthropic Workspace / OpenAI Workspace / 他) が出ても同じ枠組みで取り込み可能
4. **ozzy-labs handbook (`multi-agent-repo`) との整合** — 既存組織方針と一致

### Negative / Trade-offs

1. **vendor 固有機能の最先端を取り込みにくい** — 例: Claude Code のみが持つ特定機能を活用しにくい
2. **テスト負荷** — 4 CLI で動作確認する負担。実用上は Claude Code を primary とし、他 3 つは定期的に smoke test 程度
3. **docs / 設定ファイル数の増加** — `.claude/` / `.codex/` / `.gemini/` / `.github/` を維持
4. **agent 固有 metadata の取り扱いコスト** — frontmatter の vendor 別フィールドが増えると保守性が落ちる

## 軽減策

1. **primary agent を Claude Code に設定** — 最も成熟しているため初期開発の中心とする。他 3 つはローテーション利用 + 定期検証
2. **Vendor 固有最適化が必要になったら ADR を起票** — 例外の見える化
3. **共通 prompt は `prompts/*.md` 単一ファイル + frontmatter で vendor 適用範囲を明示**
4. **Skill 出力は `ozzy-labs/skills` の adapter layer に任せる** — OpsHub 内で個別 vendor 出力を生成しない

## Alternatives Considered

### 1. Claude Code 単独サポート

却下理由:

- Anthropic の利用ポリシー / 価格戦略変更で全機能ロスのリスク
- ユーザーが既に 4 vendor の plan を購入している実態に合わない
- ozzy-labs 全体が multi-agent 前提で設計されている (handbook conventions)

### 2. Claude Code + 1 つだけ (例: + Codex CLI)

却下理由:

- 中途半端な vendor lock-in
- `.gemini/` / `.github/` ディレクトリが既に commons から sync されているため、追加コストはわずか

### 3. CLI 経路を捨てて MCP / SDK 経路に統一

却下理由:

- MCP サーバー保守コストが MVP 規模に見合わない (ADR-0006)
- SDK は vendor 別に存在し、結局 multi-agent 対応が必要

### 4. 各 vendor の特性に応じた専用 wrapper を提供

却下理由:

- 4 つの wrapper を保守するコスト
- OpsHub 本体の機能進化が wrapper 適合に縛られる
- vendor-neutral CLI が同等のことを実現できる

## 関連

- [Principles 5 (Multi-Agent Neutral)](../principles.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
- [ADR-0006: CLI-first MVP, defer MCP](0006-cli-first-mvp.md)
- [ADR-0011: Ozzy-Labs Ecosystem Adoption](0011-ozzy-labs-ecosystem-adoption.md)
- 知識 MCP: `ai/practice/multi-agent-repo`
