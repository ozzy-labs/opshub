# 0006. CLI-first MVP, defer MCP

- Status: Accepted
- Date: 2026-05-16
- Deciders: ozzy

## Context

OpsHub に agent (Claude Code / Codex / Gemini / Copilot) が接続する方法は主に 3 つ。

1. **CLI** — agent が Bash 経由で `opshub` コマンドを呼ぶ
2. **MCP server** — agent と stdio / HTTP で接続し、ツールを公開
3. **REST API** — Web 化して agent から HTTP 呼び出し

MCP は構造化 tool 入力と Resources (auto-context 注入) を提供できる一方で:

1. **context 常駐コスト** — tool 定義が毎ターン context に乗る (5 サーバー × 数ツールで数 K トークン)
2. **保守コスト** — 認証・スキーマ・更新追従を MCP サーバー側で引き受ける必要
3. **エコシステム比** — `gh` / `m365` / `box-cli` 等の CLI は MCP 比で枯れている

CLAUDE.md グローバル方針 (CLI 優先) と整合的にも、CLI を主経路にする方が摩擦が少ない。

## Decision

MVP では **CLI を唯一の agent 接続経路** とする。MCP は MVP に含めない。

将来 MCP を追加する判断基準:

1. **CLI で実現困難な需要**が明確に発生したとき
   - 例: Resource として inbox サマリを毎セッション自動注入したい
2. **構造化入力で安全性が決定的に向上する**操作
   - 例: 送信系操作のスキーマ検証
3. **Sampling / Elicitation** が必要になったとき
   - サーバー側 LLM 呼び出し / ユーザー追加情報要求

判断時は `src/opshub/agents/mcp_server.py` に最小実装を追加する (Python MCP SDK を採用)。

CLI の設計原則:

1. **stdout 出力は `--format md|json|tsv` で切替可能** — agent からは json をパースし、人間からは md を読む
2. **副作用なし read 操作は引数のみで完結** (agent からの呼びやすさ)
3. **副作用あり write 操作は `--dry-run` を提供** — agent が確認に使う
4. **permission 摩擦削減** — `.claude/settings.json` の `permissions.allow` に `Bash(opshub:*)` を追加

## Consequences

### Positive

1. **context 経済** — tool 定義の常駐コストゼロ
2. **保守単純化** — CLI ロジックがそのまま agent インタフェース
3. **既存知識を活用** — agent は CLI を学習済み
4. **shell との合成性** — `opshub inbox list --format json | jq ...` のような自由な組み合わせ
5. **CI / cron との親和性** — sync workers が CLI で実装でき、agent との実装共通化

### Negative / Trade-offs

1. **Resource 自動注入が使えない** — daily digest を毎セッション冒頭に挟む場合、`SessionStart` hook + `opshub digest > today.md` の workaround で代用
2. **構造化入力のスキーマ自動検証なし** — Typer + Pydantic で CLI 引数を validate するため、影響は限定的
3. **将来 MCP 追加時に既存 CLI 利用箇所との重複** — ADR 起票 + migration plan で対応

## 軽減策

1. **`.claude/settings.json` の SessionStart hook** で `opshub digest --since yesterday > ~/.claude/context/today.md` を実行し、agent に自動注入する
2. **CLI 引数は Typer + Pydantic で型付け** — MCP の構造化入力と同等の安全性
3. **`opshub <op> --help` / `--examples`** を充実 — agent が CLI を学習しやすくする

## Alternatives Considered

### 1. MCP-first (MVP から MCP サーバーを提供)

却下理由:

- 保守コストが MVP の規模に見合わない
- agent context 常駐コストが累積する
- CLI を別途用意しないと cron / CI / shell 利用ができない (二重実装)

### 2. CLI + MCP を MVP から両提供

却下理由:

- 二重実装で工数が増える
- CLI と MCP のスキーマ整合維持コストが継続発生
- MVP 段階で需要証明されていない

### 3. REST API

却下理由:

- ローカル利用に Web サーバーは不要
- 認証 / セキュリティ層が増える
- CLI で同等のことができる

### 4. Python SDK のみ提供 (CLI なし)

却下理由:

- agent が `python -c "..."` で呼ぶ形になり、引数渡しが煩雑
- shell / cron / 他言語からの利用が困難
- CLI ラッパーは最終的に必要

## 関連

- [Principles 4 (Agent Runtime Boundary)](../principles.md)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md)
- [ADR-0031: CLI Command Surface Organization](0031-cli-command-surface-organization.md) — CLI top-level group の組織方針 (noun-first / per-noun group / 2 階層) は本 ADR の延長として ADR-0031 で確定。ADR-0006 §決定 の 4 原則 (`--format` 切替 / read 副作用なし / write `--dry-run` / permission 摩擦削減) は ADR-0031 でも維持
- 知識 MCP: `ai/platform/mcp-protocol` (将来 MCP 追加時の参照)
