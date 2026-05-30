---
name: file-lookup
description: 「Box にあったあの資料」「先週共有された PDF」「<キーワード>含むファイル」と頼まれたら、opshub MCP の recall.search で本文ベース横断検索を実行し、Box / Slack / GitHub / MS365 / Box Drive を横断して該当 source を返す。本文取得は読み取り経路のみで、外部 SaaS を直接叩かない。
---

# file-lookup — opshub の本文ベース横断検索で資料を引く

opshub MCP server (`opshub mcp serve`、ADR-0022) の `recall.search` で、Phase 10 Sub-issue B (#214 merged) の本文ベース embedding + SQLite FTS5 横断検索を使い、Box / Slack / GitHub / MS365 / Box Drive を横断して該当ファイル / メッセージを引く。

ADR-0020 (本文ローカル保持) + ADR-0012 改訂 (本文 embedding) で、要約 (summary) ではなく本文に対する hit が返るため、固有名詞や細部のキーワードでも引ける (Phase 10 plan §3-B)。

## 何が起きるか

1. ユーザーが「あの資料」「先週共有された〜」「<キーワード>含むファイル」と頼む
2. ホストが本 skill を発火
3. ホストが `recall.search` を呼ぶ
4. 戻り値の `hits[]` を整形 (source_type 別にグルーピング、新しい順) して返す
5. ユーザーがリンク (`url` / 元の `external_id`) からブラウザで開く

## 呼び出し順 (MCP tool)

### Step 1: 本文ベース横断検索

```text
tool: recall.search
input:
  query: "<キーワード or トピック>"
  limit: 30
```

`recall.search` は ADR-0012 hybrid recall (sqlite-vec + FTS5 + graph 補強) で、ADR-0020 で本文がローカル保持されているため本文中の細部も hit する。

### Step 2 (任意): source_type で絞り込み

ユーザーが「Box の」「Slack の」と source 種別を絞っている場合、ホスト側で `hits[]` を `source_type` でフィルタする (`recall.search` の input に source_type filter が無い現実装では post-filter で対応)。

| ユーザー語彙 | source_type |
|---|---|
| Box にあるあの〜 | `box_event` / `box_drive_file` |
| Slack の〜 | `slack_message` |
| GitHub の〜 | `issue` / `pull_request` / `notification` |
| Outlook / メール | `ms365_outlook` |
| カレンダー予定 | `ms365_calendar` |
| OneDrive のファイル | `ms365_onedrive` |

source_type 値は connector 実装の SSOT (`src/opshub/connectors/<name>/{api,mapper}.py` の `source_type=...` リテラル) に対応する。GitHub connector は `github_` prefix を持たず素の `issue` / `pull_request` / `notification` を発行する点に注意 (Phase 10 監査で SKILL.md ↔ 実装間の drift として固定済)。

### Step 3 (任意): 詳細を recall.search で展開

特定 hit の詳細をユーザーが知りたい場合、その source_id をクエリに含めて再 search するか、ホストが取得済み hit の `snippet` を表示する。本文全体を context window に詰めない (ADR-0022 §(d) context 効率原則)。

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
- ...

## Box Drive (ローカル sync、n 件)
- <rel_path> (<mtime>) — <snippet 抜粋>
```

## 自律範囲

- **read tool のみ** — `recall.search` (`readOnlyHint=true`)。確認なしで呼んでよい
- 外部 SaaS への問い合わせは発生しない (recall は opshub ローカル DB のみ)
- 取得した本文を agent context に丸ごと注入しない (snippet ベースで表示)

## できないこと / やらない

- 外部 SaaS の最新を直接 fetch する (`connector.sync` は write tool、本 skill 外。ユーザーが明示的に「同期して」と頼んだ場合のみ別経路で人確認付き)
- ファイルのダウンロード / 外部送信
- recall hit を opshub の durable state (task / inbox / decision) に自動追記 — `task.create` 等は別 skill / 人確認付き
- 本文を agent context に full payload で渡す (ADR-0022 §(d))

## 参考

- ADR-0012 改訂 (本文 embedding、Phase 10 §18)
- ADR-0020 (本文ローカル保持)
- ADR-0022 §(d) (context 効率)
- Phase 10 Sub-issue B (#214 merged、本文 FTS5 / search command)
- docs/secretary-agent.md
