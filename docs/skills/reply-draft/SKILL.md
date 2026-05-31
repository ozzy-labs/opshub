---
name: reply-draft
description: 「返信案を考えて」「下書き作って」「これに返信したい」と頼まれたら、opshub の propose generate --reply-to を CLI 経由で叩いて返信下書きを生成する。外部 SaaS への送信は行わず、ユーザーが下書きを確認して手で送る。Sub-issue E で実装済みの ReplyDraftCandidatePayload を利用。
---

# reply-draft — 返信下書きを opshub に生成させる

opshub の MCP write tool `propose.generate`（mode: `reply_to_source_id`、PR #231 で実装、`src/opshub/mcp/_registry.py` の `WriteCategory.PROPOSE_GENERATE`）を第一経路として返信下書きを作る。CLI 経路 `opshub propose generate --reply-to <source_id>` は同じ engine path を叩く fallback。Phase 10 Sub-issue D で書き、Sub-issue E (#217 merged) で `ReplyDraftCandidatePayload` が実装済み。ADR-0016 §決定 (i)+(j)+(k) で吸収された。Phase 11 で Outlook body deep retention (#244 / ADR-0020 改訂) が入り、`ms365_outlook` への reply-draft が本格機能化した（差出人の本文を full payload で context 注入できるようになった）。

## 何が起きるか

1. ユーザーが「これに返信下書き作って」「この slack に返信案」「メール返信どうする?」と頼む
2. ホストが本 skill を発火
3. ホストが対象の `source_id` を特定 (recall.search で source を引く or ユーザー入力)
4. MCP `propose.generate` (`reply_to_source_id` 指定) を呼んで `ProposalGenerated` event を発行 (HITL write tool)
5. 候補を CLI `opshub propose list` で確認、`opshub propose apply <proposal_id> <candidate_index>` で下書き保存 (人確認)
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

#### Step 2 fallback: CLI 経路

MCP server が起動していない場合や、CLI で手動確認したい場合は同等の経路:

```bash
opshub propose generate "" \
  --reply-to <source_id> \
  --expand-graph
```

位置引数 `topic` は `--reply-to` 指定時は無視されるため空文字 `""` で OK (CLI 実装の docstring 参照)。`--expand-graph` (ADR-0017) は知識グラフから文脈 source を拾うフラグ。ADR-0016 §決定 (k) の `<context_source>` 注入経路。MCP と CLI は同じ `build_proposal_service` engine path を叩くため挙動は同じ。

### Step 3: 候補を確認 (CLI 経路のみ)

`propose list` は今も CLI 経路のみ (MCP `propose.list` write tool は未実装。read 系で候補を引きたい場合は本 CLI を使う):

```bash
opshub propose list --state pending
```

`propose list` には `--kind` フィルタは無いため、reply_draft 候補も task / decision 候補と同じ pending bucket に並ぶ。`generated_at DESC` ソートで最新が先頭に来るので、直前に生成した reply_draft は通常 1 行目に出る。

最新 candidate の本文を表示。複数候補がある場合はユーザーに選ばせる。

### Step 4 (人確認必須、CLI 経路のみ): apply で下書き保存

`propose apply` も CLI 経路のみ (MCP `propose.apply` write tool は未実装。auto-apply 経路を意図的に作らないため、tool poisoning 攻撃面を縮減する設計):

```bash
opshub propose apply <proposal_id> <candidate_index>
```

apply は durable state を変える (`ProposalApplied` event を発行)。ホストは必ずユーザーに「この下書きを保存しますか?」と確認する (ADR-0016 §決定 (c) HITL 必須)。

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
- 良ければ `apply` で保存
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
- Phase 11 plan (`docs/phase-11-plan.md`)
- docs/secretary-agent.md
