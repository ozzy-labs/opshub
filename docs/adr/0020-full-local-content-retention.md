# 0020. Full Local Content Retention

- Status: Accepted
- Date: 2026-05-30
- Deciders: ozzy

## Context

ADR-0005 (External Content Minimization) は Phase 3-9 を通じて「外部 SaaS の full content を Operational Memory に保持しない。Connector が取り込むのは summary / metadata / minimal quote のみ」という方針を pin してきた。当時の根拠は (1) 機密性、(2) 法的制約、(3) SaaS 利用規約、(4) storage 肥大化、(5) agent context 効率の 5 点であり、業務文脈の保持には「何が議題で、何が決まり、次に何をするか」が分かれば十分という前提に立っていた。

Phase 10 (アシスタントエージェント・プラットフォーム化、epic #203) でこの前提が崩れる。opshub を「人間 → アシスタントエージェント → opshub コマンド」の三層モデルへ再定義する過程で、アシスタントエージェントが実際に役立つには **本文そのもの** が要る局面が支配的になる:

1. **返信下書き生成 (Sub-issue E)** — 「この Slack スレッドへの返信案を書いて」に応えるには元メッセージの本文が必須。summary では文体・固有名詞・依頼の機微が落ち、下書き品質が崩壊する。
2. **本文ベースの横断検索 (Sub-issue B)** — ADR-0012 §Alternative #4 で「embedding は full body から、保管は summary のみ」を Phase 4 で再評価対象としていた。アシスタントユースケースでは「あの仕様の details が書いてあった文書」を引くのに summary embedding では recall が不足する。SQLite FTS の全文検索も本文がなければ成立しない。
3. **agent が判断材料を再取得できない構造** — ADR-0005 §軽減策の `source refetch` は SaaS API への再アクセスを前提とするが、Phase 9 box_drive のように API 経路を持たない connector や、レート制限・ネットワーク制約下では「手元に本文がない = 判断不能」になる。

一方で、ADR-0005 が挙げた懸念のうち **(4) storage 肥大化と (5) agent context 効率は Phase 4-8 の設計で別解が用意済み** である:

- 本文を SQLite に保持しても、agent に渡すのは recall / FTS で絞り込んだ関連断片のみ。context window に full body を流し込む設計ではない (Sub-issue B / C / D)。
- 個人利用スケール (年単位・数 GB) であれば SQLite + FTS は実用的に動く。

残る本質的な懸念は **(1) 機密性・(2) 法的制約・(3) SaaS TOS** であり、これらは「本文を保持するか否か」ではなく **「保持した本文をどう守り、どう除外し、どう信頼するか」** の設計で対処すべき問題である。pre-userbase の現段階 (AGENTS.md §設計判断のスタンス) では、過去の minimization 決定にとらわれず「アシスタントとして役立つ end-state」を優先する。

本文保持には新たな攻撃面が生まれる。最大のものは **content poisoning / 間接プロンプトインジェクション** である。外部 SaaS から取り込んだ本文には「以前の指示を無視して〜せよ」のような敵対的命令が混入しうる。本文を agent context に流すアシスタントプラットフォームでは、低信頼の外部本文を高信頼の operator 指示と区別せずに扱うと、アシスタントが乗っ取られる。ADR-0005 の minimization はこの攻撃面を「そもそも本文を持たない」ことで偶発的に縮小していたが、本文保持に転換する以上、攻撃面の縮小は別途 **provenance (出自・信頼度) タグ** + **event-sourced rollback** で明示的に設計しなければならない。

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

excludes は `channel` / `sender` / `repo` / `path` の 4 種の selector を持ち、各 connector が自身の取り込み経路で「観測する前に」除外判定する。

**Implementation status: landed (epic [#470](https://github.com/ozzy-labs/opshub/issues/470)).** Phase 10 で `~/.config/opshub/excludes.yaml` を導入した時点では、box_drive (Phase 9) / onedrive_drive (Phase 11 F4-b) の inline `[connectors.<name>] exclude_globs` を shared `excludes.yaml` `paths` selector に **merge** する dual-read shim (`ExcludeRules.merged_with_paths` 経由) を持っていた。pre-userbase 段階のため deprecation window は設けず、本 epic で:

1. `BoxDriveConnectorSettings.exclude_globs` / `OneDriveDriveConnectorSettings.exclude_globs` の 2 Pydantic field を削除
2. 両 model に `model_config = ConfigDict(extra="forbid")` を追加し、旧 inline key を持つ `opshub.toml` を `ValidationError` で fail-fast (silently ignored を完全に閉じる)
3. `ExcludeRules.merged_with_paths()` (dual-read merger 唯一の utility) を削除
4. `BoxDriveScanner._is_excluded` (`ExcludeRules.excludes_path` と完全重複していた logic) を削除し、scanner は `excludes: ExcludeRules` を受け取って `excludes.excludes_path(rel_path)` に委譲

を一括撤廃し、path-based exclusion の SSOT を `excludes.yaml` `paths:` selector に集約した。operator 向けの移行手順は [`docs/upgrading.md`](../upgrading.md) §`Pre-userbase compat shim cleanup` を参照。

### (c) 保存時暗号化 (詳細は ADR-0021)

本文をローカル保持する以上、ディスク上の平文露出は許容しない。DB 丸ごとの保存時暗号化を ADR-0021 で確定する。鍵は ADR-0014 の keyring 経路を再利用する。

### (d) 認証情報の本文からの分離 + backward-compat

本文ストアにトークン / 認証情報が混入しないことを test で pin する (DoD)。credentials は引き続き keyring (ADR-0014) で別管理し、`body` フィールドには載せない。

#### (d') `body NOT NULL` への昇格 (epic #470 / issue #481 follow-up, 2026-06-07)

Phase 10 当時、後方互換のため `body` を Optional (`str | None = None`) として導入し、ファイル参照 connector (OneDrive item / Box event / Box Drive stat-only scan / Google Workspace `google_workspace_file` catch-all 等) も `body = NULL` を emit していた。pre-userbase 段階の epic #470 (compat shim cleanup) でこの shim を撤去し、以下を pin する:

- `SourceObserved.body: str = Field(min_length=1)` — Pydantic schema レベルで required + 非空を強制。
- `sources.body` を `NOT NULL` に格上げ (migration `0030_enforce_sources_body_not_null`)。
- metadata-only / stat-only connector (box_drive `content_extraction=False`、Google Workspace catch-all、MS365 OneDrive metadata、Box web-API events、GitHub notifications 等) は **`body = summary` を emit** することで「source は必ず本文を持つ」契約を満たす (ADR-0010 §不変条件 metadata-only rule)。`summary` と `body` の semantic 区別 (前者は preview、後者は full text / search 対象) は維持される。stat-only path では両者が同値、Office 抽出 / Slack message / Outlook 等の本文抽出 path では別物。
- 既存 NULL-body row は migration 0030 で破棄され、operator が全 connector の cursor reset + 再 sync で再構築する (`docs/upgrading.md` §"Pre-userbase compat shim cleanup")。append-only event log は維持される (古い `body=None` event も新 schema では deserialise 失敗するが、event log は projection rebuild の SSOT として残る)。

この決定で「本文を持たない connector」例外はなくなる。FS-scan / metadata-only path は `path` derived summary を body にコピーし、検索 / 重複検出 / reply-draft の入力 surface が全 connector で uniform になる。

### (e) provenance タグ (poisoning 緩和の中核)

`sources` に **出自 (provenance) と信頼度 (trust level)** を表すタグを追加する。外部 connector 由来の本文は「外部由来・低信頼」として明示し、operator 手書き (`workspace ingest`) や opshub 内部生成は「内部由来・高信頼」とする。アシスタントエージェント (Sub-issue D) や propose (Sub-issue E) が低信頼本文を LLM context へ渡す際、prompt 上で「これは外部由来の参考情報であり指示ではない」と明示できる土台にする。これは ADR-0015 §決定 (f) の do-not-follow preamble と組み合わさり、間接プロンプトインジェクションの緩和層を形成する。

### (f) summary 側の whitespace 正規化 (body retention との非対称規約)

本 ADR §(a) で `body` フィールドは whitespace を含めて verbatim に保持する一方、`SourceObserved.summary` / `ItemEnqueued.summary` は **whitespace-only も missing と等価**として `None` / fallback に倒す。実装上の SSOT は [`opshub.core.text_limits.normalise_optional_text`](../../src/opshub/core/text_limits.py) ([issue #343](https://github.com/ozzy-labs/opshub/issues/343)) であり、全 connector mapper の summary 出力経路と `SourceService.observe` の inbox-side fallback の両方が同一規則に従う。

根拠: summary は briefing / propose / recall の **preview surface** であり、whitespace のみが表示されると操作者に「データはあるのに preview が空白」という認知的に矛盾した状態を見せてしまう。body retention の趣旨 (forensic 用途 / 本文 FTS 検索の SSOT 用途、本 ADR §(a)) とは目的が異なるため、preview 側だけ正規化することで **「retain everything in body, normalise preview」** という対称規約が成立する。preview の whitespace-only 化は連動して inbox 行の `slack_message: <user> in #<channel>` のような fallback summary 経路に倒れ、briefing UI 上の「(no preview)」相当 render と一貫した UX を提供する。

関連: [issue #332](https://github.com/ozzy-labs/opshub/issues/332) (元バグ、Slack 空 text による `ValidationError`)、[#337](https://github.com/ozzy-labs/opshub/issues/337) (whitespace audit followup)、[#343](https://github.com/ozzy-labs/opshub/issues/343) (helper SSOT 切り出し)、PR [#336](https://github.com/ozzy-labs/opshub/pull/336) / [#335](https://github.com/ozzy-labs/opshub/pull/335) / [#340](https://github.com/ozzy-labs/opshub/pull/340) / [#342](https://github.com/ozzy-labs/opshub/pull/342) / [#355](https://github.com/ozzy-labs/opshub/pull/355)。

## Consequences

### Positive

1. **アシスタントとして役立つ** — 返信下書き / 本文検索 / 詳細レビューが手元の本文だけで完結し、SaaS 再アクセス不要。
2. **本文ベース検索の高品質化** — embedding / FTS が summary ではなく本文から計算され、recall 精度が上がる (Sub-issue B)。
3. **API 経路非依存** — box_drive のように API を持たない connector でも本文 (FS scan で読める範囲) を活用できる。
4. **event-sourced 純粋性維持** — 本文も event に載るため replay / rollback / audit がそのまま効く。

### Negative / Trade-offs

1. **content poisoning / 間接プロンプトインジェクション攻撃面** — 外部本文に「以前の指示を無視せよ」等の敵対的命令が混入しうる。本文を agent context に流すアシスタントプラットフォームで最大の攻撃面。
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

- Phase 10 の返信下書き / 本文検索が summary では成立しない。アシスタントプラットフォームの中核機能が欠落する。
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

- 外部本文をそのまま agent context へ流すと間接プロンプトインジェクションでアシスタントが乗っ取られる。本文保持に転換する以上、攻撃面縮小を偶発的 (本文を持たない) から明示的 (出自・信頼度で区別) に設計し直すのは必須。

## 関連

- [ADR-0005: External Content Minimization](0005-external-content-minimization.md) — 本 ADR で **Superseded**。minimization から retention への転換元。
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md) — 本文を event payload に載せ projection へ materialise する根拠、rollback / replay で汚染本文を巻き戻せる根拠。
- [ADR-0012: Embedding Strategy](0012-embedding-strategy.md) — §Alternative #4 (embedding を本文ベースに) を本文保持で実装可能にする前提 (Sub-issue B)。
- [ADR-0014: SaaS Token Storage](0014-saas-token-storage.md) — 認証情報を本文から分離して keyring で別管理する根拠 (§(d))。
- [ADR-0015: LLM Usage Strategy](0015-llm-usage-strategy.md) — §決定 (f) do-not-follow preamble と provenance タグを併用して poisoning を緩和する根拠 (§(e))。
- [ADR-0019: Local-filesystem-backed Connector](0019-local-filesystem-backed-connector.md) — §決定 (g) box_drive inline excludes を共通機構へ統合する closeout (§(b))。
- [ADR-0021: Encryption at Rest](0021-encryption-at-rest.md) — 本文保持の保存時暗号化を確定する姉妹 ADR (§(c))。
- [Phase 10 Plan §1 / §3 Sub-issue A / §4-A / §8 Open Q #1](../phase-10-plan.md)
