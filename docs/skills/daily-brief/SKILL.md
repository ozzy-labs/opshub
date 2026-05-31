---
name: daily-brief
description: 「今日のまとめ」「状況教えて」「最近どうなってる」と聞かれたら、opshub MCP の brief (LLM 要約) または recall.search / task.list / inbox.list / decision.list を順に叩いて当日 (or 直近 24h) の主要な動きを要約する。LLM 推論ループは外部ホスト (Claude Code 等) 側、本 skill は手順書のみで実処理を持たない。
---

# daily-brief — 今日の状況を opshub から組み立てて返す

opshub MCP server (`opshub mcp serve`、ADR-0022) 経由で当日の operational memory を要約する。Phase 10 Sub-issue D / ADR-0004 §(c) で確定した secretary agent skill の 1 つ。

## 何が起きるか (host 側の流れ)

1. ユーザーが「今日のまとめ」「最近どうなってる」「状況教えて」のような表現で問い合わせる
2. 外部ホスト (Claude Code 等) が本 skill を発火させる
3. ホストが下記「呼び出し順」に従って opshub MCP tool を読み取り (read) 系のみ呼び出す
4. 戻り値 (JSON 文字列) を集約し、ユーザー向けに要約する

opshub 側で能動的に「日次まとめを送る」runtime は走らない (ADR-0004 §(a) 形A)。本 skill はリクエスト駆動で、ユーザーが問い合わせた瞬間にホストがツールを叩く。

## 呼び出し順 (MCP tool)

### Step 0 (オプション): 一発要約は `brief` を使う

LLM backend が configured なら、`brief` 一発で当日の Markdown 要約が返る。Step 1〜4 を回す前にこの 1 呼び出しを試して、戻り値の Markdown を素のまま提示できれば Step 1-4 は省略可能。

```text
tool: brief
input:
  topic: "today"        # ホストが当日トピックに置き換える
  format: "md"
  max_sources: 20
```

戻り値: `{"format":"md", "briefing_id":"...", "markdown":"...", "source_count": N}`. LLM 未設定 / token 不足の場合は失敗するので、その場合は Step 1〜4 に fallback。

### Step 1: 直近の重要シグナルを recall で拾う

```text
tool: recall.search
input:
  query: "today"  # ホストが当日トピックに置き換える
  limit: 20
```

戻り値の `hits[]` を「source / task / decision のどれを優先表示するか」を判断する素材とする。`recall.search` は ADR-0012 hybrid recall (本文 embedding + FTS5 + graph 補強) で、Sub-issue B 以降は本文ベースで hit する。

### Step 2: アクティブな task を列挙

```text
tool: task.list
input:
  state: "active"   # 当日に絞らず、未完了の全体感を見せる
  limit: 30
```

戻り値の `tasks[]` から「期限が近い / 担当 self / 進捗が止まっている」ものをホスト側で抽出。durable state は変えない (read のみ)。

### Step 3: 未処理 inbox を列挙

```text
tool: inbox.list
input:
  state: "pending"
  limit: 20
```

triage 前 (`pending`) の inbox item を拾う。ADR-0010 で「connector が直接 task 化しない」境界が確立されているため、inbox を見ないと外部由来の未処理シグナルが漏れる。

### Step 4: 最近の decision を確認

```text
tool: decision.list
input:
  limit: 10
```

当日の意思決定 / コミットメントを確認。再質問されたときの根拠として使う。`decision.list` は時間範囲フィルタを取らない (`limit` のみ受け付ける、`src/opshub/mcp/_registry.py` の Phase 10 schema) ため、当日に絞り込みたい場合はホスト側で戻り値 `decisions[i].recorded_at` を見て post-filter する。

## 出力フォーマット (ホスト側)

ホストが以下のような構造でユーザーに返す。具体的な文面はホスト側 LLM が決める (本 skill は構造のみ pin)。

```text
# 今日の状況

## 注目シグナル (recall.search 上位)
- ...

## 進行中の task
- [P1] ... (due: YYYY-MM-DD)
- ...

## 未処理 inbox
- ...

## 直近の decision
- ...
```

## 自律範囲

- **自律 OK** — すべて read 系 tool (`readOnlyHint=true`、ADR-0022 §(c))。確認なしで呼んでよい。
- durable state を変える tool (`task.create` / `inbox.add` / `connector.sync`) は本 skill では呼ばない。

## できないこと / やらない

- 外部 SaaS への投稿 / 通知送信 (ADR-0010 §禁止事項 7、Phase 10 plan §1 #6)
- 当日の動きを能動的に push する (ADR-0004 §(a)、Phase 10 plan §1 #5、能動機能は当面持たない)
- 推論結果を opshub の durable state に書き戻す (`opshub propose generate` / `apply` は別 skill / 操作の責務)

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0022 (MCP Server Surface)
- ADR-0025 (Office 文書本文抽出)
- ADR-0010 §改訂 (connector contract、Phase 11 で Teams 追加)
- ADR-0020 §改訂 (Outlook body deep retention、Phase 11)
- Phase 10 plan §3-D (skill ↔ MCP tool マッピング)
- Phase 11 plan (`docs/phase-11-plan.md`)
- docs/secretary-agent.md (利用例の catalog)
