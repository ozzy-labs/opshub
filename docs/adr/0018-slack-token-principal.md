# 0018. Slack Connector Token Principal — User Token First-Class

- Status: Accepted
- Date: 2026-05-18
- Deciders: ozzy

## Context

Phase 7 (epic #113) で Slack connector を実装した際、token storage 経路 (ADR-0014) に従って keyring key を `connector:slack:bot_token` と命名し、`SlackAuth` の docstring も Slack Bot Token (`xoxb-...`) を第一前提として記述した。User Token (`xoxp-...`) は prefix チェックで accept されてはいたが、コード文言 / docstring / 環境変数名 / 内部定数すべてが Bot Token を first-class、User Token を rare-operator 向けの escape hatch として扱っていた。

しかし opshub の基本性質と他 connector の設計を踏まえると、この principal 選定は乖離している。

### opshub の性質

- **Principle 1 (Local-first)** — opshub は **personal Operational Memory** を構築する CLI ツール。個人の SaaS 横断業務文脈をローカルに集約することが目的で、組織にデプロイされる team-shared bot ではない
- **Principle 6 (External Content Minimization)** — 取り込むのは summary / metadata / 最小引用のみ。可視範囲の広さは保存内容のサイズに直結しない (取り込み側で常に minimize する)

### 他 connector の principal

Phase 7 完了時点で opshub は 4 connector を持ち、Slack 以外はすべて **user-principal** (インストールした人間名義) で動作する:

| Connector | secret key | credential type | principal |
|---|---|---|---|
| GitHub | `connector:github:pat` | PAT | user |
| Slack | `connector:slack:bot_token` | Bot Token | **bot** ← outlier |
| MS365 | `connector:ms365:refresh_token` | OAuth refresh | user |
| Box | `connector:box:refresh_token` | OAuth refresh | user |

Slack のみが bot-principal で 4 connector の中で唯一乖離していた。

### Bot Token の構造的制約

Slack の Bot Token は以下の可視性制約を持つ ([Token types](https://api.slack.com/authentication/token-types)):

- **public channel**: bot が `/invite` された channel の history のみ可視
- **private channel**: bot がメンバーの channel のみ
- **DM / MPIM**: bot 宛 DM のみ
- **search.messages**: 非対応 (Bot Token は `search:read` scope を持てない)

これに対し User Token は「インストールしたユーザーが Slack UI で見られるもの」が可視範囲になる。personal Operational Memory として「自分の業務文脈を集約したい」という opshub の目的に対して、Bot Token は invite per channel の運用負荷を operator に強い、かつ DM / private / search を構造的に取り込めない。

### 反論側の精査

- **Slack platform は Bot-first を志向している**: 事実だが、classic Web API (slack_sdk 経由) の User Token は active maintenance 下にあり、deprecation schedule も公表されていない。万一 Slack が方針転換したら本 ADR を superseded として書き直す
- **Enterprise Grid / workspace policy で User Token scope が拒否される**: 一部組織では admin が User Token scope を承認しない。これは Bot Token を alternative として残すことで対応する
- **Audit log で bot principal を要求される組織**: 同上、Bot Token alternative で対応
- **token 失効リスク**: User Token はユーザー退職 / パスワード変更で失効するが、opshub は personal tool で operator 自身が両方を握るため実害なし

## Decision

Slack connector の token principal を **User Token first-class** に転換する。Bot Token は alternative として継続 support。

具体決定:

1. **first-class**: User Token (`xoxp-...`、Slack app の OAuth & Permissions ページ "User Token Scopes" で発行)
2. **alternative**: Bot Token (`xoxb-...`、同ページ "Bot Token Scopes" で発行) — workspace policy で User Token scope が拒否される、または audit policy で bot principal を要求するケース向け
3. **secret key 命名**: principal を baked-in しない `connector:slack:token` に rename (`pat` / `refresh_token` と同じ「credential type」軸での命名)。GitHub PAT が user-principal だが key 名に `user` を含まないのと同様、Slack も principal-neutral な suffix にする
4. **環境変数**: `OPSHUB_CONNECTOR_SLACK_TOKEN` (旧 `OPSHUB_CONNECTOR_SLACK_BOT_TOKEN`)
5. **prefix チェック**: `xoxp-` / `xoxb-` 両許可は維持。docstring / error message での記述順序は `xoxp-` 先
6. **principal 可観測性**: `SlackAuth.test_token()` の返り値に `principal: "user" | "bot"` を追加 (Slack `auth.test` の `bot_id` 有無で判別)
7. **MVP scope**: `channels:history` / `channels:read` / `users:read` (3 scope) を User Token Scopes として登録する前提に書き換え。optional scope menu として `groups:history` (private) / `im:history` (DM) / `mpim:history` (MPIM) / `search:read` (search) / `files:read` (files) を README に記載

## Consequences

### Positive

1. **opshub の他 connector との principal 一貫性** — 4 connector が等しく user-principal で動作し、「personal Operational Memory」という positioning と整合
2. **operator の運用負荷削減** — Bot Token 必須だった `/invite @opshub-bot` per channel が不要 (User Token なら operator が見られる channel は自動カバー)
3. **DM / private / search の取り込み経路が開く** — optional scope の追加で operational context の取りこぼしを減らせる
4. **principal を code から観測可能** — `SlackAuth.test_token()` 返り値の `principal` フィールドで使用中の token 種別を確認できる

### Negative / Trade-offs

1. **User Token は失効リスクが Bot Token より高い** — ユーザー退職 / パスワード変更で revoke される。personal tool 用途では operator 自身が握るため実害は薄いが、企業内 SSO の token rotation policy に当たれば再 install が必要
2. **Slack platform の bot-first 化に逆行する選択** — Slack 側が User Token capability を縮退した場合、本 ADR を superseded として書き直す必要がある
3. **Enterprise Grid の workspace policy で User Token が拒否されるケース** — その workspace では Bot Token alternative を使うことになり、可視範囲の制約 (invite per channel / DM 不可) は依然として発生する
4. **breaking change for existing operators** — key 名 / env var 名のリネームで既存 keyring 保存値・env var 設定は失効する。opshub v0.1.x かつ実ユーザーゼロ前提のため互換 shim は導入しない (本 ADR 採択時点での状態評価)

## Alternatives Considered

### 1. Bot Token を first-class のまま継続 (現状維持)

却下: opshub の他 3 connector が user-principal で動作する中、Slack のみ bot-principal という outlier は personal Operational Memory の positioning と乖離する。Bot Token の独立 identity / 退職耐性 / 組織監査の利点は team-installed integration app に対するもので、personal tool では benefit にならない。

### 2. Bot Token を完全に廃止し、User Token のみ受理

却下: Enterprise Grid 等で workspace policy が User Token scope を拒否する組織が存在する。Bot Token を alternative として残すことで、そのような組織でも opshub を運用可能に保つ。

### 3. key 名を `connector:slack:user_token` に rename

却下: principal を key 名に baked-in する設計は再現性が低い (Bot Token 採用時にこの key 名と矛盾する)。GitHub PAT が user-principal でも key 名に `user` を含まないのと整合させ、principal-neutral な `connector:slack:token` を採択する。

### 4. dual-read / deprecation period 付きで段階移行

却下: opshub は v0.1.x で実ユーザーゼロ前提 (本 ADR 採択時点)。互換 shim は技術的負債を固定化するだけで benefit がない。1.0 リリース以降に同様の principal 変更を行う場合は別途互換戦略を ADR 化する。

## Validation

本 ADR の決定は以下のテストで pin する:

- `tests/unit/connectors/slack/test_auth.py`:
  - `SLACK_TOKEN_SECRET_KEY == "connector:slack:token"` 定数 pin
  - `OPSHUB_CONNECTOR_SLACK_TOKEN` env var override の end-to-end test
  - `SlackAuth.test_token()` 返り値に `principal: "user" | "bot"` を含む pin
  - `xoxp-` token が construction で reject されない pin (既存) は維持
- `tests/unit/cli/test_connector_auth.py`:
  - CLI writer (`opshub connector auth set slack` / `connector:slack`) が新 key (`connector:slack:token`) に書く round-trip pin
- `tests/integration/test_phase7_slack_sync.py` / `test_phase7_connector_atomicity.py` / `test_phase7_lifecycle.py`:
  - sync e2e で新 env var (`OPSHUB_CONNECTOR_SLACK_TOKEN`) を使用、cursor 永続化と event 連鎖が破綻しないことを確認

## 関連

- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — 本 ADR の決定で §Phase 7 Validation の Slack key 命名規約に cross-ref を追加
- [ADR-0010: Connector Contract](0010-connector-contract.md) — Connector 責務 (fetch + normalize + event 化) は principal 切替で破綻しないことを確認
- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — token 可視範囲が広がっても保存内容は変わらない (summary ≤ 200 chars enforce)
- [Principles 1: Local-first](../principles.md) — personal Operational Memory positioning の根拠
- Phase 7 Sub A: Slack connector ([#110](https://github.com/ozzy-labs/opshub/issues/110)) — 本 ADR 起票の起点となった実装
- [ADR-0033: Slack Mention / DM Demand Digest](0033-slack-mention-demand-digest.md) — 本 ADR §決定 7 で MVP として登録する `channels:read` / `channels:history` (User Token) または invite 済 channel の Bot Token 同等 scope のみで、@mention / DM / MPIM の demand 信号検出が完結する (`search:read` scope 追加不要)。ADR-0033 §(a) §不変条件 7 で本 ADR を cross-ref
