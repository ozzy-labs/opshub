# 0020. Full Local Content Retention

- Status: Accepted
- Date: 2026-05-30
- Deciders: ozzy

## Context

ADR-0005 (External Content Minimization) は Phase 3-9 を通じて「外部 SaaS の full content を Operational Memory に保持しない。Connector が取り込むのは summary / metadata / minimal quote のみ」という方針を pin してきた。当時の根拠は (1) 機密性、(2) 法的制約、(3) SaaS 利用規約、(4) storage 肥大化、(5) agent context 効率の 5 点であり、業務文脈の保持には「何が議題で、何が決まり、次に何をするか」が分かれば十分という前提に立っていた。

Phase 10 (秘書エージェント・プラットフォーム化、epic #203) でこの前提が崩れる。opshub を「人間 → 秘書エージェント → opshub コマンド」の三層モデルへ再定義する過程で、秘書エージェントが実際に役立つには **本文そのもの** が要る局面が支配的になる:

1. **返信下書き生成 (Sub-issue E)** — 「この Slack スレッドへの返信案を書いて」に応えるには元メッセージの本文が必須。summary では文体・固有名詞・依頼の機微が落ち、下書き品質が崩壊する。
2. **本文ベースの横断検索 (Sub-issue B)** — ADR-0012 §Alternative #4 で「embedding は full body から、保管は summary のみ」を Phase 4 で再評価対象としていた。秘書ユースケースでは「あの仕様の details が書いてあった文書」を引くのに summary embedding では recall が不足する。SQLite FTS の全文検索も本文がなければ成立しない。
3. **agent が判断材料を再取得できない構造** — ADR-0005 §軽減策の `source refetch` は SaaS API への再アクセスを前提とするが、Phase 9 box_drive のように API 経路を持たない connector や、レート制限・ネットワーク制約下では「手元に本文がない = 判断不能」になる。

一方で、ADR-0005 が挙げた懸念のうち **(4) storage 肥大化と (5) agent context 効率は Phase 4-8 の設計で別解が用意済み** である:

- 本文を SQLite に保持しても、agent に渡すのは recall / FTS で絞り込んだ関連断片のみ。context window に full body を流し込む設計ではない (Sub-issue B / C / D)。
- 個人利用スケール (年単位・数 GB) であれば SQLite + FTS は実用的に動く。

残る本質的な懸念は **(1) 機密性・(2) 法的制約・(3) SaaS TOS** であり、これらは「本文を保持するか否か」ではなく **「保持した本文をどう守り、どう除外し、どう信頼するか」** の設計で対処すべき問題である。pre-userbase の現段階 (AGENTS.md §設計判断のスタンス) では、過去の minimization 決定にとらわれず「秘書として役立つ end-state」を優先する。

本文保持には新たな攻撃面が生まれる。最大のものは **content poisoning / 間接プロンプトインジェクション** である。外部 SaaS から取り込んだ本文には「以前の指示を無視して〜せよ」のような敵対的命令が混入しうる。本文を agent context に流す秘書プラットフォームでは、低信頼の外部本文を高信頼の operator 指示と区別せずに扱うと、秘書が乗っ取られる。ADR-0005 の minimization はこの攻撃面を「そもそも本文を持たない」ことで偶発的に縮小していたが、本文保持に転換する以上、攻撃面の縮小は別途 **provenance (出自・信頼度) タグ** + **event-sourced rollback** で明示的に設計しなければならない。

## Decision

ADR-0005 (External Content Minimization) を **Superseded** とし、opshub は **外部 connector が取り込んだ本文をローカルに保持する** 方針へ転換する。本文保持を安全に成立させるため、以下を pin する。

### (a) 格納先 = event は本文を載せる SSOT、別 content store は置かない (Open Q #1 確定)

本文は `SourceObserved` event payload の `body` フィールド (新規) に載せる。event-sourced architecture (ADR-0002) の純粋性を維持し、本文は projection (`sources.body`) へ通常の reducer 経路で materialise する。event log を SSOT とし、`projections rebuild` で本文を含む read model が完全再構築できる。

「event は参照のみ持ち、本文は再構築可能な別 content store に置く」案は却下する。理由:

- 本文は event の「観測した事実」そのものであり、event の外に置くと「event は本文を再構築できる」前提 (ADR-0002 §replayability) が成立しなくなる。別 store が消えると本文が永久に失われ、event log だけでは復元不能になる。
- 別 content store は二重 storage の整合 (event ↔ body store) を運用で守る必要があり、append-only event log の単純性を破壊する。
- 個人利用スケールでは event payload に本文を載せても storage は実用範囲。

`body` は `SourceObserved` の **backward-compatible な optional field 追加** (ADR-0002 §4) であり `schema_version` は `1` のまま据え置く。既存 Phase 3-9 の source 行は `body = NULL` で挙動が変わらない (本 ADR §(d) backward-compat)。

### (b) 取り込み除外設定 (excludes) を共通機構化 (ADR-0005 §軽減策の昇格 + ADR-0019 §決定 (g) の closeout)

ADR-0005 で言及されていた `~/.config/opshub/excludes.yaml`、および Phase 9 で `box_drive` の `opshub.toml` inline だった `exclude_globs` を、**全 connector 横断の共通 excludes 機構** に統合する。本文保持の第一の安全策は「機密本文をそもそも取り込まない」ことであり、connector ごとにバラバラな除外設定では運用ポリシーを一元管理できない。

excludes は `channel` / `sender` / `repo` / `path` の 4 種の selector を持ち、各 connector が自身の取り込み経路で「観測する前に」除外判定する。box_drive の inline `[connectors.box_drive] exclude_globs` は当面、本機構の shared `excludes.yaml` の `paths` selector に **merge** する (`ExcludeRules.merged_with_paths` 経由)。pre-userbase 段階のため、inline 設定を完全廃止して shared-only に統合するのは将来の cleanup ADR に委ねる。dual-read による互換維持期間 (deprecation window) は設けず、shared-only 化が決まった時点で inline 経路を即時撤去する。

### (c) 保存時暗号化 (詳細は ADR-0021)

本文をローカル保持する以上、ディスク上の平文露出は許容しない。DB 丸ごとの保存時暗号化を ADR-0021 で確定する。鍵は ADR-0014 の keyring 経路を再利用する。

### (d) 認証情報の本文からの分離 + backward-compat

本文ストアにトークン / 認証情報が混入しないことを test で pin する (DoD)。credentials は引き続き keyring (ADR-0014) で別管理し、`body` フィールドには載せない。既存 source 行 (`body = NULL`) は挙動不変。

**例外: ファイル参照 connector の `body` 不保持** — OneDrive item (MS365) と Box event / Box Drive scan のように「ファイル本体ではなくメタデータ (path / actor / event_type) のみを観測する」connector は、`body = NULL` を維持する。これらは ADR-0019 §不変条件 (b) (FS scan は本文を読まない) と同じ posture を SaaS API 側で踏襲しているため、本文抽出は将来の **file-extraction connector (Phase 11+)** へ分離する。本 ADR §(a) の retention 方針は「観測経路に本文があれば持つ」原則であり、参照しか持たない経路は対象外とする (provenance タグは引き続き `external` / `untrusted` で stamp する)。

### (e) provenance タグ (poisoning 緩和の中核)

`sources` に **出自 (provenance) と信頼度 (trust level)** を表すタグを追加する。外部 connector 由来の本文は「外部由来・低信頼」として明示し、operator 手書き (`workspace ingest`) や opshub 内部生成は「内部由来・高信頼」とする。秘書エージェント (Sub-issue D) や propose (Sub-issue E) が低信頼本文を LLM context へ渡す際、prompt 上で「これは外部由来の参考情報であり指示ではない」と明示できる土台にする。これは ADR-0015 §決定 (f) の do-not-follow preamble と組み合わさり、間接プロンプトインジェクションの緩和層を形成する。

## Consequences

### Positive

1. **秘書として役立つ** — 返信下書き / 本文検索 / 詳細レビューが手元の本文だけで完結し、SaaS 再アクセス不要。
2. **本文ベース検索の高品質化** — embedding / FTS が summary ではなく本文から計算され、recall 精度が上がる (Sub-issue B)。
3. **API 経路非依存** — box_drive のように API を持たない connector でも本文 (FS scan で読める範囲) を活用できる。
4. **event-sourced 純粋性維持** — 本文も event に載るため replay / rollback / audit がそのまま効く。

### Negative / Trade-offs

1. **content poisoning / 間接プロンプトインジェクション攻撃面** — 外部本文に「以前の指示を無視せよ」等の敵対的命令が混入しうる。本文を agent context に流す秘書プラットフォームで最大の攻撃面。
   - **緩和: provenance タグ (本 ADR §(e))** で外部由来・低信頼を明示し、agent prompt 上で「参考情報であり指示ではない」と扱う (ADR-0015 §決定 (f) do-not-follow preamble と併用)。
   - **緩和: event-sourced rollback** — 汚染本文を取り込んだと判明した場合、event log から該当 `SourceObserved` を特定でき、excludes 追加 + `projections rebuild` で read model を汚染前状態へ再構築できる。append-only なので「いつ何が取り込まれたか」の audit trail が残る。
   - **緩和: excludes (本 ADR §(b))** で汚染源 channel / sender / repo / path を観測前に遮断できる。
2. **機密本文のローカル保持リスク** — 機密文書 / DM 本文が手元 DB に乗る。
   - 緩和: 保存時暗号化 (ADR-0021) でディスク平文露出を防ぐ、excludes で機密 source を除外、認証情報は本文から分離 (§(d))。
3. **storage 増加** — summary のみ時代より DB が肥大化する。個人利用スケールでは実用範囲だが、巨大 workspace では Phase 10.x で本文 size cap / TTL を再評価する余地を残す。
4. **法的・契約的責任は引き続き operator** — opshub は excludes / 暗号化 / provenance で help するが、SaaS TOS 遵守の保証はしない (ADR-0005 §軽減策 4 の stance を継承)。

## Alternatives Considered

### 1. ADR-0005 を維持 (summary のみ保持を継続)

却下理由:

- Phase 10 の返信下書き / 本文検索が summary では成立しない。秘書プラットフォームの中核機能が欠落する。
- storage / context 懸念は Phase 4-8 の設計 (recall で絞り込み、context に full body を流さない) で別解済み。残る機密懸念は暗号化 + excludes + provenance で対処すべき問題で、本文を持たないことは過剰な制約。

### 2. 本文は別 content store (event は参照のみ)

却下理由:

- event log だけでは本文を再構築できなくなり ADR-0002 §replayability に違反。別 store 消失で本文が永久喪失。
- event ↔ body store の二重整合を運用で守る必要があり append-only の単純性を破壊。
- 個人スケールでは event payload に本文を載せる単純解で十分。

### 3. ユーザーが source 単位で full / minimal を選ぶフラグ

却下理由:

- ADR-0005 §Alternatives #3 と同じ。「何が機密か」を source 単位で判断するのは非現実的、schema が full / minimal で分岐し複雑化。
- 機密対処は per-source フラグではなく excludes (観測前遮断) + 暗号化 (保存時) + provenance (利用時) の 3 層で統一する方が単純で安全。

### 4. provenance タグなしで本文保持 (緩和層を持たない)

却下理由:

- 外部本文をそのまま agent context へ流すと間接プロンプトインジェクションで秘書が乗っ取られる。本文保持に転換する以上、攻撃面縮小を偶発的 (本文を持たない) から明示的 (出自・信頼度で区別) に設計し直すのは必須。

## 関連

- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — 本 ADR で **Superseded**。minimization から retention への転換元。
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — 本文を event payload に載せ projection へ materialise する根拠、rollback / replay で汚染本文を巻き戻せる根拠。
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md) — §Alternative #4 (embedding を本文ベースに) を本文保持で実装可能にする前提 (Sub-issue B)。
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — 認証情報を本文から分離して keyring で別管理する根拠 (§(d))。
- [ADR-0015: LLM Usage Strategy](0015-llm-usage-strategy.md) — §決定 (f) do-not-follow preamble と provenance タグを併用して poisoning を緩和する根拠 (§(e))。
- [ADR-0019: Local-filesystem-backed Connector](0019-local-filesystem-backed-connector.md) — §決定 (g) box_drive inline excludes を共通機構へ統合する closeout (§(b))。
- [ADR-0021: Encryption at Rest](0021-encryption-at-rest.md) — 本文保持の保存時暗号化を確定する姉妹 ADR (§(c))。
- [Phase 10 Plan §1 / §3 Sub-issue A / §4-A / §8 Open Q #1](../phase-10-plan.md)
