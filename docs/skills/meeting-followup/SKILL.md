---
name: meeting-followup
description: 「会議後の action items」「ミーティングのフォローアップ」「議事録から task 抽出」「昨日の会議どうだった」「打ち合わせのフォロー」と頼まれたら、opshub MCP の source.list (source_type=ms365_calendar) で直近の会議を集め、source.get で議事録 / 関連やりとりを recall.search で引いた上で propose.generate (mode=meeting_followup) で task / decision 候補を生成し、ユーザー確認後に propose.apply で承認分のみ HITL 保存する。auto-apply 経路は存在しない (ADR-0016 §決定 (c))。
---

# meeting-followup — 直近の会議から action items を抽出する (HITL)

opshub MCP の `source.list`（read tool、`source_type=ms365_calendar` + `observed_after` / `observed_before` の時間フィルタは Phase 12 H1 で追加）で直近の会議を集め、`source.get` + `recall.search` で議事録や関連やりとりを context として引き、`propose.generate`（`mode=meeting_followup`、Phase 12 H4 で追加された dispatch key、ADR-0016 改訂 §決定 (l)(b)）で task / decision 候補を生成し、ユーザーが個別承認した候補のみ `propose.apply`（Phase 12 H1 で MCP に露出、`WriteCategory.PROPOSE_APPLY`、`read_only=false` + `idempotent=true`）で durable state に書き戻す。

`meeting-prep`（会議前）と pair をなす HITL write skill。

## 何が起きるか

1. ユーザーが「会議後の action items」「ミーティングのフォローアップ」「議事録から task 抽出」と頼む
2. ホストが本 skill を発火
3. ホストが `source.list` で直近 24h (or 指定 window) の `ms365_calendar` を集める
4. ホストが対象会議の `source.get` + `recall.search` で議事録 / 関連やりとりを context 化
5. ホストが `propose.generate`（`mode=meeting_followup`、`topic` = 会議トピック）を呼び、候補 (task / decision) を生成（`ProposalGenerated` event を durable log に書く）
6. ホストが候補をユーザーに整形して提示
7. ユーザーが個別承認した候補のみ `propose.apply` で保存（HITL、idempotent）

opshub 側で外部 SaaS に通知 / 投稿する経路は **存在しない**（ADR-0010 §禁止事項 7）。

## 呼び出し順

### Step 1: 直近の会議を列挙

```text
tool: source.list
input:
  source_type: "ms365_calendar"
  observed_after: "<24h 前 ISO 8601>"
  observed_before: "<now ISO 8601>"
  limit: 20
```

`observed_after` / `observed_before` は Phase 12 H1 で追加された physical column ベースの時間フィルタ（`sources.observed_at` 半開区間）。ユーザーが「昨日の」「先週の」と言ったら window を調整する。

### Step 2: 会議の本文を取得

```text
tool: source.get
input:
  source_id: "<ms365_calendar ULID>"
```

戻り値の `body` / `summary` / `title` から会議トピック・参加者・議題を抽出。複数会議を対象にする場合は `source.get` を会議ごとに呼ぶ。

### Step 3: 関連やりとりを recall で補強

```text
tool: recall.search
input:
  query: "<会議トピック / 参加者>"
  limit: 10
```

`hits[]` から会議前後の Slack / Teams / Outlook やりとり、関連 PR / Issue を引いて context に追加する。

### Step 4: 候補を生成（write tool、HITL boundary）

```text
tool: propose.generate
input:
  topic: "<会議トピック + 主要な議題>"
  mode: "meeting_followup"
  expand_graph: true       # graph 1-hop で文脈拡張 (ADR-0017 §(e)+(f))
  max_candidates: 8        # 1〜20。会議は action items が多めなので余裕を持つ
```

`mode=meeting_followup` は Phase 12 H4 で追加された dispatch key（ADR-0016 改訂 §決定 (l)(b)）。`proposals.scope` に `meeting_followup` が stamp され、後から audit 可能。`reply_to_source_id` とは排他。

`propose.generate` は write tool（`destructiveHint=true`、ADR-0022 §(c)）。LLM round-trip を伴い `ProposalGenerated` event が durable log に乗る。apply は Step 6 で別途人が叩く（auto-apply 経路は存在しない）。

### Step 5: 候補を提示

戻り値の `candidates[]` をユーザーに整形して提示。各候補に `index` / `kind` (`task` / `decision`) / payload が付くので、host LLM は「会議名 → 候補」のマッピングで並べる。

戻り値スキーマ（抜粋）：

```json
{
  "ok": true,
  "proposal_id": "01H...",
  "scope": "meeting_followup",
  "candidates": [
    {"index": 0, "kind": "task", "title": "...", "body": "..."},
    {"index": 1, "kind": "decision", "text": "...", "context": "..."}
  ],
  "hitl_apply_required": true
}
```

### Step 6 (人確認必須): 承認分のみ propose.apply

ユーザーが「0 番採用、1 番は skip」と選んだら、`candidate_index` を 1 件ずつ `propose.apply` で叩く。

```text
tool: propose.apply
input:
  proposal_id: "<proposal ULID>"
  candidate_index: <approved index>
```

`propose.apply` は HITL write tool（`read_only=false`、`destructive=false`、`idempotent=true`）。handler 層で `OpsHubError("already applied")` を catch → `{ok:true, already_applied:true, applied_entity_type, applied_entity_id}` に正規化されるため、再叩きは no-op。必ずユーザーに「この候補を保存しますか?」と確認する（ADR-0016 §決定 (c) HITL 必須）。

## 出力フォーマット (ホスト側)

```text
# 会議フォローアップ候補

## 対象会議
- title: ...
- source_id: ...
- observed_at: ...

## 候補 (action items)
### #0 [task] ...
> title / body

### #1 [decision] ...
> text / context

## 次のアクション
- 採用 index を教えてください → `propose.apply` で保存
- 全部 skip でよければ何もしない
```

## 自律範囲 / HITL boundary

- **read tool (`source.list` / `source.get` / `recall.search`)** — host LLM 自律 OK
- **write tool (`propose.generate`)** — LLM round-trip を伴う durable write、`destructiveHint=true`。ホストは人確認を取る（ADR-0022 §(c)）
- **write tool (`propose.apply`)** — `read_only=false`、durable state を変える。ホストは必ずユーザーに「これを保存しますか?」と確認する（ADR-0016 §決定 (c) auto-apply 禁止）

`propose.generate` → `propose.apply` の 2 段ゲートが本 skill の HITL boundary。両方とも host LLM が自動連鎖させてはいけない。

## できないこと / やらない

- **auto-apply** — ADR-0016 §決定 (c) で禁止、`propose.apply` を確認なしで叩く挙動は本 skill 仕様の外
- **会議参加者への通知 / 議事録投稿** — ADR-0010 §禁止事項 7（connector に `post` / `send` メソッドを実装しない契約）
- **会議の作成 / キャンセル** — Phase 12 時点で calendar への書き戻し経路は存在しない（read-only）
- **新 candidate kind** — ADR-0016 §決定 (l)(d) で `Candidate` discriminated union は `task | decision | reply_draft` の 3 kind で freeze

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0010 §禁止事項 7（write-back 禁止契約、Phase 11 で Teams 追加）
- ADR-0016 (Action Loop、§決定 (c) auto-apply 禁止、§決定 (l) draft 系統一方針 + `mode` 引数射程)
- ADR-0017 §(e)+(f)（graph 1-hop expand）
- ADR-0020 §改訂（Outlook body deep retention、Phase 11）
- ADR-0022 (MCP Server Surface、Phase 12 H1 改訂で `propose.apply` 露出 + `source.list` 時間フィルタ追加)
- PR #231 (MCP `propose.generate` + `source.get` widening)
- Phase 12 H1 (`docs/phase-12-plan.md` §3 H1)
- Phase 12 H4 (`docs/phase-12-plan.md` §3 H4)
- docs/secretary-agent.md
