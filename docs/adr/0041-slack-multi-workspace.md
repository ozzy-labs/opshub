# 0041. Slack Multi-Workspace (alias × team_id 二層 identity)

- Status: Accepted (Phase 24, epic [#552](https://github.com/ozzy-labs/opshub/issues/552))
- Date: 2026-06-12
- Deciders: opshub maintainers
- Supersedes: [ADR-0039](0039-slack-single-workspace-non-goal.md) (single-workspace non-goal — §決定 3 の出口条件を本 ADR で引いた)
- Related: [ADR-0010](0010-connector-contract.md) (connector / cursor contract — `external_id` 規約と cursor schema を本 ADR が改める), [ADR-0030](0030-slack-thread-reply-ingestion.md) / [ADR-0038](0038-slack-sync-gap-backfill.md) (compound cursor schema — 本 ADR §(d) が per-alias nest 化), [ADR-0040](0040-slack-feature-scope-ssot-and-readiness.md) (readiness — per-workspace token 評価に拡張), [ADR-0018](0018-slack-token-principal.md) (token principal — workspace ごとに独立、principal 選択軸は不変), [ADR-0014](0014-saas-token-storage.md) (keyring slot 規約 — per-alias slot に拡張)

## Context

Phase 23-H ([ADR-0039](0039-slack-single-workspace-non-goal.md)) は single-workspace
を明示的 non-goal に pin し、出口条件として「必要時は新 top-level Phase + 新 ADR +
`team_id` re-key（pre-userbase → hard flip、compat shim なし）」を §決定 3 に明記した。
本 ADR はその出口を引く: **1 install = N Slack workspaces** を first-class でサポートする。

単一 workspace 前提は 4 箇所に焼き付いている (ADR-0039 §Context):

1. **token slot**: keyring `connector:slack:token` 固定 (`connectors/slack/auth.py:58`)
2. **connector instance**: `name = "slack"` 単一登録 (`connector.py:253`)
3. **external_id**: `f"{channel_id}:{ts}"` — workspace-wide 一意性を前提
   (`mapper.py:229`。channel id は workspace 跨ぎで衝突しうる)
4. **compound cursor**: scalar `team_id` 軸 1 本で install 全体を bind
   (`connector.py:237`、Phase 23-H guard)

加えて epic #552 の adversarial 設計レビューで「暗黙の単一 workspace 前提」が 2 箇所判明した:

1. **demand digest の self-id**: operator 自身の `U...` id を install 全体で 1 つ
   memoise (`projections/slack_demand_digest.py:297-323, 451-496`)。`U...` id は
   workspace ごとに別物なので、N workspace 化すると mention 検出・自己発言抑制が
   他 workspace で全 miss する
2. **excludes**: `channels` / `senders` が bare id の flat set
   (`core/excludes.py:109-124`)。workspace 跨ぎの channel id 衝突で誤除外しうる

pre-userbase につき後方互換は不要 ([AGENTS.md](../../AGENTS.md) §設計判断のスタンス)。
旧形式の config / cursor / データは compat shim を作らず hard flip する。

## Decision

以下 (a)–(j) を pin する。

### (a) workspace の二層 identity: alias（操作層）× team_id（データ層）

- **config / CLI / keyring は operator 命名の alias で引く**:
  `[connectors.slack.workspaces.<alias>]`、`--workspace <alias>`、keyring slot
  `connector:slack:<alias>:token`。
- **alias の正規表現は `^[a-z0-9][a-z0-9_]*$`（`-` は不許可）**。根拠: keyring の
  env override 名を導出する `_env_var_name` (`core/secrets.py:40-42`) が `-` と
  `:` を共に `_` へ潰すため、`my-ws` と `my_ws` が同一 env 名
  `OPSHUB_CONNECTOR_SLACK_MY_WS_TOKEN` に衝突する。`-` を文法から排除して
  alias → env 名の単射を保つ。
- **データ層の identity は Slack の `team_id`**（安定・rename 不能）:
  `external_id = f"{team_id}:{channel_id}:{ts}"` に re-key する。alias rename は
  当該 workspace の cursor を失う（cursor は alias key で nest するため、§(d)）が、
  external_id が team_id ベースなので **re-fetch は冪等 upsert に落ち、source は
  重複しない**（`(connector_name, external_id)` unique + inbox `source_ref`
  partial unique [#522](https://github.com/ozzy-labs/opshub/issues/522) で
  inbox 側も成立）。これが「操作層 = mutable alias / データ層 = immutable
  team_id」の二層分離が成立する load-bearing な根拠である。
- **per-alias bind guard**: ADR-0039 の team_id bind guard semantics を per-alias
  に一般化する。各 workspace cursor entry が自分の `team_id` を bind し、alias
  配下の token を別 workspace に差し替えたら fetch 前 `ConfigError`
  （ADR-0039 semantics 不変、誘導先は per-alias の cursor reset）。
- **重複 team_id 検知**: 2 つの alias が同一 `team_id` に解決した場合も
  `ConfigError`。同一 workspace の二重登録は cursor / digest の意味を壊すため
  loud に拒否する。
- **guard の適用範囲拡大**: guard は通常 sync 経路に加えて `backfill_channel`
  経路（`connector.py:610-677`、現在 guard を通らない）にも挿入する。

### (b) connector は単一 instance のまま、sync 内で workspace loop + error isolation

**`connector_name = "slack"` 固定 + external_id の team_id prefix で名前空間分離**
を採用する。

sync は workspace を直列 loop し、**per-workspace error isolation** を入れる:

- CLI driver は `connector.sync()` の正常 return 時のみ cursor を永続化する契約
  (`_connector_common.py:213-245`) のため、素朴な「最後に集約例外を raise」では
  成功 workspace の cursor 前進が失われる。そこで **既存の partial-progress
  checkpoint 機構**（`cursor_set(sync_started=True)` + `ConnectorSyncStarted`
  upsert、replay でも同値復元: `connector_cursors.py:103-110`）を流用し、
  **各 workspace 完了ごとに checkpoint を書いてから**次へ進む。
- 全 workspace 走破後に失敗があれば、stderr に**失敗 alias を列挙**したうえで
  集約例外（non-zero exit）。`record_sync_failure` が exception 型名しか残さない点
  (`source_service.py:376-383`) も failed alias を message に含めて補強する。
- 現在の at-entry checkpoint snapshot は単一 workspace 3 軸前提
  (`connector.py:339, 362, 408`) のため per-alias 形に再構成する。

### (c) config 形状: `workspaces.<alias>` table、旧 flat 形式は hard flip

```toml
[connectors.slack]
enabled = true
sync_since = "90d"               # connector-wide default（per-workspace で上書き可）
thread_activity_window = "30d"   # 同上（per-workspace で上書き可 — floor と対称）

[connectors.slack.workspaces.acme]
channels = ["C0123", "C0456"]    # 従来の string / table 両形式をそのまま受理
sync_since = "30d"               # workspace-level override
thread_activity_window = "7d"    # workspace-level override

[connectors.slack.workspaces.oss]
channels = ["C0789"]
```

- 旧 flat 形式（`[connectors.slack] channels = ...`）は **`ConfigError` で reject**
  し、新形式への書き換え例をエラー本文に同梱する。単一 workspace 構成でも
  `workspaces.<alias>` table 必須 — 暗黙 default workspace の特例コードパスを
  作らない（pre-userbase、epic
  [#530](https://github.com/ozzy-labs/opshub/issues/530) の complexity-reduction
  継承）。
- flat config の reader は config 層の外に散在する（`connector.py:269-290, 363,
  489` / `_slack_status.py:167-170` / `cli/slack.py:203-211` の sync notice）。
  **flip PR が全 reader を同時に書き換える**（中間状態で main を壊さないため、
  flip は原子的な 1 PR に集約する。epic #552 §実装計画）。
- floor 解決は 3 段に拡張する: per-channel `since` → per-workspace `sync_since`
  → connector-wide `sync_since`（[ADR-0036](0036-slack-sync-date-floor.md) の
  2 段解決の自然拡張）。`thread_activity_window` も per-workspace override 可
  （2 段、floor と対称）。
- env override は pydantic-settings の nested delimiter
  （`OPSHUB_CONNECTORS__SLACK__WORKSPACES__<ALIAS>__...`）に乗せ、docs に構文例を
  明記する。env 名の大文字化と alias 小文字規約の整合、`NoDecode` comma 形式が
  nest 下でも効くかは Phase 24-C で検証する。

### (d) cursor schema: per-alias nest

```json
{
  "workspaces": {
    "acme": {
      "channels": {"C0123": "1717000000.000100"},
      "backfill": {"C0123": "1714000000.000000"},
      "threads": {"C0123:1717000000.000100": "1717100000.000500"},
      "team_id": "T0123"
    }
  }
}
```

- 既存 4 軸 shape（`SlackCursorState`: `channels` / `backfill` / `threads` /
  `team_id`、[ADR-0030](0030-slack-thread-reply-ingestion.md) §(d) +
  [ADR-0038](0038-slack-sync-gap-backfill.md) §(a) + ADR-0039 §決定 1）を
  per-alias でそのまま nest する。軸の意味・lifecycle は不変。
  `connector_cursors` row は 1 本のまま。`ConnectorSyncStarted` /
  `ConnectorSyncCompleted` replay で nest がそのまま同値復元されることは
  確認済み。
- 旧 23-H shape（top-level `channels` / `team_id`）は **silent migration せず
  `ConfigError`** で reject し `opshub slack cursor reset --all` へ誘導する
  （Phase 20-B / Phase 23-A
  [#531](https://github.com/ozzy-labs/opshub/issues/531) の posture 継承 —
  reject 誘導先は rebuild ではなく working な cursor reset）。

### (e) 既存データの upgrade path: DB 再 init（hard flip、re-key migration なし）

external_id re-key により、既存の un-prefixed 行（`C123:1612...`）と新形式
（`T999:C123:1612...`）は同一 message でも別 source になる。cursor reject →
reset → 全量 re-fetch は旧形式行と**重複**する。pre-userbase の posture 通り
re-key migration は作らず、`docs/upgrading.md` §Phase 24 に **「DB 再 init +
全 connector re-sync」を正規 upgrade path** として明記する（event store ごと
作り直しのため Slack 以外の connector データも再取得になる点を明示する）。

### (f) CLI surface: `--workspace <alias>` と default 解決規則

| コマンド | `--workspace` | default（flag なし） |
|---|---|---|
| `slack auth set` / `auth test` | 受理 | 1 workspace 構成ならそれ、複数なら `ConfigError`（alias 一覧を提示） |
| `slack sync` | 受理（絞り込み） | **全 workspace を直列 sync**（error isolation §(b)） |
| `slack conversations` | 受理 | 1 つならそれ、複数なら `ConfigError` |
| `slack status` | 受理（絞り込み） | 全 workspace を per-workspace block で表示 |
| `slack cursor reset` | 受理 | `--all` = 全 workspace の全 channel reset + unbind。`--channel` は auth と同じ default 解決規則（1 workspace ならそれ、複数なら `--workspace` 必須 — 同一 channel id が複数 workspace に存在しうるため曖昧解決はしない） |
| `slack cursor backfill` | 受理 | 1 つならそれ、複数なら `ConfigError` |

- `conversations --format=toml` の paste-ready 出力
  (`_slack_conversations.py:600-612`) は `[connectors.slack.workspaces.<alias>]`
  block を emit する形に追従する（emit 先 alias は上記 default 解決規則を流用）。
  sync notice (`cli/slack.py:205-211`) と `init.py:63` の案内文も同時更新する。
- MCP `connector.sync` (`mcp/_writes.py:123-191`) は同じ `sync()` を駆動するため
  **常に全 workspace**（filter なし）。その旨を tool description に明記する。

### (g) projection / MCP: demand digest の workspace 軸 + per-workspace self-id

- `slack_demand_digest` の row key を `(team_id, channel)` に拡張する
  （channel id は workspace 跨ぎで衝突しうるため）。
- **per-workspace self-id 解決**: 現在 install 全体で 1 つの self_user_id
  （`slack_demand_digest.py:297-323`、env `OPSHUB_SLACK_SELF_USER_ID`、引数なし
  `SlackAuth()` fallback）を、event の external_id から `team_id` を取り、
  `{team_id: self_user_id}` map（per-alias `SlackAuth(alias).test_token()` で
  解決・memoise）に置換する。env override も per-alias 構文に拡張する。
  mention literal（`<@U...>`）も per-workspace に解決する。
- MCP `slack.demand.list` の出力に workspace field（team_id + alias）を追加する。
  filter param は実需が出るまで保留。`opshub slack mentions list`
  (`cli/slack.py:488`) も digest key 変更の影響先として同時追従する。
  `_slack_status._channel_names`（flat dict、`_slack_status.py:66-94`）は
  per-workspace block 化に伴い team_id filter を入れる。
- **external_id parse 箇所の追従は re-key と同一 PR 必須**:
  `slack_demand_digest._parse_slack_external_id`（`:571-579`、`float(tail)` 失敗で
  全 Slack event を silent drop → re-key と分離すると digest が完全停止する）と
  `_slack_cursor.py:206, 214`（`LIKE "{channel}:%"` / `partition(":")` — backfill
  `--until` 推定が全 miss して誤 `ConfigError`）。なお thread cursor key
  （`{channel_id}:{thread_ts}`）は per-alias nest 内に閉じるため追従不要。

### (h) readiness / scopes: SSOT 不変、評価は per-workspace token

`FEATURE_SCOPES` SSOT（[ADR-0040](0040-slack-feature-scope-ssot-and-readiness.md)）
は workspace 非依存のまま不変とする。`auth test` の readiness ブロックは
per-workspace token で評価する（`--workspace` で切替、default 解決規則は §(f)）。

### (i) mapper への team_id 貫通 + bind guard fail-soft arm の hard 化

- mapper が external_id を組むには map 時点で `team_id` が要る。bind guard が
  fetch 前に `auth.test` で解決済みのため、`RawSlackMessage`（`fetcher.py:114`）
  に `team_id` field を追加して貫通させる（追加 API コール 0）。
- 現在の bind guard は `auth.test` が team_id を返さないとき **warn して続行**
  する（`connector.py:1139-1145`）。Phase 24 では team_id が external_id の
  構成要素になるため、この fail-soft arm は **hard `ConfigError` に変更**する
  （team_id なしで map すると external_id 規約が壊れるため、loud に止める）。

### (j) excludes の workspace 修飾

excludes の `channels` / `senders`（`core/excludes.py:109-124`）に
**`<alias>/` 修飾形式**を追加する: `acme/C123` = acme workspace の C123 のみ除外、
bare `C123` = 全 workspace で除外（後者は「全 workspace の同名 id を消したい」
意図として有効に残す）。channel id 衝突の誤除外を operator が制御できるようにする。

## Consequences

### Positive

- 1 install で複数 Slack workspace（side-projects / OSS / employer）を取り込める。
  alias rename しても external_id が team_id ベースのため source が重複しない
  （re-fetch は冪等 upsert）。
- per-alias bind guard + 重複 team_id 検知により、ADR-0039 が塞いだ
  silent-corruption 経路（token 差し替えによる workspace 混線）は N workspace
  化後も閉じたまま。guard は `backfill_channel` 経路にも及ぶ。
- per-workspace error isolation により、1 workspace の障害（token 失効等）が
  他 workspace の cursor 前進を巻き込まない。失敗 alias は stderr と failure
  event で観測できる。
- demand digest の per-workspace self-id 解決により、mention 検出・自己発言抑制が
  全 workspace で正しく機能する（単一 self-id のままなら他 workspace で全 miss）。

### Negative / Trade-offs

- **既存 install は DB 再 init + 全 connector re-sync が必須**（§(e)）。re-key
  migration を提供しないため、Slack 以外の connector データも再取得になる。
- alias rename は当該 workspace の cursor 喪失 = 全量 re-fetch コスト（source は
  重複しないが API 取得コストは乗る）。
- 単一 workspace 構成でも `workspaces.<alias>` table が必須になり、最小構成の
  config が 1 行分冗長になる（暗黙 default の特例コードパスを作らないことの対価）。
- sync は workspace 直列 loop のため、N workspace の合計時間は線形に伸びる
  （並列化は実需が出るまで defer）。
- external_id が 3-token になり、parse 箇所（digest / cursor backfill 推定）の
  追従を re-key と同一 PR にしないと silent drop / 誤 `ConfigError` が起きる
  （§(g) 後段で同一 PR を必須化して回避）。

## Alternatives Considered

- **multi-instance 化（`slack:<alias>` を connector_name にする）** — rejected:
  registry idempotency / `ConnectorSyncCompleted` / `connector_cursors` / inbox
  `source_ref` prefix（`source_service.py:283`）の connector_name 前提に全面波及
  し、かつ alias rename で sources が orphan する（connector_name に mutable な
  alias が混入するため）。`connector_name = "slack"` 固定 + external_id の
  team_id prefix の方が波及が小さく rename 耐性もある。
- **alias を external_id prefix に使う（team_id re-key しない）** — rejected:
  alias は operator が rename しうる mutable key で、rename のたびに全 source が
  orphan する。データ層は Slack が保証する安定 id（team_id）でなければならない。
- **暗黙 default workspace（flat config を単一 workspace として受理）** —
  rejected: 特例コードパスが恒久に残り、epic #530 の complexity-reduction に
  反する。pre-userbase の今 hard flip する方が安い。
- **旧 cursor / 旧 external_id の silent migration** — rejected: Phase 20-B /
  23-A で確立した loud-reject posture（silent migration は silent corruption の
  温床）を継承する。upgrade path は §(e) の DB 再 init。
- **alias regex で `-` を許可する** — rejected: `_env_var_name` が `-` / `:` を
  共に `_` へ潰すため env override 名が衝突する（§(a)）。
- **失敗 workspace で即 abort（error isolation なし）** — rejected: 先頭
  workspace の token 失効が全 workspace の sync を止め、後続 workspace の cursor
  が永遠に前進しない。checkpoint 流用の isolation はコード追加が小さい。

## Validation

実装は Phase 24-B〜E（epic #552 §実装計画 / §テスト計画）で pin する。主な対象:
mapper 3-token external_id / `RawSlackMessage.team_id` 貫通 / team_id 欠落
`ConfigError` / backfill 経路 guard（24-B）、config parse / alias validation /
per-alias keyring slot / nested cursor round-trip / legacy shape reject /
per-alias bind guard matrix / 2-workspace error isolation / channel id 衝突分離 /
per-workspace self-id / excludes 修飾 / env nest 構文（24-C）、status
per-workspace 表示 / digest `(team_id, channel)` key / MCP workspace field
（24-D）、2-workspace e2e（bind → sync → re-sync 冪等 → token swap reject →
alias rename 後の冪等 re-fetch、24-E）。

## スコープ外

- **Enterprise Grid の cross-workspace ingestion** — ADR-0039 §決定 5 の判定を
  維持: `auth.test` は home `team_id` を返すので guard は誤発火しない。Grid 内の
  他 workspace channel の取り込みは引き続き scope 外。
- **Slack 以外の connector の multi-account 化**（Google / GitHub / MS365）—
  本 Phase の「alias × stable-id 二層」pattern を将来の前例にできるが、実需が
  出るまで追わない（epic #530 YAGNI 判定の継承）。
- **旧データの自動 re-key migration**（§(e)、pre-userbase hard flip）。
- **MCP `slack.demand.list` の workspace filter param**（出力 field のみ先行、
  filter は実需待ち）。

## 関連

- [ADR-0039: Slack Single-Workspace Non-Goal](0039-slack-single-workspace-non-goal.md) — 本 ADR が supersede。bind guard semantics は per-alias に一般化して継承
- [ADR-0010: Connector Contract](0010-connector-contract.md) — Slack の cursor schema / `external_id` 規約は本 ADR が改める
- [ADR-0030: Slack Thread Reply Ingestion Policy](0030-slack-thread-reply-ingestion.md) / [ADR-0038: Slack Sync Gap Backfill](0038-slack-sync-gap-backfill.md) — compound cursor の軸定義は不変のまま per-alias nest（§(d)）
- [ADR-0036: Slack Sync Date Floor](0036-slack-sync-date-floor.md) — floor 解決を 3 段に拡張（§(c)）
- [ADR-0040: Slack feature→scope SSOT + auth-test readiness](0040-slack-feature-scope-ssot-and-readiness.md) — SSOT 不変、readiness は per-workspace token 評価（§(h)）
- [ADR-0033: Slack Mention / DM Demand Digest](0033-slack-mention-demand-digest.md) — digest row key + self-id 解決の workspace 軸拡張（§(g)）
- epic [#552](https://github.com/ozzy-labs/opshub/issues/552) — 設計レビュー経緯 + PR 分割（24-A〜E）
