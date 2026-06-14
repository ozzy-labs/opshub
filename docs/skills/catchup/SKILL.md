---
name: catchup
description: '「前回見て以降どうなった」「久しぶりに状況確認」「最後に見てから何が変わった」「差分だけ教えて」「catchup したい」「未読を消化したい」と聞かれたら、opshub MCP の catchup tool を叩いて前回 catchup 以降に積み上がった差分 (新規 source / 期日超過を含む open commitment / 新着 Slack demand) だけを優先度順に要約する。catchup は seen-marker を前進させる非破壊 write tool で、実行すると「ここまで見た」が記録され「ここまで見た」が記録される (次回 catchup はその先の差分だけを返す)。LLM 推論ループは外部ホスト (Claude Code 等) 側、本 skill は手順書のみで実処理を持たない。pair: personal-brief (期間ベースの総覧) に対し、catchup は seen-marker ベースの「前回以降の差分」に特化する。'
---

# catchup — 前回見て以降の差分だけを優先度順に返す

opshub MCP server (`opshub mcp serve`、ADR-0022) 経由で「前回 catchup してから今までに積み上がった差分」だけを surface する。期間ラベル (今日 / 今週) を指定する `personal-brief` と違い、catchup は **seen-marker** (「operator が最後に catchup した時刻」を保持する singleton) を基準に差分を計算する (Phase 25-E、epic #566)。

非常駐原則 (ADR-0004 §(a) 形A) を維持する: opshub 側で能動的に「変更があったら通知する」runtime は走らない。本 skill はリクエスト駆動で、ユーザーが「前回以降どうなった?」と聞いた瞬間にホストが catchup tool を叩く。

## personal-brief との違い (pair)

| 軸 | personal-brief | catchup |
|---|---|---|
| 基準 | 期間ラベル (今日 / 今週 / 先月) | seen-marker (前回 catchup 時刻) |
| 対象 | 期間内の主要な動き全般 | 前回以降に積み上がった差分のみ |
| 状態変化 | なし (read-only) | seen-marker を前進させる (「ここまで見た」を記録) |
| 用途 | 定点観測 / 振り返り | 久しぶりの復帰 / 未読消化 |

「今週どうなってる」は personal-brief、「前回見て以降どうなった」「久しぶりに状況確認」は catchup。

## 何が起きるか (host 側の流れ)

1. ユーザーが「前回見て以降どうなった」「久しぶりに状況確認」「差分だけ教えて」のような表現で問い合わせる
2. 外部ホスト (Claude Code 等) が本 skill を発火させる
3. ホストが opshub MCP の `catchup` tool (非破壊 write) を 1 回呼ぶ
4. 戻り値 (JSON) の 3 セクション (新規 source / open commitment / 新着 Slack demand) を集約し、ユーザー向けに要約する
5. catchup の実行で seen-marker が前進する (次回 catchup はこの先の差分だけを返す)

## 呼び出し (MCP tool)

```text
tool: catchup
input:
  limit: 20            # オプション: セクションごとの表示件数上限 (件数 total は常に全差分を反映)
  advance: true        # オプション: seen-marker を前進させるか (default true)。
                       # ドライプレビュー (marker を動かさず差分だけ見たい) なら advance: false
```

戻り値 (JSON):

```json
{
  "since": "2026-06-01T09:00:00+00:00",
  "advanced_to": "2026-06-14T10:00:00+00:00",
  "new_sources_total": 12,
  "new_sources": [
    {"id": "01...", "connector_name": "slack", "source_type": "slack_message", "title": "...", "url": null, "observed_at": "..."}
  ],
  "open_commitments_total": 5,
  "overdue_commitments_total": 2,
  "open_commitments": [
    {"id": "01...", "direction": "i_owe", "counterparty": "person:01...", "due": "2026-06-10", "text": "...", "overdue": true}
  ],
  "new_demand_total": 3,
  "new_demand": [
    {"team_id": "T...", "channel_id": "C...", "channel_name": "#general", "demand_kind": "mention", "last_demand_user_id": "U...", "last_demand_excerpt": "...", "last_demand_permalink": "https://...", "last_demand_at": "..."}
  ]
}
```

- `since` が `null` のときは初回 catchup (履歴全体が「未見」扱い)。
- `advanced_to` が `null` のときは `advance: false` (ドライプレビュー、marker 不変)。
- 各 `*_total` は全差分の件数、リストは `limit` で切り詰められる。

## 3 セクションの読み方

1. **新規 source** (`new_sources`) — 前回以降に取り込まれた `sources` 行 (Slack / Gmail / Box / GitHub / MS365 / Google Workspace 等、connector 横断)。`observed_at` 降順。「何が来たか」の入口。
2. **open commitment** (`open_commitments`) — 旗艦コミットメント台帳 (Phase 25-C、ADR-0042) の open な約束。**期日超過 (`overdue: true`) を先頭に** 出す。`direction` が `i_owe` なら「自分が負っている約束」、`owed_to_me` なら「相手に頼んで待っている件」。`overdue_commitments_total` を最優先シグナルとして扱う。
3. **新着 Slack demand** (`new_demand`) — 前回以降に更新された `slack_demand_digest` 行 (Phase 18-B、ADR-0033)。自分宛 @mention / DM のうち operator がまだ返していないもの。`last_demand_permalink` を付けると Slack UI に直接飛べる。

## 出力フォーマット (ホスト側)

ホストが以下のような構造でユーザーに返す。具体的な文面はホスト側 LLM が決める (本 skill は構造のみ pin)。`brief` (ADR-0015) と同型の section grouping を seen-marker 起点で適用する。

```text
# 前回 (<since>) 以降の差分

## ⚠️ 期日超過のコミットメント (overdue_commitments_total)
- [→ I owe] (due 2026-06-10) 〜を送る  ← 最優先

## 自分宛の新着 Slack (new_demand_total)
- [mention #general] @alice: 「<excerpt>」 → permalink

## 新規に取り込まれた source (new_sources_total)
- [2026-06-13] slack/slack_message: 〜
```

## 自律範囲

- **自律 OK** — `catchup` は非破壊 write tool (`readOnlyHint=false` / `destructiveHint=false`、ADR-0022 §(c))。marker 前進は「catch me up」の consented な結果なので確認なしで呼んでよい。
- ただし catchup は副作用として **seen-marker を前進させる** (durable state)。これは「次回 catchup の基準点を進める」だけの軽い前進で、external な送信や proposal の apply は伴わない。marker を動かしたくない (差分の二度見をしたい) ときは `advance: false` を指定する。
- `commitment.resolve` / `commitment.dismiss` 等の状態遷移や `task.create` などの durable write は本 skill では呼ばない (operator の明示操作 / 別 skill の責務)。

## できないこと / やらない

- 外部 SaaS への投稿 / 通知送信 (ADR-0010 §禁止事項 7)
- 差分を能動的に push する (ADR-0004 §(a)、能動機能は当面持たない)。あくまでリクエスト駆動。
- コミットメントの督促 (外部送信) — 台帳は read signal、督促は行わない (ADR-0042 §督促境界 / ADR-0010 write-back ban)
- 期間ラベル (今日 / 今週) ベースの総覧 → `personal-brief` skill の責務 (pair)

## 参考

- ADR-0004 (Agent Runtime Boundary、形A)
- ADR-0015 (Briefing、本 skill は seen-marker 起点で section grouping を応用)
- ADR-0022 改訂 (MCP Server Surface、`catchup` read tool)
- ADR-0042 (Commitment ledger、Phase 25-C) — open commitment セクションの根拠
- ADR-0033 (Slack mention / DM demand digest、Phase 18) — new_demand セクションの根拠
- epic #566 (秘書化 v1) / Phase 25-E (#570) — seen-marker + `opshub catchup` + 本 skill
- docs/assistant-agent.md (Skill catalog SSOT、15 skills 責務マップ)
