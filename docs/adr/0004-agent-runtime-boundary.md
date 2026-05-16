# 0004. Agent Runtime Boundary

- Status: Proposed
- Date: 2026-05-16
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

## Decision

Agent は Operational DB / Workspace を **直接変更しない**。すべての書き込みは以下を経由する。

1. **`opshub` CLI** — 標準経路。`opshub task create` / `opshub event append` / `opshub note save` 等
2. **Application service** — Python から直接呼ぶ場合の経路 (内部利用のみ)
3. **Repository** — service 内部で DB アクセスを集約する層
4. **JSON patch proposal** — agent が提案 → 人間が承認 (Phase 2+)

agent が以下を行うことは禁止する。

- 直接 `sqlite3` で `events` / projection tables を書き換える
- `generated/` 配下の markdown を直接編集する
- application service / CLI を介さず `events` ファイルや `db.sqlite` を操作する
- audit log / agent_runs テーブルへの書き込みを bypass する

CI / lefthook で次を検出する。

- agent が `sqlite3` / DB クライアントを Bash 経由で呼んでいないか (settings.json の permissions で deny)
- `generated/` への直接 write が発生していないか (`workspace doctor` で検出)
- `agent_runs` を経由しない event が存在しないか (Phase 2+ で実装)

## Consequences

### Positive

1. **完全な audit log** — すべての変更が `agent_runs` + `events` に記録される
2. **safety** — 不正な状態遷移を service 層で弾ける
3. **lock 制御** — service 内で lock 取得 → 操作 → 解放を統一できる
4. **複数 agent の共存** — 同じ CLI を共有するため、競合は service 層に閉じる
5. **テスト容易性** — service interface のテストで agent 経路を網羅できる

### Negative / Trade-offs

1. **mediation latency** — 直接 SQL 比で 10-100 ms オーダーの overhead
2. **CLI surface 設計コスト** — agent が必要なすべての操作を CLI に表現する必要がある
3. **agent への教育** — `AGENTS.md` / `CLAUDE.md` で boundary を繰り返し説明する必要
4. **緊急時の hack difficulty** — DB 直接修復は禁止扱いだが、データ破損時の救済手順 (`opshub admin repair`) を別途用意する必要

## 軽減策

1. **`AGENTS.md` / `CLAUDE.md` で明示** — 「agent は `opshub` CLI 経由でのみ DB / markdown を変更する」を冒頭に書く
2. **`.claude/settings.json` で `Bash(sqlite3:*)` / `Bash(rm:* generated/*)` を deny** — permission レベルで防ぐ
3. **`opshub agent session start` を強制** — agent が work を始める前に必ず session を開かせる (Phase 2)
4. **`opshub admin repair`** — 緊急時の DB 直操作を、それ自体が CLI コマンドとして提供することで境界を維持

## Alternatives Considered

### 1. Agent に DB 直接アクセス権を与える

却下理由: audit / safety / coordination のすべてが崩れる。multi-agent 前提では論外。

### 2. Read-only DB アクセス + write は CLI

却下理由: read を直接許すと、agent が projection の中身を context に詰め込みやすく便利。ただし projection の構造変更時に agent prompt の修正コストが伴い、結局 CLI 経由 (例: `opshub task list --json`) の方が安定。

### 3. MCP サーバー経由でツール公開 (CLI でなく)

却下理由: MVP では CLI 経路で十分。MCP は context 常駐コストとサーバー保守コストがあるため、明確な需要が出るまで延期。

→ [ADR-0006: CLI-first MVP, defer MCP](0006-cli-first-mvp.md)

### 4. ファイルロック + 楽観的並行制御で直接アクセスを許可

却下理由: 競合解決はできても audit / replayability が保たれない。event-sourced 設計と整合しない。

## 関連

- [Principles 4 (Agent Runtime Boundary)](../principles.md)
- [Architecture 2.8 (Agent Runtime Boundary)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
- [ADR-0006: CLI-first MVP, defer MCP](0006-cli-first-mvp.md)
