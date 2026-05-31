---
name: announcement-draft
description: 「リリース告知文書いて」「announcement 作って」「アナウンス文章まとめて」「release notes 草案」「お知らせ案ほしい」と頼まれたら、opshub MCP の recall.search (関連 release / change context) + decision.list (recorded_after=last_release) + brief (announcement tone) を読み取り系で組み立て、ホスト LLM が告知文 text を構成して返す。persist しない (text-only、ADR-0016 §決定 (l)(a))。propose.generate を経由せず候補保存 / apply 経路を持たない。ユーザーが受け取った text を手で SaaS (Slack / Notion / GitHub release / メール 等) に投稿する。
---

# announcement-draft — 告知文 text を opshub の context から組み立てて返す

opshub MCP server (`opshub mcp serve`、ADR-0022) の read tool 群 (`recall.search` / `decision.list` / `brief`) をホスト LLM が合成して、リリース告知 / お知らせ文の text を 1 発で組み立てる。Phase 12 H5 (`docs/phase-12-plan.md` §3 H5) で導入された Tier 2 draft 系 skill。

**persist しない**: `reply-draft` と異なり、本 skill は `propose.generate` + `propose.apply` 経路を **使わない**。告知文は自発生成で natural key を持たないため (返信元 source のような対応付けがない)、proposal table に保存しても operator から見て idempotency / 削除 / 編集の semantics が曖昧になる。ADR-0016 §決定 (l)(a) で「返信元 source の有無」で persist 境界を切る方針が pin されており、本 skill は text-only 返却に留める。

## 何が起きるか

1. ユーザーが「リリース告知書いて」「announcement 作って」「アナウンス文章」と頼む
2. ホストが本 skill を発火
3. ホストが下記「呼び出し順」に従って read tool を順次呼ぶ
4. 戻り値を集約してホスト LLM が告知文 text を 1 発で構成 (Markdown 推奨、投稿先 SaaS の syntax は user が手で調整)
5. ユーザーに text を提示 (画面表示のみ)
6. ユーザーが手で SaaS (Slack / Notion / GitHub release / メール 等) に投稿する

opshub 側に候補保存 / apply 経路は存在しない (`propose.generate` / `propose.apply` を呼ばない)。「保存」のような UX を提示しないこと。

## 呼び出し順 (MCP tool)

### Step 1: 関連 release / change context を recall

```text
tool: recall.search
input:
  query: "<リリース対象 / 機能名 / プロジェクト名>"
  limit: 20
```

告知対象の機能 / 変更 / リリース内容を recall で拾う。ADR-0012 hybrid recall (本文 embedding + FTS5 + graph 補強) で GitHub PR / Slack 議論 / Office 文書 / Outlook メール等から該当 context を集める。

ユーザーが PR 番号 / リリースタグ / 機能名を明示している場合は、それを query にそのまま渡す。

### Step 2: last_release 以降の decision を列挙

```text
tool: decision.list
input:
  recorded_after: "<前回 release の timestamp (ISO 8601)>"
  limit: 30
```

前回リリース以降の意思決定を引いて「何を変えたか / なぜ変えたか」の素材にする。前回 release の timestamp は user に確認するか、ホストが GitHub release / tag の merged_at から導出する。明確な前回 release がない場合は `recorded_after` を「直近 N 週間」のような相対指定で埋める (user の発言から判断)。

Phase 12 H1 (ADR-0022 改訂) で `decision.list` に `recorded_after` / `recorded_before` (`decisions.recorded_at` 半開区間) が追加されたため、これを使う。

### Step 3: announcement tone の brief で 1 発要約

```text
tool: brief
input:
  topic: "<リリース内容 / 告知対象を 1 行で>"
  format: "md"
  max_sources: 20
```

`brief` は LLM backend が configured なら指定 topic の Markdown 要約を 1 発で返す (`source_count` 上限まで関連 source を取り込んで LLM 要約)。

告知文用に **tone** を制御したい場合は、`topic` 文字列に「リリース告知のトーンで」「カジュアル」「フォーマル」のような指示を含める。`brief` MCP tool 自体に `tone` 引数はなく、`topic` 文字列で誘導する (ADR-0022 §決定、Phase 12 H1 でも tone 引数は追加されていない)。

LLM 未設定 / token 不足で `brief` が失敗した場合は、Step 1+2 の戻り値だけでホスト LLM が text を組み立てる (`brief` 結果なしでも本 skill は機能する fallback path)。

## 出力フォーマット (ホスト側、text として user に提示のみ)

ホスト LLM が以下の骨子で Markdown text を組み立て、画面に提示する。**保存はしない**。

```text
# <リリース / 告知タイトル>

## TL;DR
<1-2 行のサマリ>

## 何が変わったか
- <機能 / 修正 / 改善 1> — <user-facing な影響>
- <機能 / 修正 / 改善 2> — ...

## なぜ変えたか (decision.list より)
- <decision title>: <要旨>
- ...

## 補足 / 既知の注意点
- <recall.search で拾った context、移行が必要な場合の手順等>

## リンク
- <PR / release tag / docs / 関連 source の url>
```

text は user に提示するのみ。user は手で Slack / Notion / GitHub release / メール等にコピペし、投稿先に応じた syntax (Slack mrkdwn / Slack Block Kit / GitHub release body 等) に調整する。

## 自律範囲

- **すべて read tool** (`recall.search` / `decision.list` / `brief`、`readOnlyHint=true`)。確認なしで呼んでよい
  - `brief` は LLM 呼び出しを内部で行うが、`readOnlyHint=true` (Phase 6 で確立、ADR-0022 §(c))。durable state は変えない (briefing 投影への append は read-only context の延長として allowed)
- **write tool は呼ばない** — `propose.generate` / `propose.apply` / `task.create` / `inbox.add` / `connector.sync` 等 durable state を変える tool は本 skill では一切呼び出さない
- 告知文 text を opshub の durable state に書き戻さない (ADR-0016 §決定 (l)(a))

## できないこと / やらない

- **候補保存 / apply** — `propose.generate` / `propose.apply` を呼ばない。本 skill は text-only (ADR-0016 §決定 (l)(a))。`AnnouncementDraftCandidatePayload` のような新 candidate kind も存在しない (ADR-0016 §決定 (l)(d) で Candidate discriminated union は `task | decision | reply_draft` の 3 kind で freeze)
- **外部 SaaS への投稿** — Slack / Notion / GitHub release / メールに直接投稿しない (ADR-0010 §禁止事項 7)。user が手で貼り付ける
- **GitHub release / tag の自動作成** — `gh release create` 等 release artifact 作成は opshub の責務外
- **triage 値の付与** — §決定 (l)(c) で 3 値 triage (`respond` / `notify` / `ignore`) は reply_draft 専用と pin。本 skill は triage を持たない
- **`propose.generate` の `mode` 引数経由の dispatch** — §決定 (l)(b) で `mode` 引数は persist 経路を持つ structured-output dispatch key に限定 (`reply_draft` / `inbox_triage` / `source_extract` / `meeting_followup`)。`announcement_draft` mode は存在しない

## 参考

- ADR-0016 §決定 (l) (Draft 系統一方針、Phase 12 H1 で追加)
  - (a) persist 境界は「返信元 source の有無」で切る (announcement-draft は text-only)
  - (b) `mode` 引数の射程 (persist 経路を持つ 4 mode のみ)
  - (c) Triage は reply_draft 文脈のみ
  - (d) Candidate discriminated union freeze (3 kind: task / decision / reply_draft)
  - (e) 将来 persist 需要が出たら schema versioning パターンで追加
- ADR-0004 (Agent Runtime Boundary、形A、能動 push なし)
- ADR-0010 §禁止事項 7 + §改訂 (write-back 当面 scope 外)
- ADR-0012 改訂 (本文 embedding hybrid recall)
- ADR-0022 改訂 (MCP Server Surface、physical-column 時間フィルタ)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H5)
- docs/secretary-agent.md (Skill catalog SSOT、14 skills 責務マップ)
- pair: handoff-draft (引き継ぎ書、こちらも text-only)
