# 0030. Slack Thread Reply Ingestion Policy

- Status: Accepted
- Date: 2026-06-02
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

### (d) Cursor 戦略: 親 ingest と同期取得、thread 専用 cursor は持たない

`SlackFetcher` の resume cursor (`cursor[channel_id] = 最大 ts`) は現行維持。thread reply 専用 cursor は導入しない。

- 親メッセージ ingest と同じ for-loop の中で `conversations.replies` を同期取得する
- partial-progress checkpoint (caller がメッセージ単位で cursor を commit する既存パターン) は親 ingest 後に advance するため、thread reply 取得失敗時の resume も自然に成立する (次回 sync で親が同じ cursor 位置から再取得され、reply 取得が再試行される)

#### Late thread reply の取り扱い (将来オプション)

`conversations.replies` は親 ingest 時点でのスナップショットしか取得できない。親が thread として ingest された **後で** 追加の子返信が投稿されるケース (late reply) は、現行 cursor strategy では検知できない (親の `ts` は不変なので resume cursor を超えない)。

これは本 ADR では **解決しない**。将来オプションとして以下を残す:

- `reply-draft` 等の skill で「特定 source の late reply を考慮した最新化が必要」と判定されたとき、その source の `thread_ts` に対して `conversations.replies` を on-demand 再 fetch する経路
- `opshub connector sync slack --include-late-thread-replies` のような opt-in 再 sync flag (active thread 集合を別 cursor で再走査)

両 option とも本 ADR §(f) の実装 scope 外で、Phase 17 候補時に必要性を再評価する。

### (e) Rate limit budget: `_retry.retry_on_rate_limit` helper を share

`conversations.replies` は Slack Tier 3 (`50+ /min`、Slack 公式 rate limit docs)。新 call site も `opshub.connectors.slack._retry.retry_on_rate_limit` helper ([#377](https://github.com/ozzy-labs/opshub/issues/377) → PR [#378](https://github.com/ozzy-labs/opshub/pull/378)) を経由する。

- 3 attempts、`Retry-After` honoured、fallback 1s / 2s / 4s exponential
- 既存 3 call site (`SlackFetcher._call_history` + `conversations._call_history_oldest` + `conversations._call_list`) に並ぶ 4 つ目の share 利用者となる
- helper 1 箇所更新で全 call site の retry policy が同期される構造 (#377 で意図された設計) を維持

active channel × thread 比率次第で API call が増える可能性に対し、実装 Phase で `--max-threads-per-sync` cap (per-sync で `conversations.replies` を叩く上限) を検討する余地を残す (本 ADR では cap 数値も命名も pin しない)。

### (f) 実装 scope: 本 ADR は方針 pin のみ、実装は別 issue で起票

本 ADR は **方針 pin のみ**。以下の実装作業は **本 ADR の scope 外** とし、Phase 17 候補または Post-Phase 15 Maintenance 節の別 issue で起票する:

1. `SlackFetcher.fetch_messages` で `reply_count > 0` の親について `conversations.replies` を追加 call
2. `RawSlackMessage` に `thread_ts: str | None` field 追加
3. `SlackMessageMapper` で `SourceObserved.raw["thread_ts"]` 保持
4. `_call_replies` を新規追加し `_retry.retry_on_rate_limit` helper を経由
5. `tests/integration/test_phase7_slack_sync.py` に thread happy path / late reply / rate limit のシナリオ追加
6. `--max-threads-per-sync` cap の必要性検討
7. `CLAUDE.md` / `AGENTS.md` の Slack 取り込み単位記述更新 (本 PR の doc 同期 §で初期化済、実装 Phase で詳細追記)

実装 PR は本 ADR を参照し、§(a)-(e) の不変条件を変更しない範囲で着地させる。

### (g) 非対称設計の不採用: `slack_thread` source_type 新設しない

「親 + 全返信を 1 source row に集約した `slack_thread` source_type を新設する」案を **不採用** とする。理由:

- event store immutability と摩擦 (返信 append 毎に thread record を再書きすると event log が無限増殖)、上記 §設計上の制約と同根
- Gmail (`gmail_message`、ADR-0010 §Phase 14 改訂 (k) 不変条件 3) / Outlook (`ms365_outlook`) と非対称になり、アシスタント 14 Skill 側で source_type 別に分岐 logic を増やす必要が出る (recall を均一に扱えなくなる)
- 動的集約は projection 層の責務として後続 Phase に置けば、`sources` projection を touch せず追加実装できる

詳細は §採用しなかった代替 §2 を参照。

## 不変条件

本 ADR で確立する不変条件:

1. **取り込み単位は message 単位 (`slack_message`)** — 親も子返信も同じ source_type 1 種で表現。thread 単位の source_type (`slack_thread`) は作らない。Gmail / Outlook の message 単位と symmetric ([ADR-0010 §Phase 14 改訂 (l) 不変条件 1](0010-connector-contract.md))
2. **`thread_ts` は field 保持** — `SourceObserved.raw["thread_ts"]` に Slack API verbatim で保持。`sources` projection に新 column は追加しない (Phase 15+ で projection 層の thread aggregation を切るときに別 ADR で議論)
3. **duplicate dedup は `external_id = f"{channel_id}:{ts}"` の UNIQUE 制約に委ねる** — `conversations.replies` レスポンスの `messages[0]` (親自身) を mapper 側で skip しなくても、`sources.external_id` UNIQUE で idempotent に弾かれる構造を維持
4. **thread reply 専用 cursor を持たない** — `cursor[channel_id] = 最大 ts` 1 軸で resume する既存 contract を維持。late reply は本 ADR scope 外
5. **`conversations.replies` の retry は `_retry.retry_on_rate_limit` helper を share** — 新 call site 専用の retry 実装を作らない (PR [#378](https://github.com/ozzy-labs/opshub/pull/378) で確立した「policy 1 箇所更新で全 call site 同期」原則を維持)

## Consequences

### Positive

- **Gmail / Outlook との対称性回復** — message 単位 + thread field 保持の 3 connector 統一が成立。アシスタント 14 Skill 側で source_type 別に分岐 logic を増やす必要なし
- **`find-document` / `recall.search` / `search` で thread 子返信が hit** (本 ADR 受諾 + §(f) 実装 Phase 完了後) — operator の「#foo であの議論」相当の Skill リクエストが thread 内容まで到達
- **`reply-draft` の文脈精度向上** — 兄弟返信 (同 `thread_ts` の他の子返信) を `recall.search` 経由で参照できるようになる
- **`sources` projection schema 不変** — migration 不要、Phase 17 候補時の実装コストが下がる

### Negative / Trade-offs

- **API call 増** — 親メッセージに `reply_count > 0` がある分だけ追加の `conversations.replies` call が発生。active channel + thread 数次第で rate limit budget を圧迫する可能性がある (Tier 3、`50+ /min`)。実装 Phase で `--max-threads-per-sync` cap を検討
- **late thread reply の取りこぼし** — 親 ingest 後に投稿される子返信は本 ADR §(d) 経路では検知できない。on-demand 再 fetch / `--include-late-thread-replies` opt-in flag を将来オプションとして残す
- **同期取得の latency** — `conversations.replies` を親 ingest と同期 (シリアル) 取得するため、active な channel の cold-start sync に追加 latency が乗る。並列化 (asyncio / thread pool) は本 ADR scope 外、実装 Phase で必要性が顕在化した時点で別 ADR

## Implementation plan (deferred)

本 ADR §(f) で defer 確定した実装作業を Phase 17 候補 / Post-Phase 15 Maintenance で扱うときの想定タスク:

1. **Fetcher 拡張** — `SlackFetcher._fetch_channel` に `reply_count > 0` の分岐追加、`_call_replies` を `_retry.retry_on_rate_limit` 経由で実装
2. **Mapper 拡張** — `RawSlackMessage.thread_ts: str | None` field 追加、`SlackMessageMapper` で `raw["thread_ts"]` を保持
3. **Event field 保持** — `SourceObserved.raw["thread_ts"]` の field 保持 (mapper layer で対応、event schema は touch せず)
4. **Retry helper share** — `_call_replies` が `opshub.connectors.slack._retry.retry_on_rate_limit` を経由することを test pin (既存 3 call site の test を参考)
5. **Integration test 追加** — `tests/integration/test_phase7_slack_sync.py` に以下を追加:
    - thread happy path (親 + 子返信 2 件で `slack_message` 3 件 ingest)
    - late reply (親 ingest 後に追加 reply が来ても resume cursor が advance しない確認)
    - rate limit (`conversations.replies` 側の 429 が helper 経由で retry される確認)
6. **`--max-threads-per-sync` cap 検討** — per-sync で `conversations.replies` を叩く上限。命名と数値は実装 Phase で決定
7. **Docs 同期** — `docs/assistant-agent.md` の Slack 取り込み単位記述に「thread reply も対象」を反映 (本 ADR の doc 同期 §では Post-Phase 15 Maintenance 追記のみで、詳細は実装 PR で追加)

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
