# 0030. Slack Thread Reply Ingestion Policy

- Status: Accepted + Landed (revised, Phase 20)
- Date: 2026-06-02 (revised 2026-06-07 — §(d) を Phase 20 実装に合わせて改訂、`## Implementation plan` を deferred → landed 化)
- Deciders: opshub maintainers
- Related: [ADR-0036](0036-slack-sync-date-floor.md) — sync の date floor で親メッセージが `oldest` 以前に落ちると、その thread reply (`conversations.replies`) も取得対象外になる (floor と整合)

## Context

現状 Slack connector の fetcher (`src/opshub/connectors/slack/fetcher.py::SlackFetcher.fetch_messages`) は `conversations.history` API のみを呼び、**スレッド配下の子返信 (thread reply) を一切取り込んでいない**。Slack API 仕様 (`docs.slack.dev/reference/methods/conversations.history`) では `conversations.history` はチャンネル直系の親メッセージ (top-level message) のみを返し、子返信は専用エンドポイント `conversations.replies(channel, thread_ts)` で取得する必要がある。

### Claude Code / Codex CLI ユーザー視点の失敗モード

opshub MCP 経由でアシスタント 14 Skill を使うとき、構造的に取りこぼしが発生する:

| Skill | 失敗モード |
|---|---|
| `find-document` / `research` / `recall.search` | 「#foo であの議論」と聞かれたとき、議論本体がスレッドで行われていた場合、親投稿しか hit せず文脈が欠落する。FTS5 substring match (ADR-0028 trigram) も embedding semantic recall も、source 自体が ingest されていない以上回復不能 |
| `reply-draft` | Slack message を返信元 (`reply_to_source_id`) として指定したとき、その親が thread だった場合、兄弟返信 (同 `thread_ts` を持つ他の子返信) を考慮できず、文脈不足の draft しか出せない |
| `personal-brief` / `next-actions` | 自分が thread 内で受けた action item や ping が抽出経路に乗らない (Slack 本文が parent に無い場合) |
| `meeting-followup` / `inbox-triage` | 議事録共有が thread で続く Slack 運用 (channel topic は静的、議論は thread に流れる) で議論本体が抜ける |

### Gmail との不均衡

対称な position にある Gmail connector は [ADR-0010 §Phase 14 改訂 (k) 不変条件 3](0010-connector-contract.md#phase-14-改訂-k--本文抽出契約-outlook-流継承-gmail--calendar) で:

> threadId は field 保持、replied_to link 化は Phase 15+ defer — Gmail `threadId` は `SourceObserved` の field として保持 (event store immutability 整合、thread 単位の dynamic 集約は projection 層の責務として後続 Phase)。Gmail thread 単位の source_type (`gmail_thread` 等) は **作らない** (message 単位 `gmail_message` で固定、Outlook と symmetric)

と「**message 単位で全 message を取り込み、thread aggregation は projection 層に defer**」する方針を pin している。Outlook (`ms365_outlook`) も同様に message 単位。Slack だけが **そもそも子返信を取り込めていない** 状態で、Gmail / Outlook と非対称。

### 設計上の制約

- **event store immutability 不変** ([ADR-0002](0002-event-sourced-architecture.md)) — `SourceObserved` event は append-only。thread 単位で record を持つと子返信 append のたびに thread record を再書きする必要があり、event log が無限増殖する。Gmail / Outlook が message 単位を選んだ根拠と同根
- **形 A 不変** ([ADR-0004 §決定 (a)](0004-agent-runtime-boundary.md)) — opshub は能動性を持たない。Bolt / Slack Events API による push 経路 (子返信 webhook) は段階 1-4 通知層の領域で、本 ADR 段階では `conversations.replies` polling のみ
- **外部書き戻し禁止 不変** ([ADR-0010 §禁止事項 7](0010-connector-contract.md)) — 本 ADR は read-only 経路の拡張。Slack 側に reply を投稿する経路は持たない
- **rate limit budget** — `conversations.replies` は Slack Tier 3 (`50+ /min`)。`reply_count = 0` の親では追加 API call が発生しないため、active な thread 数が増えるほど budget を消費する。Phase 15 で導入した `opshub.connectors.slack._retry.retry_on_rate_limit` helper ([#377](https://github.com/ozzy-labs/opshub/issues/377) → PR [#378](https://github.com/ozzy-labs/opshub/pull/378)) を新 call site も share
- **cursor 一貫性** ([ADR-0010 §責務 4](0010-connector-contract.md)) — Slack connector の resume cursor は `cursor[channel_id] = ts` (per-channel 最大 ts)。thread reply は親 ingest と同期取得することで、partial-progress checkpoint が thread reply 後に advance する natural resume を維持する

## Decision

Slack thread reply の取り込み方針を、Gmail / Outlook と symmetric な **「子返信も独立した `slack_message` source row として ingest し、`thread_ts` を field 保持」** に確定する。thread 単位の集約 source_type (`slack_thread` 等) は **作らない**。

7 軸で意思決定する。

### (a) 取り込み単位: message 単位 (Gmail / Outlook と symmetric)

子返信は親メッセージと同じく **独立した `slack_message` source row** として ingest する。thread 単位の source_type (`slack_thread`) は **新設しない**。

- Gmail unit = message 単位 (`gmail_message`、[ADR-0010 §Phase 14 改訂 (l) 不変条件 1](0010-connector-contract.md#phase-14-改訂-l--gmail-unit--calendar-unit--label--attendee-表現契約)) と symmetric
- Outlook unit = message 単位 (`ms365_outlook`) と symmetric
- thread 単位の動的集約 (親 + 全返信を 1 record にまとめる) は **projection 層の責務** として後続 Phase に defer。本 ADR では projection 層の集約も実装しない (Gmail と同じ defer 状態)

### (b) Fetcher 拡張: `reply_count > 0` の親について `conversations.replies` を追加 fetch

`SlackFetcher.fetch_messages` は `conversations.history` で取得した各メッセージについて以下を実施する:

1. メッセージの `reply_count` (Slack API field、`int`、子返信を持たない親メッセージは `0` または key 不在) を確認
2. `reply_count > 0` ならば `conversations.replies(channel=<channel_id>, ts=<thread_ts>)` を追加で呼ぶ
3. `conversations.replies` のレスポンスは `messages[0]` が親メッセージ自身 (parent message)、`messages[1:]` が子返信。**親自身は duplicate ingest 回避のため skip**

duplicate dedup には `external_id = f"{channel_id}:{ts}"` を natural key として使う (各 Slack message の `ts` は per-channel 一意、Slack 公式仕様)。`sources.external_id` UNIQUE 制約により親メッセージが二重取り込みされても idempotent に弾かれる構造を維持する。

### (c) Mapper: `thread_ts` を `RawSlackMessage` field として追加、`SourceObserved.raw` で保持

`RawSlackMessage` dataclass に `thread_ts: str | None` field を追加する。値の解釈:

| 状況 | `thread_ts` の値 |
|---|---|
| 親メッセージ (返信を持たないか持つかに関わらず top-level) | `raw["thread_ts"]` が key 不在、または `raw["thread_ts"] == raw["ts"]` (Slack の慣習で親自身を thread root として表現する場合) のいずれかで判定。前者は `None`、後者は `ts` と同値を field 保持 |
| 子返信 (thread reply) | `raw["thread_ts"]` が親の `ts` を指す。mapper はそのまま field 保持 |

`SourceObserved` event の `raw` (JSON column) には Slack API レスポンスを verbatim 保持する既存契約を維持し、`thread_ts` も `raw["thread_ts"]` として自然に保持される。

**`sources` projection には新 column を追加しない**。理由:

- event store immutability + projection 追加の two-fold cost を回避 (migration 不要)
- `thread_ts` を使う消費者は `recall.search` / `find-document` ではなく `reply-draft` が中心で、消費側は `SourceObserved.raw["thread_ts"]` を読めば足りる
- thread 単位の集約クエリ (「この thread に属する全 source を recall」) は projection 層を追加するときに別 ADR で議論する

### (d) Cursor 戦略: 2 軸 compound cursor + late reply polling + activity window pruning (Phase 20 revised)

`SlackFetcher` の resume cursor は **2 軸 compound 構造** に拡張する (Phase 20-B `connector_cursors.cursor_value` schema 改訂、PR [#473](https://github.com/ozzy-labs/opshub/pull/473)):

```json
{
  "channels": {"C012345": "1717000000.000100", ...},
  "threads":  {"C012345:1717000000.000100": "1717100000.000500", ...}
}
```

- `channels` 軸 = 旧 Phase 7 互換の `{channel_id: 最大 ts}` (channel-level resume)
- `threads` 軸 = `{f"{channel_id}:{parent_ts}": 最大 reply ts}` (per-thread resume)

両軸とも `_max_ts(prior, new)` で monotonic に advance し、片方の axis を mid-sync で advance しても他方を巻き戻さない。compound envelope の dump は axes を sorted で deterministic 出力する。

旧 flat-dict cursor (`{channel_id: ts}`) は Phase 20-B で **silent migration を持たず** `ConfigError` で reject し、`opshub projections rebuild` を案内する (pre-userbase posture、ADR-0034 §migration 系列を継承)。

#### Phase 1 (channel history) と Phase 2 (thread late-reply polling) の 2 phase sync

Phase 20-A ([#474](https://github.com/ozzy-labs/opshub/pull/474)) で `SlackFetcher.fetch_messages` を拡張し、Phase 20-C ([#476](https://github.com/ozzy-labs/opshub/pull/476)) で Phase 2 polling を追加した。1 回の `opshub slack sync` は次の 2 phase を順に流す:

1. **Phase 1 — channel history + snapshot replies**
   - `conversations.history(channel, oldest=channels_cursor)` で親メッセージを取り直す
   - 各親について `latest_reply` を保持していれば (= `reply_count > 0` の thread)、その場で `conversations.replies(channel, ts=thread_ts)` を呼び `messages[0]` (親自身) を skip して child のみ yield
   - reply yield 時の cursor element は **親の `ts`** (reply ts ではなく) を採用し、reply ts が親 ts 間の隙間を skip しないようにする
   - parent ingest と同時に `threads` 軸へ `latest_reply_ts` を seed する (Phase 2 が同じ reply を二重取得しないため)
2. **Phase 2 — late-reply polling for known threads**
   - `threads` 軸に entry を持つ各 `(channel, thread_ts)` について、`oldest=threads_cursor + inclusive=False` で `conversations.replies` を再 call し、Phase 1 以降に投稿された late reply のみ ingest する
   - 各 reply ingest 毎に `threads` 軸を `_max_ts(prior, reply.ts)` で advance
   - happy path 完了後、activity window (下記) を超えた entry を `threads` 軸から prune する

これにより、ADR §Context で挙げた「親 ingest 後に投稿される子返信が永久に取りこぼされる」失敗モードが解消される。

#### Activity window pruning (`thread_activity_window`、default 30d)

cold thread を永久に polling し続けないために、`[connectors.slack] thread_activity_window` (default `"30d"`、CLI `--thread-activity-window` / env `OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW` で上書き) を設けた:

- Phase 2 polling 開始時、`threads` 軸の entry のうち `now - threads_cursor > thread_activity_window` のものを skip
- Phase 2 happy path 完了時に同 entry を `threads` 軸から prune (mid-iteration crash は prune を実行せず entry を残す = resume safety)
- `parse_since` 共通経路 ([ADR-0036](0036-slack-sync-date-floor.md)) を流用、`"all"` 指定で prune 無効化

window は **pruning だけに作用する floor** で、ADR-0036 の `sync_since` (history floor) とは独立。

#### Cold thread reactivation は limitation (将来オプション)

`thread_activity_window` 経過後の thread に late reply が投稿されても、`threads` 軸から prune されているため Phase 2 は触らず、Phase 1 は親 ts が `channels_cursor` を超えないため再取得しない。これは **本 ADR scope の意図された limitation** で、cold thread = inactive と判断する SaaS-side 慣習に整合する。reactivation が必要なケースは `opshub projections rebuild` で cursor をリセットするか、将来オプションとして以下を残す:

- `reply-draft` 等の skill で「特定 source の cold thread を強制再 fetch」と判定されたとき、その source の `thread_ts` に対して `conversations.replies` を on-demand 再 fetch する経路
- `opshub slack sync --include-cold-threads` のような opt-in flag (`thread_activity_window` を bypass する run-time override)

両 option とも本 ADR scope 外で、必要性が顕在化した時点で別 ADR を切る。

### (e) Rate limit budget: `_retry.retry_on_rate_limit` helper を share

`conversations.replies` は Slack Tier 3 (`50+ /min`、Slack 公式 rate limit docs)。新 call site も `opshub.connectors.slack._retry.retry_on_rate_limit` helper ([#377](https://github.com/ozzy-labs/opshub/issues/377) → PR [#378](https://github.com/ozzy-labs/opshub/pull/378)) を経由する。

- 3 attempts、`Retry-After` honoured、fallback 1s / 2s / 4s exponential
- 既存 3 call site (`SlackFetcher._call_history` + `conversations._call_history_oldest` + `conversations._call_list`) に並ぶ 4 つ目の share 利用者となる
- helper 1 箇所更新で全 call site の retry policy が同期される構造 (#377 で意図された設計) を維持

active channel × thread 比率次第で API call が増える可能性に対し、実装 Phase で `--max-threads-per-sync` cap (per-sync で `conversations.replies` を叩く上限) を検討する余地を残す (本 ADR では cap 数値も命名も pin しない)。

### (f) 実装 scope: Phase 20 で landed (4 sub-PR)

本 ADR §(a)-(e) を Phase 20 ([epic #465](https://github.com/ozzy-labs/opshub/issues/465)) で 4 sub-PR に分割して着地させた。具体的な PR / 検証範囲は `## Implementation plan (landed)` を参照:

1. Phase 20-A (PR [#474](https://github.com/ozzy-labs/opshub/pull/474)) — fetcher 拡張 + `thread_ts` field + `_call_replies` helper share
2. Phase 20-B (PR [#473](https://github.com/ozzy-labs/opshub/pull/473)) — `connector_cursors.cursor_value` 2 軸 compound schema
3. Phase 20-C (PR [#476](https://github.com/ozzy-labs/opshub/pull/476)) — Phase 2 late-reply polling + `thread_activity_window` prune
4. Phase 20-D (本 PR) — ADR §(d) revise + landed 化 + docs 一括更新

§(a) 取り込み単位 / §(b) Fetcher 拡張 / §(c) Mapper / §(e) Retry helper share の不変条件は維持し、§(d) は cursor 戦略を 2 軸 compound + late-reply polling + activity window prune に書き換えた (旧「late reply は scope 外」記述を撤回)。`--max-threads-per-sync` cap は採用せず、`thread_activity_window` (Phase 20-C) で rate limit budget を抑える設計に確定。

### (g) 非対称設計の不採用: `slack_thread` source_type 新設しない

「親 + 全返信を 1 source row に集約した `slack_thread` source_type を新設する」案を **不採用** とする。理由:

- event store immutability と摩擦 (返信 append 毎に thread record を再書きすると event log が無限増殖)、上記 §設計上の制約と同根
- Gmail (`gmail_message`、ADR-0010 §Phase 14 改訂 (k) 不変条件 3) / Outlook (`ms365_outlook`) と非対称になり、アシスタント 14 Skill 側で source_type 別に分岐 logic を増やす必要が出る (recall を均一に扱えなくなる)
- 動的集約は projection 層の責務として後続 Phase に置けば、`sources` projection を touch せず追加実装できる

詳細は §採用しなかった代替 §2 を参照。

## 不変条件

本 ADR で確立する不変条件 (Phase 20 revised):

1. **取り込み単位は message 単位 (`slack_message`)** — 親も子返信も同じ source_type 1 種で表現。thread 単位の source_type (`slack_thread`) は作らない。Gmail / Outlook の message 単位と symmetric ([ADR-0010 §Phase 14 改訂 (l) 不変条件 1](0010-connector-contract.md))
2. **`thread_ts` は field 保持** — `SourceObserved.raw["thread_ts"]` に Slack API verbatim で保持し、`RawSlackMessage.thread_ts: str | None` mapper field でも明示保持する (Phase 20-A landed)。`sources` projection に新 column は追加しない (Phase 15+ で projection 層の thread aggregation を切るときに別 ADR で議論)
3. **duplicate dedup は `external_id = f"{channel_id}:{ts}"` の UNIQUE 制約に委ねる** — `conversations.replies` レスポンスの `messages[0]` (親自身) を mapper 側で skip しなくても、`sources.external_id` UNIQUE で idempotent に弾かれる構造を維持
4. **resume cursor は 2 軸 compound schema (`channels` + `threads`)** — Phase 20-B で `{"channels": {...}, "threads": {...}}` envelope に拡張。両軸とも `_max_ts(prior, new)` で monotonic、片軸の advance が他軸を巻き戻さない。旧 flat-dict schema (`{channel_id: ts}`) は silent migration せず `ConfigError` で `opshub projections rebuild` を案内 (pre-userbase posture)
5. **late thread reply は Phase 2 polling で追従、activity window 経過後の cold thread は prune** — `thread_activity_window` (default 30d、`parse_since` 経路で `"all"` 指定可) を超えた `threads` 軸 entry は happy path 完了時に prune される。cold thread reactivation は本 ADR の意図された limitation で `opshub projections rebuild` か将来 opt-in flag で対応
6. **`conversations.replies` の retry は `_retry.retry_on_rate_limit` helper を share** — 新 call site (Phase 1 の `_call_replies` + Phase 2 polling) も `oldest` / `inclusive` kwarg を helper の signature に乗せて 4 番目以降の share 利用者となる (PR [#378](https://github.com/ozzy-labs/opshub/pull/378) で確立した「policy 1 箇所更新で全 call site 同期」原則を維持)

## Consequences

### Positive

- **Gmail / Outlook との対称性回復** — message 単位 + thread field 保持の 3 connector 統一が成立。アシスタント 14 Skill 側で source_type 別に分岐 logic を増やす必要なし
- **`find-document` / `recall.search` / `search` で thread 子返信が hit** (本 ADR 受諾 + §(f) 実装 Phase 完了後) — operator の「#foo であの議論」相当の Skill リクエストが thread 内容まで到達
- **`reply-draft` の文脈精度向上** — 兄弟返信 (同 `thread_ts` の他の子返信) を `recall.search` 経由で参照できるようになる
- **`sources` projection schema 不変** — migration 不要、Phase 17 候補時の実装コストが下がる

### Negative / Trade-offs

- **API call 増** — Phase 1 で `reply_count > 0` の親 1 件あたり 1 回、Phase 2 polling で `threads` 軸 entry 1 件あたり 1 回の `conversations.replies` call が追加発生する。active channel × thread 数次第で rate limit budget (Tier 3、`50+ /min`) を圧迫する可能性があるため、`thread_activity_window` (Phase 20-C landed、default 30d) で polling 対象を窓内 entry に絞る経路を持つ
- **cold thread reactivation の structural drop** — `thread_activity_window` を経過した thread に late reply が投稿されても本 ADR §(d) 経路では検知できない (`threads` 軸から prune 済み、`channels` 軸は親 ts を超えない)。`opshub projections rebuild` で cursor をリセットするか、将来オプションの `--include-cold-threads` flag / skill 経由 on-demand 再 fetch で対応
- **同期取得の latency** — `conversations.replies` を Phase 1 で親 ingest と同期 (シリアル) 取得し、Phase 2 polling も channel × thread 順に直列に流すため、active な channel が多い workspace の cold-start sync に追加 latency が乗る。並列化 (asyncio / thread pool) は本 ADR scope 外、必要性が顕在化した時点で別 ADR

## Implementation plan (landed)

Phase 20 ([epic #465](https://github.com/ozzy-labs/opshub/issues/465)) で本 ADR の実装作業を 4 つの sub-PR に分割して着地させた。

1. **Phase 20-A** — Fetcher 拡張 + `RawSlackMessage.thread_ts` field landed (PR [#474](https://github.com/ozzy-labs/opshub/pull/474), closes #466)
    - `SlackFetcher._iter_channel` で `latest_reply` 持ち parent について `conversations.replies` を Phase 1 で追加 fetch、`messages[0]` (parent 自身) skip
    - 新 `_call_replies` が `_retry.retry_on_rate_limit` helper の 4 番目の share 利用者となる
    - Reply yield 時の cursor element は親の `ts` を採用 (reply ts が親 ts 間隙を skip しないため)
    - `fetch_messages` に optional `excludes` kwarg 追加 → 親が `excludes.channels` / `excludes.senders` 該当時は `conversations.replies` を skip (API budget guard)
    - integration test (thread happy path / mixed threads / idempotent re-run) + unit test (`thread_ts` field set / cursor element / 429 retry / excludes 該当 skip)
2. **Phase 20-B** — `connector_cursors.cursor_value` compound schema landed (PR [#473](https://github.com/ozzy-labs/opshub/pull/473), closes #467)
    - 旧 flat-dict `{channel_id: ts}` を `{"channels": {...}, "threads": {...}}` envelope に張り替え
    - 旧 shape は `ConfigError` で reject し `opshub projections rebuild` を案内 (pre-userbase posture、silent migration なし)
    - DB schema (migration) は touch せず、`cursor_value` TEXT column の JSON 値のみ拡張
    - `SlackFetcher.fetch_messages` の signature (`cursor_per_channel=`) は維持し、20-A と並行実装可能
    - unit test (round-trip / 両軸 empty / legacy reject / missing axis reject / deterministic dump) + integration test (legacy cursor reject + monotonicity)
3. **Phase 20-C** — Phase 2 late-reply polling + activity window pruning landed (PR [#476](https://github.com/ozzy-labs/opshub/pull/476), closes #468)
    - `SlackFetcher.fetch_thread_replies(channel_id, thread_ts, oldest_reply_ts)` を新規追加し、`threads` 軸 cursor を起点に late reply のみ ingest
    - `[connectors.slack] thread_activity_window` (default 30d) + CLI `--thread-activity-window` flag + env `OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW`
    - `_call_replies` に `oldest` kwarg 追加し `inclusive=False` で境界 reply の二重 emit を防止
    - Phase 1 で parent ingest 時に `threads` 軸を `latest_reply_ts` で seed (Phase 2 が snapshot を再 fetch しないため)
    - Window prune は happy path のみ実行 (mid-iteration crash は cursor を残し resume-safe)
    - unit test 13 件 (`test_thread_polling.py`) + integration test 3 件 (late-reply ingest / prune / 429 retry)
4. **Phase 20-D** — 本 ADR §(d) revise + landed 化 + docs 同期 (本 PR, closes #469)
    - 本 ADR §(d) を late-reply 対応の最終形に書き換え、`## Implementation plan` を deferred → landed に昇格
    - `CLAUDE.md` / `AGENTS.md` / `docs/assistant-agent.md` / `docs/architecture.md` / `docs/troubleshooting.md` / `docs/upgrading.md` を一括更新

`--max-threads-per-sync` cap は本 Phase では採用せず、`thread_activity_window` が prune 経路を提供することで rate limit budget を抑える設計に確定した。

## 採用しなかった代替

### 1. 取り込まない (現状維持)

却下理由:

- Gmail / Outlook との対称性が崩れたまま (本 ADR §Context 「Gmail との不均衡」)
- `reply-draft` が thread 内文脈を取りこぼし続け、operator の「これに返信案考えて」要求に応えられない構造的失敗が固定化
- `find-document` / `recall.search` で「#foo で議論したあの件」が hit しないため、アシスタント 14 Skill のうち read 自律 OK の Skill 群 (10 件中、Slack 経由情報を扱うすべて) の質が下がる
- opshub の pre-userbase スタンス ([AGENTS.md §設計判断のスタンス](../../AGENTS.md)) を踏まえても、実ユーザー獲得時に必ず再起票される問題で、defer する利益が小さい

### 2. `slack_thread` source_type を新設して親 + 全返信を 1 record に集約

却下理由:

- event store immutability と摩擦 — 返信 append 毎に thread record を再書きすると event log が無限増殖、`SourceObserved` の append-only 契約と相容れない ([ADR-0002](0002-event-sourced-architecture.md))
- Gmail [ADR-0010 §Phase 14 改訂 (k) 不変条件 3](0010-connector-contract.md#phase-14-改訂-k--本文抽出契約-outlook-流継承-gmail--calendar) (`gmail_thread` は作らない、message 単位で固定) と非対称
- アシスタント 14 Skill (`find-document` / `research` / `reply-draft` / etc.) が source_type 別に分岐 logic を持つ必要が出る (`gmail_message` は message 単位だが `slack_thread` は集約、等)
- thread 単位の動的集約は projection 層の責務として後続 Phase で切れば、`sources` projection / event schema を touch せず追加実装できる (本 ADR §(a) + §(c) で defer)

### 3. Bolt / Slack Events API で push 経路を張る

却下理由:

- 能動性混入 — Slack Events API は HTTP webhook 受信が前提で、opshub が常時起動 server を要求される (形 A 違反、[ADR-0004 §決定 (a)](0004-agent-runtime-boundary.md))
- 受信 server の運用コスト (TLS endpoint + Slack app の Event Subscriptions 設定 + retry semantics) が CLI / MCP stdio surface の現状契約 ([ADR-0006](0006-cli-first-mvp.md) / [ADR-0022](0022-mcp-server-surface.md)) と乖離
- 本 ADR では `conversations.replies` polling のみで `conversations.history` の poll 経路と整合させ、能動性なしの read-only 契約を維持

### 4. thread root だけ ingest、子返信は body に追記する

却下理由:

- `recall.search` の semantic embedding が long-form body に対して粒度を失う ([ADR-0012](0012-embedding-strategy.md))。message 単位 1 chunk のほうが retrieval 精度が高い
- 子返信ごとの author / ts / permalink を表現できなくなり、`reply-draft` で「誰の発言を引用するか」を Skill が選べない
- body の長さが Slack 公式の `[truncated]` cap に張り付くと、最新の子返信が削られる構造的欠落が発生

## 関連

- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — message 単位 ingest が append-only 契約の根拠
- [ADR-0004: Agent Runtime Boundary](0004-agent-runtime-boundary.md) — 形 A (能動性なし) の根拠、Events API push 経路を採用しない理由
- [ADR-0010: Connector Contract](0010-connector-contract.md) — §Phase 14 改訂 (k) 不変条件 3 (Gmail thread の field 保持) と symmetric な決定。本 ADR は Slack 側に同パターンを適用
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md) — message 単位 ingest が embedding 粒度の根拠
- [ADR-0018: Slack Connector Token Principal](0018-slack-token-principal.md) — Slack connector の auth principal、本 ADR は新 scope を要求しない (既存 token で `conversations.replies` は呼べる)
- [ADR-0022: MCP Server Surface](0022-mcp-server-surface.md) — `search` / `recall.search` / `reply-draft` が thread reply を hit する経路の上流契約
- [ADR-0028: FTS5 sources_fts tokenizer choice (trigram + short-query LIKE fallback)](0028-fts5-japanese-tokenizer.md) — thread reply 本文も同 FTS5 tokenizer 経路を共有
