---
name: pr-review
description: 「この PR レビューして」「#123 確認して」「PR どう思う」と頼まれたら、opshub MCP の recall.search で関連 source / decision / 過去 review を引き、必要に応じて gh pr diff の出力を組み合わせてレビュー観点を提示する。read 系のみで構成され、PR への comment 投稿は外部送信扱いで本 skill では行わない。
---

# pr-review — opshub の記憶を引いて PR レビューを補強する

opshub MCP server (`opshub mcp serve`、ADR-0022) の read 系 tool で、GitHub PR レビューに必要な「過去にこのコンポーネントで何が決まったか」「関連する task / decision / source は何か」を引いてホスト側 LLM に渡す。

opshub 単体では LLM 推論ループを持たない (ADR-0004 §(a) 形A)。実際の差分理解とコメント生成は外部ホスト (Claude Code 等) 側が行う。本 skill はあくまで **opshub の記憶層からどう情報を引くか** の手順書。

## 何が起きるか

1. ユーザーが「PR #123 レビュー」「この差分どう?」と頼む
2. ホストが本 skill を発火
3. ホストは GitHub 側情報 (差分・既存コメント) を `gh` CLI で取得 (skill 範囲外、ホスト側責務)
4. 同時に opshub MCP の `recall.search` で関連記憶を引く
5. 2 種の情報を組み合わせて指摘事項を組み立てる
6. PR への comment 投稿は **本 skill 外** (外部送信、ADR-0010 §禁止事項 7 のスピリットに従いユーザーが手で `gh pr review` を実行)

## 呼び出し順 (MCP tool)

### Step 1: PR 番号 / リポ名から関連 source を recall

```text
tool: recall.search
input:
  query: "PR #<N> <component-name>"
  limit: 15
```

`hits[]` から GitHub source (PR / issue / commit) と関連する task / decision を抽出。本文ベース embedding (Sub-issue B、ADR-0012 改訂) によりコメント内容や commit message も hit する。

### Step 2: 過去の同コンポーネントの decision を確認

`decision.list` は Phase 12 H1 (ADR-0022 改訂) で physical-column ベースの時間フィルタ `recorded_after` / `recorded_before` (ISO 8601、`decisions.recorded_at`) を取れるようになった。直近 N 週間の decision だけ引きたい場合は:

```text
tool: decision.list
input:
  recorded_after: "<N 週間前 ISO 8601>"
  limit: 30
```

component / module path で絞り込みたい場合は `recall.search` を component 名で発火し、戻り値 `hits[]` を `entity_type == "decision"` で post-filter する経路を取る:

```text
tool: recall.search
input:
  query: "<component or module path>"
  limit: 30
```

ホスト側で `hits[]` から `entity_type` が `decision` のものだけを抽出し、上位 10 件程度を「過去の同コンポーネントの意思決定」として表示する。本文ベース embedding (Sub-issue B、ADR-0012 改訂) により module path や component 名が decision 本文に含まれていれば hit する。

### Step 3 (任意): 同コンポーネントの open task を確認

```text
tool: task.list
input:
  state: "active"
  limit: 50
```

過去に「ここは Phase X.x で直す」と pin されている open task が無いか確認。レビュー指摘の重複を避ける。

### Step 4 (任意): 関連 entity の graph を追う

`recall.search` で hit した task / decision / source の周辺に何が紐付いているか確認するなら graph 系 tool が使える。指摘候補の根拠付けに使える。

```text
tool: graph.related
input:
  entity_type: "<task|decision|source>"
  entity_id: "<ULID>"
  direction: "both"
  limit: 30
```

過去の意思決定が現在どの task / source に波及しているかを追うなら `graph.trace` (provenance 方向、backward) や `graph.expand` (双方向 N-hop) も有効。すべて read-only。

```text
tool: graph.trace
input:
  entity_type: "source"
  entity_id: "<該当 PR の source ULID>"
  depth: 3
```

## 出力フォーマット (ホスト側)

```text
# PR #N レビュー観点

## 関連する過去の意思決定
- ADR-XXXX で ... と pin 済
- decision #M で ... と決定済

## 関連する未完了 task
- [P1] #task-id ...

## 指摘候補 (ホスト LLM が差分と合わせて生成)
- ...

## コメント投稿
- 良ければユーザーが `gh pr review --comment` で投稿 (opshub は GitHub への書き戻しを行わない)
```

## 自律範囲

- **read tool のみ** — `recall.search` / `decision.list` / `task.list` はすべて `readOnlyHint=true`
- PR へのコメント投稿 / approval は **本 skill 範囲外**。ユーザーが `gh` CLI で手動実行

## できないこと / やらない

- GitHub PR への comment 投稿 / approval / merge (ADR-0010 §禁止事項 7、Phase 10 plan §1 #6)
- task や inbox への自動追記 (write は別 skill / 確認経路)
- 「LGTM」の自動承認 — レビューの最終判断は人 (ホスト LLM ではなく、ユーザー自身)

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0010 §禁止事項 7 + §改訂 (外部 SaaS 書き戻し禁止、Phase 11 で Teams 追加)
- ADR-0012 改訂 (本文 embedding で recall 品質)
- ADR-0020 §改訂 (Outlook body deep retention、Phase 11)
- ADR-0025 (Office 文書本文抽出)
- Phase 11 plan (`docs/phase-11-plan.md`)
- docs/assistant-agent.md
