---
name: external-brief
description: '「上司向け週次報告」「クライアント向け進捗まとめ」「外向きステータス」「マネージャーに送る report」「お客さんに見せる進捗」と頼まれたら、opshub MCP の task.list (state=completed, updated_after=対象期間開始) と decision.list (recorded_after=対象期間開始) を組み合わせて完了タスク + 意思決定を引き、brief で外向き tone のまとめを返す。persist なし、本 skill は手順書のみで実処理を持たない。pair: personal-brief (自分向け) と対をなす外向き skill。'
---

# external-brief — 上司 / クライアント向けの進捗まとめを opshub から組み立てる

opshub MCP server (`opshub mcp serve`、ADR-0022) 経由で「外向き」の進捗 / 完了 / 意思決定をまとめる Tier 1 skill。Phase 12 H3 (`docs/phase-12-plan.md` §3 H3) で `personal-brief` の pair として導入。`personal-brief` が自分向け (粒度細かめ、進行中タスクや未処理 inbox も含む) なのに対し、`external-brief` は外向き (完了済みの動き / 確定した意思決定が中心、未処理 inbox は基本的に出さない)。

外向き report は text 返却のみ (persist なし、ADR-0016 改訂 §決定 (l) draft 系統一方針)。host LLM が user に提示し、user が手動で SaaS や docs に転記する。opshub 側では外部 SaaS への書き戻し経路を持たない (ADR-0010 §禁止事項 7、緊張点③)。

## 何が起きるか (host 側の流れ)

1. ユーザーが「上司向け週次報告」「クライアント向け進捗まとめ」「外向きステータス」のような表現で問い合わせる
2. 外部ホスト (Claude Code 等) が本 skill を発火させる
3. ホストが対象期間を ISO 8601 timestamp に解釈する (今週 / 先週 / 今月 / 先月、デフォルト直近 7d)
4. ホストが下記「呼び出し順」に従って opshub MCP read tool を呼び出す
5. 戻り値を集約し、外向き tone (要点先出し / 進捗 + 確定事項 / 主観や進行中タスクの混入を抑制) でユーザー向けに整形する

opshub 側で能動的に「週次報告を送る」runtime は走らない (ADR-0004 §(a) 形A)。本 skill はリクエスト駆動で、ユーザーが問い合わせた瞬間にホストがツールを叩く。

## 期間の解釈 (ホスト側)

ユーザー発言からホストが ISO 8601 timestamp を解釈する：

| ユーザー語彙 | 期間（半開区間） | フィルタ |
|---|---|---|
| 今週 / 週次報告 (デフォルト) | 今週月曜 00:00 (local TZ) 〜 now | `*_after=今週月曜00:00` |
| 先週 | 先週月曜 00:00 〜 先週日曜 23:59 | `*_after=先週月曜` / `*_before=今週月曜` |
| 今月 / 月次報告 | 月初 00:00 〜 now | `*_after=今月1日00:00` |
| 先月 | 先月 1 日 〜 今月 1 日 | `*_after=先月1日` / `*_before=今月1日` |
| 直近 (デフォルト) | 直近 7d | `*_after=now-7d` |

各 tool は **physical-column ベース**の独立した argument 名を持つ (ADR-0022 改訂 §決定、Phase 12 H1)：

- `task.list`: `updated_after` / `updated_before`（→ `tasks.updated_at`）
- `decision.list`: `recorded_after` / `recorded_before`（→ `decisions.recorded_at`）

**注**: `task.list` の時間フィルタは physical column `tasks.updated_at` ベース (Phase 12 H1)。`completed_at` 列は projection に存在しないため、「期間内に完了した task」は `state="completed"` + `updated_after` の組合せで近似する (期間内に completed 化されたものは `updated_at` も更新されるため、実用上 hit する)。

## 呼び出し順 (MCP tool)

### Step 1: 期間内に完了した task を列挙

```text
tool: task.list
input:
  state: "completed"
  updated_after: "<期間開始 ISO 8601>"
  updated_before: "<期間終了 ISO 8601>"   # オプション (今週なら不要、先週なら必要)
  limit: 50
```

戻り値の `items[]` を外向き report の「完了した作業」セクションに使う。ホスト側で title / body / external_link を整形して提示する。`completed_at` 列が無いため厳密には「期間内に状態が completed になった task」ではなく「completed 状態かつ期間内に更新された task」を返す近似であることに留意 (Phase 12 H3 plan の注、§3 H3)。

### Step 2: 期間内に記録された decision を列挙

```text
tool: decision.list
input:
  recorded_after: "<期間開始 ISO 8601>"
  recorded_before: "<期間終了 ISO 8601>"   # オプション
  limit: 20
```

戻り値の `items[]` を外向き report の「確定した意思決定 / コミットメント」セクションに使う。decisions は ADR-0002 で immutable のため `recorded_at` が唯一の時間アンカー。

### Step 3: 一発要約は `brief` を使う (外向き tone 指示)

LLM backend が configured なら、`brief` 一発で対象期間 + 外向きトーン指示を含む topic を投げて Markdown 要約を取る。Step 1 + Step 2 で取得した task / decision の id / title を topic 文字列に含めると、`brief` が graph 補強で関連 source を辿って文脈付きで要約する。

```text
tool: brief
input:
  topic: "<期間ラベル> の外向き進捗報告。完了 task: <title list>、確定 decision: <title list>。上司 / クライアント向けに要点先出しでまとめる"
  format: "md"
  max_sources: 30
  expand_graph: true   # 任意。task / decision に紐づく source も拾いたいとき
```

戻り値: `{"format":"md", "briefing_id":"...", "markdown":"...", "source_count": N}`。LLM 未設定 / token 不足の場合は失敗するので、Step 1 + Step 2 の素材だけでホスト LLM が組み立てる fallback に切り替える。

**重要**: `brief` の `topic` 引数に「外向き」「上司向け」等のトーン指定を含めて誘導すること。これにより、自分向け (personal-brief) と外向き (external-brief) の出力差分が、同じ `brief` MCP tool を使っても確実に出る (host LLM が tone shift を実現する責務)。

## 出力フォーマット (ホスト側)

ホストが以下のような構造でユーザーに返す。具体的な文面はホスト側 LLM が決める (本 skill は構造のみ pin)。

```text
# <期間ラベル>の進捗報告

## ハイライト
- <要点先出し、3-5 行>

## 完了した作業
- [<title>] (<completed date>) — <一行サマリ>
- ...

## 確定した意思決定
- [<title>] (<recorded date>) — <意図 / 理由 / 影響>
- ...

## 次の見通し (任意)
- <短い見通し or 進行中の主要作業>
```

進行中の task や未処理 inbox は **混ぜない**（混ぜると personal-brief と差分がなくなる）。それらが欲しい場合は personal-brief の責務。

## 自律範囲

- **自律 OK** — すべて read 系 tool (`task.list` / `decision.list` / `brief`、ADR-0022 §(c) `readOnlyHint=true`)。確認なしで呼んでよい。
- durable state を変える tool (`task.create` / `inbox.add` / `connector.sync` / `propose.apply`) は本 skill では呼ばない。

## できないこと / やらない

- 外部 SaaS への投稿 / 通知送信 (ADR-0010 §禁止事項 7、Phase 10 plan §1 #6)。生成した report は user が手動で SaaS や docs に転記する
- 期間の動きを能動的に push する (ADR-0004 §(a)、能動機能は当面持たない)
- 推論結果を opshub の durable state に書き戻す (ADR-0016 改訂 §決定 (l)、handoff-draft / announcement-draft と並んで persist なし)
- 進行中タスクや未処理 inbox を外向き report に混ぜる → `personal-brief` skill の責務
- 個別 task の詳細経緯 → `decision-rationale` skill (decision 経緯) や `meeting-prep` (会議準備) の責務

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0016 改訂 §決定 (l) draft 系統一方針 (Phase 12 H1、persist 境界は「返信元 source の有無」で切る)
- ADR-0022 改訂 (MCP Server Surface、Phase 12 H1 で physical-column 時間フィルタ追加)
- ADR-0010 §禁止事項 7 (外部 SaaS 書き戻し禁止)
- ADR-0002 (Append-only Event Log、decision immutability)
- Phase 10 plan §3-D (skill ↔ MCP tool マッピング)
- Phase 12 plan (`docs/phase-12-plan.md` §3 H3)
- docs/assistant-agent.md (Skill catalog SSOT、14 skills 責務マップ)
- pair: `docs/skills/personal-brief/SKILL.md` (自分向け)
