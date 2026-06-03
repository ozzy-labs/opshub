# 0035. Slack Sort Axis Consolidation (`--sort` 軸統合 / `--activity` 廃止)

- Status: Accepted
- Date: 2026-06-04
- Deciders: opshub maintainers
- Supersedes: [ADR-0034](0034-slack-engagement-axis.md) §(b) §(g) §(h) §(i) §不変条件 2 の **表現のみ** (CLI surface の rename)。§(d) Bot Token 不可 / §(e) silent fallback なし / §(g) field 二重化解消の motivation / §(i) indexing-lag notice / §不変条件 3, 5, 6, 7 は完全継承

## Context

Phase 19-B ([ADR-0034](0034-slack-engagement-axis.md)、
[#441](https://github.com/ozzy-labs/opshub/issues/441)) で
`opshub slack conversations --since` の default が engagement 軸
(`--activity=mine`、`search.messages?query=from:@me` 経由) に切り替わり、
`--activity={mine|any}` flag と `last_self_post_ts` / `last_activity_ts` の
field 二重化解消が着地した。

着地後の UX レビューで以下が判明した。

1. **`--activity` flag は実装都合のリーク** — operator が考えているのは
   「自分の最終投稿日時で並べたい」または「誰でも良いから最終発言で並べたい」
   という **sort key の選択** であって、「軸を `mine` / `any` で切替えつつ
   cutoff (`--since`) を別 flag で渡す」というモデルではない。`--activity` は
   CLI 側に engagement 軸の実装手段 (`search.messages?query=from:@me`) が
   漏れ出した命名で、operator にとっては余分な flag
2. **default format `table` だが実用 use case は TOML 貼り付け** —
   `opshub slack conversations` の主用途は出力を `opshub.toml` の
   `[connectors.slack]` channels list に貼り付けて sync 対象を確定させること
   ([#366](https://github.com/ozzy-labs/opshub/issues/366) 由来)。table 出力は
   eyeball 確認用で副次的だが、現状の default は `table`。毎回
   `--format toml` を付けさせる摩擦が残る
3. **default sort が `--since` の有無で flip する** — ADR-0034 §(b) で
   default を engagement 軸に切替えた結果、`--since` 無しは name 昇順 /
   `--since` 有りは ts 降順 になり、`--since 30d` を付けただけで出力順が
   切り替わる挙動になっている。operator は同じ default sort key で「filter を
   足す」と思っているのに、表示順が flip する混乱を生む

ADR-0034 が pin した不変条件 (engagement 軸 / Bot Token 不可 / silent fallback
なし / field 二重化解消 / `--all` 非両立 / indexing-lag notice) は **decision
の本質として全て継承し**、CLI surface の表現のみ再編する。本 ADR は
ADR-0034 の **部分 supersede** (CLI surface only) であり、ADR-0034 §(a)
engagement 信号源 / §(c) `search:read` 利用条件格上げ / §(d) Bot Token 不可 /
§(e) silent fallback なし / §(f) projection 化 scope 外 / §(g) field 二重化
解消 / §(i) indexing-lag notice は完全に有効。

### 設計上の制約 (ADR-0034 継承)

- **event store immutability 不変** ([ADR-0002](0002-event-sourced-architecture.md))
  — engagement 信号は projection 化しない (ADR-0034 §(a) §(f) 不変)
- **形 A 不変** ([ADR-0004](0004-agent-runtime-boundary.md) §決定 (a)) —
  webhook 経路を採用しない (ADR-0034 継承)
- **CLI noun-first 不変** ([ADR-0031](0031-cli-command-surface-organization.md)
  §決定 (4)) — sort 切替は既存 `opshub slack conversations` の flag として
  実装し、新 verb は切らない
- **token redaction 不変** ([ADR-0027](0027-observability-and-troubleshooting-logging.md))
  — `search.messages` 呼び出し時の token redaction は既 stance を inherit
- **CLI progress 不変** ([ADR-0026](0026-cli-progress-reporting.md)) —
  `_progress.indeterminate` の単一 spinner で表現する pattern を踏襲。
  description 文字列は §(g) で `--sort=last_self_post` 経路に書き換え

### 設計上の論点

CLI surface の再編は 6 軸で意思決定する必要がある。

1. **default format**: `table` のままか / `toml` に切替えるか
2. **default sort**: `--since` の有無で flip させるか / 固定するか
3. **軸選択 flag の命名**: `--activity={mine|any}` 継続か / `--sort=<key>` に
   統合するか / 別の表現か
4. **`--sort=name` + `--since` の軸暗黙 default**: reject か / engagement 軸を
   暗黙起動するか
5. **`--sort=last_*` + `--since` なしの cutoff 扱い**: 全期間 search を許可
   するか / 暗黙 cutoff を当てるか
6. **継承範囲の明示**: ADR-0034 のどの決定を継承し、どこまでが本 ADR の
   supersede 範囲か

## Decision

`opshub slack conversations` の CLI surface を以下 6 軸で再編する。decision の
本質 (engagement 軸 / scope 要件 / field 二重化解消 / `--all` 非両立 /
indexing-lag notice) は ADR-0034 から完全継承し、CLI 表現のみ rename する。

### (a) default format = `toml`

`opshub slack conversations` の default `--format` を `table` から `toml` に
切替える。主用途 (`opshub.toml` の `[connectors.slack]` channels への貼り付け)
で毎回 `--format toml` を付ける摩擦を解消する。

- `--format=table` で旧 default を再現可能。table 出力自体は廃止しない
  (eyeball 確認 / debug 用途で有用)
- pre-userbase ([memory: opshub-pre-userbase-compat-stance]) のため compat
  shim なし。`docs/upgrading.md` §Phase 19-D に breaking change として記載

不採用案: 「主用途を TOML と決め打ちするのは早い、両方需要がある」 —
operator feedback (#366 起票元) で TOML 貼り付けが圧倒的に主用途であることが
確認されており、table 用途は副次的。default 切替で頻出 use case の摩擦を
解消する方が UX として正しい。

### (b) default sort = `name` 固定 (`--since` の有無で flip しない)

default sort は `type bucket (public→private→mpim→im) → display_name 昇順`
固定。`--since` の有無に関係なく default は常に name 順。ADR-0034 §(b) で
「`--since` 指定で sort が ts 降順に flip」した挙動を廃止する。

- name 順は workspace 探索 / sync 対象選定で「アルファベット順に並んだ
  channel 一覧を眺めて選ぶ」という基本 use case に合う
- `--since` は filter として独立した責務 (cutoff のみを与え、sort key は
  別軸) に分離する
- ts 順で並べたい operator は `--sort=last_self_post` / `--sort=last_activity`
  を明示する (§(c) §(d) §(e))

### (c) `--sort` flag 新設、`--activity` flag 完全削除

新 flag `--sort=name|last_self_post|last_activity` (default `name`) を導入し、
ADR-0034 §(b) で追加した `--activity={mine|any}` flag を完全削除する。キー名
で軸が一意に決まり、`--activity` 別 flag は不要になる。

- **不採用案 1** (`--sort=name|activity` + `--activity={mine|any}` 併存) —
  UX 観点で `--activity` のリークを残したまま、flag 数も減らない。本 ADR の
  motivation 1 (`--activity` flag が実装都合のリーク) を解消できない
- **不採用案 2** (`--sort=mine|any` 等の ADR-0034 用語継承) — キー名が DB
  field (`last_self_post_ts` / `last_activity_ts`) と一致せず、JSON 出力との
  対応が悪い。operator が JSON output を眺めて「あ、`last_self_post_ts` で
  sort できるのか」と推測できる方が discoverable
- **不採用案 3** (`--sort=engagement|activity`) — semantic は明快だが DB
  field と表記が乖離する。operator が JSON を眺めて発見しづらく、CLI / JSON /
  DB schema の語彙が揃わない
- **採用案の根拠**: `--sort=last_self_post` / `--sort=last_activity` は DB
  field (`last_self_post_ts` / `last_activity_ts`) と語彙が一致し、CLI / JSON
  / DB schema の 3 層で同じ語彙が貫通する。`--sort=name` は表示 sort key 名
  そのもの (display_name 昇順) で混乱しない

### (d) `--sort=name` + `--since` の軸暗黙 default = engagement (`last_self_post`)

`--since` を単独で指定した (sort 未指定 = default `--sort=name`) 場合は、
engagement 軸 (`last_self_post`) で probe + cutoff filter を行い、name 順
listing、`last_self_post_ts` 列を表示する。

- **不採用案 1** (`--sort=name` + `--since` を `typer.BadParameter` で reject、
  `--since requires --sort=last_self_post or --sort=last_activity`) — UX が
  制限的で、本 ADR motivation 3 (「name 順 + ts 列 を一発で得たい」) に
  応えられない。operator は「`--since 90d` だけで filter + 列表示 + name 順
  並び」が直感的
- **不採用案 2** (`--sort=name` + `--since` は filter 無視 + stderr 警告) —
  exit 0 だが operator 期待 (filter したい) に反する confusion を生む
- **採用案の根拠**: engagement 軸を暗黙 default にすることで operator が
  `--since 90d` だけで「filter + ts 列 + name 順 listing」を一発で得られる。
  軸の暗黙性は doc (`docs/upgrading.md` / `docs/troubleshooting.md`) で
  明文化し、silent magic にしない
- **副作用 (明文化)**: `--sort=name + --since` でも engagement 軸 probe が
  走るため `search:read` scope 要件が発生する (ADR-0034 §(c) 継承)。Bot Token
  user は `--since` 単独で `ConfigError` になる (ADR-0034 §(d) 継承、§(f) で
  本 ADR にも適用と明示)。これは UX trade-off として doc 化

### (e) `--sort=last_self_post|last_activity` + `--since` なし → 暗黙 cutoff `--since 90d` + stderr notice

`--sort=last_self_post` / `--sort=last_activity` を `--since` なしで指定した
場合、暗黙 cutoff `--since 90d` を当てた上で probe を実行し、stderr に notice
1 行を出力する。

- **理由**: `search.messages` (engagement 軸) / `conversations.history`
  (activity 軸) を全期間 search すると重い上に page 消化が膨らむ。chatty
  workspace では API budget が爆発し、Slack rate limit (Tier 2-4) の 429
  retry exhaustion を招く。`--since` 必須化は UX を制限する一方、暗黙 cutoff
  と notice の組合せなら「明示 `--since` で override 可能」と balance できる
- **default cutoff 90d の根拠**: 任意 choice。3 ヶ月という discovery 用途で
  妥当な範囲 (operator が「最近 active な channel」と直感する範囲、ADR-0034
  §不変条件 では具体的な値は pin されていない)。Phase 20+ で operator feedback
  を受けて調整可能。short cap (7d / 30d) は discovery には狭すぎ、long cap
  (180d / 365d) は API budget が膨らむため、中庸として 90d
- **notice 文言** (本 ADR §(e) で pin):

  ```text
  notice: --sort=<sort> defaulted to --since 90d to cap probe cost; pass --since explicitly to override.
  ```

  `<sort>` には実際の sort 値 (`last_self_post` / `last_activity`) が入る。
  ADR-0034 §(i) の indexing-lag notice (`notice: search.messages may lag by
  minutes; ...`) とは独立した別 notice (engagement 軸の場合は両方 emit され
  得る)

### (f) ADR-0034 の決定継承範囲

ADR-0034 §(d) §(e) §(g) §(h) §(i) §不変条件 3, 5, 6, 7 を **完全継承** し、
表現を `--sort` 表記に書き換えるのみ、決定の本質は不変。具体的には:

- **Bot Token + engagement 軸経路 → `ConfigError`** (ADR-0034 §(d) 継承) —
  本 ADR では engagement 軸経路 = `--sort=last_self_post`、および §(d) で
  追加した `--sort=name + --since` (暗黙 engagement) の両方が対象
- **`search:read` 欠落 + engagement 軸経路 → `ConnectorFailedError`**
  (ADR-0034 §(e) 継承) — 同上の経路で発火
- **`last_self_post_ts` / `last_activity_ts` の field 二重化解消** (ADR-0034
  §(g) 継承) — JSON renderer は populated 側のみ emit、table 列 label は sort
  軸で動的切替 (`LAST_POST` / `LAST_ACTIVITY`)
- **`--all` + engagement 軸経路 → `ConfigError`** (ADR-0034 §(h) 継承、
  組合せ条件のみ拡張) — `--all` と `--sort=last_self_post` の組合せ、および
  `--all` と `--sort=name + --since` (暗黙 engagement) の組合せの両方で
  reject。activity 軸 (`--sort=last_activity`) は self-member 制約がないため
  `--all` と組合せ可能 (ADR-0034 §(h) の非両立範囲は engagement 軸に閉じて
  いる)
- **indexing-lag notice** (ADR-0034 §(i) 継承) — engagement 軸 probe 時に
  1 度 stderr emit、`-q` / `OPSHUB_LOG_LEVEL` で suppress しない (ADR-0034
  §不変条件 7)。本 ADR §(e) の暗黙 cutoff notice と直交した別 notice

## 不変条件

本 ADR で確立する不変条件 (ADR-0034 から継承される不変条件と組合せ):

1. **CLI default format は `toml`** (§(a)) — `--format=table` で旧 default
   を opt-in 再現可能
2. **CLI default sort は `name` 固定** (§(b)) — `--since` の有無で flip
   しない。ADR-0034 §(b) の「`--since` で ts 降順 flip」を廃止
3. **軸選択は `--sort` のキー名で表現する** (§(c)) — `--activity` 等の別
   flag は持たない (ADR-0034 §(b) の `--activity={mine|any}` を完全削除)
4. **`--since` 単独 (`--sort=name + --since`) の軸 default は engagement
   (`last_self_post`)** (§(d)) — silent magic にせず doc で明文化
5. **`--sort=last_self_post|last_activity` + cutoff なし時は暗黙 `--since 90d`
   を当て、stderr に notice 1 度** (§(e)) — API budget 保護と UX balance
6. **ADR-0034 §(d) §(e) §(g) §(h) §(i) §不変条件 3, 5, 6, 7 を完全継承**
   (§(f)) — engagement 軸の信号源 / scope 要件 / error 経路 / field 二重化
   解消 / `--all` 非両立 / indexing-lag notice は本質不変

## Consequences

### Positive

- **`--activity` flag 廃止で surface 1 つ減** — operator が意識する flag
  数が減り、`--sort` 1 つで sort key を選ぶ mental model に統一
- **`--sort` キー名で軸が直感的、DB field 名と一致** — CLI / JSON / DB
  schema の 3 層で語彙が貫通し、operator が JSON output を眺めて
  「`last_self_post_ts` で sort できるのか」と発見できる discoverable な
  設計
- **default format = toml で主用途の摩擦解消** — `opshub.toml` 貼り付けで
  毎回 `--format toml` を付ける必要がなくなる
- **default sort 固定で `--since` の有無による flip がなくなる** — `--since`
  は cutoff filter として独立した責務に分離され、sort key の変化と filter
  の追加が混ざらない

### Negative / Trade-offs

- **ADR-0034 を Phase 19-B 着地直後 (~1 週間) に supersede するため ADR
  履歴が短期に動く** — pre-userbase ([memory:
  opshub-pre-userbase-compat-stance]) なので許容するが、ADR-0034 と本 ADR の
  関係が「部分 supersede (CLI surface only)」であることを冒頭 status section
  で明示する必要がある
- **`--sort=name + --since` で engagement 軸が暗黙起動するため、Bot Token
  user / `search:read` なし User Token user が `--since` 単独で意図せず
  `ConfigError` になる UX** — doc (`docs/upgrading.md` /
  `docs/troubleshooting.md`) で明文化、§(d) で trade-off として整理。
  代替案 (reject / silent fallback) のいずれも UX として劣るため許容
- **暗黙 `--since 90d` cutoff (sort=last_* 単独時)** — 全期間 search を期待
  した operator は notice を読んで `--since` 明示で override する必要がある。
  逆に notice 文言で「90d で cap した」と明示されるため silent magic ではない
- **breaking change が複数ある** — `--activity` flag 完全削除 / default
  `--format` 変更 / `--since` 指定時の sort 挙動変更 / 暗黙 `--since 90d`
  cutoff の 4 点が pre-userbase で compat shim なしで着地する。
  `docs/upgrading.md` §Phase 19-D で旧 → 新コマンドの対応表を提供

### Breaking changes (pre-userbase compat shim なし)

1. **`--activity` flag 完全削除** (§(c)) — `--activity=mine` → `--sort=last_self_post`
   へ rewrite が必要。`--activity=any` → `--sort=last_activity` へ rewrite
2. **default `--format` 変更** (§(a)) — `table` → `toml`。script user は
   `--format=table` を明示する必要
3. **`--since` 指定時の sort 挙動変更** (§(b)) — 旧: ts 降順 / 新: name 順
   固定 (engagement 軸 ts 列は表示)。ts 軸 sort は `--sort=last_self_post`
   / `--sort=last_activity` を明示
4. **暗黙 `--since 90d` cutoff** (§(e)) — `--sort=last_*` 単独時に発火。
   全期間 search したい operator は `--since` で明示的に override

### Scope 外 (Phase 20+ 候補)

- **`--sort=name` + `--since` の軸を `--sort` で明示可能にする** — 現状は
  暗黙 engagement (§(d))。`--sort=name --since-axis=last_activity` 等の
  flag を切る可能性はあるが、現状の UX では暗黙 engagement で十分
- **engagement / activity の hybrid sort** — `--sort=last_either` 等で
  どちらか populated 側で sort する mode。現状は 2 axis を独立に扱う
- **engagement projection 化** — ADR-0034 §(f) で Phase 20+ 候補として
  defer 済。本 ADR ではこの境界は不変
- **その他 sort key** — `--sort=member_count` / `--sort=created` 等の channel
  metadata ベース sort。本 ADR では `name` / `last_self_post` / `last_activity`
  の 3 値に閉じる

## Implementation plan

epic [#448](https://github.com/ozzy-labs/opshub/issues/448) で 2 PR に分割する。
本 ADR (19-D-1) は方針 pin のみで、コード変更を持たない。

- **19-D-1** ([#449](https://github.com/ozzy-labs/opshub/issues/449)): 本 ADR
  新規 + ADR-0034 supersede 注記 + 関連 ADR cross-reference (docs only、
  本 PR)
- **19-D-2** ([#450](https://github.com/ozzy-labs/opshub/issues/450)): CLI
  surface 実装 (`--activity` 削除 / `--sort` 追加 / default format 変更 /
  暗黙 `--since 90d` cutoff + notice / `--sort=name + --since` 暗黙 engagement
  経路) + tests + docs 一括 (`README.md` / `CLAUDE.md` / `AGENTS.md` /
  `docs/upgrading.md` / `docs/troubleshooting.md` / `docs/mcp-setup.md` /
  `docs/architecture.md` / `docs/adr/README.md` の Phase 19 行)

## 採用しなかった代替

### 1. ADR-0034 の `--activity={mine|any}` flag をそのまま維持する

却下理由:

- 本 ADR motivation 1 (`--activity` flag が実装都合のリーク) を解消できない
- operator が考えているのは「sort key の選択」であって「軸 + cutoff の別 flag
  指定」ではない
- pre-userbase で UX を正しく直す機会は今しかなく、後から rename するコストの
  ほうが高い

### 2. `--sort=name|activity` + `--activity={mine|any}` の併用 (flag 2 つ)

却下理由 (§(c) 不採用案 1 参照):

- UX 観点で `--activity` のリークを残したまま、flag 数も減らない
- sort key と軸を別々に指定するモデルは operator の mental model と不整合

### 3. `--sort=mine|any` 等の ADR-0034 用語継承

却下理由 (§(c) 不採用案 2 参照):

- キー名が DB field (`last_self_post_ts` / `last_activity_ts`) と一致せず、
  JSON 出力との対応が悪い
- operator が JSON を眺めて「あ、これでソートできるのか」と発見しづらい
- 「mine / any」は user-facing には曖昧 (mine = 何の mine か?)

### 4. `--sort=engagement|activity` (semantic 寄りの命名)

却下理由 (§(c) 不採用案 3 参照):

- semantic は明快だが DB field と表記が乖離
- operator が JSON を眺めて発見しづらく、CLI / JSON / DB schema の 3 層で
  語彙が揃わない
- `--sort=last_self_post` / `--sort=last_activity` の方が DB field 名と
  一対一対応で discoverable

### 5. `--sort=name` + `--since` を `typer.BadParameter` で reject

却下理由 (§(d) 不採用案 1 参照):

- UX が制限的、本 ADR motivation 3 (「name 順 + ts 列 を一発で得たい」) に
  応えられない
- operator が `--since 90d` だけで filter + ts 列 + name 順 を期待する
  use case を構造的に排除する

### 6. `--sort=name` + `--since` で filter 無視 + stderr 警告

却下理由 (§(d) 不採用案 2 参照):

- exit 0 だが operator 期待 (filter したい) に反する confusion
- silent に filter を無視するのは ADR-0034 §(e)「silent fallback しない」
  原則と整合しない

### 7. `--sort=last_*` + `--since` なし → 全期間 search を許可

却下理由 (§(e) 不採用案):

- chatty workspace で API budget が爆発、Slack rate limit (Tier 2-4) の
  429 retry exhaustion を招く
- discovery 用途では 90d cap で十分な情報量が得られる
- 全期間 search が必要な operator は `--since 365d` 等で明示的に override
  可能

### 8. `--since` 必須化 (`--sort=last_*` 単独で必ず `--since` を要求)

却下理由 (§(e) 不採用案):

- UX を制限的にする (operator は「とりあえず last_self_post で並べたい」
  という ad-hoc 用途で毎回 `--since` を考える必要)
- 暗黙 cutoff 90d + notice の方が「明示 override 可能」と balance できる

## 関連

- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
  — engagement 信号は projection 化しない方針 (ADR-0034 §(a) §(f) 継承) で
  event store には touch しない
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md) —
  形 A (能動性なし) の根拠、Events API webhook 経路を採用しない理由
  (ADR-0034 継承)
- [ADR-0010: Connector Contract](0010-connector-contract.md) — Slack
  connector の fetch + normalize + event 化 契約。本 ADR は new fetch 経路を
  追加しないため契約改訂なし
- [ADR-0018: Slack Connector Token Principal](0018-slack-token-principal.md)
  — `search:read` scope の利用条件 (engagement 軸利用時に必要、ADR-0034 §(c)
  で格上げ) を本 ADR §(d) (`--sort=name + --since` 暗黙 engagement) + §(f)
  でも適用。Bot Token は `search:read` を構造的に持てないため engagement 軸
  利用不可 (ADR-0034 §(d) 継承、本 ADR §(f) で再確認)。ADR-0018 §決定 7 で
  `--activity=mine` 表記を `--sort=last_self_post` 表記に更新
- [ADR-0026: CLI Progress Reporting](0026-cli-progress-reporting.md) —
  `_progress.indeterminate` の単一 spinner で `--sort=last_self_post` 経路を
  表現する根拠 (ADR-0034 から継承)。spinner description は本 ADR §(g)
  実装 PR (19-D-2) で `--activity=mine` 表記から `--sort=last_self_post`
  表記に書き換え (description 文字列自体 `"listing conversations + engagement"`
  は不変)。ADR-0026 で本 ADR を cross-ref
- [ADR-0027: Observability & Troubleshooting Logging](0027-observability-and-troubleshooting-logging.md)
  — `search.messages` 呼び出し時の Slack OAuth token redaction は既 stance
  を継承 (ADR-0034 から継承)。本 ADR §(e) の暗黙 cutoff notice / ADR-0034
  §(i) の indexing-lag notice はいずれも `-q` / `OPSHUB_LOG_LEVEL` で
  suppress しない one-shot teaching message (verbosity 制御と直交)。ADR-0027
  で本 ADR を cross-ref
- [ADR-0031: CLI Command Surface Organization](0031-cli-command-surface-organization.md)
  — sort 切替は既存 `opshub slack conversations` の flag として実装し、新
  verb は切らない根拠 (§決定 (4) noun-first、ADR-0034 から継承)
- [ADR-0033: Slack Mention / DM Demand Digest](0033-slack-mention-demand-digest.md)
  — demand 軸 (`slack.demand.list`) と engagement 軸 (本 ADR および ADR-0034
  `opshub slack conversations`) は orthogonal で共存。ADR-0034 §不変条件 4
  (demand 軸との orthogonal) を本 ADR §(f) で継承。ADR-0033 §関連で本 ADR
  および ADR-0034 を cross-ref
- [ADR-0034: Slack Engagement Axis (Self-Posted Last Activity)](0034-slack-engagement-axis.md)
  — 本 ADR が部分 supersede する元 ADR。CLI surface (§(b) §(g) §(h) §(i)
  §不変条件 2) のみ表現を書き換え、engagement 信号源 / Bot Token 不可 /
  silent fallback なし / field 二重化解消 / indexing-lag notice の決定本質は
  完全継承。ADR-0034 冒頭 status section に本 ADR への部分 supersede 注記を
  追加
- Phase 19-D epic [#448](https://github.com/ozzy-labs/opshub/issues/448) —
  本 ADR の起票元、2 PR 分割 (19-D-1 / 19-D-2) の SSOT
