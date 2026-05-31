---
name: find-document
description: 「Box にあったあの資料」「先週共有された PDF」「<キーワード>含むファイル」「あの議事録どこ」「あの Google Doc」「Sheets の <X>」「Google Slides で説明したやつ」と頼まれたら、opshub MCP の search (FTS5) で本文ベース横断検索を実行し、Box / Slack / GitHub / MS365 (Outlook / Calendar / OneDrive) / Teams / Box Drive / OneDrive Drive / Office 文書 (Word / Excel / PowerPoint) / Google Workspace (Docs / Slides / Sheets) を横断して該当 source を返す。本文取得は読み取り経路のみで、外部 SaaS を直接叩かない。意味検索ハイブリッドが必要な場合は recall.search を補助的に併用してもよい。
---

# find-document — opshub の本文ベース横断検索で資料を引く

opshub MCP server (`opshub mcp serve`、ADR-0022) の `search` (FTS5、Phase 12 H1 で MCP に露出) で、Phase 10 Sub-issue B (#214 merged) の本文ベース SQLite FTS5 横断検索を使い、Box / Slack / GitHub / MS365 (Outlook / Calendar / OneDrive) / Teams / Box Drive / OneDrive Drive / Office 文書 (Word / Excel / PowerPoint) / Google Workspace (Docs / Slides / Sheets) を横断して該当ファイル / メッセージを引く。Phase 11 で Teams chat (`teams_message`)、Outlook 本文 deep retention (`ms365_outlook`)、Office 文書抽出 (`word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`、markitdown 経由) が追加され、`onedrive_drive` ローカル FS connector も検索対象に入った (ADR-0025、Phase 11 plan §3 F1-F6)。Phase 13 で Google Workspace 由来 (`google_doc` / `google_slides` / `google_sheets`、Drive API `files.export` → markitdown 経由) と catch-all (`google_workspace_file`、metadata-only) が追加された (ADR-0025 §決定 (d')+(j)、Phase 13 plan §3 G1-G5)。

ADR-0020 (本文ローカル保持) で、要約 (summary) ではなく本文に対する hit が返るため、固有名詞や細部のキーワードでも引ける (Phase 10 plan §3-B)。

本 skill の前身は Phase 10 で導入された Tier 1 でしたが、Phase 12 H1 で `find-document` に rename + `search` (FTS5) MCP tool の直接利用に変更しました（詳細は `docs/phase-12-plan.md` §3 H1-c）。

## 何が起きるか

1. ユーザーが「あの資料」「先週共有された〜」「<キーワード>含むファイル」と頼む
2. ホストが本 skill を発火
3. ホストが `search` を呼ぶ（FTS5 ベースの本文 hit が最も精度高い）
4. 補助的に `recall.search` を呼んで意味検索 hit も統合（任意、Step 2）
5. 戻り値の `items[]` / `hits[]` を整形 (source_type 別にグルーピング、新しい順) して返す
6. ユーザーがリンク (`url` / 元の `external_id`) からブラウザで開く

## 呼び出し順 (MCP tool)

### Step 1: `search` (FTS5) で本文ベース横断検索

```text
tool: search
input:
  query: "<キーワード or トピック>"
  limit: 30
  connector_name: "<box | slack | github | ms365 | teams | box_drive | onedrive_drive | google_workspace>"   # 任意
```

`search` は SQLite FTS5 の `sources_fts` 仮想表に MATCH を投げ、`sources` projection に join 戻して title / url / summary を返す。**ホストが渡すクエリ文字列は phrase quote されるため、FTS5 の syntax 文字 (括弧 / コロン等) をエスケープする必要はない**（ADR-0022 改訂 §決定、Phase 12 H1）。

戻り値の `items[]` 各行は `{entity_id, connector_name, source_type, title, url, snippet, score}`。`score` は `-bm25` 降順。

### Step 2 (任意): 意味検索ハイブリッドが欲しい場合は `recall.search` を併用

固有名詞や明示的なキーワードでなく、似た意味 / 言い換えでも当てたい場合は `recall.search` も呼んで結果を統合する。

```text
tool: recall.search
input:
  query: "<キーワード or トピック>"
  entity_type: "source"
  limit: 20
```

`recall.search` は ADR-0012 hybrid recall (sqlite-vec embedding + FTS5 + graph 補強)。`search` の純粋な FTS5 hit と比べ、意味的に近いものを引く可能性がある一方、embedder のコスト・呼び出し時間が発生する。固有名詞検索の主力は `search` で十分。

### Step 3 (任意): source_type / connector で絞り込み

ユーザーが「Box の」「Slack の」と source 種別を絞っている場合、`search` の `connector_name` 引数で絞るか、ホスト側で `items[]` を `source_type` でフィルタする。

| ユーザー語彙 | source_type | connector |
|---|---|---|
| Box にあるあの〜 | `box_event` / `box_drive_file` | `box` / `box_drive` |
| Slack の〜 | `slack_message` | `slack` |
| GitHub の〜 | `issue` / `pull_request` / `notification` | `github` |
| Outlook / メール | `ms365_outlook` | `ms365` |
| カレンダー予定 | `ms365_calendar` | `ms365` |
| OneDrive のファイル (Graph 経由) | `ms365_onedrive` | `ms365` |
| Teams のチャット | `teams_message` | `teams` |
| Word 文書 | `word_document` | `box_drive` / `onedrive_drive` |
| Excel スプレッドシート | `excel_spreadsheet` | `box_drive` / `onedrive_drive` |
| PowerPoint スライド | `powerpoint_slide_deck` | `box_drive` / `onedrive_drive` |
| OneDrive Desktop (ローカル FS) | `onedrive_drive_file` | `onedrive_drive` |
| Google Docs | `google_doc` | `google_workspace` |
| Google Sheets | `google_sheets` | `google_workspace` |
| Google Slides | `google_slides` | `google_workspace` |
| Google Drive 上のその他 (PDF / image / folder etc.) | `google_workspace_file` | `google_workspace` |

source_type 値は connector 実装の SSOT (`src/opshub/connectors/<name>/{api,mapper}.py` の `source_type=...` リテラル) に対応する。GitHub connector は `github_` prefix を持たず素の `issue` / `pull_request` / `notification` を発行する点に注意 (Phase 10 監査で SKILL.md ↔ 実装間の drift として固定済)。Phase 11 で追加された `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` は `box_drive` / `onedrive_drive` の `content_extraction = true` opt-in が必要 (markitdown 経由、ADR-0025)。Phase 13 で追加された `google_doc` / `google_slides` / `google_sheets` も同じく `google_workspace` の `content_extraction = true` opt-in が必要 (Drive API `files.export` → MS Office mediatype → markitdown 経由、ADR-0025 §決定 (j))。`google_workspace_file` (catch-all 非 native) は Drive が 403 `fileNotExportable` を返すため `content_extraction = true` 設定下でも metadata-only (`body=None`) で persist される。

### Step 4 (任意): 特定 hit の詳細を引く

特定 hit の詳細をユーザーが知りたい場合、その `entity_id` を `source.get` に渡して row を取得する。

```text
tool: source.get
input:
  source_id: "<entity_id from search hits>"
```

本文全体を context window に詰めない (ADR-0022 §(d) context 効率原則)。

## 出力フォーマット (ホスト側)

```text
# 「<クエリ>」の検索結果

## Box (n 件)
- [<title>] (<date>) — <snippet 抜粋>
  → <url>

## Slack (n 件)
- ...

## GitHub (n 件)
- ...

## MS365 (n 件)
- Outlook (`ms365_outlook`、body deep retention 含む): ...
- Calendar (`ms365_calendar`): ...
- OneDrive (`ms365_onedrive`、Graph 経由 metadata): ...

## Teams (n 件)
- [<channel/chat title>] (<date>) — <snippet 抜粋>  ← `teams_message`

## Box Drive (ローカル sync、n 件)
- <rel_path> (<mtime>) — <snippet 抜粋>

## OneDrive Drive (ローカル sync、n 件)
- <rel_path> (<mtime>) — <snippet 抜粋>  ← `onedrive_drive_file`

## Office 文書 (markitdown 抽出、n 件)
- <rel_path> (<mtime>) — <snippet 抜粋>  ← `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`

## Google Workspace (Drive API export → markitdown、n 件)
- <title> (<modified>) — <snippet 抜粋>  ← `google_doc` / `google_slides` / `google_sheets` / `google_workspace_file` (catch-all、本文なし)
```

## 自律範囲

- **read tool のみ** — `search` / `recall.search` / `source.get` (`readOnlyHint=true`)。確認なしで呼んでよい
- 外部 SaaS への問い合わせは発生しない (`search` は opshub ローカル DB のみ)
- 取得した本文を agent context に丸ごと注入しない (snippet ベースで表示)

## できないこと / やらない

- 外部 SaaS の最新を直接 fetch する (`connector.sync` は write tool、本 skill 外。ユーザーが明示的に「同期して」と頼んだ場合のみ別経路で人確認付き)
- ファイルのダウンロード / 外部送信
- 検索 hit を opshub の durable state (task / inbox / decision) に自動追記 — `task.create` 等は別 skill / 人確認付き
- 本文を agent context に full payload で渡す (ADR-0022 §(d))

## 参考

- ADR-0012 改訂 (本文 embedding、Phase 10 §18)
- ADR-0020 改訂 (本文ローカル保持、Phase 11 で Outlook body deep retention 追加)
- ADR-0022 改訂 (MCP Server Surface、Phase 12 H1 で `search` (FTS5) 露出)
- ADR-0025 (Office 文書本文抽出、markitdown 経由)
- ADR-0010 §改訂 (connector contract、Phase 11 で Teams 追加、Phase 13 で Google Workspace 追加)
- ADR-0014 §改訂 (SaaS token storage、Phase 13 で Google Refresh Token rotation pin を 3 件目として追加)
- Phase 10 Sub-issue B (#214 merged、本文 FTS5 / search command)
- Phase 11 plan (`docs/phase-11-plan.md`)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H1)
- Phase 13 plan (`docs/phase-13-plan.md` §3 G1-G5、Google Workspace 8 番目の connector)
- docs/secretary-agent.md (Skill catalog SSOT)
- docs/google-workspace-setup.md (Google OAuth + Drive API setup)
