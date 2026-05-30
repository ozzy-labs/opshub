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

```text
tool: decision.list
input:
  related_to: "<component or module path>"
  limit: 10
```

`decision.list` の `related_to` パラメータが未対応な場合はホスト側で `recall.search` 結果から decision のみフィルタする (Phase 10 Sub C の現実装に合わせる)。

### Step 3 (任意): 同コンポーネントの open task を確認

```text
tool: task.list
input:
  status: "active"
  limit: 50
```

過去に「ここは Phase X.x で直す」と pin されている open task が無いか確認。レビュー指摘の重複を避ける。

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
- ADR-0010 §禁止事項 7 (外部 SaaS 書き戻し禁止)
- ADR-0012 改訂 (本文 embedding で recall 品質)
- docs/secretary-agent.md
