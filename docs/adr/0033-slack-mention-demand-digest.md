# 0033. Slack Mention / DM Demand Digest

- Status: Accepted
- Date: 2026-06-03
- Deciders: opshub maintainers

## Context

Phase 17 ([#374](https://github.com/ozzy-labs/opshub/issues/374)) で `opshub slack conversations` の出力に
`LAST_ACTIVITY` 列を追加したが、この値は `conversations.history?limit=1` の最終 message
timestamp、つまり「**誰かが喋った最終日時**」であり「**自分視点で読むべき未処理**」の
priority signal にはならない (`src/opshub/connectors/slack/conversations.py:221-228,
871-890`)。

ユーザー要望 (Phase 18 epic [#426](https://github.com/ozzy-labs/opshub/issues/426)) は
「自分への demand (@mention / DM 相手の最終発言) を **次に読むべき未処理 signal** として
可視化したい」というもの。アシスタント 14 Skill (`next-actions` / `personal-brief` /
`inbox-triage`) がこの signal を読めば、operator は Slack UI を直接見回すことなく
「自分が放置している ping」を Claude Code / Codex CLI / Copilot CLI 経由で把握できる。

### 既存実装の前提

epic #426 § 改訂履歴で audit した結果、opshub の Slack 取り込み経路には既に必要な情報が
揃っていることが分かった。

- **opshub は event-sourced architecture** ([ADR-0002](0002-event-sourced-architecture.md))
  で、`opshub slack sync` が起動するたびに **全 Slack message を `SourceObserved` event
  として event store に append している** (body full text 含む、
  `src/opshub/connectors/slack/mapper.py:211,221,269`)。@mention は Slack 公式の
  `<@U12345>` 形式で body 内に literal 保持される。DM (`channel_type=im`) と
  MPIM (`channel_type=mpim`) は `SourceObserved.raw["channel_type"]` で判別可能
- **self user_id** は Slack `auth.test()` の `user_id` field から取得できる
  (`src/opshub/connectors/slack/auth.py:181-187`、Phase 7 で確立済み)。User Token /
  Bot Token の両 principal で同じ field が返る
- **既存 token の scope (`channels:read` / `channels:history`、ADR-0018 §決定 7)** で
  上記 demand 信号は検出可能。`search:read` scope 追加は不要

### 設計上の制約

- **event store immutability 不変** ([ADR-0002](0002-event-sourced-architecture.md)) —
  `SourceObserved` event は append-only。demand 信号生成のために event を再書きしては
  ならない。**消費は projection 層** で行う
- **形 A 不変** ([ADR-0004 §決定 (a)](0004-agent-runtime-boundary.md)) — opshub は
  能動性を持たない。Demand 検出は SaaS push (Slack Events API webhook) 経路ではなく
  既存 sync で append された event を **projection が消費する** pull 経路
- **外部書き戻し禁止 不変** ([ADR-0010 §禁止事項 7](0010-connector-contract.md)) —
  本 ADR は read-only 経路の拡張。Slack への通知 / reaction / reply 投稿は持たない
- **MCP 5 不変条件継承** ([ADR-0022](0022-mcp-server-surface.md) §決定) — 新 MCP tool
  `slack.demand.list` は stdio 一択 / token passthrough 禁止 / read 系自律 OK / context
  効率 (要約返却) / OTel naming 準拠を継承
- **CLI noun-first 不変** ([ADR-0031](0031-cli-command-surface-organization.md) §決定
  (4)) — debug CLI を追加する場合は `opshub slack <verb>` の per-noun group に置く

### 設計上の論点

5 軸で意思決定する必要がある。

1. **信号生成経路**: 新 fetch を追加するか / 既存 event を消費する projection を作るか
2. **read model の形**: 既存 projection (inbox / sources) に統合するか / 専用 table を
   作るか
3. **skill 出口**: 既存 MCP tool で透過するか / 新 MCP tool を切るか
4. **signal 軸**: demand のみか / engagement (自分の最終投稿日時) も同時導入か
5. **type 優先順位**: mention / DM / MPIM に tier を持たせるか / `--types` filter で
   水平に扱うか

## Decision

Slack の demand 信号 (@mention / DM / MPIM の自分宛 受信) を、既存 `SourceObserved`
event を消費する **専用 projection + 専用 MCP tool** として最小実装する。6 軸で
方針を pin する。

> **改訂 (Phase 23-D、[issue #534](https://github.com/ozzy-labs/opshub/issues/534))**: 当初の §(a)「connector / fetcher / mapper / event schema には触れない」前提を一部見直した。本 projection の致命的な誤報 = 「自分が返信し終えた DM / mention が最上位に出る」は **message author の identity が event に乗っていない** ことが根因で、projection 内では抑制できない (`title` の display 名は曖昧で id ではない)。そのため以下を着地させた:
> 1. **author id 貫通** — Slack mapper が `raw.user_id` を `SourceObserved.author_id` (新 optional field、`fingerprint` と同じ後方互換追加で `schema_version` は 1 のまま) に貫通させ、projection が `author_id == self_user_id` の行を demand から除外する。過去 event は `author_id=NULL` のため遡及不能で、全 channel に効かせるには full re-sync が必要 (`docs/upgrading.md` §Phase 23-D)。
> 2. **epoch → ISO 8601** — MCP 出力 `last_demand_ts` (raw Slack epoch float) を `last_demand_at` (ISO 8601 UTC) に統一 (他 read tool と方言を揃える)。`since_ts` *filter* 入力は epoch のまま。
> 3. **名前解決** — DM 行は相手の表示名、channel 行は `#name` を `channel_name` に解決 (opaque `D...` id 露出を解消、§不変条件 #4 強化)。
> 4. **死に値 enum 削除** — §(b) / §不変条件 #2 の `demand_kind` 3 値固定から `"mpim"` を削除し 2 値 (`"mention"` / `"dm"`) に絞る (apply が書かないため構造上 0 件、pre-userbase posture で migration `0032` が CHECK 制約を張り替え)。
>
> **scope 外 (本 issue では入れない)**: 「自分が後続発言したら retire する decay」は往復 thread で逆の誤報 (相手が ball を持つケースの誤 retire) を生むため別 issue。author id 貫通だけで誤報の大半が消える。

### (a) demand 信号は既存 `SourceObserved` event を消費する projection で生成する

新 fetch 経路は作らない。`opshub slack sync` が既に append している
`SourceObserved` event を、新規 projection `slack_demand_digest` が消費する。

- event-sourced architecture ([ADR-0002](0002-event-sourced-architecture.md)) の純粋な
  下流追加 — connector / fetcher / mapper / event schema には触れない
- **`opshub projections rebuild` で透過的に rebuild** — Phase 7 で確立した projection
  rebuild infrastructure (`opshub.projections.rebuild_all`) に新 projection を登録する
  だけで CLI 表面は変えない (CLI noun-first 不変、ADR-0031)
- 不採用案 1 (`search.messages` API + 新 fetch 経路): 新 scope (`search:read`) 追加が
  不要、Slack 側 indexing 遅延の影響を受けない、event store の単一の真実から派生する
  ほうが integrity が高い。詳細は §採用しなかった代替 §1 を参照
- 不採用案 2 (`search:read` scope 追加): 上記 (a-不採用 1) と表裏一体で却下

### (b) 新 read model `slack_demand_digest` table を materialize する

新 projection の物理 schema は以下を想定する (詳細 column 名 / 型 / index は Phase 18-B
の Alembic migration PR で確定するため、本 ADR では論理形のみ pin)。

- **粒度**: `(channel_id, demand_kind)` の組ごとに 1 行 upsert
  - `demand_kind ∈ {"mention", "dm", "mpim"}` の 3 値固定 enum
  - mention は public / private channel の `<@self_user_id>` literal hit (DM/MPIM とは
    overlap せず、後者は channel 単位で別 row として扱う)
- **保持値**: `last_demand_ts` (Slack ts、Unix epoch float) / `excerpt` (本文の周辺断片、
  ADR-0005 / ADR-0020 の minimization 契約に従う) / `permalink` (Slack web URL) /
  `source_id` (元 `SourceObserved` event を生んだ `sources.id`、provenance trail)
- **incremental vs full rebuild**: incremental は projection cursor (`event_offset`) で
  追従、full rebuild は `opshub projections rebuild slack_demand_digest` (CLI 表面追加
  なし、既存 `projections rebuild` 経路に新 projection 名を登録)
- 不採用案 (既存 `inbox_items` / `sources` projection 統合): 全 Slack message が既に
  `sources` row として存在し、加えて inbox 統合すると **demand 信号を inbox row 化する
  分が重複** する (同一 message が `sources` + `inbox_items` + demand 信号源で 3 重に
  カウントされる)。詳細は §採用しなかった代替 §3 を参照

### (c) skill 出口は新 MCP tool `slack.demand.list`

アシスタント 14 Skill (`next-actions` / `personal-brief` / `inbox-triage`) が demand
信号を読む経路として、新 MCP read tool `slack.demand.list` を追加する。これは
[ADR-0022 §決定 (f)](0022-mcp-server-surface.md) で確定した 5 不変条件 (stdio / token
passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) を継承する読み取り
tool であり、Phase 18-C で `_registry.py` に追加する。

- **入力 schema (論理形)**: 4 引数すべて optional。
  - `types` (string 配列、enum = `im` / `mpim` / `private` / `public`、default 全 4 値)
    — channel 種別フィルタ。物理列写像 = `slack_demand_digest.channel_type`
  - `demand_kinds` (string 配列、enum = `mention` / `dm` / `mpim`、default 全 3 値)
    — demand 種別フィルタ。物理列写像 = `slack_demand_digest.demand_kind`
  - `since_ts` (number、Slack epoch float、optional、default なし) — physical column
    写像 = `slack_demand_digest.last_demand_ts` (これより strict に古い row を除外)
  - `limit` (integer、optional、default 50、max 200)
  - `order` (string、enum = `last_demand_desc` の 1 値固定、default 同値) — §(e)
    で type tier を採用しないため Phase 18 では唯一の値。Phase 19+ で `oldest_first`
    等の追加を想定した forward-compat 余地として宣言だけ取る
- **出力 schema (論理形)**: `[{channel_id, channel_type, channel_name, demand_kind,
  last_demand_ts, last_demand_user_id, last_demand_excerpt, last_demand_permalink,
  last_source_id}]` のリスト + `total` / `truncated` / `next_offset` pagination
  hint ([ADR-0022 §決定 (d)](0022-mcp-server-surface.md) context 効率に整合)
- **annotation**: `readOnlyHint=true, destructiveHint=false, idempotent=true,
  openWorldHint=false` (`recall.search` / `search` / `task.list` 等の既存 read tool と
  同パターン、SQLite ローカルのみ)
- **物理列写像方針**: `types` → `channel_type`、`demand_kinds` → `demand_kind`、
  `since_ts` → `last_demand_ts` がいずれも物理列に直接写像する (Phase 12 H1 で
  `task.list` / `inbox.list` / `decision.list` / `source.list` の時間フィルタを物理
  列ベースに正規化した方針、[ADR-0022 §決定 (f-3)](0022-mcp-server-surface.md) の延長)
- 不採用案 1 (既存 `inbox.list` 透過): §(b) の inbox 統合不採用と整合
- 不採用案 2 (`recall.search` 透過): demand 信号は本文 substring 検索ではなく
  「mention parse + channel_type 判定 + last_ts upsert」のパスがあり、`recall.search`
  経由にすると skill 側のロジックが複雑化する (semantic recall の noise も混じる)。
  詳細は §採用しなかった代替 §4 を参照

### (d) engagement 軸は Phase 18 scope 外

「自分が最後に投稿した日時」(engagement 軸) は demand 軸とは別 signal で、振り返り
用途 (例: 「先週 1 度も発言しなかった channel」) に寄る。Phase 18 では **demand 軸
のみ** を提供し、engagement は将来必要なら別 sort / 別 tool として追加する
(Phase 19+ 候補)。

> **Update (Phase 19 / [ADR-0034](0034-slack-engagement-axis.md))**: 本 §(d) で
> Phase 19+ 候補として defer した engagement 軸は、Phase 19 で
> [ADR-0034](0034-slack-engagement-axis.md) として独立 ADR 化し、defer 解除した。
> ad-hoc `search.messages?query=from:@me` 経由で `opshub slack conversations`
> の sort 切替 (`--activity=mine` default) として実装され、projection 化は scope 外
> (ADR-0034 §(a) §(f))。本 ADR §不変条件 4 (「engagement 軸は Phase 18 scope 外」)
> は ADR-0034 で別 ADR scope に閉じた形で解除される (本 ADR の demand 軸 projection
> 経路には影響なし、両 axis は orthogonal)。
>
> **Update (Phase 19-D / [ADR-0035](0035-slack-sort-axis-consolidation.md))**:
> ADR-0034 の CLI surface (`--activity={mine|any}`) は ADR-0035 で
> `--sort=name|last_self_post|last_activity` に部分 supersede され、
> engagement 軸経路の表記は `--sort=last_self_post` (または `--sort=name +
> --since` 単独時の暗黙 engagement default) に rename される。demand 軸 (本
> ADR `slack.demand.list`) と engagement 軸 (ADR-0034 / ADR-0035
> `opshub slack conversations`) が orthogonal で共存する不変条件は完全継承
> され、本 ADR の demand 軸 projection 経路には影響なし。

理由 (Phase 18 時点):

- demand 軸が「次に読むべき未処理」という最優先ユーザー要望に直接答える
- 2 軸同時導入は read model schema が 2 軸 × 3 値 で複雑化し、最初の出荷で過剰機構
- 既存 `opshub slack conversations` の `LAST_ACTIVITY` ([#374](https://github.com/ozzy-labs/opshub/issues/374))
  は「他人 + 自分」の混在で別概念だが、用途 (channel 一覧の sort key) が異なるため
  本 ADR の demand digest と共存

### (e) type tier は導入しない (現状の日時 primary + `--types` filter を維持)

mention / DM / MPIM に静的な重み (例: 「DM > mention > MPIM」) を付けず、`--types`
filter で operator (または skill) が必要な type を選ぶ。sort key は `last_demand_ts`
降順のみ。

理由:

- type の優先度は context 依存 (DM が常に最優先とは限らない、特定 channel の mention
  のほうが優先な日もある) で、static tier では誤判定する
- [ADR-0031 §決定 (4)](0031-cli-command-surface-organization.md) noun-first 整合 — CLI
  filter は flag で水平に出すのが既存方針 (`opshub slack conversations --types ...`
  と同パターン)
- skill 側は `--types mention,dm` 等で必要な切り口だけを呼べる

### (f) self user_id 解決経路は `auth.test()` の `user_id` field

projection が mention parse / DM 判定で必要とする self user_id は、`SlackAuth.test()`
([`src/opshub/connectors/slack/auth.py:181-187`](../../src/opshub/connectors/slack/auth.py))
の返り値 `user_id` field を使う。Phase 7 で確立済みの経路を再利用する。

- User Token (`xoxp-`) でも Bot Token (`xoxb-`) でも同じ `user_id` field が返る
  ([ADR-0018 §決定 6](0018-slack-token-principal.md) `principal` field の判別経路と
  同源)
- projection は起動時に 1 回だけ `auth.test()` を呼んで cache する (毎 event 評価で
  叩かない、rate limit budget 保護)
- **再認証で user_id が変わる edge case は Phase 18 scope 外** — operator が token を
  作り直して新 user_id を得た場合、過去 event の mention は旧 user_id を指したまま
  になる。Phase 19+ で `auth.test()` 結果の cache invalidation + projection partial
  rebuild を検討する (現状は full rebuild で対処可能)

## 不変条件

本 ADR で確立する不変条件:

1. **demand 信号生成は既存 `SourceObserved` event を消費する projection の責務** —
   connector / fetcher / mapper / event schema には触れない。`search.messages` 等の
   新 fetch 経路は作らない (§(a))
2. **`slack_demand_digest` read model は channel × demand_kind の組ごとに 1 行 upsert** —
   `demand_kind ∈ {"mention", "dm", "mpim"}` の 3 値固定 enum (§(b))
3. **skill 出口は専用 MCP tool `slack.demand.list`** — 既存 `inbox.list` /
   `recall.search` には統合しない。ADR-0022 §決定 (f) の 5 不変条件 (stdio / token
   passthrough 禁止 / read/write 分離 / context 効率 / OTel naming) を継承 (§(c))
4. **engagement 軸 (自分の最終投稿日時) は Phase 18 scope 外** — demand 軸のみで
   出荷。engagement は将来必要なら別 sort / 別 tool で追加 (§(d))
5. **type tier は持たない** — sort key は `last_demand_ts` 降順のみ、`--types` filter
   で水平 selection (§(e))
6. **self user_id は `auth.test().user_id` 経由で 1 回 cache** — 毎 event 評価で
   `auth.test()` を叩かない (§(f))
7. **`search:read` scope 追加禁止** — 既存 `channels:read` / `channels:history` (User
   Token) または invite された channel に対する Bot Token の同等 scope のみで demand
   検出が完結する ([ADR-0018](0018-slack-token-principal.md) §決定 7 と整合、§(a))

## Consequences

### Positive

- **新 fetch 経路不要 / scope 追加不要** — 既存 token と既存 sync 経路をそのまま使える。
  operator は Phase 18 完了後 `opshub projections rebuild slack_demand_digest` を 1 回
  叩くだけで demand digest が立ち上がる
- **event store の単一の真実から派生** — demand 信号と他の Slack 由来 derived data
  (`sources` / `inbox_items` / embeddings) が同じ `SourceObserved` event から projection
  で導出される構造を維持。replay 可能性 / debuggability が保たれる
  ([ADR-0002](0002-event-sourced-architecture.md) 整合)
- **event-sourced architecture 整合** — Phase 1-9 で確立した「event は append-only、
  projection は再構築可能」の規範を新 projection も継承
- **`opshub projections rebuild` で透過的に発火** — Phase 7 で確立した projection
  rebuild infrastructure に新 projection を登録するだけで CLI 表面は変えない
- **Gmail / Outlook 等の他 messaging connector との非対称を作らない** — demand 信号は
  Slack 固有概念 (@mention literal + DM/MPIM channel_type) で、Gmail / Outlook 側は
  別 ADR で symmetric な signal (例: To: self / inbox label) を切れる将来余地を残す

### Negative / Trade-offs

- **既存全 channel の event を scan するため初回 rebuild に時間がかかる** —
  incremental は projection cursor で対応するが、新規環境 / 全 rebuild 時は全
  `SourceObserved` event を走査する必要がある。1 操作者・1 マシンの想定なので絶対量は
  小さい (Phase 9 で確認済み)
- **mention parse の脆さ** — Slack の `<@U12345>` literal 形式に依存する。Slack が
  message body format を変えた場合 (例: `@username` literal で `U12345` を保持しない)
  に projection を改修する必要がある。Slack API 公式仕様変更は monitoring で検知し、
  必要なら本 ADR を superseded で書き直す
- **late thread reply への追従** — [ADR-0030](0030-slack-thread-reply-ingestion.md)
  Phase 20-C で `thread_activity_window` (default 30d) 内の late thread reply は
  Phase 2 polling で追従する経路が landed。窓経過後の cold thread reply は引き続き
  本 demand projection にも届かない (`threads` 軸から prune される、ADR-0030
  §(d) 不変条件 #5 の意図された limitation)。`opshub projections rebuild` で cursor
  をリセットすれば再取得可能

### Scope 外 (Phase 19+ 候補)

- **demand decay / TTL** — 6 ヶ月前の DM を top に置かない weighting / aging。現状は
  `last_demand_ts` 降順 + MCP tool `slack.demand.list` の `since_ts` 引数による下限
  打ち切りのみ (CLI `opshub slack mentions list` には `--since` 等の時間フィルタなし)
- **archived channel handling** — archive 済 channel の demand row を停止 or 除外する
  論理。現状は archive 後も最終 demand 状態が残る
- **self user_id rotation handling** — 再認証で user_id が変わる edge case の
  cache invalidation + projection partial rebuild (§(f) 参照)
- **engagement 軸** — 自分の最終投稿日時 (§(d) 参照)。Phase 19 で
  [ADR-0034](0034-slack-engagement-axis.md) として defer 解除済 (`opshub slack
  conversations --activity=mine` 経路、ad-hoc `search.messages` ベース、projection
  化は scope 外)。Phase 19-D で [ADR-0035](0035-slack-sort-axis-consolidation.md)
  が CLI surface を `--sort=last_self_post` 表記に部分 supersede (decision 本質
  不変)。本 ADR の demand projection 経路とは orthogonal で相互依存なし
- **Slack 以外の SaaS への demand 信号拡張** — Gmail (To: self / inbox label) /
  MS Teams (mention) / Calendar (organizer = others, attendee = self の未応答 invite)

## Implementation plan

epic [#426](https://github.com/ozzy-labs/opshub/issues/426) で 3 PR に分割する。本 ADR
(18-A) は方針 pin のみで、コード変更を持たない。

- **18-A** ([#427](https://github.com/ozzy-labs/opshub/issues/427)): 本 ADR 新規 +
  関連 ADR cross-reference (docs only、本 PR)
- **18-B** ([#429](https://github.com/ozzy-labs/opshub/issues/429)):
  `slack_demand_digest` projection + Alembic migration + 既存 `opshub projections
  rebuild` で発火 + debug CLI `opshub slack mentions list`。`docs/architecture.md`
  §2 と `docs/troubleshooting.md` の手順記述を同期
- **18-C** ([#430](https://github.com/ozzy-labs/opshub/issues/430)): 新 MCP tool
  `slack.demand.list` + アシスタント Skill 拡張 (`next-actions` / `inbox-triage` /
  `personal-brief`) + docs 一括更新 + `opshub skills install` 再生成 ([ADR-0029
  §dogfood](0029-distribute-assistant-skills-via-opshub-package.md))

## 採用しなかった代替

### 1. `search.messages` API + 新 fetch 経路 + 新 connector module

却下理由:

- 新 scope (`search:read`) 追加が必要 ([ADR-0018](0018-slack-token-principal.md)
  §決定 7 の optional scope menu には含まれるが、本 demand 信号のためだけに追加する
  価値が低い)
- Slack 側 indexing 遅延 (`search.messages` は Slack 内部の indexing pipeline 経由で
  公開され、最新 message は数分〜数十分の lag が生じる) があり、event store 経由の
  ほうが新鮮
- 既存 `SourceObserved` event 内に必要情報 (body 全文 + `channel_type` + `user_id`)
  が揃っているため新経路を作る必要がない
- opshub の event-sourced architecture ([ADR-0002](0002-event-sourced-architecture.md))
  の単一の真実 (event log) から逸れた fetch を増やすと、replay 可能性 / debuggability
  が劣化

### 2. inbox 統合 (demand 信号を `inbox_items` row として表現)

却下理由:

- 全 Slack message が既に `sources` row として存在し、加えて `inbox_items` row として
  も保存すると **同一情報が 3 重に表現される** (sources + inbox + demand row source)。
  data integrity が崩れる
- `inbox_items` は「人手で triage する未処理キュー」(ADR-0016 系) の semantic で、
  「自動派生した demand digest」とは責務が違う
- skill 側 (`inbox-triage`) は `inbox.list` で人手 triage row を読み、`slack.demand.list`
  で別カテゴリの demand row を読む 2 経路にしたほうが境界が明確

### 3. `recall.search` 透過で demand 信号も検索

却下理由:

- `recall.search` は semantic embedding ベース ([ADR-0012](0012-embedding-strategy.md))
  で、demand 信号 (mention parse + channel_type 判定 + last_ts upsert) には semantic
  noise が混じる
- skill 側で「mention に絞る」「DM だけ」「直近 1 週間」等の sharp filter を書きづらく、
  プロンプト engineering で同等の精度を出すコストが高い
- demand 信号は専用 read model + 専用 read tool に分けることで、skill が optimize
  しやすく / debuggable

### 4. type tier (DM > mention > MPIM 等の static 重み付け)

却下理由:

- 優先度は context 依存で static tier では誤判定する (DM が常に最優先とは限らない)
- skill / operator が `--types` filter で必要な切り口だけ呼べば static tier 相当の
  挙動は再現可能
- ADR-0031 §決定 (4) noun-first との整合 — CLI filter は flag で水平に出すのが既存方針

### 5. engagement 軸 (自分の最終投稿日時) を同時導入

却下理由 (本 ADR §(d) 参照):

- ユーザー要望の中核は demand 軸 (「次に読むべき未処理」)
- read model schema が 2 軸 × 3 値で複雑化し、初回出荷で過剰機構
- engagement 軸は振り返り用途 (Phase 19+ で必要性が顕在化したら別 ADR)

## 関連

- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — demand
  信号生成を projection 層に閉じる根拠 (event は append-only、derived data は
  projection で構築)
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md) — 形 A (能動性
  なし) の根拠、Events API webhook 経路を採用しない理由
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) —
  `excerpt` field の保存粒度の上流契約
- [ADR-0010: Connector Contract](0010-connector-contract.md) — Slack connector の
  fetch + normalize + event 化 契約。本 ADR は connector を touch しないため契約
  改訂なし
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md) — `recall.search` 透過
  不採用 (§採用しなかった代替 §3) の根拠
- [ADR-0016: Action Loop and Structured Output](0016-action-loop-and-structured-output.md)
  — `inbox_items` の semantic (人手 triage キュー) との分離根拠 (§採用しなかった
  代替 §2)
- [ADR-0018: Slack Connector Token Principal](0018-slack-token-principal.md) —
  既存 scope (`channels:read` / `channels:history` の User Token、または invite 済
  channel の Bot Token 同等 scope) で demand 検出が可能。新 scope `search:read` は
  要求しない (§決定 (a) 不採用案 2 + 不変条件 7)
- [ADR-0020: Full Local Content Retention](0020-full-local-content-retention.md) —
  本文保持で mention literal `<@U12345>` が projection から読める前提
- [ADR-0022: MCP Server Surface](0022-mcp-server-surface.md) — 新 MCP tool
  `slack.demand.list` の policy-as-data 表現と 5 不変条件継承。§決定 (f) 補遺で
  本 ADR を cross-ref
- [ADR-0028: FTS5 sources_fts tokenizer choice](0028-fts5-japanese-tokenizer.md) —
  日本語 mention literal は FTS5 経路ではなく projection 側の parse で扱うため
  tokenizer 改訂の影響を受けない
- [ADR-0030: Slack Thread Reply Ingestion Policy](0030-slack-thread-reply-ingestion.md)
  — thread reply 内の mention も `SourceObserved` event として取り込まれる
  (ADR-0030 §(a) message 単位 ingest) ため、本 projection は thread 子返信の demand
  も自動的に検知できる。late thread reply の取りこぼしは ADR-0030 と同じ将来オプション
- [ADR-0031: CLI Command Surface Organization](0031-cli-command-surface-organization.md)
  — debug CLI `opshub slack mentions list` を per-noun group に置く根拠 (§決定 (4))
- [ADR-0034: Slack Engagement Axis (Self-Posted Last Activity)](0034-slack-engagement-axis.md)
  — 本 ADR §(d) §不変条件 4 で Phase 19+ 候補として defer された engagement 軸を
  ADR-0034 で別 ADR scope に閉じた形で解除。demand 軸 (本 ADR 受信、`slack.demand.list`)
  と engagement 軸 (ADR-0034 発信、`opshub slack conversations`) は orthogonal で
  共存し、相互依存なし
- [ADR-0035: Slack Sort Axis Consolidation](0035-slack-sort-axis-consolidation.md)
  — ADR-0034 の CLI surface (`--activity={mine|any}`) を
  `--sort=name|last_self_post|last_activity` に部分 supersede。engagement 軸経路の
  表記は `--sort=last_self_post` に rename される。demand 軸 (本 ADR) と engagement
  軸 (ADR-0034 / ADR-0035) の orthogonal 共存は完全継承され、本 ADR の demand 軸
  projection 経路には影響なし
- Phase 18 epic [#426](https://github.com/ozzy-labs/opshub/issues/426) — 本 ADR の
  起票元、3 PR 分割 (18-A / 18-B / 18-C) と Scope 外項目の SSOT
