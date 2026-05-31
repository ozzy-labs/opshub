---
name: reply-draft
description: 「返信案を考えて」「下書き作って」「これに返信したい」と頼まれたら、opshub MCP の propose.generate (reply_to_source_id 指定) で返信下書きを生成し、ユーザー確認後に propose.apply で保存する。外部 SaaS への送信は行わず、ユーザーが下書きを確認して手で送る。Sub-issue E で実装済みの ReplyDraftCandidatePayload を利用。
---

# reply-draft — 返信下書きを opshub に生成させる

opshub の MCP write tool `propose.generate`（mode: `reply_to_source_id`、PR #231 で実装、`src/opshub/mcp/_registry.py` の `WriteCategory.PROPOSE_GENERATE`）と `propose.apply`（Phase 12 H1 で MCP に露出、`WriteCategory.PROPOSE_APPLY`、idempotent）を第一経路として返信下書きを作る。Phase 10 Sub-issue D で書き、Sub-issue E (#217 merged) で `ReplyDraftCandidatePayload` が実装済み。ADR-0016 §決定 (i)+(j)+(k) で吸収された。Phase 11 で Outlook body deep retention (#244 / ADR-0020 改訂) が入り、`ms365_outlook` への reply-draft が本格機能化した（差出人の本文を full payload で context 注入できるようになった）。

## 何が起きるか

1. ユーザーが「これに返信下書き作って」「この slack に返信案」「メール返信どうする?」と頼む
2. ホストが本 skill を発火
3. ホストが対象の `source_id` を特定 (recall.search で source を引く or ユーザー入力)
4. MCP `propose.generate` (`reply_to_source_id` 指定) を呼んで `ProposalGenerated` event を発行 (HITL write tool)
5. 候補をホストがユーザーに提示、ユーザー確認後に MCP `propose.apply` で下書き保存（HITL、idempotent）
6. ユーザーが下書きを見て手で送信先 SaaS に貼り付ける

opshub 側で外部 SaaS に直接投稿する経路は **存在しない** (ADR-0010 §禁止事項 7 + Sub-issue E test pin)。

## 呼び出し順

### Step 1: 返信元 source を特定

ユーザー入力に slack URL / メッセージ ID / GitHub issue 番号などがある場合はそれを使う。なければ recall で検索:

```text
tool: recall.search
input:
  query: "<返信したい相手 / トピック>"
  limit: 10
```

`hits[]` から source_id を抜き出す。`source_type` は CLI に渡す必要はない (service 側が source row から判定する)。

### Step 2: 下書き生成 (MCP 経路、第一)

PR #231 で MCP write tool `propose.generate` が実装済み (`src/opshub/mcp/_registry.py:673-743`、`src/opshub/mcp/_writes.py:215`、`WriteCategory.PROPOSE_GENERATE`)。ホストは MCP 経由で:

```text
tool: propose.generate
input:
  topic: ""                          # 空でよい (reply_to_source_id 指定時は無視)
  reply_to_source_id: "<source ULID>"
  expand_graph: true                  # ADR-0017 §(e)+(f) graph 1-hop で文脈拡張
  max_candidates: 3                   # 1〜20 (既定 5)
  from_briefing_id: ""                # 任意。previous briefing markdown を seed に使う場合のみ
```

`propose.generate` は write tool (`destructiveHint=true` 相当の HITL 境界、ADR-0016 §決定 (c))。LLM round-trip を伴い `ProposalGenerated` event を durable log に書く。apply はこの後の Step 4 で別途人が叩く (auto-apply 経路は存在しない)。

文体は `author = self` の過去送信 event を recall で引き `<style_example>` として注入される (ADR-0016 §決定 (k))。ホストが追加で style を渡す必要はない。

### Step 3: 候補を提示

ホストが `propose.generate` 戻り値の `candidates[]` をユーザーに整形して提示する。候補 1 件ずつ `body` (下書き本文) と `index` を併記。複数候補がある場合はユーザーに選ばせる。

戻り値スキーマ（抜粋）：

```json
{
  "ok": true,
  "proposal_id": "01H...",
  "candidates": [
    {"index": 0, "kind": "reply_draft", "body": "...", ...},
    {"index": 1, "kind": "reply_draft", "body": "...", ...}
  ],
  "hitl_apply_required": true
}
```

### Step 4 (人確認必須): MCP `propose.apply` で下書き保存

Phase 12 H1 (ADR-0022 改訂) で MCP に `propose.apply` write tool が露出した（`WriteCategory.PROPOSE_APPLY`、`idempotent=true`）。`destructive=false` + handler 層で `OpsHubError("already applied/rejected")` を catch → `{ok:true, already_applied:true, applied_entity_type, applied_entity_id}` に正規化する semantics を持つ：

```text
tool: propose.apply
input:
  proposal_id: "<proposal ULID>"
  candidate_index: <integer (Step 3 で選んだ index)>
```

apply は durable state を変える (`ProposalApplied` event を発行)。ホストは必ずユーザーに「この下書きを保存しますか?」と確認する (ADR-0016 §決定 (c) HITL 必須)。

戻り値：
- 1 回目：`{"ok": true, "already_applied": false, "applied_entity_type": "reply_draft", "applied_entity_id": "<ULID>"}`
- 2 回目（同じ `(proposal_id, candidate_index)` で再呼び出し）：`{"ok": true, "already_applied": true, "applied_entity_type": "reply_draft", "applied_entity_id": "<同じ ULID>"}` — `OpsHubError` を投げず idempotent semantics 成立

## 出力フォーマット (ホスト側)

```text
# 返信下書き候補

## 返信元
- source_id: ...
- source_type: slack_message / ms365_outlook / teams_message / issue / pull_request / ...

## 候補 1
> <下書き本文>

## 候補 2 (あれば)
> ...

## 次のアクション
- 良ければ `propose.apply` で保存
- 修正したければユーザーが手で編集して送る
- 送信は opshub から行わない (ユーザーが手で外部 SaaS に貼り付け)
```

## できないこと / やらない

- **外部 SaaS への送信** — 設計上不可。connector に `post` / `send` / `comment` / `reply` メソッドを実装しない契約が ADR-0010 §禁止事項 7 + Sub-issue E test pin で保証されている
- 確認なしの apply — ADR-0016 §決定 (c) で auto-apply は禁止、Phase 6.x 以降も追加しない方針
- `<style_example>` を上書きする外部スタイル指定 — recall した本人の過去送信 event を SSOT とする方針 (ADR-0016 §決定 (k))
- triage カテゴリ (`respond` / `notify` / `ignore`) の durable 化 — auto-apply 禁止の延長で hint 用途に留まる (ADR-0016 §決定 (j))

## 参考

- ADR-0010 §禁止事項 7 + §改訂 (write-back 禁止契約、Phase 11 で Teams 追加)
- ADR-0016 §決定 (i)(j)(k) (reply_draft candidate / triage / style-source recall)
- ADR-0017 §決定 (b) (link_type `reply_draft_replies_to` / `referenced_in_reply_draft`)
- ADR-0020 §改訂 (Outlook body deep retention、Phase 11 #244)
- ADR-0025 (Office 抽出、reply-draft が `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` を context として引ける)
- Sub-issue E PR #217 (`ReplyDraftCandidatePayload`、triage 3 分類、write-back 非存在 test pin)
- PR #231 (MCP `propose.generate` write tool)
- Phase 12 H1 (MCP `propose.apply` write tool、idempotent semantics)
- Phase 11 plan (`docs/phase-11-plan.md`)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H1)
- docs/secretary-agent.md
