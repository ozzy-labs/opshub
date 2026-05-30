# 0005. External Content Minimization

- Status: Superseded by ADR-0020
- Date: 2026-05-16
- Deciders: ozzy

> **Superseded (2026-05-30, ADR-0020 Full Local Content Retention)**: Phase 10 で opshub を秘書エージェント・プラットフォームへ拡張するにあたり、本 ADR の「summary / metadata / minimal quote のみ保持し full body を持たない」方針を撤回し、外部 connector が取り込んだ本文をローカル保持する方針へ転換した ([ADR-0020](0020-full-local-content-retention.md))。返信下書き生成 / 本文ベース検索が summary では成立しないこと、storage / context 懸念が Phase 4-8 の設計で別解済みであることが転換理由。残る機密・poisoning 懸念は本文を持たないことでの偶発的縮小ではなく、excludes (観測前遮断) と保存時暗号化 ([ADR-0021](0021-encryption-at-rest.md)) と provenance タグ (利用時の信頼度明示) の 3 層で明示的に対処する。以下は転換前の歴史的記録として残す。

## Context

OpsHub の Connector は GitHub / Slack / Microsoft 365 / Box などから業務情報を取り込む。これらに含まれる情報には次の特性がある。

1. **機密性が高い** — Slack DM / 社内メール / 機密文書本文
2. **法的制約がある** — GDPR / 顧客機密 / NDA 対象
3. **SaaS 利用規約上の制約** — ローカルへの大量保持を制限する条項
4. **ストレージ / 検索インデックスへの負荷** — full body をすべて取り込むと SQLite が肥大化
5. **agent コンテキスト効率** — 大量本文は LLM context を埋めて精度を落とす

業務文脈の保持には、必ずしも本文全文は不要。多くの場合「何が議題で、何が決まり、次に何をするか」が分かれば十分。

## Decision

外部 SaaS の **full content を Operational Memory に保持しない**。Connector が取り込むのは以下のみ。

### 保持するもの

| 種別 | 例 |
|---|---|
| External ID | GitHub issue number / Slack message TS / Box file ID |
| URL | 元コンテンツへの permalink |
| Summary | LLM 生成または抽出された 1-3 文要約 |
| Metadata | title / author / participants / labels / created_at / updated_at |
| Extracted Action Items | LLM 抽出した actionable items |
| Minimal Quotation | 文脈保持のための最小引用 (例: 数行) |

### 保持しないもの

| 種別 | 理由 |
|---|---|
| Full Slack history | 機密 + 容量 + SaaS TOS |
| Full email bodies | 機密 + 容量 |
| Confidential documents | 機密 |
| Credentials / Access tokens | セキュリティ (OS keychain で別管理) |
| Binary artifacts | 容量 + 検索性なし |
| 個人情報 (連絡先・住所等) | privacy |

### Cache 戦略

短期 cache は `~/.cache/opshub/` に置く。

- 保持期限: デフォルト 24h、user-configurable
- 用途: agent が summary 生成や action item 抽出を再実行する際の API 再ヒット回避
- projection ではない (event store 外、再構築可)
- 期限切れで自動削除

### Connector 実装ルール

1. Connector は fetch → **summary 化 / minimal quote 抽出** → event 生成、の経路を取る
2. 生 body は cache に書くが Operational Memory には書かない
3. 機密分類が不明な場合は **取り込まない** (fail-safe)
4. ユーザー指定の `~/.config/opshub/excludes.yaml` で特定 channel / sender / repo を除外可能

## Consequences

### Positive

1. **法的・契約的リスク低減** — 機密 / TOS 違反リスクを最小化
2. **小さい event store** — 個人利用で年単位でも数百 MB 以内に収まる見込み
3. **agent context 効率** — summary 中心の取り込みで LLM context を圧迫しない
4. **検索品質** — full text 検索より「要点検索」の方が agent triage に向く

### Negative / Trade-offs

1. **再フェッチコスト** — full body が必要な操作 (詳細レビュー等) で SaaS API を再叩く必要がある
2. **summary 品質依存** — LLM 抽出が誤ると重要情報を取りこぼす
3. **「あの会話の続き」が見えにくい** — Slack スレッドの全文が手元にない
4. **complianceの責任は引き続きユーザー** — OpsHub は help するが保証はしない

## 軽減策

1. **`opshub source open <id>`** で元コンテンツの URL をブラウザで開ける CLI を提供
2. **`opshub source refetch <id>`** で必要時に本文を一時取得 (cache に保存、期限後削除)
3. **summary 不足の検出** — extracted action items 0 件 / summary 過短 etc. を `opshub doctor` で検出
4. **excludes 設定の dry-run** — Connector sync 前に「何を取り込むか」を preview する機能

## Alternatives Considered

### 1. Full body を取り込む (最大利便性)

却下理由:

- 機密 / TOS / 容量 / agent context のすべてで不利
- 個人利用でも秒単位で増える Slack history を全保持するのは現実的でない

### 2. Encrypted local body保持

却下理由:

- 機密性は担保できるが TOS / 容量問題は残る
- agent からの利用に decrypt が必要で API が複雑化
- summary ベースで充分な業務文脈は得られる

### 3. ユーザー判断で full / minimal を選べるフラグ

却下理由:

- 設計が複雑化 (Operational Memory のスキーマが分岐)
- 「何が機密か」をユーザーがソース単位で判断するのは現実的でない
- minimal 統一で運用ポリシーを単純化する方が安全

### 4. Vector embedding は full body から、保管は summary のみ

検討に値する。Phase 4 で再評価。embedding 計算時のみ full body を一時取得 → embed → 元 body は破棄。これなら semantic 検索品質を保ちつつ minimal 方針を維持できる。

## 関連

- [Principles 6 (External Content Minimization)](../principles.md)
- [Architecture 8.1 (External Content Retention)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
