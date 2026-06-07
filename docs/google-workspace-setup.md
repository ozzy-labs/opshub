# Google Workspace Setup (Phase 13 + 14 connectors)

`google_workspace` connector は Google Drive API v3 経由で Google Docs /
Slides / Sheets の metadata + 本文を取り込む (Phase 13)。Phase 14 で同じ
OAuth principal を共有する `google_mail` (Gmail API v1) と `google_calendar`
(Calendar API v3) の 2 connector が追加された。**3 connector で 1 Google account
= 1 principal を共有** し、scope は固定 list `drive.readonly + gmail.readonly +
calendar.readonly` を 1 回の paste-code flow で取得 ([Phase 14 plan §1 OQ6](phase-14-plan.md#1-確定済み事項))。
auth.py は Phase 14 G2 で `connectors/google_auth/auth.py` に shared module
として抽出された (`connectors/google_workspace/` は新場所から import に re-wire)。

**OAuth 2.0 Refresh Token + offline access + アプリ層 refresh + rotation 書き戻し**
を採用 ([ADR-0010](adr/0010-connector-contract.md) §Phase 13 改訂 (h) = MS365 / Box
pattern、Phase 14 改訂 (m) で scope 拡張)。Phase 11 で導入した Teams pattern
(verbatim user token + アプリ層 refresh なし) とは **別系統** であり、両 pattern が
ADR-0010 内に並立する。

Phase 13 で 8 つ目の connector として追加され (Web API 経路)、Phase 14 で
Gmail + Calendar を加えて opshub 全体で 10 connector 体制になった。
Phase 10 で確立した本文ローカル保持 ([ADR-0020](adr/0020-full-local-content-retention.md))

+ 暗号化 ([ADR-0021](adr/0021-encryption-at-rest.md)) と同じ規律で
`google_doc` / `google_slides` / `google_sheets` / `google_workspace_file` (Phase 13)
+ `gmail_message` / `google_calendar` (Phase 14) を `sources` projection に persist
する。Workspace native 本文抽出は Phase 11 で確立した markitdown 1 本経路を
Workspace export 経由で再利用 ([ADR-0025](adr/0025-office-document-content-extraction.md)
§決定 (d') + (j))。Gmail / Calendar 本文抽出は **Outlook / ms365_calendar と
symmetric** = text/plain 優先 → text/html 生保持、markitdown なし、添付 retain なし
([ADR-0010](adr/0010-connector-contract.md) §Phase 14 改訂 (k))。Symmetry は
`tests/unit/connectors/test_mapper_symmetry.py` で機械検証 (Outlook ↔ Gmail
8 ケース + ms365_calendar ↔ google_calendar 6 ケース = 計 14 ケース)。

## 対応 platform

すべての OS で動作する (httpx の Python 経路、ローカル daemon 不要)。
ネットワーク到達性のみが要件。Google Drive for Desktop の WSL2 mount は
不安定なため、本 connector はローカル FS 経路ではなく **Web API 経路** で統一
(Phase 13 plan §1 OQ1)。

## 1. Google Cloud OAuth client の作成

operator が事前に Google Cloud Console で OAuth client を登録する
(opshub 側から自動化はしない、IT policy 上の前提共有のため)。

### (a) GCP プロジェクトの用意

1. <https://console.cloud.google.com/> にサインインし、画面上部のプロジェクト
   セレクタで **New Project** を選ぶ。
2. 名前は任意 (例: `opshub-google-workspace`)。組織がある場合は IT に
   相談 (Drive API 利用ポリシーが組織で管理されていることがある)。

### (b) Drive API の有効化

1. 左メニュー **APIs & Services** → **Library** で **Google Drive API**
   を検索 → **Enable**。
2. (任意) **Library** で **Google Drive Activity API** も検索できるが、
   Phase 13 では **使わない** (`changes.list` poll のみで delta 検出可能、
   activity feed は不要、Phase 13 plan §Alternatives §2)。

### (c) OAuth consent screen の設定

1. 左メニュー **APIs & Services** → **OAuth consent screen**。
2. **User Type**: 個人 Google アカウントの場合は `External`、Google Workspace
   組織内利用の場合は `Internal` を選ぶ (`Internal` だと組織外ユーザーに
   consent を出さなくて済む)。
3. **App information**: 名前 (例: `OpsHub Google Workspace Connector`) /
   user support email / developer email を入れる。
4. **Scopes**: **Add or Remove Scopes** で次を追加 (Phase 14 完了時点で
   3 つすべて必須。Phase 13 までは `drive.readonly` 単独だったが、
   Phase 14 plan §1 OQ6 + §X.1 で「1 Google account = 1 principal、
   Drive / Gmail / Calendar を 1 回の consent で取得」を確定):
   + `https://www.googleapis.com/auth/drive.readonly` (必須、Drive 取り込み)
   + `https://www.googleapis.com/auth/gmail.readonly` (必須、Phase 14 G3
     Gmail 取り込み。Phase 14 完了済、`opshub google_mail sync` で利用。
     scope は事前に
     consent しておく方が `incremental authorization` の re-prompt を避
     けられる)
   + `https://www.googleapis.com/auth/calendar.readonly` (必須、Phase 14
     G4 Google Calendar 取り込み。同上)

   `drive.metadata.readonly` は `drive.readonly` の subset なので併記
   しない。Gmail / Calendar も `readonly` 系のみで外部書き戻しは行わない
   (ADR-0010 §禁止事項 7)。
5. **Test users**: テストモード (Publishing status = Testing) のうちは、
   operator 自身の Google アカウントを **Add Users** で追加する。テスト
   モードは refresh token が 7 日で失効する制限あり (Google docs)。継続
   利用するなら次の **Publishing** を進める。
6. **Publishing**: 個人利用なら Testing のまま (operator アカウントを
   test users に固定)、組織 Workspace 利用なら **Publish App** で
   `Internal` 公開を行う (組織外 consent は不要)。組織外公開
   (`In production`) には Google の verification が必要 (本 connector の
   用途では不要なはず)。

### (d) Desktop App credential の作成

1. 左メニュー **APIs & Services** → **Credentials** → **Create Credentials**
   → **OAuth client ID**。
2. **Application type**: **Desktop app** を選択。Web app / iOS / Android
   ではない。Desktop App credential は `client_secret` を「文書化された
   非秘密」として扱う (Google は「installed app の secret は配布バイナリ
   から抽出可能であり真の secret ではない」と documented; ただし OAuth
   wire protocol は client_secret を毎回要求する)。
3. **Name**: 任意 (例: `OpsHub Connector`)。
4. 作成後、**Download JSON** で `client_id` + `client_secret` を控える。

> **token の取り扱い**: `tests/_secrets.py` の連結ビルド規範に従い、
> テスト fixture では文字列を分割して保存する (`tests/_secrets.py` 参照)。
> 実 token は OS keychain (`connector:google_workspace:refresh_token`) で
> 管理する。`opshub.toml` には refresh token を書かない (ADR-0014)。

## 2. opshub.toml 設定

> **TOML 読込**: `opshub.toml` は起動時に毎回読まれ、`OPSHUB_*` 環境変数は TOML を上書きする (優先順位 `init args > env > toml > defaults`、[ADR-0032](adr/0032-runtime-toml-config-loading.md))。`OPSHUB_CONFIG_DIR=<dir>` で config dir 全体を差し替え可。詳細は [`docs/troubleshooting.md` §3.10](troubleshooting.md)。

```toml
[connectors.google_workspace]
enabled = true                                  # default: false
client_id = "<your-client-id>.apps.googleusercontent.com"
client_secret = "<your-client-secret>"
redirect_uri = "http://localhost"               # default; GCP Console に登録した値と一致
content_extraction = false                      # default: false (G3 metadata-only)
fallback_window_days = 30                       # default: 30 (changes.list TTL 失効時の full-pass 窓)
```

`excludes.yaml` 側の設定例 (top-level flat key — ADR-0020 §(b)、
`src/opshub/core/excludes.py` が parse する shape。`google_workspace: { ... }`
等の nested 形式は `ConfigError` で fail-fast):

```yaml
paths:
  - "/Confidential/**"                          # Drive item path に match
```

## 3. paste-code OAuth flow

```bash
opshub google_workspace auth set
```

実行すると次の手順が走る (MS365 / Box の paste-code flow と対称):

1. opshub が auth URL を構築 (`access_type=offline` + `prompt=consent` +
   `scope=drive.readonly gmail.readonly calendar.readonly` — Phase 14 G2
   以降は 3 scope 同時要求) し、ターミナルに表示する。
2. operator がブラウザで URL を開き、Google アカウントにサインインして
   consent。
3. Google が `http://localhost/?code=...&scope=...` にリダイレクト
   (opshub はこの URI で listen しない = paste-code flow)。ブラウザの
   address bar から URL 全体 (または `code=` パラメータの値) をコピー。
4. opshub のプロンプトに貼り付け。opshub が code を token endpoint に
   POST して access token + refresh token を取得。
5. **refresh token** は `connector:google_workspace:refresh_token` keyring
   slot に永続化 ([ADR-0014](adr/0014-saas-token-storage.md) §Phase 7
   Validation 3 件目)。**access token** は in-memory のみ (~1 hour TTL、
   次の sync で自動 refresh)。

### env var override (CI / 一時利用)

```bash
export OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN="<refresh-token>"
opshub google_workspace sync
```

env var を設定すると keyring lookup を skip する。CI / container 等では
こちらが便利。

## 4. sync 実行

```bash
opshub google_workspace sync
```

差分検出は Drive API v3 `changes.list` の `startPageToken` cursor で行う。
2 回目以降は前回の page token を再投入し、差分のみ取り込む。
initial sync は `changes.getStartPageToken` でブートストラップ + 初回の
`files.list` (全件) を実行。

### page token 失効時の挙動 (ADR-0010 §Phase 13 改訂 (g))

Drive は page token を一定期間 (~30 日) で失効させる (`400 invalidToken` /
`404 startPageToken expired` / `410 Gone`)。OpsHub は **自動で full-pass
fallback** に切り替える (Phase 11 Teams delta-link と同パターン、ADR-0010
§Phase 13 改訂 (g)):

1. WARNING log: `google_workspace page token invalidated; falling back to recent window`
2. `changes.getStartPageToken` で新しい page token を取得
3. 直近 `fallback_window_days` (default 30) を `files.list` で full-pass
4. 新しい page token を `connector_cursors` に保存
5. 次回 sync は通常の `changes.list` 差分モードに復帰

fallback 自体が失敗した場合は `ConnectorSyncFailed` event を append
(ADR-0010 §責務 4)。重複は `SourceObserved` の dedup
(`external_id = <fileId>`) で吸収される。

### Refresh Token rotation (ADR-0010 §Phase 13 改訂 (h))

Google OAuth 2.0 は refresh token を周期的に rotation することがある (Google
docs)。opshub は **rotation を検出すると新しい値を keyring に書き戻す**
(`tests/unit/connectors/google_workspace/test_auth.py::test_get_access_token_persists_rotated_refresh_token`
で pin、ADR-0014 §Phase 7 Validation rotation pin リスト 3 件目)。書き戻しが
ないと次の sync 起動時に Google が `invalid_grant` で reject する → operator が
paste-code flow から再認証する羽目になる。

### Workspace export → 本文抽出 (content_extraction = true 時のみ)

```toml
[connectors.google_workspace]
content_extraction = true                       # opt-in
```

`[office]` extras (`uv tool install "ozzylabs-opshub[office]"`) も併用する
必要がある (markitdown 経路、[ADR-0025](adr/0025-office-document-content-extraction.md))。

`content_extraction = true` の opt-in 時のみ、Workspace native 形式
(`google_doc` / `google_slides` / `google_sheets`) は Drive API
`files.export(fileId, mimeType=<MS Office mediatype>)` で MS Office バイト列
として取得し、`core/document_extract.extract_workspace_export(bytes, source_type)`
経由で markitdown で抽出 → `sources.body` に persist する。3 形式とも
MS Office mediatype 経由 (Docs → docx / Slides → pptx / Sheets → xlsx)
で統一 (Phase 13 plan §1 OQ2、ADR-0025 §決定 (j))。

非 native ファイル (Drive にアップロードされた PDF / 画像 / フォルダ等の
catch-all `google_workspace_file`) は `files.export` が `403 fileNotExportable`
で reject するため、`content_extraction = true` 設定下でも catch-all path は
`body = summary` (非空) で persist される (Phase 13 G4 #278 の wiring)。
`SourceObserved.body` は epic #470 で必須化 (`min_length=1`、ADR-0020 §(d'))
されており、metadata-only path でも mapper が `summary` を `body` に再利用
することで projection は常に非空文字列を保持する。

### 定期実行

OS scheduler を operator が設定する (常駐 daemon は Phase 13 scope 外、
Drive `files.watch` push notification は禁止 = 形 A 整合):

```cron
# crontab -e
0 */2 * * * opshub google_workspace sync
```

## 制約事項

+ **添付ファイル本文以外の Drive Comments / Suggestions は取り込まれない**
  (Phase 13 MVP は files の本文 + metadata のみ)。決定経緯の context source
  としての Comments / Suggestions 取り込みは Phase 15+ candidate
  (Phase 14 では Gmail + Calendar が優先され着手せず)。
+ **画像 OCR なし** (Phase 13 から繰り越し、Phase 14 を経て再 defer、Phase 15+ candidate)。
+ **書き戻し非対応** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7)。
  Drive write API (`files.update` / `files.create` / `files.copy` /
  `comments.create` / `permissions.*`) は connector に実装しない。
  返信下書きは `opshub propose generate --reply-to <source-id>` で **下書き**
  のみ生成可能 (送信は手動コピペ)。
+ **Drive `files.watch` push notification 禁止** (ADR-0010 §Phase 13 改訂 (e)
  §禁止事項拡張)。`changes.list` poll のみ。能動性混入 (形 A 抵触) を防ぐ。
+ **`google_workspace_file` (catch-all、非 native) は metadata-only**。
  Drive にアップロードされた PDF / 画像 / フォルダ等を区別したい場合は
  `external_id` (= Drive fileId) や `title` で post-filter。
+ **token は keyring か env で operator が管理** (opshub 側で自動 refresh は
  done、ただし rotation 書き戻し済)。長期失効 (90+ 日 inactive、
  scope revocation 等) で `invalid_grant` が返ったら再 auth が必要。
+ **Shared Drives (Team Drives) のサポート**: Phase 13 G3 着手時に
  `supportsAllDrives=true + includeItemsFromAllDrives=true` を含めるかを
  確定 (OQ10)。
+ **multi-account 非対応** (single-slot principal、Phase 13 plan §Alternatives
  §7)。operator 個人 GCP + 業務 Workspace の併用は Phase 15+ で
  multi-account extension として検討。

## トラブルシューティング

### `401 Unauthorized` / `Invalid Credentials`

access token が expire したか refresh に失敗。`opshub google_workspace sync`
が自動 refresh するはずだが、refresh も失敗した場合は
refresh token 自体が revoked / invalidated されている。Step 3 の paste-code
flow を再実行して refresh token を取り直す。

### `invalid_grant` / `Token has been expired or revoked`

OAuth consent screen が Testing モードのまま 7 日経過した、もしくは
operator が Google アカウント設定で当該 OAuth client を revoke した可能性。
Step 3 の paste-code flow を再実行。長期運用するなら Step 1 (c) の
Publishing で Internal 公開する。

### `403 fileNotExportable` (content_extraction = true 時のみ)

`google_workspace_file` (catch-all 非 native) は `files.export` 不可。
これは想定挙動で、metadata-only で persist される。`SourceObserved.body`
が `None` のままになるが、抽出失敗としてではなく Drive の制約として扱う。
WARNING log にも出ない (`google_workspace_file` は最初から export を試みない)。

### `400 invalidToken` / `404 startPageToken expired` / `410 Gone`

`changes.list` の page token が失効した。連 connector は自動的に
full-pass fallback (本 doc Step 4 §page token 失効時の挙動) に切り替わる
ので、WARNING log を確認しつつ次の sync を待つだけで OK。

### `429 Too Many Requests` / Quota exceeded

Drive API rate limit (per-user 1000 req/100s、per-project 20000 req/100s 程度、
Google docs)。connector は exponential backoff (1s/2s/4s, max 3 retries) で
再試行する。それでも超過したら sync 頻度を下げる (`crontab` を 2 時間 → 4 時間)
か、quota 増加申請 (Google Cloud Console → APIs & Services → Quotas)。

### content_extraction = true で markitdown エラー

`uv tool install "ozzylabs-opshub[office]"` で `[office]` extras を入れて
いない可能性。markitdown 経路は extras gated。`opshub google_workspace sync`
時に `ConfigError: markitdown not installed` の skip_reason
が出ていたら extras 追加 + 再 sync。

## Phase 14: Gmail + Google Calendar 追加 setup

### Gmail (`google_mail` connector)

```toml
# ~/.config/opshub/config.toml
[connectors.google_mail]
enabled = true
fallback_window_days = 30          # users.history.list 7-day TTL invalidation fallback window
# 任意 override:
# initial_window_days = 7           # first-sync backfill window (cursor=None pass の遡及範囲)
```

```bash
opshub google_mail sync
```

+ **API**: Gmail API v1 — `users.messages.list` (initial sync) + `users.messages.get(format=full)` (本文取得) + `users.history.list` (delta)
+ **Cursor**: History API `startHistoryId` を `connector_cursors` に保存。7 日 TTL 失効時 (HTTP 404) は `connector.history_list.expired` WARNING を出して `users.messages.list` の full-pass fallback ([ADR-0010](adr/0010-connector-contract.md) §Phase 14 改訂 (j) で delta-cursor 型一般に generalize)
+ **Mapper**: Outlook ([ADR-0020](adr/0020-full-local-content-retention.md) Phase 11 deep retention) と symmetric。`source_type=gmail_message`、body = text/plain 優先 → text/html 生保持 / markitdown なし / 添付 retain なし、`[Labels: ...]` prepend、`[gmail body truncated: N / M chars]` tag、threadId field 保持 (thread aggregation projection は Phase 15+ defer)
+ **OAuth**: 上記 `connector:google_workspace:refresh_token` slot を共有。Phase 14 で scope が `drive.readonly + gmail.readonly + calendar.readonly` の 3 scope 固定 list に拡張済 — Phase 13 までに setup を済ませていた operator は **Step 3 の paste-code flow を 1 回再実行** して新 scope を再 consent する必要がある (詳細は [`docs/upgrading.md`](upgrading.md) §Phase 14 節)

### Google Calendar (`google_calendar` connector)

```toml
# ~/.config/opshub/config.toml
[connectors.google_calendar]
enabled = true
# 任意 override (MVP default = 過去 90 日 + 未来 365 日、syncToken 410 GONE fallback の window walk にも兼用):
# time_min_days = 90
# time_max_days = 365
```

```bash
opshub google_calendar sync
```

+ **API**: Calendar API v3 — `events.list(syncToken=...)` (delta) + `events.list(timeMin, timeMax, singleEvents=false, showDeleted=true)` (full-pass、initial sync / 410 fallback)
+ **Cursor**: `syncToken` を `connector_cursors` に保存。410 GONE (`SyncTokenExpiredError`) 失効時は `connector.events_list.expired` WARNING を出して `time_min_days` + `time_max_days` window walk fallback (`singleEvents=false` + `showDeleted=true` pinned、ADR-0010 §Phase 14 改訂 (j))
+ **Mapper**: MS365 Calendar (Phase 7) と symmetric。`source_type=google_calendar`、master event only (`recurringEventId` 無し)。Google API は recurring の override (`recurringEventId` + `originalStartTime` 持ち) を独立 event として返すため、それも **`google_calendar` source_type の独立 record として emit** + body に `Override of: <master_id> (originalStart: <iso>)` back-pointer を追加。summary = `f"{start_iso} - {end_iso} ({attendees_count} attendees)"`、attendee email list / 議題 (description) / 会議室 (location) を body に追記、RRULE は field 保持 (instance 展開 projection は Phase 15+ で ms365 / google 両 calendar 同時)
+ **OAuth**: Gmail と同じ shared principal、同 keyring slot、同 re-consent (Phase 14 で 3 scope 固定 list)
+ **MVP scope**: primary calendar のみ。secondary calendar (operator が複数 calendar を持つケース) は Phase 15+ extension

### Mapper symmetry pin test

両 connector の mapper は `tests/unit/connectors/test_mapper_symmetry.py` で
Outlook ↔ Gmail (8 ケース) / ms365_calendar ↔ google_calendar (6 ケース) =
計 14 ケースの field / summary / body フォーマット同形性を機械検証する。mapper を fork して
vendor-specific な調整を入れた場合は、この pin test を確認して divergence が
intentional か判断すること。

## 関連 docs

+ [ADR-0010: Connector Contract](adr/0010-connector-contract.md) (Phase 13 改訂 (e)/(f)/(g)/(h) + **Phase 14 改訂 (i)/(j)/(k)/(l)/(m)**)
+ [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md) (Phase 13 改訂 = rotation pin リスト 3 件目、**Phase 14 改訂 = google_workspace slot scope 拡張**)
+ [ADR-0020: Full Local Content Retention](adr/0020-full-local-content-retention.md)
+ [ADR-0025: Office Document Content Extraction](adr/0025-office-document-content-extraction.md) (Phase 13 改訂 (d') + (j))
+ [Phase 13 Plan](phase-13-plan.md)
+ [Phase 14 Plan](phase-14-plan.md)
+ [Upgrading guide — Phase 14](upgrading.md#phase-14-gmail--google-calendar-connectors) (re-consent 手順)
+ [SECURITY.md](../SECURITY.md) "Phase 13 — Google Workspace ingest" 節 + "Phase 14 — Gmail + Google Calendar ingest" 節
+ Google Drive API v3: <https://developers.google.com/drive/api/v3>
+ Google Drive API v3 changes.list: <https://developers.google.com/drive/api/v3/manage-changes>
+ Google Drive API v3 files.export: <https://developers.google.com/drive/api/v3/reference/files/export>
+ Gmail API v1: <https://developers.google.com/gmail/api>
+ Gmail API v1 users.history.list: <https://developers.google.com/gmail/api/v1/reference/users/history/list>
+ Google Calendar API v3: <https://developers.google.com/calendar/api>
+ Google Calendar API v3 events.list (syncToken): <https://developers.google.com/calendar/api/v3/reference/events/list>
+ Google OAuth 2.0 for installed apps: <https://developers.google.com/identity/protocols/oauth2/native-app>
