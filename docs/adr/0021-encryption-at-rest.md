# 0021. Encryption at Rest

- Status: Accepted
- Date: 2026-05-30
- Deciders: ozzy

## Context

ADR-0020 (Full Local Content Retention) は ADR-0005 (External Content Minimization) を Superseded とし、外部 connector が取り込んだ本文をローカル SQLite に保持する方針へ転換した。本文には機密文書 / Slack DM / 社内メールが含まれうるため、ADR-0020 §(c) は「ディスク上の平文露出を許容しない」= 保存時暗号化を必須要件として切り出した。本 ADR はその暗号化方式と鍵管理を確定する (Phase 10 Open Q #2)。

opshub の storage は単一 SQLite ファイル (ADR-0001 / ADR-0002)。event log (`events` table、本文を含む `SourceObserved` payload) と全 projection (本文を materialise した `sources.body` を含む) が同一 DB に同居する。暗号化の選択肢は 2 系統:

1. **DB 丸ごと暗号化** — SQLCipher (SQLite の透過的 AES 暗号化派生) で DB ファイル全体を暗号化する。アプリ層は通常の SQLAlchemy / SQL でアクセスし、暗号化・復号は driver 層で透過的に行われる。
2. **アプリ層の列暗号化** — `sources.body` / event payload の本文列のみをアプリ層で暗号化して TEXT/BLOB に格納する。鍵は同じく keyring。

第一の論点は **平文露出面の広さ** である。本文は `SourceObserved` event payload (`events.payload`) と `sources.body` projection の 2 箇所に存在し、さらに Sub-issue B で FTS インデックス (`sources_fts` 等) と embedding 用の一時データにも波及する。列暗号化は「どの列・どの index が本文を含むか」を実装者が網羅し続ける必要があり、1 箇所でも漏れると平文がディスクに落ちる。FTS index は暗号化列を index できない (検索が成立しない) という根本的な矛盾も抱える。

第二の論点は **鍵管理** である。どちらの方式でも DB 暗号鍵は keyring (ADR-0014) に保管し、opshub が自前で鍵をディスクに書かない方針を継承する。鍵不在時の挙動 (新規 DB なら鍵生成、既存 DB なら鍵必須) を明確にする必要がある。

第三の論点は **性能と依存サイズ** である。SQLCipher は `pysqlcipher3` / `sqlcipher3-binary` 等の native extension を要し、ADR-0001 の `uv tool install opshub` 配布制約 (OS-specific binary を core 依存にしない) と整合させる必要がある。列暗号化は pure-Python (`cryptography`) で済むが、前述の網羅性問題が残る。

## Decision

opshub の SQLite DB を **SQLCipher で丸ごと AES-256 暗号化** する。鍵は ADR-0014 の keyring 経路を再利用する。アプリ層の列暗号化は却下する。以下を pin する。

### (a) SQLCipher で DB 丸ごと AES-256

DB ファイル全体を SQLCipher (AES-256-CBC + HMAC-SHA512、SQLCipher 4 default) で透過的に暗号化する。event payload・全 projection・FTS index・あらゆる派生テーブルが **例外なく** 暗号化されるため、本文がどのテーブル / index に波及しても平文露出面が構造的に生まれない。

driver 層で復号されるため、SQLAlchemy / projection / service / FTS query は暗号化を意識しない (透過性)。`db/engine.py` の connect listener で `PRAGMA key = '<key>'` を全 connection に適用する (既存の PRAGMA listener と同経路、SQLCipher の鍵は connection ごとに必要なため)。

### (b) 鍵は keyring 再利用 (ADR-0014)

DB 暗号鍵は keyring service `"opshub"` の専用 key (`db:encryption_key`) に保管する。ADR-0014 の `core/secrets` 薄ラッパー (env var override `OPSHUB_DB_ENCRYPTION_KEY` → keyring の順) をそのまま再利用し、opshub が鍵をディスク平文に書かない原則を継承する。

鍵不在時の挙動:

- **新規 DB (ファイル未作成) かつ鍵未設定** — `opshub init` が CSPRNG (`secrets.token_hex`) で鍵を生成し keyring に保管する。生成した鍵で DB を初期化。
- **既存の暗号化 DB かつ鍵不在** — 復号不能。actionable error (`db:encryption_key が見つかりません。OPSHUB_DB_ENCRYPTION_KEY env var を設定するか keyring を確認してください`) で fail-fast し、誤って新規平文 DB を作らない。
- **暗号化無効 (opt-out)** — `[storage] encryption = false` で平文 DB を許容する (CI / 一時環境 / 暗号化を望まない operator 向け)。default は `encryption = false` (opt-in)。本文を保持する以上 sensitive workload を扱う operator は明示的に opt-in する。cold-install footprint を保つため SQLCipher 依存は extras (`encryption`) で隔離 (§(d))。

Phase 18 改訂: `[storage] encryption` の TOML 読込経路は [ADR-0032](0032-runtime-toml-config-loading.md) で実装される。

### (c) 列暗号化は却下

`sources.body` / event payload 列のみをアプリ層で暗号化する案は却下する (§Alternatives #1)。理由は平文露出面の網羅困難 + FTS との非互換 (本 ADR §Context)。

### (d) SQLCipher 依存は extras 隔離 (ADR-0001 整合)

SQLCipher native binding (`sqlcipher3-binary` 等) は `[project.optional-dependencies]` の専用 extras に隔離し、core install を軽量に保つ (ADR-0014 §`secrets` extras と同方針)。暗号化 DB を使う operator のみ `opshub[encryption]` を install する。extras 未 install で `encryption = true` の場合は actionable error で「`opshub[encryption]` を install せよ」と促す。

## Consequences

### Positive

1. **平文露出面ゼロ** — event / projection / FTS index / 派生テーブルすべてが透過的に暗号化され、本文の波及先を実装者が網羅し続ける必要がない。
2. **透過性** — SQLAlchemy / projection / service / FTS は暗号化を意識せず、本文ベース検索 (Sub-issue B) がそのまま暗号化 DB 上で動く。
3. **鍵管理の一元化** — DB 鍵も SaaS token も同じ keyring 経路 (ADR-0014)、operator は一つのメンタルモデルで済む。
4. **event-sourced 整合** — 暗号化は storage 層に閉じ、event immutability / replay / rollback (ADR-0020 §poisoning 緩和) に影響しない。

### Negative / Trade-offs

1. **native 依存** — SQLCipher は native binding を要し pure-SQLite より install が重い。
   - 緩和: extras 隔離 (§(d))、暗号化を使わない operator は影響なし。
2. **性能オーバーヘッド** — AES 透過暗号化で read/write に数 % のコスト。個人利用スケールでは無視できる範囲。
3. **鍵紛失 = DB 復号不能** — keyring から鍵を失うと既存 DB が読めなくなる。
   - 緩和: actionable error で鍵不在を fail-fast、env var override (`OPSHUB_DB_ENCRYPTION_KEY`) でバックアップ鍵を注入する経路、operator への鍵バックアップ案内を docs に記載。
4. **headless / CI で keyring 不在** — ADR-0014 と同じ headless Linux 問題。
   - 緩和: env var override 経路、または `encryption = false` で平文 DB。

## Alternatives Considered

### 1. アプリ層の列暗号化 (`sources.body` / event payload 列のみ)

却下理由:

- **平文露出面の網羅困難** — 本文は event payload + projection + Sub-issue B の FTS index + embedding 一時データに波及する。どの列・index が本文を含むか実装者が網羅し続ける必要があり、1 箇所漏れるとディスクに平文が落ちる。
- **FTS との根本的非互換** — 暗号化列は FTS で index できず全文検索が成立しない。本文検索 (Sub-issue B) と列暗号化は両立不能。
- SQLCipher の DB 丸ごと暗号化なら波及先すべてが透過的に守られ、網羅問題も FTS 非互換も発生しない。

### 2. OS / ファイルシステムレベルの暗号化に委ねる (FileVault / LUKS / dm-crypt)

却下理由:

- operator 環境依存で opshub が保証できない (WSL2 / 暗号化していない外部ボリューム / 一時マウント)。
- 「opshub が本文を保持するなら opshub が保存時暗号化を提供する」という責任境界を明確にする (ADR-0020 §(c) が opshub の要件として切り出した)。
- DB 単位の暗号鍵 (keyring) なら opshub が鍵ライフサイクルを制御でき、OS 全体暗号化より粒度が適切。

### 3. 暗号化なし + 機密本文を excludes で除外して運用回避

却下理由:

- excludes (ADR-0020 §(b)) は「取り込まない」前段の防御で、取り込んだ本文の保存時保護にはならない。機密判定が完全でない以上、保存時暗号化は別レイヤーとして必須。
- 本文を保持する以上、保存時暗号化を opt-in で備えること自体がアシスタントプラットフォームの信頼性要件 (default は §(b) のとおり `encryption = false`、sensitive workload を扱う operator は明示的に opt-in する)。

## 関連

- [ADR-0020: Full Local Content Retention](0020-full-local-content-retention.md) — 本文保持の保存時暗号化要件 (§(c)) を確定する姉妹 ADR、本 ADR の前提。
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — DB 暗号鍵を keyring + env var override で管理する経路の再利用元 (§(b))。
- [ADR-0001: Python Stack](0001-python-stack.md) — `uv tool install opshub` 配布制約、SQLCipher native binding を extras 隔離する根拠 (§(d))。
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — 暗号化が storage 層に閉じ event immutability / replay に影響しない根拠。
- [Phase 10 Plan §1 #4 / §3 Sub-issue A / §4-A / §8 Open Q #2](../phase-10-plan.md)
