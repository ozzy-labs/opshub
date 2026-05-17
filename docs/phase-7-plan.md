# Phase 7 Implementation Plan

> Status: Draft (planning). Last reviewed: 2026-05-17. Scope: Connectors Wave 2 = Slack + Microsoft 365 + Box connectors (Phase 3 framework + ADR-0010 + ADR-0014 + ADR-0005 を再利用)。Multi-machine sync (principles.md §Open Q #5) / `links` projection 本実装 / observability layer / 共通 OAuth helper refactor は Phase 7.x / 8 で別途。

Phase 7 の目的は **Connectors Wave 2** を Phase 1-6 の foundation 上に追加すること。Phase 3 で確立した connector framework (ADR-0010 connector contract + `connectors/<name>/` package structure + `opshub connector auth set <namespace>:<name>` + `opshub connector sync <name>` + `sources` projection + `connector_cursors` projection) を再利用して、Slack / Microsoft 365 / Box の 3 SaaS connector を投入する。

これにより agent が周囲の業務 context (Slack の channel 会話 / Microsoft 365 のカレンダー予定・OneDrive 変更・Outlook メッセージ / Box の file events) を読み込めるようになり、Phase 5 `opshub brief` と Phase 6 `opshub propose` の input source surface が拡張される。Phase 4 の semantic recall + Phase 5/6 の LLM 経路はそのまま動作 — Phase 7 は ingestion surface のみの拡張で、上位レイヤーには touch しない。

各 connector は ADR-0005 (External Content Min) を遵守して metadata + 短い summary (~200 chars) のみを `sources` projection に persist する。全文 body は取り込まない。token / OAuth credentials は ADR-0014 (`core/secrets` + keyring + env var override) を再利用し、Phase 5 A5 で generic 化された `opshub connector auth set connector:<name>` の経路で保存される。

Phase 5 で freeze 済の `LLMClient` Protocol / Phase 6 の Proposal flow / Phase 4 frozen Embedder / VectorStore Protocol は **本 phase で一切変更しない**。Phase 7 は Phase 3 framework の純粋な拡張のみ。

## 1. 着手前に解消する TODO

Phase 6 完了時点で Phase 7 着手前に解消が必要な事項は **なし**。Phase 1-6 で確立した実装契約 (uow_factory / `EventStore.append(event, conn)` / `Projector.apply(event, conn)` / `projections/registry.all_projections()` SSOT / `AllEvent` discriminated union / `cli/* import` whitelist / atomic failing-projector test / `core/secrets` + ADR-0014 token storage / Pluggable backend Protocol freeze + factory pattern / `core/sanitise.sanitise_error_message` / `<source>` delimiter wrap + html.escape) は Phase 7 も全て継承する。

**確定済み事項** (Phase 7 着手前に確定):

1. **Scope の絞り込み** — Phase 7 MVP = Slack + Microsoft 365 + Box の 3 connector + closeout。`links` projection 本実装 / multi-machine sync (principles.md §Open Q #5 残置) / observability layer / 共通 OAuth helper refactor は Phase 7.x / 8 で別 plan
2. **Default state** — 全 connector は opt-in。`[connectors.<name>] enabled = false` を default。operator が `opshub connector auth set ...` + config 編集で有効化
3. **取り込み範囲 (ADR-0005 準拠)**:
   - **Slack**: 指定 channel の最近 N 件 message → summary (text 前 200 chars) + author display name + permalink URL + timestamp。本文全体は取り込まない
   - **Microsoft 365**: Calendar events (subject + start/end + 参加者 N 名), OneDrive files (name + path + modified_at), Outlook messages (subject + body preview ~200 chars + sender)
   - **Box**: file/folder events (item name + event type + path + modified_at + actor)
4. **Token storage** — `core/secrets` + keyring (ADR-0014) を再利用。key 規約 `connector:<name>:<purpose>` (例: `connector:slack:bot_token`, `connector:ms365:refresh_token`, `connector:box:refresh_token`)。env var override `OPSHUB_CONNECTOR_<NAME>_<PURPOSE>` (例: `OPSHUB_CONNECTOR_SLACK_BOT_TOKEN`)
5. **OAuth handling** — Microsoft 365 + Box は OAuth 2.0 が必要。**paste-code flow を default** とする (operator が browser で auth URL を踏み、redirect 後の URL から code を CLI に paste)。device flow は connector 側が support している場合のみ opt-in。refresh token は keyring 保存、access token は短命 (in-memory only)。token 失効時は自動 refresh、refresh 失敗で `ConnectorFailed` event + 再 auth 案内
6. **CI mock 戦略** — 全 connector の test は SDK / HTTP を fully mock。CI で実 SaaS API / OAuth endpoint を叩かない (Phase 3-6 と同規律、`pytest.importorskip` で SDK 不在環境では skip)
7. **Pagination / cursor** — 各 connector の sync は cursor-based。Phase 3 の `connector_cursors` projection を再利用 (`connector_name`, `cursor_key`, `cursor_value`)
8. **Rate limit handling** — 各 connector の公式 documented rate limit を尊重。超過時は exponential backoff (max 3 retries, 1s / 2s / 4s) → 最終失敗で `ConnectorFailed` event。Slack `Retry-After` header / MS Graph `Retry-After` / Box `X-RateLimit-*` headers を読む
9. **External Content Min (ADR-0005 contract)** — body 全文は **絶対に** projection に書かない。summary は LLM が後で recall / brief / propose で使えるよう、必ず entity-identifying な metadata (id / URL / title / actor / timestamp) と最大 200 chars の summary text のみ
10. **`source_type` discriminator** — 既存の `sources` projection の `source_type` カラムに新 enum を追加: `slack_message` / `ms365_calendar` / `ms365_onedrive` / `ms365_outlook` / `box_event`。projection schema 自体は変更不要 (Phase 3 で `source_type: String` を nullable=False で定義済)

## 1.1 Prep PR (Phase 1-6) で確立した実装契約 (Phase 7 全 PR が継承)

- 新規 connector は `src/opshub/connectors/<name>/` package を作成、`auth.py` / `fetcher.py` / `mapper.py` の 3 module 構成 (Phase 3 GitHub connector の precedent)
- 新規 connector の token / OAuth credentials は `core/secrets.get_secret(...)` 経由のみ。直接 env var 読みは禁止 (env var override は `core/secrets` 内部で処理される)
- 新規 connector は `connectors/registry.py` の `register_connector(name, fetcher_factory, mapper)` 呼出で登録 (registry SSOT、Phase 3 で確立)
- 新規 connector の sync 経路は既存 `services/connector_sync_service.py` の `sync(name, ...)` を経由 (内部で fetcher + mapper を呼ぶ、新規 service を作らない)
- 新規 connector の test は SDK or HTTP を fully mock (実 API 叩かない、Phase 3-6 と同規律)
- 新規 extras は `[connectors-<name>]` 形式 (例: `[connectors-slack]`)、`pyproject.toml` に追加 + `uv.lock` 更新
- Cold-start guard: `connectors/<name>/` の module-level import は `__future__` / `typing` / `pathlib` / stdlib のみ。SDK / httpx / msal 等は **関数内で遅延 import**
- CI recipe (`justfile` + `.github/workflows/ci.yaml`) には 3 connector extras (`--extra connectors-slack --extra connectors-ms365 --extra connectors-box`) を追加
- 新規 source_type で recall / brief / propose が回ることを Phase 7 D1 integration test で pin
- ADR-0005 (External Content Min) を厳守: body 全文取り込み禁止、summary は ~200 chars cap

## 2. Phase 7 Commit 順序

Conventional Commits 準拠。1 step = 1 PR = 1 commit (squash 後) を厳守。各 PR 番号は forecast — step 番号で追う。

### 2.1 Sub-issue A: Slack connector (3 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| A1 | `feat(connectors/slack): auth + extras` | `[connectors-slack]` extras (`slack-sdk>=3.27` or pure httpx — slack-sdk 採択推奨、token management + auth.test の SDK helper が使える)。`src/opshub/connectors/slack/__init__.py` + `src/opshub/connectors/slack/auth.py` 新設: `SlackAuth.get_bot_token() -> str` (`core/secrets.get_secret("connector:slack:bot_token")` + `OPSHUB_CONNECTOR_SLACK_BOT_TOKEN` env override)。`auth.test_token() -> dict` で token validity check。`opshub connector auth set connector:slack` 経路が `connector:slack:bot_token` で keyring 保存可能であることを test pin (Phase 5 A5 で generic 化済の auth CLI を使う、新 CLI subcommand は不要) | A |
| A2 | `feat(connectors/slack): fetcher` | `src/opshub/connectors/slack/fetcher.py`: `SlackFetcher(token, channels: list[str])` + `fetch_messages(since_cursor: str | None) -> Iterator[RawMessage]`。conversations.history API を paginate (`cursor` + `limit=100`)。複数 channel 並行 fetch は不要 (1 channel ずつ順次、操作観測性優先)。rate-limit response header (`Retry-After`) を読み 1s/2s/4s exponential backoff。token expiry (`invalid_auth` error) は `ConnectorFailed` で fail-fast。test は `unittest.mock.patch` で SDK の `client.conversations_history` を mock | A |
| A3 | `feat(connectors/slack): mapper + sync integration` | `src/opshub/connectors/slack/mapper.py`: `SlackMapper.map_message(raw) -> SourceObserved`。`external_id = f"{channel_id}:{message_ts}"`、`source_type="slack_message"`、`title = f"{author_display_name} in #{channel_name}"`、`summary = text[:200]`、`url = permalink (chat.getPermalink API or constructed)`、`observed_at = message_ts → datetime`。本文全体は `summary` に切り詰める。`connectors/registry.py` で `register_connector("slack", ...)` 呼出。`opshub connector sync slack` の e2e (mock) test を追加し、新規 source row が `sources` projection に persist されることを pin | A |

### 2.2 Sub-issue B: Microsoft 365 connector (3 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| B1 | `feat(connectors/ms365): auth + oauth flow + extras` | `[connectors-ms365]` extras (`msal>=1.30` + `httpx>=0.27`)。`src/opshub/connectors/ms365/auth.py`: OAuth 2.0 authorization code flow (paste-code default、device flow opt-in)。`MS365Auth.start_auth_flow() -> str` (auth URL 返し、operator が browser で踏む) / `MS365Auth.complete_auth_flow(code) -> None` (refresh_token を keyring 保存) / `MS365Auth.get_access_token() -> str` (refresh token から access token を取得、in-memory cache)。Microsoft Graph API scope は `Calendars.Read Files.Read Mail.Read offline_access`。`opshub connector auth set connector:ms365` を拡張して paste-code flow を起動する CLI 経路 (Phase 5 A5 の generic auth set を override しない、別 subcommand `opshub connector auth set connector:ms365 --interactive` を追加するか既存に分岐を入れる、本 PR で決定)。msal の `ConfidentialClientApplication` or `PublicClientApplication` どちらを使うかは本 PR の implementation note で記録 | B |
| B2 | `feat(connectors/ms365): fetcher` | `src/opshub/connectors/ms365/fetcher.py`: Microsoft Graph API client (httpx 経由、msal で取得した access token を Authorization header に乗せる)。3 endpoint:`fetch_calendar_events(since)` (`/me/calendar/events?$filter=lastModifiedDateTime ge ...`), `fetch_onedrive_changes(delta_link)` (`/me/drive/root/delta`), `fetch_outlook_messages(since)` (`/me/messages?$filter=receivedDateTime ge ...`)。delta link cursor は `connector_cursors` projection に保存 (3 種別を別 cursor_key で持つ)。429 → `Retry-After` → exponential backoff。401 → access token refresh 試行、失敗で `ConnectorFailed`。test は httpx mock (Phase 6 A4 Ollama の pattern を使う) | B |
| B3 | `feat(connectors/ms365): mapper + sync integration` | `src/opshub/connectors/ms365/mapper.py`: 3 種類の item をそれぞれ `SourceObserved` に map。`source_type` で区別: `ms365_calendar` (`external_id = event.id`、title = subject、summary = `f"{start} - {end} ({attendees} attendees)"`、URL = webLink) / `ms365_onedrive` (`external_id = file.id`、title = name、summary = path、URL = webUrl) / `ms365_outlook` (`external_id = message.id`、title = subject、summary = bodyPreview[:200]、URL = webLink)。registry 登録。`opshub connector sync ms365` の e2e mock test。3 source_type で recall + brief が回ることも pin | B |

### 2.3 Sub-issue C: Box connector (3 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| C1 | `feat(connectors/box): auth + oauth flow + extras` | `[connectors-box]` extras (`boxsdk>=3.10` or pure httpx — boxsdk 採択推奨)。`src/opshub/connectors/box/auth.py`: OAuth 2.0 (3-legged authorization code flow、JWT app は scope 違いで Phase 7.x)。paste-code flow。refresh_token を `connector:box:refresh_token` で keyring 保存。`BoxAuth.get_access_token()` で refresh 経路。test は SDK mock | C |
| C2 | `feat(connectors/box): fetcher` | `src/opshub/connectors/box/fetcher.py`: Box Events API (`/events?stream_position=...&stream_type=all`) で incremental sync。stream_position cursor を `connector_cursors` projection に保存。429 → exponential backoff。test は SDK mock | C |
| C3 | `feat(connectors/box): mapper + sync integration` | `src/opshub/connectors/box/mapper.py`: events → `SourceObserved`。`source_type="box_event"`、`external_id = event.event_id`、`title = f"{event_type}: {item.name}"`、`summary = f"path: {item.path_collection}"`、`url = item.url`、`observed_at = event.created_at`。registry 登録。`opshub connector sync box` の e2e mock test | C |

### 2.4 Sub-issue D: Phase 7 closeout (1 PR)

| # | Commit | 概要 | Sub-issue |
|---|---|---|---|
| D1 | `test: phase 7 end-to-end + docs` | `tests/integration/test_phase7_lifecycle.py`: 3 connector それぞれを mock で `opshub connector sync` → `sources` projection に新規 row → `opshub embeddings rebuild` → `opshub recall` で SaaS source がヒット → `opshub brief "<topic>"` で SaaS context が briefing に含まれる → `opshub propose generate` で SaaS source 由来の candidate が出る、までの連鎖を CLI 経由で検証。`tests/integration/test_phase7_connector_atomicity.py`: rate-limit 失敗 / token expiry / 部分成功 (途中 message が malformed) の各経路で `ConnectorFailed` event + projection rollback の atomicity 検証。README に 4 connector (GitHub + Slack + MS365 + Box) を一覧追記。AGENTS.md / CLAUDE.md / docs/principles.md (§9 Phase 7 = ✅ Complete、§Open Q 残置 = Multi-machine sync のみ明示) / docs/architecture.md §2.9 (新規) Connectors Wave 2 layer 追記 / docs/repository-structure.md (`[P7]` annotation) / docs/decisions-log.md (Phase 7 entry) を更新 | D |

= 合計 **10 PR** (A 3 + B 3 + C 3 + D 1)。

**Wave 構成** (DAG):

```text
Wave 1: A1 Slack auth + B1 MS365 auth + C1 Box auth → 3 並列 (各 connector 内 sequential、connector 間 independent)
Wave 2: A2 Slack fetcher + B2 MS365 fetcher + C2 Box fetcher → 3 並列 (各 fetcher が同 connector の auth に依存)
Wave 3: A3 Slack mapper + B3 MS365 mapper + C3 Box mapper → 3 並列 (各 mapper が同 connector の fetcher に依存)
Wave 4: D1 closeout (全 sub-issue 依存)
```

= 4 wave。Phase 7 は 3 connector が完全に独立しているため、Phase 6 (6 wave) / Phase 5 (7 wave) より wave 数が少ない。並列度は Wave 1-3 で常に 3。Phase 7 全体の所要時間は Phase 5/6 の半分程度を見込む。

## 3. 各 Sub-issue の Definition of Done

### Sub-issue A / B / C — connector ごとの DoD (3 connector 共通)

- [ ] `opshub connector auth set connector:<name>` で token / OAuth credentials が keyring 保存される
- [ ] `OPSHUB_CONNECTOR_<NAME>_<PURPOSE>` env var override が keyring より優先される (test で pin)
- [ ] `opshub connector sync <name>` が mock 経由で新規 sources を取り込み、`sources` projection に persist
- [ ] sync 2 回目 (no new data) は 0 件取り込み (cursor 冪等性、`connector_cursors` projection 経由)
- [ ] rate-limit / token expiry / API error の各失敗経路で `ConnectorFailed` event 記録 + projection rollback
- [ ] CI で実 API 叩かない (SDK / HTTP fully mocked、`pytest.importorskip` で SDK 不在環境では skip)
- [ ] 各 connector の extras 単体で install 可 (`uv sync --extra connectors-<name>`)
- [ ] `connectors/<name>/` の module-level import が cold-start guard に違反しない (`tests/integration/test_cli_imports.py` 通過)
- [ ] ADR-0005 遵守: body 全文を `sources.summary` に保存していないことを test で pin (summary 文字数 ≤ 200)

### Sub-issue D — Phase 7 closeout

- [ ] `tests/integration/test_phase7_lifecycle.py` が 3 connector の mock sync → projection → recall → brief → propose の連鎖を検証
- [ ] `tests/integration/test_phase7_connector_atomicity.py` が 3 失敗経路 (rate-limit / token expiry / partial success) の atomicity 検証
- [ ] README に 4 connector (GitHub + Slack + MS365 + Box) 一覧追記
- [ ] AGENTS.md / CLAUDE.md に Phase 7 完了反映
- [ ] principles.md §9 Phase 7 = ✅ Complete、§Open Q 残置 = Multi-machine sync (§Open Q #5) のみ明示
- [ ] architecture.md §2.9 (新規) Connectors Wave 2 layer 追記
- [ ] repository-structure.md の Phase 7 file annotation (`[P7]` for new connector dirs)
- [ ] decisions-log.md Phase 7 entry
- [ ] ADR-0010 (Connector Contract) に Phase 7 Validation 追記 (3 connector 実装で contract が validate されたことの確認)

## 4. Open Questions

Phase 7 着手時点で未確定、本 plan 内で確定すべきもの:

1. **OAuth redirect URI strategy** — localhost callback vs. paste-code vs. device flow。本 plan §1 #5 で **paste-code default** に確定。device flow は connector が support している場合 (Slack は token-based なので不要、MS365 / Box は paste-code) のみ。localhost callback は port 衝突 / firewall 問題があるため不採用
2. **Token refresh の共通化** — 各 connector が独自に refresh path を持つ。共通 `OAuthTokenManager` helper を本 phase に含めるか、Phase 7.x に持ち越すか。本 plan では **持ち越し**: 3 connector 実装後に共通パターンが見えてから refactor (premature abstraction を避ける)
3. **`connector_cursors` projection の cursor_key 設計** — MS365 は 3 endpoint × 別 cursor が必要。`(connector_name, cursor_key)` 複合 key で対応 (Phase 3 で既に `cursor_key: String` 設計済、要確認)

Phase 7 内では確定しなくてよい (Phase 7.x / 8 持ち越し):

1. **Multi-machine sync** — principles.md §Open Q #5、Phase 8 候補
2. **`links` projection 本実装** — Phase 7.x、SaaS sources が増えると graph 探索の価値が出る
3. **Connector observability** — sync 統計 / rate-limit metrics / cost (API call count) 等、Phase 7.x
4. **Connector test fixtures 共通化** — 各 connector の HTTP mock を共通 fixture 化、Phase 7.x
5. **共通 OAuth helper refactor** — 3 connector の OAuth flow が安定したあと、Phase 7.x で `OAuthTokenManager` を抽出
6. **Additional connectors** — Notion / Linear / Discord / Jira / Confluence は Phase 7.x

## 5. Phase 7.x / 8 outlook

Phase 7 完了直後の候補:

- **Multi-machine sync** (principles.md §Open Q #5 closeout、Phase 8 候補): litestream / Turso / event-sourced export-import のいずれかを採択 + ADR-0017
- **`links` projection 本実装** (`SourceReferenced` 消費 + cross-connector graph queries CLI): Slack thread の reply chain や MS365 calendar の attendee graph が graph traversal で取れるようになる
- **Common OAuth helper refactor** (3 connector の auth path を `OAuthTokenManager` に抽出): Phase 7.x
- **Connector observability + cost layer** (sync metrics + API call count projection + `opshub stats` CLI): Phase 7.x
- **`inbox_item` candidate types** (Phase 6 MVP gap): Phase 6.x または 7.x
- **Additional connectors** (Notion / Linear / Discord / Jira / Confluence): Phase 7.x
- **Briefing cache + narrow scope** (`scope=connector:slack`): Phase 5.x 名残

Phase 7.x / 8 着手時に連動して見直すべき docs: principles.md §1 (Local-first、external SaaS dependency 増加) / §6 (External Content Min、summary 200 chars cap の妥当性再評価) / ADR-0005 / ADR-0010 (Connector Contract、本 phase で 3 実装後の Validation 拡張) / ADR-0014 (OAuth credentials の追加運用パターン)。
