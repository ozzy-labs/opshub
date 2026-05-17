# 0014. SaaS Token Storage

- Status: Accepted
- Date: 2026-05-17
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

## 関連

- ADR-0001 (Python Stack、`secrets` extras 設計)
- ADR-0005 (External Content Minimization、token は metadata 扱い、最小保持)
- ADR-0010 (Connector Contract、本 ADR で token storage 経路を確定)
- principles.md §1 (Local-first)
- Phase 3 epic: #43
