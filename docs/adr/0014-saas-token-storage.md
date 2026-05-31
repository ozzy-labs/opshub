# 0014. SaaS Token Storage

- Status: Accepted (revised 2026-05-31 for Phase 14 Sub-issue G1: google_workspace slot の scope を Drive 専用 → Drive + Gmail + Calendar に拡大 + shared auth foundation 抽出方針を明示)
- Date: 2026-05-17 (initial); 2026-05-31 (Phase 13 改訂: §Phase 7 Validation 節の rotation pin リストに `connector:google_workspace:refresh_token` を追加); 2026-05-31 (Phase 14 改訂: §Phase 7 Validation 節 google_workspace slot の scope を Drive 専用 → Drive + Gmail + Calendar 全般 (drive.readonly + gmail.readonly + calendar.readonly) に拡大、shared auth foundation `connectors/google_common/auth.py` への抽出方針を明示、新 slot 追加なし)
- Deciders: ozzy

## Context

Phase 3 で SaaS connector (GitHub をはじめ、Phase 3.x で Slack / MS365 / Box) を実装するにあたり、OAuth token や PAT (Personal Access Token) を OpsHub が保管する必要がある。

要件:

- ローカルファースト (ADR-0001 Local-first principle)
- 平文 commit 禁止 (ADR-0005 External Content Minimization の延長)
- agent / 他プロセスからの暗黙参照を防ぐ
- CI / 開発時の override 手段 (env var) を残す
- cross-platform (macOS / Linux / WSL2)

選択肢:

1. `keyring` library (OS keychain backed: macOS Keychain / Secret Service / Windows Credential Locker)
2. `pass` (GPG-encrypted file storage)
3. `secret-tool` (libsecret CLI, Linux only)
4. プレーンファイル (`~/.config/opshub/secrets.toml` の 0600 file)

## Decision

`keyring` library を採用。OS 標準の keychain backend (macOS Keychain / Linux Secret Service / Windows Credential Locker) に委譲することで、token のディスク平文保管を避けつつ cross-platform 性を維持する。

実装:

- `opshub.core.secrets.get_secret(key)` / `set_secret(key, value)` / `delete_secret(key)` で薄くラップ
- service name は `"opshub"` 固定、key は `f"connector:{connector_name}:pat"` などの規約
- 開発時 / CI override: `OPSHUB_<KEY_UPPER>` env var が set されていれば優先 (e.g. `OPSHUB_CONNECTOR_GITHUB_PAT`)
- pyproject の `[project.optional-dependencies]` に `secrets = ["keyring>=24"]` として隔離 (core dependency にしない — keyring を必要としない build / CI で軽量に保つ)

## Consequences

### Positive

1. cross-platform、production / dev で同じ API
2. OS keychain への委譲で OpsHub 自身が暗号鍵管理しなくて済む
3. env var override で test / CI / contained docker 環境でも動く
4. extras 隔離で keyring を入れたくないユーザー (`opshub task` だけ使う等) の install size を増やさない

### Negative / Trade-offs

1. headless Linux (CI / docker / WSL2 with no Secret Service) では `keyring.backends.fail.Keyring` がデフォルトになり、明示的に `keyring.set_keyring(keyring.backends.null.Keyring())` などの設定が必要
   - 緩和: env var override 経路で逃げられる、また README に headless 環境向けの `keyring --list-backends` / `KEYRING_PROPERTY_*` の案内を Phase 3 closeout で記載
2. `keyring` package は最終的に `SecretStorage` / `dbus-python` 等の transitive dep を引っ張る可能性 (Linux)
   - 緩和: `secrets` extras に隔離、core install は影響なし
3. `pass` ユーザー / `secret-tool` 直叩きユーザーは別 backend を keyring.set_keyring() で差し込む必要
   - 緩和: keyring 自体が backend plugin システムを持つので、`keyrings.alt` や custom backend で対応可能 (本 ADR の決定範囲外)

## Alternatives Considered

### 1. `pass` (GPG-encrypted file storage)

却下: GPG セットアップが prerequisite、cross-platform 性が低い (Windows での運用が貧弱)、agent runtime での GPG prompt 経路が複雑

### 2. `secret-tool` (libsecret CLI)

却下: Linux only、macOS / Windows で動かない

### 3. プレーンファイル (`~/.config/opshub/secrets.toml` の 0600)

却下: OS レベルでの暗号化なし、誤って `.dotfiles` リポにバックアップされる事故が発生しやすい、credential scanner にも引っかかる

### 4. service 起動時に都度 prompt

却下: agent runtime (Phase 4 以降の MCP server 化等) で non-interactive 実行が必要、prompt は CLI 一回起動には許容できても sync ループには不向き

## Validation

### Phase 3 (Initial) validation

Phase 3 sub-issue B (GitHub connector、PR #51-55) で本 ADR の `core/secrets` + keyring + env var override 規約を初実装し、以下を end-to-end で確認した:

- `core/secrets.get_secret("connector:github:pat")` / `set_secret` / `delete_secret` の薄いラッパー (`src/opshub/core/secrets.py`)
- 規約 `f"connector:{name}:pat"` (keyring key) と `OPSHUB_CONNECTOR_GITHUB_PAT` (env var override) の優先順位 (env > keyring) を `tests/unit/connectors/github/test_auth.py` で pin
- `secrets` extras (`keyring>=24`) で core install から隔離 (`pyproject.toml [project.optional-dependencies]`)
- CLI `opshub connector auth set github` で keyring への書き込み (Phase 3 D2 で確定、`src/opshub/cli/connector.py`)

### Phase 7 (Connectors Wave 2) validation

Phase 7 (epic #113) で Slack / Microsoft 365 / Box の 3 新規 connector を追加した際、本 ADR の token storage 契約をそのまま拡張して以下を pin:

- **6 keyring keys (1 per connector、Phase 14 でも slot 件数は 6 件のまま)** — `connector:github:pat` / `connector:slack:token` / `connector:ms365:refresh_token` / `connector:box:refresh_token` / `connector:teams:token` / `connector:google_workspace:refresh_token`。secret kind (`pat` / `token` / `refresh_token`) は connector 側の OAuth 仕様に合わせて選択し、key 文字列はそのまま `core/secrets` の lookup key になる (`src/opshub/connectors/{github,slack,ms365,box,teams,google_workspace}/auth.py` の `*_SECRET_KEY` 定数)。Slack の suffix が `token` で principal を含まないのは [ADR-0018](0018-slack-token-principal.md) で User Token first-class + Bot Token alternative の両方を同じ slot に格納する設計を採択したため。Phase 7 時点では GitHub / Slack / MS365 / Box の 4 件、Phase 11 改訂で Teams が `connector:teams:token` を 5 件目として追加 (ADR-0010 §Phase 11 改訂 (d)、verbatim user token pattern)、Phase 13 改訂で Google Workspace が `connector:google_workspace:refresh_token` を 6 件目として追加 (ADR-0010 §Phase 13 改訂 (h)、MS365 / Box pattern)。**Phase 14 改訂では slot 追加なし** — Gmail / Calendar 新コネクタ追加時も `connector:google_mail:refresh_token` / `connector:google_calendar:refresh_token` を **作らず**、Phase 13 確定の `connector:google_workspace:refresh_token` 1 slot を 3 connector (Drive / Gmail / Calendar) が共有 (1 Google account = 1 principal、ADR-0010 §Phase 14 改訂 (m))。constants 定数の物理位置は Phase 14 G2 (#294) で `src/opshub/connectors/google_workspace/auth.py` から **`src/opshub/connectors/google_auth/auth.py`** に移動 (shared auth foundation 抽出。G1 時点では `google_common` を仮置き名としていたが、G2 着手時の再評価で catch-all 化リスク回避のため `google_auth` を採用、ADR-0010 §Phase 14 改訂 (m) 要点 7 と整合)、Phase 13 既存 `google_workspace` connector は新場所から import に re-wire。keyring key 文字列 `connector:google_workspace:refresh_token` 自体は変更なし (slot 物理アドレス維持 = 既存 operator の re-consent を最小化)
- **6 env var overrides (Phase 14 でも env 件数は 6 件のまま)** — `OPSHUB_CONNECTOR_GITHUB_PAT` / `OPSHUB_CONNECTOR_SLACK_TOKEN` / `OPSHUB_CONNECTOR_MS365_REFRESH_TOKEN` / `OPSHUB_CONNECTOR_BOX_REFRESH_TOKEN` / `OPSHUB_CONNECTOR_TEAMS_TOKEN` / `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` の 6 系統。`OPSHUB_CONNECTOR_<NAME>_<PURPOSE>` パターンは Phase 3 で確立した変換規則 (`src/opshub/core/secrets.py::_env_var_for_key`) をそのまま再利用 (`"connector:slack:token"` → `"OPSHUB_CONNECTOR_SLACK_TOKEN"`)。Phase 11 で `OPSHUB_CONNECTOR_TEAMS_TOKEN`、Phase 13 で `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` も同変換規則で導出される。**Phase 14 改訂では env 名追加なし** — `OPSHUB_CONNECTOR_GOOGLE_MAIL_REFRESH_TOKEN` / `OPSHUB_CONNECTOR_GOOGLE_CALENDAR_REFRESH_TOKEN` を **作らず**、`OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` 1 系統を Drive / Gmail / Calendar 3 connector が共有 (ADR-0010 §Phase 14 改訂 (m) と整合)
- **OAuth refresh token rotation の永続化** — MS365 (Microsoft Identity) と Box (Box SDK) は access token 取得 / refresh のたびに refresh_token を rotate する。両 connector とも `store_tokens` / acquire-by-refresh コールバックを実装し、rotate された refresh_token が即 keyring (または env var override 経路) に書き戻されることを保証。pin test:
  - MS365: `tests/unit/connectors/ms365/test_auth.py::test_get_access_token_persists_rotated_refresh_token`
  - Box: `tests/unit/connectors/box/test_auth.py::test_get_access_token_persists_rotated_refresh_token` (および `test_get_access_token_refreshes_when_cache_expired_by_time` で `BOX_REFRESH_TOKEN_SECRET_KEY` 経路を二重 pin)
  - Google (Phase 13 G3 配置 / Phase 14 G2 で shared auth foundation 経由に移動済): Phase 14 G2 (#294) で `tests/unit/connectors/google_auth/test_auth.py::test_get_access_token_persists_rotated_refresh_token` に **移動済** (`src/opshub/connectors/google_workspace/auth.py` を **`src/opshub/connectors/google_auth/auth.py`** に抽出したため、test も shared 側に集約。3 connector 分の rotation pin test 重複を防ぐ。G1 時点では `google_common` を仮置き名としていたが、G2 着手時の再評価で `google_auth` を採用)。`connector:google_workspace:refresh_token` keyring slot + `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override 経路を MS365 / Box と同型で pin。Google OAuth 2.0 `access_type=offline` + `prompt=consent` で取得した Refresh Token は documented rotation を行うため、書き戻し忘れは次回 refresh での失効を意味する。ADR-0010 §Phase 13 改訂 (h) で「MS365 / Box pattern を Google Workspace に適用」と確定したため、本節の rotation pin リストに google_workspace を 3 件目として追加 (Phase 13 plan §1 OQ4 で「ADR-0010 + ADR-0014 + ADR-0025 の 3 ADR 改訂、新規 ADR ゼロ」を確定)。**Phase 14 改訂で slot scope 拡大**: 本 slot は Phase 13 では Drive 専用 (`drive.readonly`) として pin したが、Phase 14 G1 / G2 で **Drive + Gmail + Calendar 全般 (`drive.readonly + gmail.readonly + calendar.readonly`)** に scope 拡大 (ADR-0010 §Phase 14 改訂 (m) と整合)。**新 slot 追加なし** (`connector:google_mail:refresh_token` / `connector:google_calendar:refresh_token` は作らない)、1 Google account = 1 principal を 3 connector (Drive / Gmail / Calendar) が共有。env override 名 `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` は変更なし、3 connector が同一 env を参照。既存 operator は scope 拡張により既存 refresh token を 1 回 invalidate (Google OAuth incremental authorization の挙動)、`opshub connector auth set google_workspace` 再実行で全 scope 取得 (詳細は Phase 14 G5 で `docs/upgrading.md` Phase 14 行 + `docs/google-workspace-setup.md` 更新)
- **Paste-code OAuth flow** — MS365 / Box は OAuth authorization code flow を採用するため、`opshub connector auth set connector:ms365` / `opshub connector auth set connector:box` の interactive CLI に「ブラウザで URL を開き、redirect された code を貼り付け」する paste-code 経路を実装 (`src/opshub/cli/connector.py`)。GitHub PAT / Slack bot token の「token 文字列を直接貼り付け」とは別経路だが、storage の終着点は同じ keyring service `"opshub"` に揃う
- **`connector:slack` CLI alias** — Phase 7 follow-up PR #133 (`fix(cli): accept connector:slack as alias for slack`) で、`opshub connector auth set slack` (legacy bare form) と `opshub connector auth set connector:slack` (Phase 7 plan で揃えた `connector:<name>` 形) の両方を accept。`src/opshub/cli/connector.py` の `name in ("slack", "connector:slack")` 分岐。keyring key 自体は常に `connector:slack:token` に正規化される (Phase 7.x で ADR-0018 採択により旧 `connector:slack:bot_token` から rename)

本 ADR の Decision (薄ラッパー + 規約ベースの key 命名 + env var override + `secrets` extras 隔離) は Phase 7 の 3 倍規模の connector 追加でも破綻なく機能した。Phase 3 で確定した signature 変更は本 phase で発生せず (ADR-0010 Phase 7 Validation と同じ整合性)、ADR-0014 は Phase 7 で touch せず Phase 7.x 以降の Additional connectors / common OAuth helper 抽出 (`src/opshub/connectors/_oauth_paste.py` 仮) のタイミングで再評価する。

## 関連

- ADR-0001 (Python Stack、`secrets` extras 設計)
- ADR-0005 (External Content Minimization、token は metadata 扱い、最小保持)
- ADR-0010 (Connector Contract、本 ADR で token storage 経路を確定。Phase 11 改訂 (d) で Teams User Token / Phase 13 改訂 (h) で Google Workspace Refresh Token = MS365 / Box pattern / Phase 14 改訂 (m) で google_workspace slot scope 拡張 + shared auth foundation 抽出を本 ADR に追加)
- principles.md §1 (Local-first)
- Phase 3 epic: #43
- Phase 13 plan §1 OQ4 / §2 改訂 ADR (`docs/phase-13-plan.md`)
- Phase 14 plan §1 OQ6 / §2 改訂 ADR (`docs/phase-14-plan.md`) — Google Workspace slot scope 拡張 + shared auth foundation 抽出 (本 ADR §Phase 7 Validation 節 Phase 14 改訂)
