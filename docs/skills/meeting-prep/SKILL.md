---
name: meeting-prep
description: '「来週の会議準備」「明日のミーティング前確認」「次の会議の context」「<会議名> の準備して」「打ち合わせ前に状況教えて」「Google Calendar の予定」「明日の Google Calendar」と頼まれたら、opshub MCP の source.list (source_type は ms365_calendar または google_calendar、observed_after/before で対象期間) で該当 calendar event を引き、recall.search で過去の関連やりとり、graph.related で関連 decisions / sources を辿って会議準備サマリ (目的 / 過去文脈 / 関連 decisions / 参考 sources) を組み立てる。Phase 14 で Google Calendar (`google_calendar`、`google_calendar` connector) も対象に追加。read-only、persist なし。pair: meeting-followup (会議後) と対をなす。'
---

# meeting-prep — 次の会議の context を opshub から組み立てる

opshub MCP server (`opshub mcp serve`、ADR-0022) の read 系 tool で、次に控えている会議 (calendar event) について「目的 / 過去関連やりとり / 関連 decisions / 参考 sources」を会議前に集約する。Phase 12 H2 で導入された info gathering 系 skill の 1 つ。

pair: 会議後の議事録 / フォローアップ生成は `meeting-followup` skill が担当する。本 skill は **会議前** に context を組み立てる側で、persist は一切しない (HITL write 経路を持たない、ADR-0016 §決定 (l) の draft 系統一方針と整合する read 系の位置付け)。

## 何が起きるか (host 側の流れ)

1. ユーザーが「明日の会議準備」「来週の打ち合わせ前に context 教えて」「<会議名> の準備して」のような表現で問い合わせる
2. 外部ホスト (Claude Code 等) が本 skill を発火させる
3. ホストが対象期間を ISO 8601 timestamp に解釈する (明日 / 来週 / 今日 これから 等)
4. ホストが下記「呼び出し順」に従って opshub MCP read tool を順に呼ぶ
5. 戻り値 (JSON 文字列) を集約し、会議 1 件ごとに「目的 / 過去関連やりとり / 関連 decisions / 参考 sources」を要約して返す

opshub 側で能動的に「会議 N 分前にリマインダ」を打つ runtime は走らない (ADR-0004 §(a) 形A)。本 skill はリクエスト駆動で、ユーザーが問い合わせた瞬間にホストがツールを叩く。

## 期間の解釈 (ホスト側)

ユーザー発言からホストが ISO 8601 timestamp を解釈する：

| ユーザー語彙 | 期間 (半開区間) | source.list フィルタ |
|---|---|---|
| 明日の会議 | 明日 00:00 (local TZ) 〜 明後日 00:00 | `observed_after=明日00:00` / `observed_before=明後日00:00` |
| 今日これから | now 〜 今日 23:59 | `observed_after=now` / `observed_before=今日23:59` |
| 来週の会議 | 来週月曜 00:00 〜 再来週月曜 00:00 | `observed_after=来週月曜` / `observed_before=再来週月曜` |
| 次の会議 (1 件) | now 〜 +24h | `observed_after=now` / `observed_before=+24h`、`limit=5` で先頭採用 |

`source.list` の時間フィルタは Phase 12 H1 (ADR-0022 改訂) で `sources.observed_at` ベースの `observed_after` / `observed_before` (半開区間、ISO 8601) として追加されたもの。

## 呼び出し順 (MCP tool)

### Step 1: 対象期間の calendar event を `source.list` で列挙

ms365 / Google Calendar のどちらか、または両方を対象にする。複数 source_type を扱う場合は呼び分ける (`source.list` は 1 呼び出しに 1 source_type)。

```text
tool: source.list
input:
  source_type: "ms365_calendar"   # または "google_calendar" (Phase 14)
  observed_after: "<期間開始 ISO 8601>"
  observed_before: "<期間終了 ISO 8601>"
  limit: 20
```

戻り値の `items[]` 各行は `{entity_id, connector_name, source_type, title, url, summary, observed_at, ...}`。`source_type = "ms365_calendar"` は ms365 connector の Calendar event 由来 (`src/opshub/connectors/ms365/mapper.py` の `CALENDAR_SOURCE_TYPE = "ms365_calendar"`、SSOT)。`source_type = "google_calendar"` は Phase 14 で追加された Google Calendar connector 由来 (`src/opshub/connectors/google_calendar/mapper.py` の `GOOGLE_CALENDAR_SOURCE_TYPE = "google_calendar"`、SSOT、ADR-0010 §改訂)。Phase 11 で ms365_calendar に本文 deep retention が追加されており、Phase 14 の google_calendar も `summary` + `description` を本文として持つため、両者とも会議の招集メッセージ / アジェンダ本文を context に入れられる。

「次の会議 1 件」を解釈する場合、戻り値を `observed_at` 昇順で 1 件目に絞り Step 2 に進む。

### Step 2: 会議タイトル / 招集本文をクエリに過去の関連やりとりを recall

各 calendar event の `title` (件名) / `summary` (本文 = アジェンダ / 招集メッセージ) からキーワードを抽出し、過去の関連やりとりを `recall.search` で引く：

```text
tool: recall.search
input:
  query: "<会議タイトル> <参加者名> <主要トピック>"
  limit: 15
```

`recall.search` は ADR-0012 hybrid recall (本文 embedding + FTS5 + graph 補強) なので、参加者・トピック・関連プロジェクト名から過去の Slack / Teams / Outlook / Box / GitHub の関連 source が hit する。これが会議の「文脈」になる。

参加者で絞り込みたい場合 (例: 「田中さんとの 1on1 準備」)、ホストが participant 名を query に含める。本文 hit でカバーされる。

### Step 3: 関連 decisions / docs を graph.related で辿る

Step 1 で取得した calendar event の `entity_id` を起点に、すでに opshub の知識グラフに紐付いている decisions / sources を辿る：

```text
tool: graph.related
input:
  entity_type: "source"
  entity_id: "<calendar event の ULID>"
  direction: "both"
  limit: 30
```

戻り値の `links[]` から `target_entity_type == "decision"` を post-filter すると「この会議の論点に関連する過去の意思決定」が出る。`target_entity_type == "source"` のうち別の `source_type` (例: `slack_message` / `word_document`) は「参考 docs / 関連やりとり」として扱う。

Step 2 の `recall.search` で hit した上位 entity が link されていない場合は、ホストが Step 2 の hits を直接「参考 sources」として採用する (graph link が形成されているとは限らないため、recall hit と graph hit は union を取る)。

### Step 4 (任意): 重要 source の詳細を `source.get` で引く

特に重要そうな関連 source (過去議事録、関連 ADR、過去 1on1 メモ等) はホスト側で 1〜3 件選び詳細を引く：

```text
tool: source.get
input:
  source_id: "<hit entity_id>"
```

ただし全件を context window に詰めず、ユーザー確認後 or 要点抜粋に留める (ADR-0022 §(d) context 効率原則)。

## 出力フォーマット (ホスト側)

会議 1 件ごとに、ホストが以下の構造でユーザーに返す。複数件あるときは件ごとにブロックを繰り返す。

```text
# <会議タイトル> (YYYY-MM-DD HH:MM 〜)

## 会議目的 / アジェンダ (招集本文から)
- ...

## 過去の関連やりとり (recall.search 上位)
- [Slack / Teams / Outlook] <date> — <snippet>
- ...

## 関連する decisions
- ADR-XXXX / decision #M — ...

## 参考 sources
- [Box / OneDrive / GitHub] <title> — <url>
- ...

## 注意点 / オープン論点 (ホスト LLM が組み立て)
- ...
```

## 自律範囲

- **read tool のみ** — `source.list` / `recall.search` / `graph.related` / `source.get` はすべて `readOnlyHint=true` (ADR-0022 §(c))。確認なしで呼んでよい
- durable state を変える tool (`task.create` / `inbox.add` / `propose.generate` / `propose.apply` / `connector.sync`) は本 skill では呼ばない
- persist しない — 出力はその場限り。会議後の議事録 / フォローアップ生成は `meeting-followup` skill (HITL write、Phase 12 H4)

## できないこと / やらない

- 外部 SaaS への投稿 / 通知送信 / 会議招待の返信 (ADR-0010 §禁止事項 7、Phase 10 plan §1 #6)
- 会議リマインダの能動 push (ADR-0004 §(a)、能動機能は当面持たない、Phase 14+ で再検討)
- 推論結果を opshub の durable state に書き戻す (`propose.generate` / `propose.apply` は別 skill / 操作の責務)
- 会議後の議事録 / アクション抽出 → `meeting-followup` skill の責務
- 本文を agent context に full payload で渡す (ADR-0022 §(d))

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0010 §改訂 (connector contract、Phase 11 で Teams 追加、Phase 14 で Gmail + Google Calendar 追加)
- ADR-0012 改訂 (本文 embedding で recall 品質、Phase 10 §18)
- ADR-0016 改訂 (Action Loop、Phase 12 H1 で draft 系統一方針追加)
- ADR-0017 (Knowledge Graph、graph.related)
- ADR-0020 §改訂 (Outlook body deep retention、Phase 11)
- ADR-0022 改訂 (MCP Server Surface、Phase 12 H1 で physical-column 時間フィルタ追加)
- ADR-0025 (Office 文書本文抽出)
- Phase 11 plan (`docs/phase-11-plan.md`)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H2)
- docs/assistant-agent.md (Skill catalog SSOT、14 skills 責務マップ)
