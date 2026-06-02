---
name: personal-brief
description: '「今日のまとめ」「今週どうなってる」「今月の動き」「先週の状況」「先月の振り返り」「最近どうなってる」「状況教えて」「自分の状況」と聞かれたら、opshub MCP の brief (LLM 要約) または recall.search / task.list / inbox.list / decision.list を順に叩いて指定期間 (デフォルト直近 24h) の主要な動きを要約する。期間は ISO 8601 timestamp を physical-column 時間フィルタ (updated_after/before / created_after/before / recorded_after/before) に渡してホスト側で組み立てる。LLM 推論ループは外部ホスト (Claude Code 等) 側、本 skill は手順書のみで実処理を持たない。pair: external-brief (外向き) と対をなす。'
---

# personal-brief — 自分向けの状況サマリを opshub から組み立てて返す

opshub MCP server (`opshub mcp serve`、ADR-0022) 経由で当日 / 今週 / 今月 / 先週 / 先月 など指定期間の operational memory を要約する。Phase 10 Sub-issue D で導入された Tier 1 skill (旧称 personal-brief の前身)、Phase 12 H1 で `personal-brief` に rename + 期間指定対応 + MCP 直接呼びに統一。

外向き (上司 / チーム / 顧客向け) のまとめは `external-brief` skill が担当する。本 skill は自分向け（粒度細かめ、雑多 OK、進行中タスクや未処理 inbox も含めて見せる）。

## 何が起きるか (host 側の流れ)

1. ユーザーが「今日のまとめ」「今週どうなってる」「先月の振り返り」のような表現で問い合わせる
2. 外部ホスト (Claude Code 等) が本 skill を発火させる
3. ホストが対象期間を ISO 8601 timestamp に解釈する (今日 / 今週 / 今月 / 先週 / 先月 等)
4. ホストが下記「呼び出し順」に従って opshub MCP tool を読み取り (read) 系のみ呼び出す
5. 戻り値 (JSON 文字列) を集約し、ユーザー向けに要約する

opshub 側で能動的に「日次まとめを送る」runtime は走らない (ADR-0004 §(a) 形A)。本 skill はリクエスト駆動で、ユーザーが問い合わせた瞬間にホストがツールを叩く。

## 期間の解釈 (ホスト側)

ユーザー発言からホストが ISO 8601 timestamp を解釈する：

| ユーザー語彙 | 期間（半開区間） | フィルタ |
|---|---|---|
| 今日 / 直近 24h | 当日 00:00 (local TZ) 〜 now | `*_after=今日00:00` |
| 今週 | 月曜 00:00 〜 now | `*_after=今週月曜00:00` |
| 今月 | 月初 00:00 〜 now | `*_after=今月1日00:00` |
| 先週 | 先週月曜 00:00 〜 先週日曜 23:59 | `*_after=先週月曜` / `*_before=今週月曜` |
| 先月 | 先月 1 日 〜 今月 1 日 | `*_after=先月1日` / `*_before=今月1日` |
| 最近 (デフォルト) | 直近 24h | `*_after=now-24h` |

各 tool は **physical-column ベース**の独立した argument 名を持つ (ADR-0022 改訂 §決定、Phase 12 H1)：

- `task.list`: `updated_after` / `updated_before`（→ `tasks.updated_at`）
- `inbox.list`: `created_after` / `created_before`（→ `inbox_items.created_at`）
- `decision.list`: `recorded_after` / `recorded_before`（→ `decisions.recorded_at`）
- `source.list`: `observed_after` / `observed_before`（→ `sources.observed_at`）

## 呼び出し順 (MCP tool)

### Step 0 (オプション): 一発要約は `brief` を使う

LLM backend が configured なら、`brief` 一発で対象期間の Markdown 要約が返る。Step 1〜4 を回す前にこの 1 呼び出しを試して、戻り値の Markdown を素のまま提示できれば Step 1-4 は省略可能。

```text
tool: brief
input:
  topic: "<期間ラベル>"   # 例: "今日" / "今週" / "先月"
  format: "md"
  max_sources: 20
```

戻り値: `{"format":"md", "briefing_id":"...", "markdown":"...", "source_count": N}`。LLM 未設定 / token 不足の場合は失敗するので、その場合は Step 1〜4 に fallback。

### Step 1: 直近の重要シグナルを recall で拾う

```text
tool: recall.search
input:
  query: "<期間ラベル or 自由文>"
  limit: 20
```

戻り値の `hits[]` を「source / task / decision のどれを優先表示するか」を判断する素材とする。`recall.search` は ADR-0012 hybrid recall (本文 embedding + FTS5 + graph 補強) で、本文ベースで hit する。

### Step 2: 期間内に更新された task を列挙

```text
tool: task.list
input:
  state: "active"
  updated_after: "<期間開始 ISO 8601>"
  updated_before: "<期間終了 ISO 8601>"   # オプション
  limit: 30
```

戻り値の `items[]` から「期限が近い / 担当 self / 進捗が止まっている」ものをホスト側で抽出。期間内に completed したものを見たい場合は `state: "completed"` + `updated_after` の組み合わせ（`completed_at` 列は projection に存在しないため `updated_at` で近似）。

### Step 3: 期間内に enqueue された inbox を列挙

```text
tool: inbox.list
input:
  state: "pending"
  created_after: "<期間開始 ISO 8601>"
  limit: 20
```

triage 前 (`pending`) の inbox item を拾う。ADR-0010 で「connector が直接 task 化しない」境界が確立されているため、inbox を見ないと外部由来の未処理シグナルが漏れる。

### Step 4: 期間内の decision を確認

```text
tool: decision.list
input:
  recorded_after: "<期間開始 ISO 8601>"
  recorded_before: "<期間終了 ISO 8601>"   # オプション
  limit: 10
```

期間内の意思決定 / コミットメントを確認。再質問されたときの根拠として使う。`recorded_after` / `recorded_before` フィルタが Phase 12 H1 で `decisions.recorded_at` ベースで追加された。

## 出力フォーマット (ホスト側)

ホストが以下のような構造でユーザーに返す。具体的な文面はホスト側 LLM が決める (本 skill は構造のみ pin)。

```text
# <期間ラベル>の状況

## 注目シグナル (recall.search 上位)
- ...

## 進行中の task
- [P1] ... (due: YYYY-MM-DD)
- ...

## 未処理 inbox
- ...

## 直近の decision
- ...
```

## 自律範囲

- **自律 OK** — すべて read 系 tool (`readOnlyHint=true`、ADR-0022 §(c))。確認なしで呼んでよい。
- durable state を変える tool (`task.create` / `inbox.add` / `connector.sync` / `propose.apply`) は本 skill では呼ばない。

## できないこと / やらない

- 外部 SaaS への投稿 / 通知送信 (ADR-0010 §禁止事項 7、Phase 10 plan §1 #6)
- 期間の動きを能動的に push する (ADR-0004 §(a)、Phase 10 plan §1 #5、能動機能は当面持たない)
- 推論結果を opshub の durable state に書き戻す (`propose.generate` / `propose.apply` は別 skill / 操作の責務)
- 外向き (上司 / チーム / 顧客向け) のまとめ → `external-brief` skill の責務

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0016 改訂 (Action Loop、Phase 12 H1 で draft 系統一方針追加)
- ADR-0022 改訂 (MCP Server Surface、Phase 12 H1 で physical-column 時間フィルタ追加)
- ADR-0025 (Office 文書本文抽出)
- ADR-0010 §改訂 (connector contract、Phase 11 で Teams 追加)
- ADR-0020 §改訂 (Outlook body deep retention、Phase 11)
- Phase 10 plan §3-D (skill ↔ MCP tool マッピング)
- Phase 11 plan (`docs/phase-11-plan.md`)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H1)
- docs/secretary-agent.md (Skill catalog SSOT、14 skills 責務マップ)
