---
name: inbox-triage
description: 「受信箱整理して」「inbox 仕分けて」「未処理アイテム捌いて」「pending を片付けて」と頼まれたら、opshub MCP の inbox.list (state=open) で未処理アイテムを集め、propose.generate (mode=inbox_triage) で各アイテムへの action 候補 (task 化 / decision 記録) を生成し、ユーザー確認後に propose.apply で承認分のみ HITL 保存する。auto-apply 経路は存在しない (ADR-0016 §決定 (c))。
---

# inbox-triage — 未処理 inbox を一気に仕分ける (HITL)

opshub MCP の `inbox.list`（read tool、ADR-0022）で未処理 inbox を集めた上で `propose.generate`（`mode=inbox_triage`、Phase 12 H4 で追加された dispatch key、ADR-0016 改訂 §決定 (l)(b)）に投げ、候補を host LLM がユーザーに提示する。ユーザーが個別に承認したものだけを `propose.apply`（Phase 12 H1 で MCP に露出、`WriteCategory.PROPOSE_APPLY`、`read_only=false` + `idempotent=true`）で durable state に書き戻す。

`reply-draft` と対をなす HITL write skill。集合 (inbox 全体) を扱う点で `source-extract`（個別 source 1 件）の pair。

## 何が起きるか

1. ユーザーが「受信箱整理して」「inbox 仕分けて」「未処理アイテム捌いて」のように頼む
2. ホストが本 skill を発火
3. ホストが `inbox.list` で未処理 (`state=open`) item を集める
4. ホストが `propose.generate`（`mode=inbox_triage`、`topic` = inbox 要約）を呼び、各 item に対する候補 (task / decision) を生成 (`ProposalGenerated` event を durable log に書く)
5. ホストが候補をユーザーに整形して提示
6. ユーザーが個別に承認した候補のみ `propose.apply` で保存（HITL、idempotent）

opshub 側で外部 SaaS に通知 / 投稿する経路は **存在しない**（ADR-0010 §禁止事項 7）。

## 呼び出し順

### Step 1: 未処理 inbox を列挙

```text
tool: inbox.list
input:
  state: "open"
  limit: 50
```

戻り値の `items[]` から「処理が必要そう」なものを host LLM が判定する。期間で絞りたい場合は `created_after` / `created_before`（ISO 8601、`inbox_items.created_at` 半開区間、Phase 12 H1 で追加）を併用する。

### Step 1b (Phase 18-C、補強): Slack demand 信号で priority 順を補強する

Slack 由来の mention / DM は既に Phase 7 connector 経由で取り込まれているため `inbox_items` ではなく `sources` 行として存在する (`source_type=slack_message`)。とはいえ「自分宛に未処理で残っている mention / DM」を inbox の triage 候補と並べて確認したい場合は、`slack.demand.list` を補助的に呼んで priority 順位を補強できる:

```text
tool: slack.demand.list
input:
  demand_kinds: ["dm", "mention"]
  limit: 20
  order: "last_demand_desc"
```

戻り値の `items[]` (`channel_id` / `channel_name` / `demand_kind` / `last_demand_ts` / `last_demand_excerpt` / `last_demand_permalink` / `last_source_id`) は「inbox 候補」そのものではなく **priority 補強用の external signal**。本 skill の triage 対象 (Step 2 で `propose.generate` に渡す素材) は引き続き `inbox.list` の row。Slack mention / DM 自体は既に inbox row として `sources` に存在し、必要なら `last_source_id` から `source.get` で本文を引ける。

Phase 18-C ([ADR-0033 §決定 (c)](../../adr/0033-slack-mention-demand-digest.md)) で追加された `slack.demand.list` は read-only / `readOnlyHint=true` / `openWorldHint=false` (local SQLite のみ、Slack API 不発火)。Slack への投稿 / reaction は本 skill scope 外 (ADR-0010 §禁止事項 7)。

### Step 2: 候補を生成（write tool、HITL boundary）

```text
tool: propose.generate
input:
  topic: "<inbox 全体の要約 or 主トピック>"
  mode: "inbox_triage"
  max_candidates: 10       # 1〜20。inbox は item 数だけ候補を出したいので多め
```

1-hop graph 拡張による context 補強は default で常時走る (ADR-0017 §(e)+(f)、epic #470 で `expand_graph` param 削除)。

`mode=inbox_triage` は Phase 12 H4 で追加された dispatch key（ADR-0016 改訂 §決定 (l)(b)）。`proposals.scope` に `inbox_triage` が stamp されるため、後から「どの skill が起こした proposal か」を audit log で join できる。`reply_to_source_id` とは排他（reply-draft mode は別経路）。

`propose.generate` は write tool（`destructiveHint=true`、ADR-0022 §(c)）。LLM round-trip を伴い `ProposalGenerated` event が durable log に乗る。apply は Step 4 で別途人が叩く（auto-apply 経路は存在しない）。

### Step 3: 候補を提示

戻り値の `candidates[]` をユーザーに整形して提示する。各候補に `index` / `kind` (`task` or `decision`) / payload が付くので、host LLM は「item A → task 化候補 / item B → decision 記録候補 / item C → skip」のような対応表として並べる。

戻り値スキーマ（抜粋）：

```json
{
  "ok": true,
  "proposal_id": "01H...",
  "scope": "inbox_triage",
  "candidates": [
    {"index": 0, "kind": "task", "title": "...", "body": "..."},
    {"index": 1, "kind": "decision", "text": "...", "context": "..."}
  ],
  "hitl_apply_required": true
}
```

### Step 4 (人確認必須): 承認分のみ propose.apply

ユーザーが「0 番と 2 番を採用」のように選んだら、選ばれた `candidate_index` だけを `propose.apply` で 1 件ずつ叩く。

```text
tool: propose.apply
input:
  proposal_id: "<proposal ULID>"
  candidate_index: <approved index>
```

`propose.apply` は HITL write tool（`read_only=false`、`destructive=false`、`idempotent=true`）。handler 層で `OpsHubError("already applied")` を catch → `{ok:true, already_applied:true, applied_entity_type, applied_entity_id}` に正規化されるため、同 `(proposal_id, candidate_index)` への 2 回目呼び出しは no-op。ユーザーに「この候補を保存しますか?」と必ず確認する（ADR-0016 §決定 (c) HITL 必須）。

未承認の候補は何もしなくてよい（`ProposalRejected` を明示的に発行したい場合のみ別途 `opshub propose reject` を使う。MCP には reject tool は露出していない）。

## 出力フォーマット (ホスト側)

```text
# 受信箱の仕分け候補

## 未処理 inbox: N 件

## 候補
### #0 [task] 〆切付きタスク化
> title / body

### #1 [decision] 決定記録
> text / context

### #2 [skip 推奨]
> 候補生成なし or 低信頼

## 次のアクション
- 採用する index を教えてください → `propose.apply` で保存
- 全部 skip でよければ何もしない
```

## 自律範囲 / HITL boundary

- **read tool (`inbox.list` / `slack.demand.list`)** — host LLM 自律 OK
- **write tool (`propose.generate`)** — LLM round-trip を伴う durable write、`destructiveHint=true`。ホストは人確認を取る（ADR-0022 §(c)）
- **write tool (`propose.apply`)** — `read_only=false`、durable state を変える。ホストは必ずユーザーに「これを保存しますか?」と確認する（ADR-0016 §決定 (c) auto-apply 禁止）

`propose.generate` → `propose.apply` の 2 段ゲートが本 skill の HITL boundary。両方とも host LLM が自動連鎖させてはいけない。

## できないこと / やらない

- **auto-apply** — ADR-0016 §決定 (c) で禁止、`propose.apply` を確認なしで叩く挙動は本 skill 仕様の外
- **外部 SaaS への通知 / 共有** — ADR-0010 §禁止事項 7（connector に `post` / `send` メソッドを実装しない契約）
- **inbox item の削除** — Phase 12 時点で inbox 行を削除する MCP tool は存在しない。apply で task / decision 化した後は元 inbox 行はそのまま残る（audit 用途）
- **新 candidate kind** — ADR-0016 §決定 (l)(d) で `Candidate` discriminated union は `task | decision | reply_draft` の 3 kind で freeze。inbox-triage 専用 kind は作らない

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0010 §禁止事項 7（write-back 禁止契約、Phase 11 で Teams 追加）
- ADR-0016 (Action Loop、§決定 (c) auto-apply 禁止、§決定 (l) draft 系統一方針 + `mode` 引数射程)
- ADR-0017 §(e)+(f)（graph 1-hop expand）
- ADR-0022 (MCP Server Surface、Phase 12 H1 改訂で `propose.apply` 露出、Phase 18 補遺で `slack.demand.list` 追加、read/write 分離)
- ADR-0033 (Slack mention / DM demand digest、Phase 18) — `slack.demand.list` で priority 補強する根拠
- PR #231 (MCP `propose.generate` write tool)
- Phase 12 H1 (`docs/phase-12-plan.md` §3 H1、`propose.apply` 露出 + 時間フィルタ追加)
- Phase 12 H4 (`docs/phase-12-plan.md` §3 H4、本 skill 含む HITL write 3 skill の追加)
- docs/assistant-agent.md
