---
name: research
description: 「<X> について調べて」「<Y> の経緯」「<Z> に関するすべての情報」「<トピック> を網羅的に教えて」「<キーワード> 周りの状況」と頼まれたら、opshub MCP の recall.search (意味検索) + search (FTS5 本文全文検索) + graph.related / graph.expand (関連 entity 拡張) + brief (LLM 統合要約) を順に叩いてトピック横断調査を実行し、sources 一覧 / 関連 entities / 経緯サマリを組み立てて返す。read-only、persist なし。
---

# research — トピック横断調査を opshub の記憶層から組み立てる

opshub MCP server (`opshub mcp serve`、ADR-0022) の read 系 tool で、指定トピックに関する **横断調査** (semantic recall + full-text search + graph 拡張 + LLM 統合要約) を実行する。Phase 12 H2 で導入された info gathering 系 skill の 1 つ。

`find-document` skill が「特定の 1 ファイルを引く」(対象が明確、URL に飛びたい) のに対し、`research` は **トピック網羅** が目的で、関連 source 一覧・関連 entities・時系列の経緯まで含む包括的な像を返す。decision-rationale (特定 decision の根拠を掘る) よりも広く、external-brief (外向きの定型まとめ) よりも探索的。

## 何が起きるか (host 側の流れ)

1. ユーザーが「<X> について調べて」「<Y> の経緯」「<Z> に関するすべての情報」のように問い合わせる
2. 外部ホスト (Claude Code 等) が本 skill を発火させる
3. ホストが下記「呼び出し順」に従って opshub MCP read tool を順に呼ぶ
4. 戻り値 (JSON 文字列) を集約し、「sources 一覧 + 関連 entities + 経緯サマリ」をユーザー向けに要約して返す

opshub 単体では LLM 推論ループを持たない (ADR-0004 §(a) 形A)。最終要約は外部ホスト LLM が行うが、`brief` tool 経由で opshub 側 LLM backend に統合要約を委ねることも可能 (Step 4)。

## 呼び出し順 (MCP tool)

### Step 1: 意味検索で関連 entity を広く拾う (`recall.search`)

```text
tool: recall.search
input:
  query: "<topic / 自由文>"
  limit: 30
```

`recall.search` は ADR-0012 hybrid recall (本文 embedding + FTS5 + graph 補強)。意味的に近い entity (task / decision / source) を広めに返す。トピックの言い換え・略語にも追従する。

戻り値の `hits[]` 各行は `{entity_id, entity_type, source_type, title, snippet, score}`。後段で `entity_type` 別 (task / decision / source) にグルーピングする。

### Step 2: 本文全文検索で固有名詞 hit を補強 (`search` FTS5)

`recall.search` は意味検索なので、固有名詞 / 略語 / コード片など「字面が完全一致する hit」を取りこぼすことがある。Phase 12 H1 で MCP に露出された `search` (FTS5) で補強する：

```text
tool: search
input:
  query: "<topic 文字列>"
  limit: 30
```

`search` は SQLite FTS5 の `sources_fts` 仮想表に MATCH を投げ、`sources` projection に join して `{entity_id, connector_name, source_type, title, url, snippet, score}` を返す (BM25 順)。**クエリ文字列は phrase quote されるので括弧 / コロン等のエスケープ不要** (ADR-0022 改訂 §決定、CLI 専用の `raw_query` flag は MCP schema から除外)。

ホスト側で Step 1 の `hits[]` と Step 2 の `items[]` を `entity_id` で重複排除して union を取る。

### Step 3: 関連 entity を graph で広げる (`graph.related` / `graph.expand`)

Step 1/2 で得た上位 entity の周辺を graph で広げて経緯を組み立てる：

```text
tool: graph.related
input:
  entity_type: "<task|decision|source>"
  entity_id: "<上位 hit の ULID>"
  direction: "both"
  limit: 30
```

近接 1-hop で十分なら `graph.related`、より広く 2-hop の subgraph が欲しいなら `graph.expand` (default depth 2、hard ceiling 5 per ADR-0017 §(e))：

```text
tool: graph.expand
input:
  entity_type: "<task|decision|source>"
  entity_id: "<上位 hit の ULID>"
  depth: 2
```

`graph.related` は inbound / outbound / both を選べる。経緯 (provenance) を遡る場合は `graph.trace` (depth 3 default、backward 限定) も併用可能。

戻り値の links / nodes から「上位 hit が他の何と紐付いているか」を抽出し、登場 entity 群を時系列 / type 別に整理する。

### Step 4 (任意): LLM 統合要約 (`brief`)

ホスト側で要約を組み立ててもよいが、opshub 側 LLM backend が configured な場合は `brief` で一発要約を取れる：

```text
tool: brief
input:
  topic: "<トピック>"
  format: "md"
  expand_graph: true
  max_sources: 30
```

`brief` は内部で `recall.search` を再実行し、`expand_graph=true` なら 1-hop graph neighbours まで広げて LLM に渡して Markdown 要約を返す (ADR-0017 §(e)+(f))。戻り値は `{format, briefing_id, markdown, source_count, source_refs}`。

`brief` の hit と Step 1〜3 の hit の overlap が高い場合、Step 1〜3 の生データを **裏付け** として並べ、`brief` の Markdown を本文に据えるのが効率良い。LLM backend 未設定 / token 不足の場合は失敗するので、その場合は Step 1〜3 の素材だけでホスト側 LLM が要約する。

### Step 5 (任意): 特定 source の詳細を引く (`source.get`)

経緯サマリで「この source が転換点」と判断したものを 1〜3 件、`source.get` で詳細取得する：

```text
tool: source.get
input:
  source_id: "<上位 entity_id>"
```

本文全体を context window に詰めず、要点抜粋に留める (ADR-0022 §(d) context 効率原則)。

## 出力フォーマット (ホスト側)

```text
# <トピック> の横断調査

## 経緯サマリ (brief or ホスト LLM 要約)
- ...
- 転換点: <date> に <decision / source>
- 現在の状態: <task / open issue>

## 関連 sources (上位 N 件、source_type 別)
- ADR / decision: ...
- Slack / Teams: ...
- Box / OneDrive / Office docs: ...
- GitHub (issue / PR / notification): ...
- Outlook / Calendar: ...

## 関連 entities (graph 拡張)
- task: [P1] ...
- decision: ADR-XXXX / decision #M ...
- linked sources: ...

## 補足 / 不明点 (ホスト LLM が組み立て)
- ...
```

## 自律範囲

- **read tool のみ** — `recall.search` / `search` / `graph.related` / `graph.expand` / `graph.trace` / `brief` / `source.get` はすべて `readOnlyHint=true` (ADR-0022 §(c))。確認なしで呼んでよい
- `brief` は LLM provider を叩くため caller-pays コストが発生する点に注意 (ADR-0022 §(c) の brief 分類コメント参照)。コスト前提でユーザーが「網羅的に」と頼んでいるときに限り使う
- durable state を変える tool (`task.create` / `inbox.add` / `propose.generate` / `propose.apply` / `connector.sync`) は本 skill では呼ばない
- persist しない — 出力はその場限り

## できないこと / やらない

- 外部 SaaS への投稿 / 通知送信 (ADR-0010 §禁止事項 7)
- 調査結果を opshub の durable state に書き戻す (`propose.generate` / `propose.apply` は別 skill / 操作の責務、ADR-0016 §決定 (c) auto-apply 禁止)
- 外部 web 検索 / 公開 docs fetch (opshub の記憶層 = ローカル DB が対象、open world fetch は本 skill 範囲外)
- 本文を agent context に full payload で渡す (ADR-0022 §(d))
- 特定の 1 ファイルを「引く」用途 → `find-document` skill の責務
- 特定 decision の「根拠」を掘る用途 → `decision-rationale` skill の責務
- 外向きの定型まとめ → `external-brief` skill の責務

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0010 §改訂 (connector contract、Phase 11 で Teams 追加)
- ADR-0012 改訂 (本文 embedding で recall 品質、Phase 10 §18)
- ADR-0016 改訂 (Action Loop、Phase 12 H1 で draft 系統一方針追加)
- ADR-0017 (Knowledge Graph、graph.related / graph.expand / graph.trace)
- ADR-0020 §改訂 (Outlook body deep retention、Phase 11)
- ADR-0022 改訂 (MCP Server Surface、Phase 12 H1 で search FTS5 を MCP に露出 + physical-column 時間フィルタ追加)
- ADR-0025 (Office 文書本文抽出)
- Phase 10 Sub-issue B (#214 merged、本文 FTS5 / search command)
- Phase 11 plan (`docs/phase-11-plan.md`)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H2)
- docs/secretary-agent.md (Skill catalog SSOT、14 skills 責務マップ)
