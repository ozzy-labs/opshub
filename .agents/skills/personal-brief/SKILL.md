---
name: personal-brief
description: '「今日のまとめ」「今週どうなってる」「今月の動き」「先週の状況」「先月の振り返り」「最近どうなってる」「状況教えて」「自分の状況」と聞かれたら、opshub MCP の brief (LLM 要約) または recall.search / task.list / inbox.list / decision.list を順に叩いて指定期間 (デフォルト直近 24h) の主要な動きを要約する。Phase 18-C で slack.demand.list を追加し、Slack の @mention / DM 信号も「状況」に含める。Phase 25-D で commitment.list を追加し、相手待ちの件・期日のある約束も「状況」に含める。期間は ISO 8601 timestamp を physical-column 時間フィルタ (updated_after/before / created_after/before / recorded_after/before) に渡してホスト側で組み立てる。LLM 推論ループは外部ホスト (Claude Code 等) 側、本 skill は手順書のみで実処理を持たない。pair: external-brief (外向き) と対をなす。'
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

### Step 5 (Phase 18-C): Slack demand 信号を「状況」に含める

```text
tool: slack.demand.list
input:
  demand_kinds: ["dm", "mention"]
  since_ts: <期間開始 epoch float>   # オプション、Slack ts は Unix epoch float
  limit: 20
  order: "last_demand_desc"
```

Phase 18-C ([ADR-0033 §決定 (c)](../../adr/0033-slack-mention-demand-digest.md)) で追加された `slack.demand.list` は Phase 18-B `slack_demand_digest` projection を読み、`<@self>` mention と DM 相手の最終発言 (operator 視点で「自分が放置している ping」) を新しい順に返す。「今日のまとめ」「今週どうなってる」のような問い合わせでは、これを「期間内に自分宛に来た Slack」セクションとして含めると situation awareness が大きく向上する。

戻り値の `items[]` (`workspace` (`team_id` + `alias`、Phase 24-D / ADR-0041) / `channel_id` / `channel_type` / `channel_name` / `demand_kind` / `last_demand_at` / `last_demand_user_id` / `last_demand_excerpt` / `last_demand_permalink` / `last_source_id`) を期間内 (`since_ts` でフィルタ) で表示。複数 Slack workspace 構成では同じ `channel_id` が workspace ごとに 1 行ずつ現れうるので、行の同定には `workspace.team_id` + `channel_id` を使う (`workspace.alias` は operator が config で命名した label、未 bind なら null)。`last_demand_at` は ISO 8601 UTC 文字列 (Phase 23-D / issue #534)、`channel_name` は DM なら相手の表示名 / channel なら `#name` (None の場合のみ `channel_id` を fall back 表示)、`last_demand_user_id` は相手の Slack `U...` id。自分が最後に発言した DM / mention は除外済み (Phase 23-D)。`last_demand_permalink` を付けると operator が直接 Slack UI に飛べる。

`slack.demand.list` は read-only / `readOnlyHint=true` / `openWorldHint=false` (local SQLite のみ、Slack API 不発火)。Slack への投稿 / reaction は本 skill から行わない (ADR-0010 §禁止事項 7)。

### Step 6 (Phase 25-D): コミットメント (相手待ち・期日のある約束) を「状況」に含める

```text
tool: commitment.list
input:
  state: "open"
  limit: 20
```

Phase 25-D ([ADR-0042](../../adr/0042-commitment-ledger.md)) で追加された `commitment.list` は `commitment.scan` が既存 source から抽出した双方向コミットメント台帳を読む。`state=open` で「未解決の約束」を引き、`direction` で「自分が頼んで待っている件 (`owed_to_me`)」と「自分が負った約束 (`i_owe`)」を分けて状況に含めると、受信トレイだけでは見えない双方向の進捗が可視化される。「今週どうなってる」のような問い合わせで「相手のボールで止まっている件」「期日が迫っている自分の約束」を出せるのが固有価値。

戻り値の `items[]` (`id` / `source_id` / `source_type` / `direction` / `counterparty` (`person:<id>` ref、未解決なら null) / `due` / `text` / `confidence` / `state`) を表示。`due` は LLM が読んだ**自由文** (「金曜まで」「2026-06-20」等) で構造化日付ではないため、期日超過の判定はホスト側で `due` を今日と突き合わせる (MCP に `due_before` フィルタは無い)。期間絞りは `commitment.list` 側には無いので、`state=open` を引いた上でホストが `due` / counterparty で取捨選択する。

`commitment.list` は read-only / `readOnlyHint=true` / `openWorldHint=false` (LLM を叩かず local SQLite の `commitments` projection を読むだけ。閲覧で再抽出は走らない、ADR-0042 §閲覧 LLM 不要)。督促の外部送信 / 状態遷移は本 skill から行わない (状態遷移は別 skill / CLI、ADR-0042 §督促境界)。

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

## Slack の demand 信号 (Phase 18-C、slack.demand.list)
- [DM] @alice 2026-06-02: 「<excerpt>」 → permalink
- [mention #general] @bob 2026-06-01: 「<excerpt>」 → permalink

## コミットメント (Phase 25-D、commitment.list)
- [相手待ち] @carol に依頼: 「<text>」 (due 2026-06-20)
- [自分の約束] 「<text>」 (due 2026-06-18、期日超過なら ⚠️)
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
- ADR-0022 改訂 (MCP Server Surface、Phase 12 H1 で physical-column 時間フィルタ追加、Phase 18 補遺で `slack.demand.list` 追加)
- ADR-0025 (Office 文書本文抽出)
- ADR-0010 §改訂 (connector contract、Phase 11 で Teams 追加)
- ADR-0020 §改訂 (Outlook body deep retention、Phase 11)
- ADR-0033 (Slack mention / DM demand digest、Phase 18) — `slack.demand.list` の根拠
- ADR-0042 (Commitment ledger、Phase 25) — `commitment.list` の根拠
- Phase 10 plan §3-D (skill ↔ MCP tool マッピング)
- Phase 11 plan (`docs/phase-11-plan.md`)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H1)
- docs/assistant-agent.md (Skill catalog SSOT、15 skills 責務マップ)
