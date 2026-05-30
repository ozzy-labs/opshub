# Secretary Agent — opshub の秘書層を使う

opshub は Phase 10 (秘書エージェント・プラットフォーム化) で「人間 → 秘書エージェント → opshub コマンド」の三層モデルへ拡張された。本 doc は秘書エージェントの使い方を、依頼例 / Skills 一覧 / できること・できないこと の順で示す。

設計の根拠は [ADR-0004 Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md) (形A: opshub は MCP + Agent Skills のみ提供、runtime は外部ホスト) と [ADR-0022 MCP Server Surface](adr/0022-mcp-server-surface.md) (MCP tool 面)。

## 形A: 何を opshub が持ち、何を外部ホストが担うか

opshub 本体が提供するもの:

1. **operational memory (①コア)** — events / projections / connectors / recall / propose / brief / graph
2. **MCP サーバ (口)** — `opshub mcp serve` (stdio)。エージェント host が ①コアを叩く経路
3. **Agent Skills (手順書)** — SKILL.md 標準。本 doc が catalog する 5 skill。`ozzy-labs/skills` リポに実体を置き Renovate preset 経由で配布
4. **skill security scan** — `tools/skill_scan.py` (4 カテゴリ + frontmatter 隠し命令検出)

opshub 本体が **持たない** もの:

- LLM 推論ループ / ReAct ループ / LangGraph state machine — 外部ホスト (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI) の責務
- 秘書の人格 / 常駐プロセス / 能動的な push / cron 内包 — 外部ホスト or OS-level scheduler の責務
- 外部 SaaS への書き戻し (返信送信 / コメント投稿 / ファイル upload) — Phase 10 では実装しない (ADR-0010 §禁止事項 7)

## 秘書への依頼例

外部ホスト (Claude Code 等) に対して以下のように頼むと、対応する skill が発火する。

| ユーザー入力 | 発火する skill | 結果 |
|---|---|---|
| 「今日のまとめ」「最近どうなってる」「状況教えて」 | [daily-brief](skills/daily-brief/SKILL.md) | 当日 / 直近 24h の主要シグナル + active task + 未処理 inbox + 直近 decision |
| 「次に何やる?」「やること教えて」「優先度高いのは?」 | [next-actions](skills/next-actions/SKILL.md) | 優先度順の next-actions リスト。新規 task 追加は人確認付き |
| 「これに返信案考えて」「下書き作って」 | [reply-draft](skills/reply-draft/SKILL.md) | 返信下書き候補。送信は行わずユーザーが手で貼り付け |
| 「PR #N レビューして」「この差分どう?」 | [pr-review](skills/pr-review/SKILL.md) | 関連 decision / task / 過去議論を引いてレビュー観点を提示 |
| 「Box にあったあの資料」「<キーワード>含むファイル」 | [file-lookup](skills/file-lookup/SKILL.md) | 本文ベース横断検索で Box / Slack / GitHub / MS365 / Box Drive から該当 source |

## Skills 一覧

| skill | 主な MCP tool | 自律範囲 | 書き込み tool (write) |
|---|---|---|---|
| daily-brief | `recall.search`, `task.list`, `inbox.list`, `decision.list` | 自律 OK | なし |
| next-actions | `task.list`, `recall.search` (+ 人確認付き `task.create`) | read 自律 / write 人確認 | `task.create` (HITL) |
| reply-draft | `recall.search` + CLI `propose generate --reply-to <source_id>` + CLI `propose apply` | apply 時は人確認 | CLI `propose apply` (HITL) |
| pr-review | `recall.search`, `decision.list`, `task.list` | 自律 OK | なし (PR comment 投稿は skill 外) |
| file-lookup | `recall.search` | 自律 OK | なし |

skill 本体 (SKILL.md) は `docs/skills/<name>/SKILL.md` に置く reference 仕様。実際の配布は [`ozzy-labs/skills`](https://github.com/ozzy-labs/skills) リポから `@ozzylabs/skills` Renovate preset 経由で各ホストの `.claude/skills/` に届く (handbook ADR-0016)。

## できること / できないこと

### できること

- opshub に蓄積された **本文ベースの operational memory** (Phase 10 Sub A: 本文保持 + 暗号化、Sub B: 本文 embedding + FTS5) を横断検索 / 要約 / 関連抽出する
- 過去の decision / task / proposal / event を踏まえた **文脈付き** の応答 (`--expand-graph` で知識グラフ拡張、ADR-0017)
- 返信下書きを「自分の過去送信 event」の文体を recall して再現 (ADR-0016 §決定 (k))
- 複数 agent host (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI) から **同一の MCP 面** を叩いて同じ記憶を共有

### できないこと (構造的な禁止)

- **外部 SaaS への書き戻し** — Slack / Box / GitHub / MS365 に返信送信 / コメント投稿 / ファイル upload しない (ADR-0010 §禁止事項 7 + Phase 10 Sub E の経路非存在 test pin)
- **能動的な push / 通知** — 「3 時に reminder 送る」「inbox を 1 時間ごとにチェック」のような常駐 runtime は持たない (ADR-0004 §(a) 形A、Phase 10 plan §1 #5)
- **LLM 推論の opshub 内蔵** — opshub は推論ループを実行しない。LLM 呼び出し (Anthropic / OpenAI / Ollama) は `opshub propose` / `opshub brief` のようなコマンド経路でユーザーが明示的に起動したときのみ発生 (ADR-0015)
- **auto-apply** — `opshub propose apply` は必ず人が叩く (ADR-0016 §決定 (c))。skill 側でも write tool (`task.create` / `inbox.add` / `connector.sync`) は HITL 必須

## セットアップ

### 1. opshub MCP server をホストから起動できるようにする

詳細は [`docs/mcp-setup.md`](mcp-setup.md) を参照。最小限は次の通り。

```bash
# 環境準備
uv tool install ozzylabs-opshub[mcp]
opshub init   # 初回のみ

# MCP server を stdio で起動 (host が subprocess として spawn する想定)
opshub mcp serve
```

### 2. 秘書 Skills をホストに配布する

`@ozzylabs/skills` Renovate preset を導入したリポでは `.claude/skills/` 配下に自動で配布される (handbook ADR-0016)。手動でセットアップする場合は `ozzy-labs/skills` リポから当該 skill の SKILL.md を `.claude/skills/<name>/` に置く。

opshub 本体リポでは `docs/skills/<name>/SKILL.md` が reference 仕様として置かれている (本リポではホスト向けの配布物ではなく、`ozzy-labs/skills` 側の SSOT として参照)。

### 3. ホストから skill を呼ぶ

各ホスト固有の skill loader 経路 (Claude Code は `Skill(skill="...")` ツール、Codex CLI は AGENTS.md 経由、等) で skill を発火する。ユーザーは「今日のまとめ」のような自然文で頼むだけでよい (skill description が日本語トリガを含むため)。

## skill security について

`tools/skill_scan.py` で 4 カテゴリ (プロンプトインジェクション / コマンドインジェクション / ハードコード鍵 / データ持ち出し) + frontmatter の隠しユニコード / 「ignore previous instructions」類のパターン検出を行う。

- 本リポ内 (`docs/skills/<name>/SKILL.md`) の仕様には test (`tests/unit/skills/test_skill_specs.py`) で適用済
- `ozzy-labs/skills` 側 CI への組み込みは別 PR (本 PR の scope 外)
- 検出ルールは scope 縮小設計 (高 precision / 中 recall)。誤検出は `# skill-scan: allow <category>` コメントで局所的に suppress 可能

## 関連

- [ADR-0004 Agent Runtime Boundary (形A)](adr/0004-agent-runtime-boundary.md)
- [ADR-0010 Connector Contract (write-back 禁止)](adr/0010-connector-contract.md)
- [ADR-0016 Action Loop (reply-draft / auto-apply 禁止)](adr/0016-action-loop-and-structured-output.md)
- [ADR-0020 Full Local Content Retention](adr/0020-full-local-content-retention.md)
- [ADR-0022 MCP Server Surface](adr/0022-mcp-server-surface.md)
- [docs/mcp-setup.md](mcp-setup.md)
- [Phase 10 Implementation Plan](phase-10-plan.md)
- handbook ADR-0016 (skills repo `ozzy-labs/skills` 配布機構)
