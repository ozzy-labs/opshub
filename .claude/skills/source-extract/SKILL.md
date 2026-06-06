---
name: source-extract
description: 「この資料から task 抽出」「これに含まれる decisions 教えて」「<source_id> から候補を」「このドキュメントから ToDo 拾って」と頼まれたら、opshub MCP の source.get で対象 source の本文を読み、propose.generate (mode=source_extract) で task / decision / reply_draft 候補を生成し、ユーザー確認後に propose.apply で承認分のみ HITL 保存する。auto-apply 経路は存在しない (ADR-0016 §決定 (c))。
---

# source-extract — 1 つの source から候補を抽出する (HITL)

opshub MCP の `source.get`（read tool、Step 1 widening で追加された PR #231）で対象 source の本文を取得し、`propose.generate`（`mode=source_extract`、Phase 12 H4 で追加された dispatch key、ADR-0016 改訂 §決定 (l)(b)）で task / decision / reply_draft 候補を生成、ユーザーが個別承認したものだけ `propose.apply`（Phase 12 H1 で MCP に露出、`WriteCategory.PROPOSE_APPLY`、`read_only=false` + `idempotent=true`）で durable state に書き戻す。

`inbox-triage` と pair。inbox-triage が「集合 (inbox 全体)」を扱うのに対し、本 skill は「個別 (source 1 件)」を扱う。

## 何が起きるか

1. ユーザーが「この資料から task 抽出」「これに含まれる決定教えて」「<source_id> から候補を」と頼む
2. ホストが本 skill を発火
3. ホストが `source.get` で対象 source 本文を取得（Phase 11 で Outlook body deep retention / Office 文書本文抽出に対応）
4. ホストが `propose.generate`（`mode=source_extract`、`topic` = source 概要）を呼び、候補 (task / decision / reply_draft) を生成（`ProposalGenerated` event を durable log に書く）
5. ホストが候補をユーザーに整形して提示
6. ユーザーが個別承認した候補のみ `propose.apply` で保存（HITL、idempotent）

opshub 側で外部 SaaS に通知 / 投稿する経路は **存在しない**（ADR-0010 §禁止事項 7）。

## 呼び出し順

### Step 1: 対象 source を特定

ユーザー入力に `source_id` がある場合はそのまま使う。なければ `recall.search` / `search` で検索:

```text
tool: source.list
input:
  source_type: "<word_document|teams_message|...>"  # 任意
  limit: 10
```

または:

```text
tool: search
input:
  query: "<キーワード>"
  limit: 10
```

`hits[]` から `source_id` を抜き出す。

### Step 2: source 本文を取得

```text
tool: source.get
input:
  source_id: "<source ULID>"
```

戻り値の `body` / `summary` / `title` を host LLM が読み、抽出すべき候補のヒント（task 化できそうな action / 記録すべき decision 等）を組み立てる。Phase 11 (ADR-0020 改訂 / ADR-0025) で Outlook body / Word / Excel / PowerPoint 本文が deep retention されているため、`source.get` 1 回で本文全体が読める。

### Step 3: 候補を生成（write tool、HITL boundary）

```text
tool: propose.generate
input:
  topic: "<source title or 抽出指示>"
  mode: "source_extract"
  max_candidates: 5        # 1〜20
```

graph 1-hop の文脈拡張は default で常時走る (ADR-0017 §(e)+(f)、epic #470 で `expand_graph` param 削除)。

`mode=source_extract` は Phase 12 H4 で追加された dispatch key（ADR-0016 改訂 §決定 (l)(b)）。`proposals.scope` に `source_extract` が stamp され、後から「どの skill が起こした proposal か」を audit log で join できる。`reply_to_source_id` とは排他（reply-draft mode は別経路で、本 skill では `topic` + `mode` 経由で source 文脈を引く）。

`propose.generate` は write tool（`destructiveHint=true`、ADR-0022 §(c)）。LLM round-trip を伴い `ProposalGenerated` event が durable log に乗る。apply は Step 5 で別途人が叩く（auto-apply 経路は存在しない）。

### Step 4: 候補を提示

戻り値の `candidates[]` をユーザーに整形して提示。各候補に `index` / `kind` (`task` / `decision` / `reply_draft`) / payload が付くので、host LLM は kind 別に並べる。

戻り値スキーマ（抜粋）：

```json
{
  "ok": true,
  "proposal_id": "01H...",
  "scope": "source_extract",
  "candidates": [
    {"index": 0, "kind": "task", "title": "...", "body": "..."},
    {"index": 1, "kind": "decision", "text": "...", "context": "..."},
    {"index": 2, "kind": "reply_draft", "body": "...", "reply_to_source_id": "..."}
  ],
  "hitl_apply_required": true
}
```

### Step 5 (人確認必須): 承認分のみ propose.apply

ユーザーが「0 番と 1 番採用」と選んだら、`candidate_index` を 1 件ずつ `propose.apply` で叩く。

```text
tool: propose.apply
input:
  proposal_id: "<proposal ULID>"
  candidate_index: <approved index>
```

`propose.apply` は HITL write tool（`read_only=false`、`destructive=false`、`idempotent=true`）。handler 層で `OpsHubError("already applied")` を catch → `{ok:true, already_applied:true, applied_entity_type, applied_entity_id}` に正規化されるため、再叩きは no-op。必ずユーザーに「この候補を保存しますか?」と確認する（ADR-0016 §決定 (c) HITL 必須）。

## 出力フォーマット (ホスト側)

```text
# source-extract 候補

## 対象 source
- source_id: ...
- source_type: word_document / teams_message / outlook_email / ...
- title: ...

## 候補
### #0 [task] ...
> title / body

### #1 [decision] ...
> text / context

### #2 [reply_draft] ...
> body

## 次のアクション
- 採用 index を教えてください → `propose.apply` で保存
- 全部 skip でよければ何もしない
```

## 自律範囲 / HITL boundary

- **read tool (`source.get` / `source.list` / `search` / `recall.search`)** — host LLM 自律 OK
- **write tool (`propose.generate`)** — LLM round-trip を伴う durable write、`destructiveHint=true`。ホストは人確認を取る（ADR-0022 §(c)）
- **write tool (`propose.apply`)** — `read_only=false`、durable state を変える。ホストは必ずユーザーに「これを保存しますか?」と確認する（ADR-0016 §決定 (c) auto-apply 禁止）

`propose.generate` → `propose.apply` の 2 段ゲートが本 skill の HITL boundary。両方とも host LLM が自動連鎖させてはいけない。

## できないこと / やらない

- **auto-apply** — ADR-0016 §決定 (c) で禁止、`propose.apply` を確認なしで叩く挙動は本 skill 仕様の外
- **外部 SaaS への投稿 / 通知** — ADR-0010 §禁止事項 7（connector に `post` / `send` メソッドを実装しない契約）
- **source 本文の編集** — 本 skill は source を読むのみ。元 source は immutable（connector が再 sync すれば外部変更は反映されるが、opshub 側から書き戻すことはない）
- **新 candidate kind** — ADR-0016 §決定 (l)(d) で `Candidate` discriminated union は `task | decision | reply_draft` の 3 kind で freeze

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0010 §禁止事項 7（write-back 禁止契約）
- ADR-0016 (Action Loop、§決定 (c) auto-apply 禁止、§決定 (l) draft 系統一方針 + `mode` 引数射程)
- ADR-0017 §(e)+(f)（graph 1-hop expand）
- ADR-0020 §改訂（Outlook body deep retention、Phase 11）
- ADR-0022 (MCP Server Surface、Phase 12 H1 改訂で `propose.apply` 露出)
- ADR-0025 (Office Document Content Extraction、Phase 11)
- PR #231 (MCP `propose.generate` + `source.get` widening)
- Phase 12 H1 (`docs/phase-12-plan.md` §3 H1)
- Phase 12 H4 (`docs/phase-12-plan.md` §3 H4)
- docs/assistant-agent.md
