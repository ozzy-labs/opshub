# 0014. SaaS Token Storage

- Status: Accepted (revised 2026-05-31 for Phase 13 Sub-issue G1: rotation pin リストに google_workspace を追加)
- Date: 2026-05-17 (initial); 2026-05-31 (Phase 13 改訂: §Phase 7 Validation 節の rotation pin リストに `connector:google_workspace:refresh_token` を追加)
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

- **4 keyring keys (1 per connector)** — `connector:github:pat` / `connector:slack:token` / `connector:ms365:refresh_token` / `connector:box:refresh_token`。secret kind (`pat` / `token` / `refresh_token`) は connector 側の OAuth 仕様に合わせて選択し、key 文字列はそのまま `core/secrets` の lookup key になる (`src/opshub/connectors/{github,slack,ms365,box}/auth.py` の `*_SECRET_KEY` 定数)。Slack の suffix が `token` で principal を含まないのは [ADR-0018](0018-slack-token-principal.md) で User Token first-class + Bot Token alternative の両方を同じ slot に格納する設計を採択したため。Phase 11 改訂で Teams が `connector:teams:token` を 5 件目として追加 (ADR-0010 §Phase 11 改訂 (d)、verbatim user token pattern)、Phase 13 改訂で Google Workspace が `connector:google_workspace:refresh_token` を 6 件目として追加 (ADR-0010 §Phase 13 改訂 (h)、MS365 / Box pattern)
- **4 env var overrides** — `OPSHUB_CONNECTOR_GITHUB_PAT` / `OPSHUB_CONNECTOR_SLACK_TOKEN` / `OPSHUB_CONNECTOR_MS365_REFRESH_TOKEN` / `OPSHUB_CONNECTOR_BOX_REFRESH_TOKEN` の 4 系統。`OPSHUB_CONNECTOR_<NAME>_<PURPOSE>` パターンは Phase 3 で確立した変換規則 (`src/opshub/core/secrets.py::_env_var_for_key`) をそのまま再利用 (`"connector:slack:token"` → `"OPSHUB_CONNECTOR_SLACK_TOKEN"`)。Phase 11 で `OPSHUB_CONNECTOR_TEAMS_TOKEN`、Phase 13 で `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` も同変換規則で導出される
- **OAuth refresh token rotation の永続化** — MS365 (Microsoft Identity) と Box (Box SDK) は access token 取得 / refresh のたびに refresh_token を rotate する。両 connector とも `store_tokens` / acquire-by-refresh コールバックを実装し、rotate された refresh_token が即 keyring (または env var override 経路) に書き戻されることを保証。pin test:
  - MS365: `tests/unit/connectors/ms365/test_auth.py::test_get_access_token_persists_rotated_refresh_token`
  - Box: `tests/unit/connectors/box/test_auth.py::test_get_access_token_persists_rotated_refresh_token` (および `test_get_access_token_refreshes_when_cache_expired_by_time` で `BOX_REFRESH_TOKEN_SECRET_KEY` 経路を二重 pin)
  - Google Workspace (Phase 13 改訂、Sub-issue G3 で配置予定): `tests/unit/connectors/google_workspace/test_auth.py::test_get_access_token_persists_rotated_refresh_token` — `connector:google_workspace:refresh_token` keyring slot + `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` env override 経路を MS365 / Box と同型で pin。Google OAuth 2.0 `access_type=offline` + `prompt=consent` で取得した Refresh Token は documented rotation を行うため、書き戻し忘れは次回 refresh での失効を意味する。ADR-0010 §Phase 13 改訂 (h) で「MS365 / Box pattern を Google Workspace に適用」と確定したため、本節の rotation pin リストに google_workspace を 3 件目として追加 (Phase 13 plan §1 OQ4 で「ADR-0010 + ADR-0014 + ADR-0025 の 3 ADR 改訂、新規 ADR ゼロ」を確定)
- **Paste-code OAuth flow** — MS365 / Box は OAuth authorization code flow を採用するため、`opshub connector auth set connector:ms365` / `opshub connector auth set connector:box` の interactive CLI に「ブラウザで URL を開き、redirect された code を貼り付け」する paste-code 経路を実装 (`src/opshub/cli/connector.py`)。GitHub PAT / Slack bot token の「token 文字列を直接貼り付け」とは別経路だが、storage の終着点は同じ keyring service `"opshub"` に揃う
- **`connector:slack` CLI alias** — Phase 7 follow-up PR #133 (`fix(cli): accept connector:slack as alias for slack`) で、`opshub connector auth set slack` (legacy bare form) と `opshub connector auth set connector:slack` (Phase 7 plan で揃えた `connector:<name>` 形) の両方を accept。`src/opshub/cli/connector.py` の `name in ("slack", "connector:slack")` 分岐。keyring key 自体は常に `connector:slack:token` に正規化される (Phase 7.x で ADR-0018 採択により旧 `connector:slack:bot_token` から rename)

本 ADR の Decision (薄ラッパー + 規約ベースの key 命名 + env var override + `secrets` extras 隔離) は Phase 7 の 3 倍規模の connector 追加でも破綻なく機能した。Phase 3 で確定した signature 変更は本 phase で発生せず (ADR-0010 Phase 7 Validation と同じ整合性)、ADR-0014 は Phase 7 で touch せず Phase 7.x 以降の Additional connectors / common OAuth helper 抽出 (`src/opshub/connectors/_oauth_paste.py` 仮) のタイミングで再評価する。

## 関連

- ADR-0001 (Python Stack、`secrets` extras 設計)
- ADR-0005 (External Content Minimization、token は metadata 扱い、最小保持)
- ADR-0010 (Connector Contract、本 ADR で token storage 経路を確定。Phase 11 改訂 (d) で Teams User Token / Phase 13 改訂 (h) で Google Workspace Refresh Token = MS365 / Box pattern を本 ADR に追加)
- principles.md §1 (Local-first)
- Phase 3 epic: #43
- Phase 13 plan §1 OQ4 / §2 改訂 ADR (`docs/phase-13-plan.md`)
