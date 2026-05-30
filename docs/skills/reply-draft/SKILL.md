---
name: reply-draft
description: 「返信案を考えて」「下書き作って」「これに返信したい」と頼まれたら、opshub の propose generate --reply-to を CLI 経由で叩いて返信下書きを生成する。外部 SaaS への送信は行わず、ユーザーが下書きを確認して手で送る。Sub-issue E で実装済みの ReplyDraftCandidatePayload を利用。
---

# reply-draft — 返信下書きを opshub に生成させる

opshub の `propose generate --reply-to <source_id>` (CLI、または将来 MCP の `propose.generate` write tool) を経由して返信下書きを作る。Phase 10 Sub-issue D で書き、Sub-issue E (#217 merged) で `ReplyDraftCandidatePayload` が実装済み。ADR-0016 §決定 (i)+(j)+(k) で吸収された。

## 何が起きるか

1. ユーザーが「これに返信下書き作って」「この slack に返信案」「メール返信どうする?」と頼む
2. ホストが本 skill を発火
3. ホストが対象の `source_id` を特定 (recall.search で source を引く or ユーザー入力)
4. CLI 経路で `opshub propose generate "" --reply-to <source_id> --expand-graph` を実行 (位置引数 `topic` は `--reply-to` 指定時は無視される)
5. 候補を `opshub propose list` / `apply` で下書き保存 (人確認)
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

### Step 2: 下書き生成 (CLI 経路)

現在 (Phase 10 D2 時点) は MCP に propose.generate tool は無いため、ホストは CLI subprocess で:

```bash
opshub propose generate "" \
  --reply-to <source_id> \
  --expand-graph
```

位置引数 `topic` は `--reply-to` 指定時は無視されるため空文字 `""` で OK (CLI 実装の docstring 参照)。`--expand-graph` (ADR-0017) は知識グラフから文脈 source を拾うフラグ。ADR-0016 §決定 (k) の `<context_source>` 注入経路。

文体は `author = self` の過去送信 event を recall で引き `<style_example>` として注入される (ADR-0016 §決定 (k))。ホストが追加で style を渡す必要はない。

### Step 3: 候補を確認

```bash
opshub propose list --state pending
```

`propose list` には `--kind` フィルタは無いため、reply_draft 候補も task / decision 候補と同じ pending bucket に並ぶ。`generated_at DESC` ソートで最新が先頭に来るので、直前に生成した reply_draft は通常 1 行目に出る。

最新 candidate の本文を表示。複数候補がある場合はユーザーに選ばせる。

### Step 4 (人確認必須): apply で下書き保存

```bash
opshub propose apply <proposal_id> <candidate_index>
```

apply は durable state を変える (`ProposalApplied` event を発行)。ホストは必ずユーザーに「この下書きを保存しますか?」と確認する (ADR-0016 §決定 (c) HITL 必須)。

## 出力フォーマット (ホスト側)

```text
# 返信下書き候補

## 返信元
- source_id: ...
- source_type: slack_message / ms365_outlook / issue / pull_request / ...

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

- ADR-0010 §禁止事項 7 (write-back 禁止契約)
- ADR-0016 §決定 (i)(j)(k) (reply_draft candidate / triage / style-source recall)
- ADR-0017 §決定 (b) (link_type `reply_draft_replies_to` / `referenced_in_reply_draft`)
- Sub-issue E PR #217 (`ReplyDraftCandidatePayload`、triage 3 分類、write-back 非存在 test pin)
- docs/secretary-agent.md
