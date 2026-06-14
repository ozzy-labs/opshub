---
name: handoff-draft
description: 「引き継ぎ書作って」「handoff 書く」「後任向け資料まとめて」「業務引継メモほしい」と頼まれたら、opshub MCP の task.list (state=in_progress) + decision.list + recall.search + graph.related を読み取り系で組み立て、ホスト LLM が引き継ぎ書 text を構成して返す。persist しない (text-only、ADR-0016 §決定 (l)(a))。propose.generate を経由せず候補保存 / apply 経路を持たない。ユーザーが受け取った text を手で SaaS (Notion / Confluence / docs / Slack 等) に貼り付ける。
---

# handoff-draft — 引き継ぎ書 text を opshub の context から組み立てて返す

opshub MCP server (`opshub mcp serve`、ADR-0022) の read tool 群 (`task.list` / `decision.list` / `recall.search` / `graph.related`) をホスト LLM が合成して、後任向け引き継ぎ書の text を 1 発で組み立てる。Phase 12 H5 (`docs/phase-12-plan.md` §3 H5) で導入された Tier 2 draft 系 skill。

**persist しない**: `reply-draft` と異なり、本 skill は `propose.generate` + `propose.apply` 経路を **使わない**。引き継ぎ書は自発生成で natural key を持たないため (返信元 source のような対応付けがない)、proposal table に保存しても operator から見て idempotency / 削除 / 編集の semantics が曖昧になる。ADR-0016 §決定 (l)(a) で「返信元 source の有無」で persist 境界を切る方針が pin されており、本 skill は text-only 返却に留める。

## 何が起きるか

1. ユーザーが「引き継ぎ書作って」「handoff 書く」「後任向けにまとめて」と頼む
2. ホストが本 skill を発火
3. ホストが下記「呼び出し順」に従って read tool を順次呼ぶ
4. 戻り値を集約してホスト LLM が引き継ぎ書 text を 1 発で構成 (Markdown)
5. ユーザーに text を提示 (画面表示のみ)
6. ユーザーが手で SaaS (Notion / Confluence / docs / Slack 等) に貼り付ける

opshub 側に候補保存 / apply 経路は存在しない (`propose.generate` / `propose.apply` を呼ばない)。「保存」のような UX を提示しないこと。

## 呼び出し順 (MCP tool)

### Step 1: 進行中 task を列挙

```text
tool: task.list
input:
  state: "in_progress"
  limit: 50
```

戻り値の `items[]` を引き継ぎ書の「進行中タスク」セクション素材として使う。Phase 12 H1 (ADR-0022 改訂) で物理列ベースの時間フィルタが追加されたが、引き継ぎ書は時点スナップショットなので時間フィルタは不要 (state=in_progress で全進行中を拾う)。

`state` 値が `active` / `in_progress` のどちらか projection 側で正規化されているため、projection の SSOT に従う (`src/opshub/projections/tasks.py`)。

### Step 2: 最近の decision を列挙

```text
tool: decision.list
input:
  recorded_after: "<引き継ぎ対象期間の開始 ISO 8601>"   # 任意、例: 過去 3 ヶ月
  limit: 30
```

「なぜそう決めたか」を後任に伝えるため、最近の decision を引く。引き継ぎ範囲をユーザーが「直近半年」「過去 1 年」のように指定したらホストが ISO 8601 で `recorded_after` を埋める。指定がなければ `recorded_after` を省略 (全 decision を新しい順で `limit` 件)。

### Step 3: 周辺コンテキストを recall で拾う

```text
tool: recall.search
input:
  query: "<引き継ぎトピック or プロジェクト名 or 担当領域>"
  limit: 20
```

進行中 task / decision だけでは抜け落ちる周辺シグナル (Slack 上の議論 / GitHub の進行中 PR / Office 文書 等) を recall で拾う。ADR-0012 hybrid recall (本文 embedding + FTS5 + graph 補強) で意味検索を効かせる。

### Step 4: 関連エンティティを graph で広げる

```text
tool: graph.related
input:
  entity_id: "<Step 1-3 で hit した重要 task / decision / source の ULID>"
  limit: 20
```

特定 task や decision の周辺 (関連 source / 派生 task / 引用 decision) を 1-hop で広げる。ホストが Step 1-3 の中から「これは確実に引き継ぐ」エンティティを 2〜3 個選び、それぞれに `graph.related` を呼ぶ。全件に呼ばないこと (コンテキスト爆発防止、ADR-0022 §(d))。

## 出力フォーマット (ホスト側、text として user に提示のみ)

ホスト LLM が以下の骨子で Markdown text を組み立て、画面に提示する。**保存はしない**。

```text
# 引き継ぎ書 (<対象範囲>)

## 概要
<担当業務 / 対象期間 / 引き継ぎ理由を 2-3 行>

## 進行中タスク (task.list state=in_progress)
- [<title>] (<priority>, due: <due_at>) — <現在のステータス> / <次のアクション>
- ...

## 最近の重要 decision (decision.list)
- [<title>] (<recorded_at>) — <要旨>
  - 背景: <なぜそう決めたか、recall / graph で拾った context>
- ...

## 周辺コンテキスト (recall.search)
- <topic 1>: <要約>
  - 関連 source: <title> (<url>)
- ...

## 関連エンティティ (graph.related で広げた範囲)
- <task / decision / source> ↔ <related>
- ...

## 後任への申し送り
- <ユーザーが追加したい注意点 / 連絡先 / ノウハウを LLM が要約>
```

text は user に提示するのみ。user は手で Notion / Confluence / docs / Slack 等にコピペする。

## 自律範囲

- **すべて read tool** (`task.list` / `decision.list` / `recall.search` / `graph.related`、`readOnlyHint=true`)。確認なしで呼んでよい
- **write tool は呼ばない** — `propose.generate` / `propose.apply` / `task.create` / `inbox.add` / `connector.sync` 等 durable state を変える tool は本 skill では一切呼び出さない
- 引き継ぎ書 text を opshub の durable state に書き戻さない (ADR-0016 §決定 (l)(a))

## できないこと / やらない

- **候補保存 / apply** — `propose.generate` / `propose.apply` を呼ばない。本 skill は text-only (ADR-0016 §決定 (l)(a))。`HandoffDraftCandidatePayload` のような新 candidate kind も存在しない (ADR-0016 §決定 (l)(d) で Candidate discriminated union は `task | decision | reply_draft` の 3 kind で freeze)
- **外部 SaaS への投稿** — Notion / Confluence / Slack に直接書き込まない (ADR-0010 §禁止事項 7)。user が手で貼り付ける
- **task の自動完了 / 状態変更** — 引き継ぎ書を生成しても task の状態は触らない
- **triage 値の付与** — §決定 (l)(c) で 3 値 triage (`respond` / `notify` / `ignore`) は reply_draft 専用と pin。本 skill は triage を持たない
- **`propose.generate` の `mode` 引数経由の dispatch** — §決定 (l)(b) で `mode` 引数は persist 経路を持つ structured-output dispatch key に限定 (`reply_draft` / `inbox_triage` / `source_extract` / `meeting_followup`)。`handoff_draft` mode は存在しない

## 参考

- ADR-0016 §決定 (l) (Draft 系統一方針、Phase 12 H1 で追加)
  - (a) persist 境界は「返信元 source の有無」で切る (handoff-draft は text-only)
  - (b) `mode` 引数の射程 (persist 経路を持つ 4 mode のみ)
  - (c) Triage は reply_draft 文脈のみ
  - (d) Candidate discriminated union freeze (3 kind: task / decision / reply_draft)
  - (e) 将来 persist 需要が出たら schema versioning パターンで追加
- ADR-0004 (Agent Runtime Boundary、形A、能動 push なし)
- ADR-0010 §禁止事項 7 + §改訂 (write-back 当面 scope 外)
- ADR-0012 改訂 (本文 embedding hybrid recall)
- ADR-0017 §決定 (graph link_type)
- ADR-0022 改訂 (MCP Server Surface、physical-column 時間フィルタ)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H5)
- docs/assistant-agent.md (Skill catalog SSOT、15 skills 責務マップ)
- pair: announcement-draft (告知文、こちらも text-only)
