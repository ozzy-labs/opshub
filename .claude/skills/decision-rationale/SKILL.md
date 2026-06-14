---
name: decision-rationale
description: 「あの決定はなぜ」「X を選んだ理由」「Y の決定経緯」「なんで A じゃなくて B にしたんだっけ」「この方針の根拠は?」と聞かれたら、opshub MCP の decision.list (期間 / トピック絞り) + graph.trace (decision を起点に source / proposal / 先行 decision へ provenance を遡る) + recall.search (関連 context の補強) を組み合わせ、決定 + 経緯 + 関連 source + 関連 prior decisions のサマリを返す。本 skill は手順書のみで実処理を持たない。
---

# decision-rationale — 「あの決定はなぜ」を opshub から組み立てる

opshub MCP server (`opshub mcp serve`、ADR-0022) 経由で、過去の意思決定の **根拠** と **経緯** を、関連する source / 先行 decision / 関連 task まで遡って提示する Tier 1 skill。Phase 12 H3 (`docs/phase-12-plan.md` §3 H3) で導入。

decisions は ADR-0002 で immutable な append-only event として保存され、各 decision は graph (ADR-0017) 経由で source / proposal / 先行 decision に backward link を持つ。本 skill はこの構造を活用し、`graph.trace` で provenance を遡って「なぜその決定に至ったか」を再現する。

## 何が起きるか (host 側の流れ)

1. ユーザーが「あの決定はなぜ」「X を選んだ理由」「Y の決定経緯」のような表現で問い合わせる
2. 外部ホスト (Claude Code 等) が本 skill を発火させる
3. ホストがユーザー発言から **トピック語彙** (X / Y / 「あの方針」が指す対象) を抽出する
4. ホストが下記「呼び出し順」に従って opshub MCP read tool を呼び出す
5. 戻り値を集約し、「決定 → 直接の根拠 source → 先行 decision → 関連 context」の順で提示する

decisions は immutable のため、本 skill は **読み取り専用**。decision の修正・取り消しは別の write 経路 (新しい decision を append する) であり、本 skill の責務外。

## 呼び出し順 (MCP tool)

### Step 1: トピックを decision コーパスから引く

ユーザーが指している decision の候補を絞る。トピック語彙が明確 (例:「auto-merge」「Phase 11 の Outlook」) なら recall.search、期間が明確 (例:「先月の決定」) なら decision.list の時間フィルタを使う。両方使ってもよい。

#### 1a. recall.search でトピック絞り

```text
tool: recall.search
input:
  query: "<topic vocabulary、例: 'auto-merge 方針' / 'Outlook body deep retention'>"
  entity_type: "decision"
  limit: 10
```

戻り値の `hits[]` から `entity_id` (decision の ULID) を取り出して Step 2 / Step 3 に渡す。

#### 1b. (任意) decision.list で期間絞り

ユーザーが「先月の」「今期の」のような期間指定を含めている場合：

```text
tool: decision.list
input:
  recorded_after: "<期間開始 ISO 8601>"
  recorded_before: "<期間終了 ISO 8601>"   # オプション
  limit: 20
```

戻り値 `items[]` から title / body をホストが読み、ユーザー意図に合う decision を 1 つ (or 数件) 選ぶ。`recorded_after` / `recorded_before` は Phase 12 H1 で `decisions.recorded_at` 物理列ベースで追加された (ADR-0022 改訂 §決定)。

### Step 2: graph.trace で経緯を遡る

選んだ decision を起点に backward provenance (link `is_caused_by` / `derives_from` / `superseded_by` 等の inbound chain) を辿る。

```text
tool: graph.trace
input:
  entity_id: "<decision ULID from Step 1>"
  entity_type: "decision"
  depth: 3
```

戻り値 `paths[]` は backward-chain link paths。各 path は `[decision] ← [proposal] ← [source]` のように connector level まで遡る。depth は ADR-0017 §(e) で最大 10、default 3。経緯が深ければ depth を上げる。

`paths` から以下を抽出する：

- **直接の根拠 source**: depth=1 で hit する `source` 系 entity (例: Slack discussion, GitHub PR/Issue, Outlook thread, Office 文書)
- **先行 decision (prior decisions)**: 同 chain 上の `decision` 系 entity (superseded_by chain を含む)
- **proposal**: decision の前段に生成された Candidate (`task | decision | reply_draft`、ADR-0016 §決定 (l))

### Step 3: 関連 context を recall で補強

decision 本文や直近の source に明示されていない周辺文脈 (関連する別 decision / 同時期の task / 議論された alternative) を補う。

```text
tool: recall.search
input:
  query: "<decision title or 主要キーワード>"
  limit: 15
```

`hits[]` のうち Step 2 で既に登場した entity_id を除外し、残ったものを「関連 context」セクションに使う。これにより「明示的な provenance」(Step 2) と「意味的に近い周辺事情」(Step 3) を分離して提示できる。

### Step 4 (任意): 個別 source の本文詳細を引く

ユーザーが特定 source の細部 (Slack message の全文 / Outlook の thread / PR description) を見たい場合、その `entity_id` を `source.get` に渡す。

```text
tool: source.get
input:
  source_id: "<entity_id from graph.trace paths>"
```

本文全体を context window に詰めない (ADR-0022 §(d) context 効率原則、Phase 11 で Outlook body deep retention 追加後も同じ)。

## 出力フォーマット (ホスト側)

ホストが以下のような構造でユーザーに返す。本 skill は構造のみ pin、文面はホスト LLM が決める。

```text
# 「<topic>」の決定経緯

## 結論
- <decision の title / 一行サマリ> (recorded: YYYY-MM-DD)

## 根拠 (直接の source)
- [<source title>] (<source_type>, <date>) — <要点>
  → <url or external_id>
- ...

## 先行する意思決定
- [<prior decision title>] (recorded: YYYY-MM-DD) — <prior decision との関係>
- ...

## 関連する周辺文脈
- [<related context title>] — <意味的に近い別 entity の要点>
- ...
```

## 自律範囲

- **自律 OK** — すべて read 系 tool (`recall.search` / `decision.list` / `graph.trace` / `source.get`、ADR-0022 §(c) `readOnlyHint=true`)。確認なしで呼んでよい。
- durable state を変える tool (`task.create` / `inbox.add` / `connector.sync` / `propose.apply`) は本 skill では呼ばない。

## できないこと / やらない

- 過去の decision の修正 / 取り消し (decisions は ADR-0002 で immutable、本 skill scope 外)
- 新しい decision の生成 (write 経路、別 skill / 別操作)
- 外部 SaaS への投稿 / 通知送信 (ADR-0010 §禁止事項 7)
- 本文を agent context に full payload で渡す (ADR-0022 §(d) context 効率原則)
- 推論結果を opshub の durable state に書き戻す (本 skill は分析・照会専用)
- decision 同士の関係 (supersedes / aligns with) を勝手に書き換える (graph 上の link は append-only、ADR-0017)

## 参考

- ADR-0002 (Append-only Event Log、decision immutability)
- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0016 改訂 §決定 (l) draft 系統一方針 (Phase 12 H1、Candidate discriminated union 凍結)
- ADR-0017 (Knowledge Graph、graph.trace の depth 上限 / link 種別)
- ADR-0020 (本文ローカル保持)
- ADR-0022 改訂 (MCP Server Surface、Phase 12 H1 で physical-column 時間フィルタ追加)
- ADR-0025 (Office 文書本文抽出、Phase 11)
- Phase 10 plan §3-D (skill ↔ MCP tool マッピング)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H3)
- docs/assistant-agent.md (Skill catalog SSOT、15 skills 責務マップ)
