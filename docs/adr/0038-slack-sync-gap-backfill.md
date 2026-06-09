# 0038. Slack Sync Gap Backfill (low-water-mark 軸 + bounded fetch)

- Status: Accepted + Landed (Phase 22 完了、epic [#516](https://github.com/ozzy-labs/opshub/issues/516))
- Date: 2026-06-08 (Accepted); 2026-06-08 (Landed: 22-A docs [#523](https://github.com/ozzy-labs/opshub/pull/523) / 22-B cursor schema [#525](https://github.com/ozzy-labs/opshub/pull/525) / 22-C bounded fetch [#526](https://github.com/ozzy-labs/opshub/pull/526) / 22-D connector gap pass [#527](https://github.com/ozzy-labs/opshub/pull/527) / 22-E `opshub slack cursor` CLI + closeout)
- Deciders: opshub maintainers
- Related: [ADR-0036](0036-slack-sync-date-floor.md) (date floor — 本 ADR が §(g) cursor authoritative を「forward 限定」へ精緻化し §Consequences の rebuild 記述を是正)、[ADR-0030](0030-slack-thread-reply-ingestion.md) (thread reply ingestion / compound cursor schema)、[ADR-0010](0010-connector-contract.md) (connector / cursor contract — `backfill` 軸を追加)、[ADR-0002](0002-event-sourced-architecture.md) (projection は event log から再構築 = `opshub projections rebuild` の意味論)

## Context

[ADR-0036](0036-slack-sync-date-floor.md) は `opshub slack sync` に date floor (`[connectors.slack] sync_since` / per-channel `since`) を入れ、`conversations.history` の `oldest` を `oldest = _max_ts(cursor, floor_ts)` で bound した (`src/opshub/connectors/slack/connector.py`)。cursor を authoritative に保つことで「既存 sync 済み環境に後から floor を有効化しても再取得・削除が起きない安全な opt-in」を実現したが、これは **過去方向への取得 (backfill) を一切持たない** ことの裏返しでもある。

帰結として、運用上の落とし穴が 2 つ顕在化した:

1. **floor を後から下げても過去が取れない。** 例えば `sync_since = 7d` で sync 済みの channel について `sync_since = 30d` に下げても、cursor が "now" 付近まで前進済みなので `oldest = max(now付近, 30d前) = now付近` となり、7〜30 日前の区間は永久に取得されない。ADR-0036 §(g) / §Consequences はこれを「意図された制約」とし、回避策として `opshub projections rebuild` を案内した。

2. **`opshub projections rebuild` は Slack cursor をリセットしない (ADR-0036 の回避策が実は機能しない)。** rebuild は event log を保持したまま全 projection を流し直す ([ADR-0002](0002-event-sourced-architecture.md))。`connector_cursors` projection は `ConnectorSyncStarted` / `ConnectorSyncCompleted` を replay して `cursor_value` を upsert / update する (`src/opshub/projections/connector_cursors.py`) ため、replay 後の cursor は **最後の `ConnectorSyncCompleted` と同じ最新値に復元される**。つまり rebuild は cursor を空に戻さない → 次回 sync の `oldest` は変わらず → **再 backfill は起きない**。ADR-0036 §Consequences / `CLAUDE.md` / `docs/troubleshooting.md` の「rebuild で取り直す」記述は事実誤認であり、**現状は過去取り直しの正規手段が存在しない**。

さらに、過去区間を素朴に再取得する設計には別の罠がある:

1. **再観測は inbox を膨張させる。** `SourceService.observe` は呼ぶたびに `SourceObserved` + `ItemEnqueued` を無条件発行する (`src/opshub/services/source_service.py`、既存 source のガードなし)。`inbox` projection は `ItemEnqueued` を毎回新 `aggregate_id` で INSERT し `source_ref` で重複排除しない (`src/opshub/projections/inbox.py`)。一方 `sources` のみ `(connector_name, external_id)` upsert で冪等 (`src/opshub/projections/sources.py`)。したがって cursor を単純に巻き戻して既取得区間を再 fetch すると inbox 行が二重化する (旧 issue #339 cascade と同型)。

本 ADR は Phase 22 (epic #516) として、floor 引き下げ時に **「新たに広がった窓だけ」を自動取得する gap backfill** を pin する。pre-userbase につき後方互換は不要 ([AGENTS.md](../../AGENTS.md) §設計判断のスタンス)。

## Decision

compound cursor に per-channel の **low-water-mark (`backfill` 軸)** を追加し、forward 取得済み区間と disjoint な gap だけを bounded fetch する。以下 (a)-(h) を pin する。

### (a) cursor schema に `backfill` 軸を追加する (additive)

Phase 20-B ([ADR-0030](0030-slack-thread-reply-ingestion.md)) の 2 軸 compound cursor を 3 軸に拡張する:

```json
{
  "channels": {"<channel_id>": "<high_water_ts>"},
  "backfill": {"<channel_id>": "<low_water_ts>"},
  "threads":  {"<channel_id>:<thread_ts>": "<last_reply_ts>"}
}
```

- `channels` (既存) = 取得済み**最新** ts (forward high-water)。`conversations.history(oldest=...)` を前進駆動。Phase 7 / 20 から不変。
- **`backfill` (新規)** = 取得済み**最古** ts 境界 (backfill low-water)。**「この ts を含まずそこまで被覆済み」** と定義する。
- `threads` (既存) = per-thread late-reply cursor。不変。

`backfill` 軸の欠損は許容し空 dict として扱う (additive migration、§(g))。pre-Phase-20-B の flat dict は従来どおり `ConfigError` で reject する (Phase 20-B の挙動を維持)。

### (b) 被覆区間の不変条件

各 channel について区間 **`(backfill[ch], channels[ch]]` は全量取得済み** とする。forward sync が上端 (`channels`) を伸ばし、backfill が下端 (`backfill`) を伸ばす。2 つの境界は独立に動く。

### (c) 境界の inclusive 仕様 (off-by-one を pin)

- forward は既存どおり `oldest=cursor, inclusive=False` で進む。cold-start では `oldest=floor, inclusive=False` なので被覆は `(floor, now]`、よって low-water = `floor` (その ts 自体は**含まない**)。
- gap backfill は `conversations.history(oldest=floor_new, latest=low_water, inclusive=True)` で **`[floor_new, low_water]`** を取得する。**注意: Slack の `inclusive` は oldest / latest 両境界に効く単一 boolean** であり per-bound flag は無い。backfill では `inclusive=True` を採る — `floor_new` / `low_water` はいずれも**合成 date floor** であり実メッセージ ts とほぼ一致しないため両境界を含めても実害がなく、forward cold-start が `oldest=low_water, inclusive=False` で `ts == low_water` を取得していない以上、backfill `[floor_new, low_water]` と forward `(low_water, now]` は **disjoint かつ contiguous** になる (union = `[floor_new, now]`)。連鎖 backfill が共有境界 `floor_new` で 1 メッセージだけ重複し得るが、合成 ts のため実際にはまず起きず、起きても `sources` upsert 冪等 + #522 で無害化される。
- backfill 完了後、`backfill[ch] = floor_new` に後退させる。

### (d) low-water ライフサイクル

**`backfill` 軸は floor を設定した channel についてのみ materialise する。** 軸の **欠損 (absent) は epoch low-water (「先頭まで被覆済み = backfill 不要」) と等価**とし、no-floor channel には spurious な epoch entry を書かない (cursor を簡潔に保つ + 既存テストの非破壊)。

- **cold-start** (`channels[ch]` 不在):
  - floor **あり**: forward は `oldest = floor` から再開するので channel は floor まで被覆される。`backfill[ch] = floor` を記録し、後の floor 引き下げで gap を検知できるようにする。
  - floor **なし** (full-history): 全量取得するので backfill すべき過去はない → 軸は**書かない (absent = epoch)**。
- **既 sync 済みだが low-water 未記録** (`channels[ch]` あり・`backfill[ch]` 不在): pre-Phase-22 channel か、no-floor sync の後に floor を足した channel。いずれも過去の被覆範囲が不明 (または既に floor 以上を被覆済み) なので **auto gap backfill はしない** (軸 absent のまま)。救済は §(f) の明示コマンド。
- **毎 sync (low-water 記録済み)**: `target_low` (= floor、floor 撤去時は epoch) を解決し、`target_low < backfill[ch]` のとき gap `(target_low, backfill[ch]]` を bounded fetch して ingest、`backfill[ch] = target_low` に後退。`target_low >= backfill[ch]` なら no-op。
- **相対 floor (`90d`)** は sync 実行ごとに ts が**前進**する ([ADR-0036](0036-slack-sync-date-floor.md) §(f)) ため `target_low < backfill[ch]` にならず、**gap backfill は発火しない**。**絶対 floor の引き下げ / `sync_since` の縮小 / floor の撤去** のときだけ 1 度発火し、追いついたら止まる。

### (e) pre-feature channel の扱い (正直な限界)

§(d) のとおり、`channels[ch]` あり・`backfill[ch]` 不在の channel (本機能着地より前に sync 済み) は過去にどの floor で cold-start したかを **復元できない**ため、`backfill` 軸を absent のまま (= epoch 等価、inflation-safe) とし **auto gap backfill はしない**。

帰結として §Context の発端シナリオ (feature 着地前に `7d` で sync 済み → `30d` に下げる) は **自動では解決しない**。pre-feature channel の救済は §(f) の明示コマンド (`opshub slack cursor backfill`) で行う。

### (f) operability: `opshub slack status`（日常）と `opshub slack cursor`（復旧）の二層

ADR-0036 が案内した壊れた rebuild 経路を置き換える、実際に機能する操作経路を提供する。Phase 23-F ([#536](https://github.com/ozzy-labs/opshub/issues/536)) で **日常 operator 面（読み取り = `opshub slack status`）と復旧面（書き換え = `opshub slack cursor`）に二層化**した。

**日常面 — `opshub slack status`**（旧 `cursor show` 昇格）:

- 3 軸 cursor を人間語で表示する: channel ごとに「前進取得済み（high-water）/ 過去取得下限（low-water、無記録なら『先頭まで』）/ 追跡中スレッド数」。configured だが未取得の channel は「未取得」と明示する。
- **cursor は「再開点」であって「被覆台帳」ではない**: `backfill` 軸は channel ごとに low-water を 1 個しか持たず飛び地（穴あき被覆）を表現できない。さらに thread late-reply に delta API が無く「静か」と「未取得」を原理的に区別できない。よって status は high-water と low-water を**別々の事実**として出し、連続被覆区間を主張しない（gap を正確に数える Option B＝cursor の区間台帳化は pre-userbase には過剰と判断し不採用）。
- cursor で確実に分かる唯一の gap signal —「次回 sync で過去取り直し予定」（実効 floor < 記録 low-water、§(d) の自動 gap backfill trigger を status 側で再現）— だけを正確に表示する。
- `--verbose` で生 3 軸 + raw ts を dump する（旧 `cursor show` の出力）。

**復旧面 — `opshub slack cursor`**（書き換え系のみ）:

- **`opshub slack cursor backfill --channel <id> --since <new> [--until <old>]`** — operator が指定した bounded 窓 `(since, until]` を §(c) の bounded fetch で取得・ingest し、`backfill[ch] = since` に後退させる。`--until` 既定 = 追跡中の low-water (`backfill[ch]`)。**pre-feature channel の発端シナリオ救済の主経路** (operator が old floor を `--until` に与え、既取得区間と disjoint な窓を明示する)。Phase 23-F-2 ([#536](https://github.com/ozzy-labs/opshub/issues/536)): low-water 未記録の pre-feature channel でも `--until` を省略でき、**CLI 層が `sources` projection からその channel の最古取得 ts (`external_id = "{channel_id}:{ts}"` の最小 ts) を逆引きして `--until` 既定値にする** (取得実績ゼロのときだけ明示要求)。逆引きは CLI/query 層に閉じ、connector は引き続き projection に依存しない (結合方向を太らせない)。
- **`opshub slack cursor reset [--channel C... | --all]`** — 対象 channel の cursor entry を除去して cold-start 化する破壊的経路 (最終手段)。reset 後の cold-start 再取得は既取得区間を再観測して inbox を膨張させ得る (§(h)) ため `AskUserQuestion` で HITL 確認し、lossy である旨を警告する。基本は `cursor backfill` を推奨。

`cursor` group は help から隠さない（pre-userbase では operator = maintainer 自身で、障害時に help が命綱。flat-dict reject エラー (§Context / [#531](https://github.com/ozzy-labs/opshub/issues/531)) が `cursor reset --all` を案内する以上、行き先を隠すのは不整合）。二層化は help 文・命名・docs で表現する。

いずれも cursor 更新は `ConnectorSyncCompleted` event の append で行い、`connector_cursors` projection に反映する (event-sourced の規律を維持)。

### (g) migration (pre-userbase、silent additive)

`backfill` 軸の欠損は **空 dict として扱い ConfigError にしない** (additive)。理由: §Context 事実 2 のとおり rebuild は cursor を reset しないため、Phase 20-B 流の「ConfigError → `opshub projections rebuild` 案内」は本 axis では **dead-end になる**。pre-Phase-20-B flat dict の hard reject は維持する (そちらは別経路)。

### (h) inbox 膨張への姿勢 (#522 を hardening dependency とする)

§(b)-(d) の disjoint 窓設計により、**feature 着地後に sync された channel では gap が既取得区間と重ならない** ため inbox は膨張しない (gap message は初観測 = `inbox` に正しく 1 行ずつ)。一方 pre-feature channel の `cursor backfill` / `cursor reset` 経路には残留 overlap リスクがある。これは [#522](https://github.com/ozzy-labs/opshub/issues/522) (inbox `ItemEnqueued` の `source_ref` 冪等化、旧 #339 class) の着地で無害化される。#522 は本 epic の **hardening dependency** だが hard blocker ではない (本 ADR の go-forward 経路は #522 なしで正しい)。横断的 inbox 意味論判断を本 epic に結合しないためスコープ分離する。

## Consequences

### Positive

- **floor を下げると次回 sync が自動で追いつく** (go-forward channel)。ADR-0036 の最大の運用上の落とし穴が解消する。
- forward / backfill が独立境界なので **partial-sync resume (issue #339 checkpoint) と整合**し、high-water cursor は決して後退しない。
- 相対 floor は誤発火しない (ts 前進のため)。誤った大量再取得を構造的に防ぐ。
- `opshub projections rebuild` の「壊れた回避策」を、実際に機能する `opshub slack cursor backfill` / `reset` に置き換える。docs も是正する。

### Negative / Trade-offs

- **pre-feature channel は auto backfill されない** (§(e))。過去 floor を復元できないため、救済は operator の明示コマンド (`cursor backfill --until`) に依存し、残留 overlap リスクは #522 着地まで残る。
- cursor schema が 3 軸に増え、connector / CLI / test の表面が広がる。
- gap backfill 発火時は一度きりとはいえ過去区間の API 取得コストが乗る (notice で透明化、`backfill_on_floor_lower` / `--no-backfill` で opt-out 可)。

## Alternatives Considered

- **明示バックフィルコマンドのみ (auto なし)** — `opshub slack cursor backfill` だけ提供し auto 検出しない案。実装は小さいが「floor を下げたら勝手に追いつく」という本来の UX 要望を満たさず、operator が毎回手で窓を計算する必要がある。auto (§(d)) を主、明示コマンド (§(f)) を pre-feature 救済の従とする現案を採用。
- **floor 引き下げ時に channels cursor を巻き戻す** — cursor を floor まで rewind して通常 sync に再取得させる案。実装は最小だが既取得区間 `(low_water, now]` を再 fetch するため inbox が膨張する (§Context 事実 3)。low-water 軸で gap のみ取る現案で回避。
- **`opshub projections rebuild` を「cursor も truncate する」挙動に変更** — rebuild に Slack cursor リセット機能を持たせる案。event-sourced の「projection は event log から再構築」という [ADR-0002](0002-event-sourced-architecture.md) の不変条件を崩し、全 connector に影響する。connector 固有の `opshub slack cursor` サブコマンド (§(f)) に閉じる現案を採用。
