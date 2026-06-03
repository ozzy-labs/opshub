# 0034. Slack Engagement Axis (Self-Posted Last Activity)

- Status: Accepted
- Date: 2026-06-03
- Deciders: opshub maintainers

## Context

Phase 17 ([#374](https://github.com/ozzy-labs/opshub/issues/374)) で
`opshub slack conversations --since` に追加した `LAST_ACTIVITY` 列は
`conversations.history?limit=1` の最終 message timestamp、つまり「**誰かが
喋った最終日時**」であり、`@channel` / `@here` の周知系や bot 通知も含む
混在 signal だった。結果、自分の関与が薄い channel / DM が「最近 active」
として上位に湧き、**discovery (どの channel を sync 対象にするか決める)**
段階での priority signal としてはノイズが多い。

ユーザー要望は「自分が最後に発言した日時」 (**engagement 軸**) を
default sort key にすることで、broadcast を除外した「自分が継続的に関与
している channel」だけを浮上させたい、というもの。

### ADR-0033 §(d) の defer

Phase 18 で landed した [ADR-0033](0033-slack-mention-demand-digest.md)
§(d) §不変条件 4 は engagement 軸を **Phase 19+ 候補** として明示的に
defer 済 (`docs/adr/0033-slack-mention-demand-digest.md:143-156`):

> 「自分が最後に投稿した日時」(engagement 軸) は demand 軸とは別 signal
> で、振り返り用途 (例: 「先週 1 度も発言しなかった channel」) に寄る。
> Phase 18 では demand 軸のみを提供し、engagement は将来必要なら別 sort
> / 別 tool として追加する (Phase 19+ 候補)。

本 ADR でこの defer を解除する。demand 軸 (ADR-0033、受信、
`slack.demand.list`) と engagement 軸 (本 ADR、発信、`opshub slack
conversations`) は orthogonal で共存する。

### 既存実装の前提

- Phase 17 の `opshub slack conversations --since` は `_progress.indeterminate`
  を 2 段 (listing pages + per-row `conversations.history?limit=1`) で
  単一 spinner に束ねる構成 ([ADR-0026](0026-cli-progress-reporting.md))
- Slack の `search.messages` API は User Token (`xoxp-`) のみ対応で、
  Bot Token (`xoxb-`) は `search:read` scope を構造的に持てない
  ([ADR-0018](0018-slack-token-principal.md) §決定 7、§Context Bot Token
  の構造的制約) — engagement 軸を `search.messages` 経由で実装すると
  Bot Token 運用は fallback (`--activity=any`) しか選べない
- ADR-0033 §決定 (a) は demand 軸について `search.messages` 経由を不採用と
  したが、その却下理由 (event store の単一の真実 / replay 可能性 / scope
  追加コスト) は **persistent な projection を作る場合の判定** であり、
  本 ADR の **ad-hoc discovery 用途** (projection 化しない) には適用範囲が
  異なる

### 設計上の制約

- **event store immutability 不変** ([ADR-0002](0002-event-sourced-architecture.md))
  — projection 化する場合は append-only event を消費する形でなければ
  ならない。本 ADR は projection 化しないため event store には touch しない
- **形 A 不変** ([ADR-0004](0004-agent-runtime-boundary.md) §決定 (a)) —
  opshub は能動性を持たない。engagement 信号は operator が CLI を叩いた
  契機での pull 経路のみで、Slack Events API webhook は採用しない
- **CLI noun-first 不変** ([ADR-0031](0031-cli-command-surface-organization.md)
  §決定 (4)) — engagement 軸は既存 `opshub slack conversations` の sort 切替
  として実装し、新 verb は切らない (Phase 20+ で projection 化する場合に
  限り `opshub slack engaged` 等を再検討)
- **token redaction 不変** ([ADR-0027](0027-observability-and-troubleshooting-logging.md))
  — `search.messages` 呼び出し時の token も既存 `core/sanitise.py` /
  `mcp/_redact.py` 経路で marker 化される (新 redaction 経路を作らず、
  既 stance を inherit)
- **CLI progress 不変** ([ADR-0026](0026-cli-progress-reporting.md)) —
  `--activity=mine` 経路は引き続き `_progress.indeterminate` の単一
  spinner で表現し、description 文字列で作業相を示す既存 pattern を
  踏襲する (determinate を 2 段重ねない)

### 設計上の論点

5 軸で意思決定する必要がある。

1. **engagement 信号源**: 新 API 経路 (`search.messages?query=from:@me`)
   を追加するか / 既存 `SourceObserved` event を消費する projection を
   作るか
2. **`--since` default semantics**: engagement (mine) と activity (any) の
   どちらを default にするか、pre-userbase compat shim を付けるか
3. **scope 不一致時の挙動**: silent fallback するか / 明示エラーで誘導
   するか
4. **`SlackConversation` の ts 表現**: 既存 `last_activity_ts` field を
   mode によって意味を切り替えるか / 新 field を切るか
5. **`--all` (workspace-wide listing) と `--activity=mine` の併用**:
   許可するか / 拒否するか

## Decision

Slack の engagement 信号 (自分の最終発言 ts) を、`search.messages?query=from:@me`
を ad-hoc に叩く `opshub slack conversations` の sort 切替として実装する。
projection 化はしない。9 軸で方針を pin する。

### (a) engagement 信号源は `search.messages?query=from:@me`

1 paginated call で workspace-wide に自分発言を集計し、channel_id ごとに
max ts を取って `SlackConversation` row に貼る。projection 化はしない
(本 ADR §(f))。

- 不採用案 1 (`conversations.history` 全 channel walk + `user == self_user_id`
  フィルタ): per-channel cost が線形に増え、自分が稀にしか発言しない
  channel ではページ消化が膨らむ。`search.messages` の 1 call paginated は
  workspace-wide にまとめて引け、cost が一定
- 不採用案 2 (既存 `SourceObserved` event を消費する projection): discovery
  段階では sync 対象が未確定で chicken-and-egg (engagement signal を
  見て sync 対象を決めたいのに、projection 化するには既に sync 済が
  前提)。ADR-0033 demand digest (sync 済 channel の中での demand 検出) と
  非対称
- 不採用案 3 (新 `slack_engagement_digest` projection + `opshub slack engaged`
  CLI を同時に切る): 本 Phase scope (discovery 段階の ad-hoc 用途) には
  過剰機構。Phase 20+ で「engagement 軸の persistent 履歴を残したい」要望が
  顕在化したら別 ADR で再検討 (§(f))

### (b) `opshub slack conversations --since` default を engagement 基準に切替

旧挙動 (activity = any message) は `--activity=any` で opt-in 保護。
default は `--activity=mine` (engagement)。pre-userbase ([memory:
opshub-pre-userbase-compat-stance]) のため compat shim なし。

- 既存 `LAST_ACTIVITY` 列は `--activity=any` 経路に残し、default の
  `--activity=mine` 経路では `LAST_POST` 列を表示する (table 出力の
  label を dynamic に切替)
- JSON 出力の field semantics は §(g) で別 field に分離する

### (c) `search:read` scope を「engagement 軸利用時に必要」と格上げ

[ADR-0018](0018-slack-token-principal.md) §決定 7 で `search:read` は
optional scope menu に既に列挙済だが、本 ADR で「engagement 軸 (default
`--activity=mine`) を使うときには必須」と利用条件を明確化する (ADR-0018
側を §決定 7 で cross-ref 更新)。

- MVP scope (`channels:history` / `channels:read` / `users:read`) には
  含めない。`--activity=any` で従来通り運用可能で、engagement 軸を使う
  operator のみが追加で `search:read` を承認する設計
- ADR-0018 §決定 7 の MVP scope セットには本 ADR でも touch しない
  (additive な利用条件追記のみ)

### (d) Bot Token は engagement 軸利用不可

Slack の `search.messages` API は User Token 専用で、Bot Token は
`search:read` scope を持てない ([ADR-0018](0018-slack-token-principal.md)
§Context Bot Token の構造的制約)。`SlackAuth.test_token()` の戻り値
`principal == "bot"` (`bot_id` populated) で判別し、Bot Token + `--activity=mine`
の組合せは §(e) の明示エラー経路で reject する。

- Bot Token 運用 operator は `--activity=any` で従来挙動 (broadcast 含む
  最終 activity) を使えるため、機能の全廃ではなく fallback 経路を提供
- principal 判別は Phase 7 で確立済の `auth.test()` 経路 ([ADR-0018](0018-slack-token-principal.md)
  §決定 6) を再利用

### (e) scope / principal 不一致時は明示エラー (silent fallback しない)

以下 2 つの不一致は `ConnectorFailedError` (exit 1) で明示し、operator を
ADR-0018 / Slack scope catalogue に誘導する。

- **scope 不足**: User Token だが `search:read` scope なし + `--activity=mine`
  → 「`search:read` scope を OAuth & Permissions ページで承認するか、
  `--activity=any` で従来挙動に切替えてください」
- **principal 不一致**: Bot Token + `--activity=mine` → 「Bot Token は
  `search:read` を持てません (ADR-0018 §Context)。User Token に切替えるか、
  `--activity=any` で従来挙動に切替えてください」

silent に `--activity=any` 相当へ降格する fallback は採用しない。
operator が「engagement 軸を期待していたのに broadcast 含む activity が
出る」事故を構造的に防ぐ。

### (f) projection 化は scope 外

engagement 信号は ad-hoc `search.messages` call のみで生成し、
`slack_engagement_digest` 等の projection 化はしない。Phase 20+ で
以下の要望が顕在化したら別 ADR で再検討する:

- 「engagement 軸の persistent 履歴を残したい」 (例: 「先週 vs 今週
  の engagement 差分」)
- `opshub slack engaged` 等の専用 CLI / projection 経路の独立化
- demand + engagement の hybrid sort (ハイブリッド priority view)

現状は discovery (ad-hoc) と振り返り (projection) の境界を明確に分け、
本 Phase では discovery 経路のみを提供する。

### (g) `SlackConversation` field semantic 二重化を解消

mine モード用に **新 field `last_self_post_ts: float | None = None`** を
追加し、any モード既存 `last_activity_ts: float | None = None` と分離
する。JSON renderer は populated 側のみ emit (両方 null なら両方省略、
片方 populated なら片方のみ emit)。table 列 label は §(b) 通り
`--activity` で動的切替 (`LAST_POST` / `LAST_ACTIVITY`)。

理由:

- JSON consumer (MCP tool / 外部 script / 将来の hybrid sort) が「この
  ts は誰の発言か」を確定可能にする
- 1 row に両 axis の ts を同時保持できるため、Phase 20+ で hybrid sort を
  追加するときも row schema 変更不要
- 既存 `last_activity_ts` の意味を変更しない (any モード経路は不変)、
  既存テストと JSON consumer に対する breaking change を最小化

不採用案: `last_activity_ts` を mode で意味を切り替える (single field
overload) — JSON consumer が `--activity` flag を見ないと「この ts は誰の?」
を判別できず、silent semantic shift で誤利用される

### (h) `--all` (workspace-wide listing) と `--activity=mine` は併用不可

`search.messages` は self が member の channel しか index hit しないため、
`--all` (`conversations.list` 経由の workspace-wide listing、operator が
join していない channel も含む) と組合せると集合が asymmetric:

- listing 側: workspace-wide 全 channel
- engagement 側: self-member channel のみ (index hit)

silent に listing 結果を trim する (engagement 取れない channel を
非表示にする) と「`--all` を指定したのに channel が消える」誤魔化し
UX になる。`ConfigError` で reject し、operator に明示的に
`--activity=any` 利用を誘導する。

理由:

- silent な集合 trim は ADR-0034 §(e) の「明示エラーで誘導」原則と整合
- `--all` の主用途 (workspace 探索) には activity 軸の方が適合
  (engagement 軸は self-member channel の discovery が主軸)
- pre-userbase ([memory: opshub-pre-userbase-compat-stance]) のため
  compat shim 不要、最初から正しい end-state を出す

### (i) indexing-lag UX (明示 notice)

mine モードは Slack 内部 indexing pipeline 経由のため数分〜数十分の lag が
出る (ADR-0033 §採用しなかった代替 §1 で indexing lag を指摘済)。CLI
runtime に 1 度だけ `notice: search.messages may lag by minutes; use
--activity=any for live activity.` を stderr に emit する。

- `_progress.indeterminate` の spinner description ([ADR-0026](0026-cli-progress-reporting.md))
  とは独立した一行通知。spinner description は作業相 (`"listing
  conversations + engagement"`)、notice は signal 特性 (lag) を伝える
- `_emit_indexing_lag_notice` は 1 度限りの teaching message として
  常時 stderr に emit、`-q` / `OPSHUB_LOG_LEVEL` の影響を受けない
  (完全 suppress したい場合は `--activity=any` を明示する)。
  structlog 経路ではなく直接 `print(..., file=sys.stderr)` で出力する
  ため [ADR-0027](0027-observability-and-troubleshooting-logging.md) の
  verbosity 制御とは独立した経路。verbosity 制御 (structlog) と
  one-shot teaching notice の責務を二重化しないことで設計を simple に
  保つ
- 「常に毎回出す」は uninformative なので 1 invocation あたり 1 度のみ

## 不変条件

本 ADR で確立する不変条件:

1. **engagement 信号は ad-hoc `search.messages` 経由のみで生成、projection
   化しない** — connector / fetcher / mapper / event schema には touch
   しない (§(a) §(f))
2. **`opshub slack conversations --since` の default semantics は
   engagement (`--activity=mine`)** — 旧挙動 (any) は `--activity=any` で
   opt-in (§(b))
3. **`search:read` 欠落 / Bot Token 利用時は明示エラー、silent fallback
   しない** (§(e))
4. **demand 軸 ([ADR-0033](0033-slack-mention-demand-digest.md)
   `slack.demand.list`) と engagement 軸 (本 ADR `opshub slack
   conversations`) は orthogonal、相互依存なし** — projection schema /
   MCP tool 集合 / CLI 表面のいずれも独立
5. **mine モードと any モードは `SlackConversation` の別 field で表現する**
   (`last_self_post_ts` / `last_activity_ts`) — JSON consumer に semantic
   を silently 書き換えない (§(g))
6. **`--all` + `--activity=mine` は `ConfigError` で reject** — silent な
   集合 trim はしない (§(h))
7. **indexing-lag notice (`_emit_indexing_lag_notice`) は 1 度限り常時
   emit、`-q` / `OPSHUB_LOG_LEVEL` で suppress しない** — 完全に
   suppress したい場合は `--activity=any` を明示する。verbosity 制御
   ([ADR-0027](0027-observability-and-troubleshooting-logging.md)) と
   one-shot teaching notice の責務を二重化しない (§(i))

## Consequences

### Positive

- **追加 scope は `search:read` のみ** — ADR-0018 §決定 7 で既に optional
  列挙済の scope を「engagement 軸利用時に必要」と注記するだけで、scope
  catalogue 自体は不変
- **event store / projection 層に touch しない** — Phase 1-9 で確立した
  event-sourced architecture ([ADR-0002](0002-event-sourced-architecture.md))
  を変更せず、ad-hoc discovery 経路として独立して追加できる
- **discovery 用途と相性 ◎** — sync 未済 channel も `search.messages` で
  hit するため、「自分が発言している channel を sync 対象にする」という
  initial setup 経路で機能する
- **demand 軸 (ADR-0033) と orthogonal 共存** — Phase 18 の demand digest と
  schema / tool / CLI 表面のいずれも干渉せず、両 axis を独立に発展可能

### Negative / Trade-offs

- **Slack 内部 indexing pipeline 経由のため数分〜数十分 lag** (§(i)) —
  discovery 用途では許容、live activity が必要なら `--activity=any` 経路で
  対応
- **User Token 必須** — Bot Token 運用 operator は `--activity=any` で
  fallback。Bot Token は `search:read` を構造的に持てないため (ADR-0018
  §Context) これは Slack platform 側の制約
- **projection でない分、再現性 / debuggability は限定** — `search.messages`
  の結果は Slack 内部 index に依存し、過去時点の engagement を replay
  できない。Phase 20+ で履歴を残す要望が出たら projection 化を再検討
  (§(f))
- **`--all` + `--activity=mine` の集合 asymmetric** (§(h)) — ConfigError
  reject で誤魔化さない UX を選んだ結果、operator は「両方ほしい場合は
  `--activity=any` に切替」という mental model を要求される

### Scope 外 (Phase 20+ 候補)

- **engagement projection 化** — `opshub slack engaged` 等の別 CLI /
  `slack_engagement_digest` projection (§(f))
- **`from:@me` 以外の engagement 種別** — `replied:@me` (自分への返信) /
  thread 参加 / reaction
- **demand + engagement の hybrid sort** — `opshub slack priority` 等の
  統合 view
- **MCP tool 経由 engagement** — `slack.engagement.list` (現状は MCP 経路
  では Phase 18 demand のみ)
- **Slack 以外の SaaS への engagement 信号拡張** — Gmail (Sent label) /
  MS Teams (own posts) / GitHub (own commits / reviews)

## Implementation plan

epic [#438](https://github.com/ozzy-labs/opshub/issues/438) で 2 PR に
分割する。本 ADR (19-A) は方針 pin のみで、コード変更を持たない。

- **19-A** ([#440](https://github.com/ozzy-labs/opshub/issues/440)): 本 ADR
  新規 + 関連 ADR cross-reference (docs only、本 PR)
- **19-B** ([#441](https://github.com/ozzy-labs/opshub/issues/441)):
  Slack `search.messages` adapter + CLI `--activity` flag +
  `last_self_post_ts` field + `--all` 拒否 + indexing-lag notice + tests +
  関連 docs 一括 (`README.md` / `CLAUDE.md` / `AGENTS.md` /
  `docs/mcp-setup.md` / `docs/troubleshooting.md` / `docs/upgrading.md` /
  `docs/architecture.md`)

## 採用しなかった代替

### 1. `conversations.history` 全 channel walk + `user == self_user_id` フィルタ

却下理由:

- per-channel cost が線形に増え、自分が稀にしか発言しない channel では
  ページ消化が膨らむ (channel 数 × history page 数のオーダー)
- `search.messages` の 1 paginated call は workspace-wide にまとめて
  引け、cost が一定
- discovery 用途 (まだ sync 対象に入っていない channel も含む) では
  history walk のスキャン量が無視できないオーダーになる

### 2. 既存 `SourceObserved` event を消費する projection

却下理由:

- discovery 段階では sync 対象が未確定で chicken-and-egg (engagement
  signal を見て sync 対象を決めたいのに、projection 化するには既に sync
  済が前提)
- ADR-0033 demand digest (sync 済 channel の中での demand 検出) と非対称
  で、「sync 対象選定の前段階」という用途には projection 経路は構造的に
  不適合

### 3. 新 `slack_engagement_digest` projection + `opshub slack engaged` CLI を同時に切る

却下理由:

- 本 Phase scope (discovery 段階の ad-hoc 用途) には過剰機構
- projection 化は persistence cost (Alembic migration / projection rebuild
  infrastructure 登録 / debug CLI) を伴うが、現状の要望「discovery 用 sort
  key 改善」には projection の persistent 履歴は不要
- Phase 20+ で「engagement 軸の persistent 履歴を残したい」要望が顕在化
  したら別 ADR で再検討 (§(f))

### 4. silent fallback (`search:read` 欠落 / Bot Token で `--activity=any` 相当に降格)

却下理由:

- operator が「engagement 軸を期待していたのに broadcast 含む activity が
  出る」事故を構造的に防げない
- ADR-0033 §採用しなかった代替 §1 と同じく、誤魔化し UX は debug 困難を
  生む
- 明示エラーで ADR-0018 / Slack scope catalogue に誘導する方が「足りない
  ものを足す」自然な mental model

### 5. `last_activity_ts` field 1 つを mode で意味を切り替える

却下理由:

- JSON consumer (MCP tool / 外部 script) が `--activity` flag を見ないと
  「この ts は誰の?」を判別できない silent semantic shift
- Phase 20+ で hybrid sort (demand + engagement) を追加するときに 1 row に
  両 ts を同時保持できず、schema 変更が必要になる
- 新 field `last_self_post_ts` の追加コストは小さく、semantic 二重化を
  解消する benefit が上回る (§(g))

### 6. `--all` + `--activity=mine` を silent に listing 側を trim する

却下理由:

- `--all` を指定したのに channel が消える誤魔化し UX
- silent な集合 trim は ADR §(e) の「明示エラーで誘導」原則と整合しない
- `--all` の主用途 (workspace 探索) には activity 軸の方が適合するため、
  ConfigError で `--activity=any` 利用を誘導する方が自然 (§(h))

## 関連

- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
  — event store / projection 層に touch しない根拠 (§(a) §(f))。本 ADR は
  ad-hoc discovery 経路として独立して追加され、Phase 1-9 で確立した
  architecture を変更しない
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md) —
  形 A (能動性なし) の根拠、Events API webhook 経路を採用しない理由
- [ADR-0010: Connector Contract](0010-connector-contract.md) — Slack
  connector の fetch + normalize + event 化 契約。本 ADR は new fetch
  経路 (`search.messages`) を追加するが、SourceObserved event は emit
  しない (ad-hoc 経路) ため契約改訂なし
- [ADR-0018: Slack Connector Token Principal](0018-slack-token-principal.md)
  — `search:read` scope の利用条件を本 ADR で「engagement 軸利用時に必要」
  と格上げ (§(c))。Bot Token は `search:read` を構造的に持てないため
  engagement 軸利用不可 (§(d))。ADR-0018 §決定 7 で本 ADR を cross-ref
- [ADR-0026: CLI Progress Reporting](0026-cli-progress-reporting.md) —
  `_progress.indeterminate` の単一 spinner で `--activity=mine` 経路を
  表現する根拠。spinner description は `"listing conversations + engagement"`
  に切替 (ADR-0026 で本 ADR を cross-ref)
- [ADR-0027: Observability & Troubleshooting Logging](0027-observability-and-troubleshooting-logging.md)
  — `search.messages` 呼び出し時の Slack OAuth token も既存 `core/sanitise.py`
  / `mcp/_redact.py` 経路で marker 化される (既 stance を inherit、新
  redaction 経路を作らない)。ADR-0027 で本 ADR を cross-ref
- [ADR-0031: CLI Command Surface Organization](0031-cli-command-surface-organization.md)
  — engagement 軸は既存 `opshub slack conversations` の sort 切替として
  実装し、新 verb は切らない根拠 (§決定 (4) noun-first)
- [ADR-0033: Slack Mention / DM Demand Digest](0033-slack-mention-demand-digest.md)
  — §(d) §不変条件 4 で defer された engagement 軸を本 ADR で解除。demand
  軸 (ADR-0033 受信) と engagement 軸 (本 ADR 発信) は orthogonal で共存。
  ADR-0033 §(d) で本 ADR を cross-ref
- Phase 19 epic [#438](https://github.com/ozzy-labs/opshub/issues/438) —
  本 ADR の起票元、2 PR 分割 (19-A / 19-B) の SSOT
