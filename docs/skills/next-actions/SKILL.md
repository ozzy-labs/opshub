---
name: next-actions
description: 「次に何をする?」「やること教えて」「タスク何が残ってる?」と聞かれたら、opshub MCP の task.list と recall.search を使って優先度順の next-actions を組み立てる。新規 task の作成は task.create が write tool のためホスト側で人確認を促す (ADR-0022 §(c))。
---

# next-actions — 「次にやること」を opshub から組み立てる

opshub MCP server (`opshub mcp serve`、ADR-0022) 経由で「次にやること」リストを返す。Phase 10 Sub-issue D / ADR-0004 §(c) で確定した secretary agent skill の 1 つ。

## 何が起きるか

1. ユーザーが「次に何やる?」「やること教えて」「優先度高いのは?」のように問い合わせる
2. ホストが本 skill を発火
3. ホストが下記順で opshub MCP read tool を呼ぶ
4. 結果を優先度付きリストとして返す

新規タスクを足したいときだけ `task.create` (write tool) を呼ぶ。これは durable state を変えるため、ホスト側で「このタスクを追加してよいですか?」と人確認を入れる (ADR-0022 §(c) `destructiveHint=true`)。

## 呼び出し順 (MCP tool)

### Step 1: active な task を列挙

```text
tool: task.list
input:
  status: "active"
  limit: 50
```

戻り値の `tasks[]` をホストが優先度 (priority field / due_at / 直近更新) でソートし上位を抽出。

### Step 2: 必要に応じて recall で文脈補強

ユーザーが特定トピック (例:「PR レビュー周りで何が残ってる?」) で絞り込んでいる場合のみ:

```text
tool: recall.search
input:
  query: "<user query>"
  limit: 10
```

`hits[]` を task 列と突き合わせ「このタスクは PR #N の文脈」のような contextual hint をホストが組み立てる。一般的な「次やること?」では Step 2 をスキップしてよい。

### Step 3 (条件付き、人確認必須): 新規 task を作る

ユーザーが明示的に「これも task に入れて」と頼んだ場合のみ:

```text
tool: task.create
input:
  title: "<text>"
  priority: P2   # ホスト側で推定
```

このとき:

- `task.create` は write tool (`destructiveHint=true`)。ホストは必ず人確認 (HITL) を取る
- 確認なしで auto-call しないこと (ADR-0004 §(b) / ADR-0022 §(c))。tool poisoning 攻撃面 (auto-approve 84% / HITL <5%) の非対称が直撃する

## 出力フォーマット (ホスト側)

```text
# 次にやること

## 今すぐ
- [P0] ...
- [P1] ...

## 今日中
- [P1] ...

## 今週
- [P2] ...
```

`task.create` を呼んだ場合は末尾に「追加した task: ...」を付ける。

## 自律範囲

- **read tool (`task.list` / `recall.search`)** — 自律 OK
- **write tool (`task.create`)** — 人確認必須

## できないこと / やらない

- task の自動完了 (`task.complete` 等の destructive 操作は本 skill scope 外、別 skill or CLI 経路)
- 外部 SaaS への通知 / 共有 (ADR-0010 §禁止事項 7)
- LLM が「これも task にしておきました」と勝手に書き戻す (ADR-0016 §決定 (c) auto-apply 禁止)

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0016 (Action Loop、auto-apply 禁止)
- ADR-0022 (MCP Server Surface、read/write 分離)
- docs/secretary-agent.md
