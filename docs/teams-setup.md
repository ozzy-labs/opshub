# Teams Setup (Phase 11 connector)

`teams` connector は Microsoft Graph の delta query で Microsoft Teams の chat
本文を取り込む。Slack ADR-0018 と同じく **User Token (delegated permissions)** を
default principal とする ([ADR-0010](adr/0010-connector-contract.md) §改訂 (d))。
Bot Token (Application permissions) は alternative として末尾の節を参照。

Phase 11 から追加された 7 つ目の connector で、Phase 10 で確立した本文ローカル
保持 ([ADR-0020](adr/0020-full-local-content-retention.md)) + 暗号化
([ADR-0021](adr/0021-encryption-at-rest.md)) と同じ規律で `teams_message` を
`sources` projection に persist する。

## 対応 platform

すべての OS で動作する (httpx + msal の Python 経路、ローカル daemon 不要)。
ネットワーク到達性のみが要件。

## 1. Azure Portal App Registration

operator が事前に Azure Active Directory tenant 上で application を登録する
(opshub 側から自動化はしない、IT policy 上の前提共有のため)。

1. <https://portal.azure.com/> → **Microsoft Entra ID** → **App registrations** → **New registration**
2. 名前は任意 (例: `OpsHub Teams Connector`)。**Supported account types** は
   通常 `Accounts in this organizational directory only (single tenant)` で OK。
3. **Redirect URI** は不要 (device code flow を使う想定)。空のままで進む。
4. 登録後、左メニュー **API permissions** → **Add a permission** →
   **Microsoft Graph** → **Delegated permissions** で次を追加:
   - `Chat.Read` (必須、自分が参加している chat の本文取得)
   - `ChannelMessage.Read.All` (任意、Teams channel メッセージも取り込む場合)
   - `User.Read` (Graph 既定、token を解釈するために必要)
5. **Grant admin consent for `<tenant>`** を押す (tenant admin 権限が要)。個人
   アカウントの場合は consent 不要。
6. 左メニュー **Overview** で **Application (client) ID** と **Directory (tenant) ID** を控える。

> 企業 IT policy で delegated consent が阻まれる場合は、**Bot Token (Application
> permissions)** が退路。`Chat.Read.All` + admin consent で application 単位の
> token を取得する。OpsHub の `teams` connector は両方を受け付ける (env var の
> 形が同じ JWT であれば auth helper はそのまま通る)。

## 2. User Token の取得

`opshub` 側に MSAL device code flow の interactive helper は同梱していない
(Phase 11 MVP は scope を小さく保つため)。次のいずれかで取得する:

### (a) Microsoft Graph Explorer から取得

<https://developer.microsoft.com/en-us/graph/graph-explorer> にサインインし、
右上のプロファイルから **Access token** をコピー。`Chat.Read` scope が含まれて
いることを確認。

### (b) MSAL CLI で取得 (推奨、自動更新用)

```bash
pip install msal
python - <<'PY'
import msal
app = msal.PublicClientApplication(
    "<APPLICATION_CLIENT_ID>",
    authority="https://login.microsoftonline.com/<TENANT_ID>",
)
flow = app.initiate_device_flow(scopes=["Chat.Read", "ChannelMessage.Read.All"])
print(flow["message"])
result = app.acquire_token_by_device_flow(flow)
print(result["access_token"])
PY
```

ブラウザの指示通り device code を入力して認証 → access token (1 時間有効、
通常は refresh token 込み) が出力される。

> **token の取り扱い**: JWT shape の Graph bearer。`xoxp-` 形式ではない (Slack
> と混同しないこと)。`tests/_secrets.py` の連結ビルド規範に従い、テスト
> fixture では文字列を分割して保存する。

## 3. opshub に token を保管

### (a) keyring 経由 (推奨、長期保管)

```bash
opshub teams auth set
# プロンプトに従って token を貼り付け
```

OS keychain の **単一 slot** `connector:teams:token` に保存される
([ADR-0014](adr/0014-saas-token-storage.md))。User Token / 将来の Bot Token
alternative は principal-neutral にこの 1 slot を共有する
(ADR-0010 §Phase 11 改訂 (d)、`src/opshub/connectors/teams/auth.py` の
`TEAMS_TOKEN_SECRET_KEY`)。

### (b) env var 経由 (CI / 一時利用)

```bash
export OPSHUB_CONNECTOR_TEAMS_TOKEN="<access_token>"
opshub teams sync
```

env var を設定すると keyring lookup を skip する。CI / container 等では
こちらが便利。

## 4. opshub.toml 設定

```toml
[connectors.teams]
enabled = true                        # default: false
fallback_window_days = 30             # default: 30、delta link 失効時の full-pass 窓
# excludes.yaml の channels / senders selector を再利用 (Slack 同パターン)
```

`excludes.yaml` 側の設定例 (top-level flat key — ADR-0020 §(b)、
`src/opshub/core/excludes.py` が parse する shape。`teams: { ... }` 等の
nested 形式は `ConfigError` で fail-fast):

```yaml
channels:
  - "19:secret-teams-channel-id"     # chat_id / chat_topic に match
senders:
  - security-bot@example.com         # sender_id / from に match
```

## 5. sync 実行

```bash
opshub teams sync
```

差分検出は Microsoft Graph の `@odata.deltaLink` で行う。
2 回目以降は前回の delta link を再投入し、差分のみ取り込む。

### Delta link 失効時の挙動 (ADR-0010 §改訂 (c))

Graph は delta link を一定期間 (典型値は数日) で失効させる (`410 Gone` /
`invalidatedDeltaToken`)。OpsHub は **自動で full-pass fallback** に切り替える:

1. WARNING log: `teams delta token invalidated; falling back to recent window`
2. 直近 `fallback_window_days` (default 30) の `$filter=lastModifiedDateTime ge <iso>` で
   フルパス
3. 新しい delta link を取得して `connector_cursors` に保存
4. 次回 sync は通常の delta 差分モードに復帰

fallback 自体が失敗した場合は `ConnectorSyncFailed` event を append
(ADR-0010 §責務 4)。重複は `SourceObserved` の dedup (`external_id =
f"{chat_id}:{message_id}"`) で吸収される。

### 定期実行

OS scheduler を operator が設定する (常駐 daemon は Phase 11 scope 外):

```cron
# crontab -e
0 */2 * * * opshub teams sync
```

## 制約事項

- **添付ファイル本文は取り込まれない** (Phase 11 MVP は chat message 本文のみ)。
  添付された Office 文書を直接取り込みたい場合は `box_drive` / `onedrive_drive`
  connector + `content_extraction = true` を併用する。
- **画像 OCR なし** (Phase 12+ 候補)。
- **書き戻し非対応** ([ADR-0010](adr/0010-connector-contract.md) §禁止事項 7)。
  返信は `opshub propose generate --reply-to <source-id>` で **下書き** のみ
  生成可能。Teams への送信は手動コピペ。
- **個人 chat (1:1 / group) と Teams channel が同じ `teams_message` source_type**
  になる。区別したい場合は `chat_topic` / `external_id` で post-filter。
- **token は keyring か env で operator が管理** (opshub 側で自動 refresh は
  しない)。expire 時は再取得して上書き。

## トラブルシューティング

### `401 Unauthorized` / `InvalidAuthenticationToken`

token が expire したか、scope が足りない。Step 2 を再実行して取得し直し、
Step 3 (a) or (b) で上書き。

### `403 Forbidden` / `Authorization_RequestDenied`

`Chat.Read` permission が consent されていない。Azure Portal で
`Grant admin consent` を再度押す (Step 1 #5)。

### `429 Too Many Requests`

Graph API の rate limit (1 user / 約 600 req/秒)。fetcher は exponential
backoff (1s/2s/4s, max 3 retries) で再試行する。それでも越えたら
`ConnectorSyncFailed`。`fallback_window_days` を狭めるか、sync 頻度を下げる。

### chat の本文が取れない

Graph の `getAllMessages` は **自分が参加している chat** にのみ delegated
access を提供する。他人の private chat は principal 不一致で見えない (これは
Microsoft Graph の仕様)。Channel messages を取りたい場合は
`ChannelMessage.Read.All` を追加。

## Bot Token (Application permissions) 経路

企業 IT policy で `delegated consent` が阻まれる場合の alternative。

1. Azure Portal の App registration で **Application permissions** を選び
   `Chat.Read.All` / `ChannelMessage.Read.All` を追加 → admin consent。
2. **Certificates & secrets** で client secret を発行。
3. MSAL の `ConfidentialClientApplication.acquire_token_for_client()` で
   tenant-scoped token を取得。
4. 同じく `OPSHUB_CONNECTOR_TEAMS_TOKEN` に貼り付ける (auth helper は token の
   発行元を区別しない)。

Bot Token は tenant 全体の chat にアクセス可能で、operator 個人の context を
超える。利用前に privacy implication を検討すること
([SECURITY.md](../SECURITY.md) の "Phase 11 — Teams ingest" 節)。

## 関連 docs

- [ADR-0010: Connector Contract](adr/0010-connector-contract.md) (Phase 11 改訂 (a)/(c)/(d))
- [ADR-0014: SaaS Token Storage](adr/0014-saas-token-storage.md)
- [ADR-0020: Full Local Content Retention](adr/0020-full-local-content-retention.md)
- [Phase 11 Plan](phase-11-plan.md)
- [SECURITY.md](../SECURITY.md)
