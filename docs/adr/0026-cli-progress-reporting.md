# 0026. CLI Progress Reporting for Long-Running Commands

- Status: Accepted
- Date: 2026-06-01
- Deciders: opshub maintainers

## Context

いくつかの CLI コマンドは完了まで数十秒〜数分かかるが、実行中は何も出力せず、完了時に一行サマリを出すだけだった。進捗が見えないため「動いているのか / フリーズしたのか」「あとどれくらいか」が operator に分からず、誤った Ctrl-C や二重起動を誘発する UX 問題があった (#316)。対象は `opshub connector sync <name>`（全コネクタ）/ `opshub embeddings rebuild` / `opshub embeddings drain` / `opshub projections rebuild`。

Phase 14.x ([#366](https://github.com/ozzy-labs/opshub/issues/366) で `slack channels` → `slack conversations` 刷新) で `opshub connector slack conversations` も同基盤に乗り、Phase 14.x ([#374](https://github.com/ozzy-labs/opshub/issues/374) で `--since` activity filter 追加) では同じ indeterminate spinner を使いつつ description を `--since` の有無で動的に切り替える運用 (no-`--since` 時 = `"listing conversations"`、`--since` 時 = `"listing conversations + activity"`) で 2 段の作業 (listing pages + per-row `conversations.history?limit=1`) を単一の spinner に集約している。両者とも `_progress.indeterminate(description)` を 1 context で開き、description 文字列で operator に作業相を示す pattern (determinate を 2 段重ねない) を採用。

Phase 19 ([ADR-0034](0034-slack-engagement-axis.md)) で `opshub slack conversations --since` の default が engagement 軸 (`--activity=mine`、`search.messages?query=from:@me` 経由で自分の最終発言 ts を集計) に切り替わり、spinner description は 2 形態に拡張される: `--activity=any` 経路 (旧挙動) は引き続き `"listing conversations + activity"`、`--activity=mine` 経路 (新 default) は `"listing conversations + engagement"`。いずれも `_progress.indeterminate(description)` 単一 context で listing pages + engagement / activity 補強の 2 段作業を集約する pattern を踏襲し、determinate を 2 段重ねない原則は不変 ([ADR-0034](0034-slack-engagement-axis.md) §(b) §(i))。`--activity=mine` 経路の indexing-lag notice (`notice: search.messages may lag by minutes; ...`) は spinner description とは独立した stderr 一行通知 (ADR-0034 §(i)) で、本 ADR の進捗表示 contract には含めない。

設計上の制約:

- **ADR-0001 cold-start**: `opshub --help` は ~300ms 以内。`cli/*.py` は重い import をモジュール先頭に置けない（`tests/integration/test_cli_imports.py` が静的に強制、`test_cold_start` が wall-clock を強制）。
- **出力チャネル分離**: stdout は各コマンドの結果サマリ専用（パイプ / スクリプト互換）。ログは `structlog` で stderr。
- コネクタはページネーションで逐次 `observe` するため、総件数は sync 完了まで分からない。

## Decision

1. **ライブラリ = `rich`**。`typer` の依存として既に lockfile に存在するため、新規依存の追加なしで採用する。
2. **2 形態**を提供する（`src/opshub/cli/_progress.py`）。総件数が事前に分かるかで選ぶ:
   - `indeterminate(description)` — スピナー + 処理済み件数 + 経過時間。総数不明のストリーミング作業向け（connector sync）。
   - `determinate(total, description)` — バー + パーセンテージ + ETA。件数既知の作業向け（embeddings rebuild/drain の pending 数、projections rebuild の event 数）。
3. **出力チャネル = stderr**（`Console(stderr=True)`）。stdout の結果サマリは不変に保ち、パイプ / スクリプト互換と既存テストの stdout assertion を維持する。
4. **TTY ゲート**: stderr が TTY でないとき（CI / パイプ / リダイレクト）は **no-op**。ANSI 制御文字を一切出さない。
5. **明示制御**: ルートの `--progress` / `--no-progress` フラグと `OPSHUB_PROGRESS` 環境変数が自動判定を上書きする。優先順位は flag > env > stderr-TTY。`OPSHUB_PROGRESS` の受理値: truthy = `1` / `true` / `yes` / `on`、falsy = `0` / `false` / `no` / `off`（大文字小文字無視、前後空白は trim、`src/opshub/cli/_progress.py` `_TRUTHY` / `_FALSY` が SSOT）。未知の値は無視し TTY 自動判定にフォールバックする。
6. **cold-start 順守**: `rich` は context manager 内で **遅延 import** する。`_progress.py` は `_` 始まりの private module（`test_cli_imports` の対象外）だが、wall-clock の `test_cold_start` を守るため遅延 import は必須。
7. **service 層に rich を持ち込まない（IoC seam）**:
   - connector sync は source service を透過プロキシ（`_ProgressSourceProxy`）でラップし、`observe` 成功ごとに spinner を advance する。`Connector` Protocol と各コネクタ実装は無改修。
   - embeddings / projections は service / driver に optional な `progress_callback`（default `None`）を渡す。`EmbeddingService.count_pending()` / `rebuild_all` の event count が determinate バーの total を供給する。
8. **ストリーミング型は事前カウントしない**: connector sync で総数を得るための事前カウントパスは走らせない（API 呼び出し倍増・レート制限リスク・delta 型コネクタでは事前カウント自体が困難）。これらは indeterminate で割り切る。
9. **structlog ログとの併存は do-nothing (黙認)**: 進捗描画中に `structlog` の stderr ログが割り込むと表示が乱れ得るが、本 PR1 (#316 epic、PR #323 / #325 / #326) では (a) `rich` Console 経由でログを出す / (b) ログを WARNING 以上に引き上げる / (c) `Progress.redirect_stderr` で標準出力を奪う のいずれも採用しない。理由は次の 2 点に絞る:
   - 対象 10 connector は通常運用で WARNING 中心の疎なログ (各 connector mapper / fetcher を確認済み) で、INFO が連投される経路がほぼなく、実害が限定的。
   - MVP の追加複雑度を最小化するため。(a) は rich/structlog の二重 sink、(b) は loglevel の global 上書き、(c) は global stream replacement と、いずれも cold-start (ADR-0001) や test 観測点に副作用を持つ。

   将来 INFO ログとの可視的衝突が顕在化した時点で (a) を採用候補とする (rich Console を structlog の sink に差し替える形が他案より影響範囲が狭いため)。

## Consequences

### Positive

- 進捗が可視化され、フリーズと実行中を区別できる。長時間コマンドの UX が改善する。
- stdout が不変のため、スクリプト / パイプ互換と既存テストが影響を受けない。
- 新規依存ゼロで cold-start 予算を維持（rich は遅延 import）。
- service 層は rich 非依存のまま（callback seam）。10 コネクタは無改修。

### Negative / Trade-offs

- ストリーミング型コネクタは真の % / ETA を出せず、処理済み件数 + 経過時間のみ。
- TTY 描画中に `structlog` のログ行が割り込むと表示が乱れ得る。コネクタは主に WARNING で疎にログするため実害は限定的で、将来ログを progress console に集約すれば解消できる。
- determinate のための事前カウント（`count_pending`）は progress 無効時にも実行される（`COUNT(*)` のみ、embed 本体に対し無視できるコスト）。

## Alternatives Considered

- **`click.progressbar`**（`typer` 同梱・依存ゼロ）: determinate/indeterminate 両対応・非 TTY 自動無効化と手堅いが、スピナー / 複数行レイアウト / ETA 表現が `rich` に劣る。`rich` も既に依存にあり追加コストがないため却下。
- **自前の `\r` 実装**: 依存ゼロだが TTY 判定・ETA・バー描画を自前保守する必要があり保守コストが高い。却下。
- **全コマンドで事前カウントして真の %**: コネクタは API 呼び出しが倍増しレート制限リスクがあり、delta 型は事前カウント自体が困難。ストリーミング型は indeterminate に割り切った（決定 8）。
- **進捗ハンドルを `Connector` Protocol に通す**: 全コネクタの改修が必要になる。透過プロキシ（決定 7）で回避した。

## 関連

- [ADR-0001 Python Stack](0001-python-stack.md) — cold-start 予算の出所。
- [ADR-0010 Connector Contract](0010-connector-contract.md) — 無改修で進捗を載せた `Connector` 契約。
- Issue #316（対応方針）、PR #323（共通基盤 + connector sync）、#325（embeddings / projections）。
- Issue [#366](https://github.com/ozzy-labs/opshub/issues/366) (slack conversations を基盤に追加)、Issue [#374](https://github.com/ozzy-labs/opshub/issues/374) / PR [#375](https://github.com/ozzy-labs/opshub/pull/375) (`--since` 経路の 2 段 description 運用)。
- [ADR-0034 Slack Engagement Axis](0034-slack-engagement-axis.md) — `--activity=mine` (新 default) 経路の spinner description (`"listing conversations + engagement"`) を本 ADR の 2 段 description 運用に追加する根拠。indexing-lag notice は本 ADR scope 外 (ADR-0034 §(i))。
