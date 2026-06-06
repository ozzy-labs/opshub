# 0036. Slack Sync Date Floor (`sync_since` + per-channel `since`)

- Status: Accepted
- Date: 2026-06-04
- Deciders: opshub maintainers
- Related: [ADR-0010](0010-connector-contract.md) (connector / cursor contract)、[ADR-0030](0030-slack-thread-reply-ingestion.md) (thread reply ingestion)、[ADR-0032](0032-runtime-toml-config-loading.md) (runtime TOML loading)、[ADR-0035](0035-slack-sort-axis-consolidation.md) (`opshub slack conversations --since` — discovery 軸との棲み分け)

## Context

`opshub slack sync` は CLI 引数を持たず、`connector_cursors` projection に保存した
per-channel cursor のみで incremental sync する
(`src/opshub/connectors/slack/connector.py`)。初回 sync は
`SlackFetcher._iter_channel` (`src/opshub/connectors/slack/fetcher.py`) が
`oldest=None` のまま `conversations.history` の全ページを
`has_more` が尽きるまで遡り、**チャンネルの全履歴をバックフィル**する
(しかも全ページを memory に buffer してから ts 昇順 sort する)。

結果、ボリュームの大きい channel / DM では初回 sync が時間・メモリの両面で重い。
一方で過去メッセージの重要度は低いことが多く、「指定日以降だけ sync したい」
という運用要望がある。`--since` は discovery 用の `opshub slack conversations`
([ADR-0035](0035-slack-sort-axis-consolidation.md)) にしか無く、sync 側には
日付下限 (floor) を入れる余地がクリーンに空いている。

## Decision

`[connectors.slack]` に **date floor** を持たせ、`conversations.history` の
`oldest` 引数を「これより古いメッセージは取らない」下限として bound する。

### (a) config に集約する

floor は config (`[connectors.slack]`) に持たせ、CLI flag は導入しない。`sync` は
cron 等での定期実行が主用途であり、「これより古いのは恒久的に不要」という意図は
per-invocation flag より config の方が自然に表現でき、後から追加した channel にも
自動で効く。

### (b) `channels` を id+`since` のスペックに拡張する (additive)

`channels` は各エントリ `{id, since?}` (`SlackChannelSpec`) を取る。**従来の
文字列配列 `channels = ["C0123ABC"]` も引き続き受理**し、before-validator が
`{"id": "C0123ABC"}` に正規化する。これにより
`opshub slack conversations --format=toml` が出力する `channels = [...]`
スニペット (ADR-0035 §(a)) と既存 config / env (`OPSHUB_CONNECTORS__SLACK__CHANNELS`)
がそのまま有効に保たれる (非破壊)。

### (c) グローバル既定 `sync_since`

`sync_since: str | None` を connector-wide の既定 floor とする。default `None` は
**従来どおり全件バックフィル** (floor は opt-in)。

### (d) per-channel `since` で上書き / 例外

`SlackChannelSpec.since`: 未指定 (`None`) は `sync_since` を継承、
`"all"` sentinel は floor を無効化してそのチャンネルだけ全件取得、
それ以外の値はチャンネル固有 floor として `sync_since` を上書きする。

### (e) floor 解決順

効果的 floor は **① channel `since` → ② `sync_since` → ③ なし (全件)**。
日付パースは [`opshub.core.time.parse_since`](../../src/opshub/core/time.py) に集約し
(core 層なので config / connector / CLI から逆依存なしに再利用、#459)、`"all"` は
`parse_since` に渡す前に short-circuit する (日付パーサは `"all"` を弾くため)。

### (f) 日付フォーマットと評価タイミング

相対 (`"90d"` / `"4w"`) と ISO 絶対 (`"2026-01-01"`、trailing `Z` 可) の両方を受理。
config には **raw 文字列のまま**格納し (validator は検証のみで parse 結果に置換しない)、
実際の floor は **sync 実行時に** `parse_since` で再評価する。したがって相対指定は
sync ごとに前進する (新規 channel が初めて sync される時刻に依存)。絶対的な下限が
欲しい場合は ISO 日付を使う。

### (g) cursor が authoritative

fetch の resume bound は `oldest = _max_ts(cursor, floor_ts)` で求める。floor は
`oldest` を cursor より過去へ**引き戻さない** (cursor が当該 channel の最大 fetched
ts であり authoritative)。帰結:

- 既存の全件 sync 済み channel は cursor が floor より新しく floor が inert →
  `sync_since` を後から有効化しても**再取得も削除も起きない** (安全な opt-in)。
- partial-sync resume (issue #339 checkpoint) と整合する。
- quiet channel で相対 floor が cursor を追い越しても、cursor..floor 区間に
  未取得メッセージは無いため取りこぼしゼロ。

floor は `oldest` (= 何を fetch するか) のみを動かし、persist する cursor
(= 観測した message の ts) は従来どおり `_max_ts(cursor, yielded)` で進める。

### (h) thread replies

`conversations.replies` で取る子返信は親 ts で取得範囲が決まる
([ADR-0030](0030-slack-thread-reply-ingestion.md))。floor で親が `oldest` 以前に
落ちれば子返信も来ないため、floor と整合する。

### (i) validation

不正な `sync_since` / `since` 値は config 読込時に fail-fast する。`parse_since` は
core 層で `ValidationError` を raise し、CLI `--since` callback は
`typer.BadParameter` (exit 2)、`SlackConnectorSettings` は `[connectors.slack]
sync_since` / `channels[].since` ラベル付きの `ConfigError` に wrap する。
重複 channel id も `ConfigError` で弾く。

## Consequences

- **利点**: 初回 / 新規追加 channel のバックフィルを時間・メモリ両面で抑制できる。
  既存環境には無影響な opt-in (cursor が authoritative)。`channels` が additive なため
  既存 config / `conversations --format=toml` 出力 / 既存テストは無改修。
- **代償 / 限界**:
  - **floor を後から下げても過去は遡って backfill されない** (cursor が authoritative)。
    古い履歴を取り直すには cursor reset / `opshub projections rebuild` が必要。floor は
    「これ以降のみ取る」下限であり遡及取得トリガではない、という非対称性を持つ。
  - 相対指定 (`"90d"`) は sync 実行時点基準で評価されるため、絶対下限が必要なら
    ISO 日付を使う。
  - table 形式を env で渡す場合 `OPSHUB_CONNECTORS__SLACK__CHANNELS` は JSON 文字列
    (`[{"id":"C1","since":"30d"}]`) になる (pydantic-settings の complex-value 規約)。
    文字列配列 `["C1"]` も引き続き有効。
- **Scope 外 (将来候補)**: 遡及トリミング (floor を上げて既存を削除)、遡及バックフィル
  (floor を下げて取り直し)、CLI `--since` での一時上書き、channel 種別ごとの一括 floor。
