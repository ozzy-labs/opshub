# Phase 10 Implementation Plan

> Status: Draft (起票骨子 / planning skeleton). Last reviewed: 2026-05-30. Scope: **Secretary Agent Platform — 秘書コア (Sub-issue A〜E)**。opshub を「ローカルファーストな秘書エージェント・プラットフォーム」へ拡張する。三層モデル（人間 → 秘書エージェント → opshub コマンド）の確立、エージェント向けツール面（MCP）の新設、秘書 Agent Skills の追加（形A＝runtime なし）、本文ローカル保持への転換（ADR-0005 見直し）、横断検索、返信下書き生成。**既存コネクタ（Slack/Box/GitHub/MS365 一部）の上で「動く秘書」を完成させる**ことが Phase 10 の到達点。
>
> **MS Office 深掘り（Teams 新コネクタ＋Word/Excel/PowerPoint 文書抽出 = Sub-issue F）は Phase 11 に分離**（コネクタ拡張波・Phase 7 と同性質）。秘書の枠組みを先に作る方が Office を載せる受け皿が整い手戻りが少ないため。Phase 11 は Phase 10 完了直後に続ける前提。
>
> 本ドキュメントは **planning skeleton** であり、各 sub-issue の詳細設計・不変条件・DoD は着手前に本 plan 内で確定する。実装契約（uow_factory / `EventStore.append` / `Projector.apply` / registry SSOT / cold-start guard 等、Phase 1-9 で確立）は Phase 10 も全て継承する。

Phase 10 の目的は、Phase 1-9 で築いた「記憶（event store）＋道具（connectors / recall / propose / brief / graph）＋ローカルファースト」という基盤の上に、**それらを束ねて人間の秘書として振る舞うエージェント層**と、**エージェントが安定して道具を呼ぶための口（MCP）**を載せ、opshub の正体を「人間と AI が対等に叩く operational memory」から「ローカルファーストな秘書エージェント・プラットフォーム」へ再定義することにある。

CLI は廃止しない。人間も従来どおり CLI を叩けるが、基本フローは 人間 → 秘書エージェント → コマンド とする（同じコアの二つの口 = CLI と MCP）。

---

## 1. 確定済み事項（設計セッション 2026-05-30 で確定）

着手前に確定済みの方針。各 ADR の前提として用いる。

1. **拡張であって新規プロダクトではない**: opshub を拡張する。理由は秘書の中核（読む→記憶→検索→提案）が既存サブシステムでほぼ実装済みで、不足は全て積層 or 既存拡張、コア置換を要さないため。pre-userbase につき互換維持のフォークは不要。
2. **三層モデル**: 人間 → 秘書エージェント（②）→ opshub コマンド（①）。CLI は残す。
3. **層配置**: 秘書エージェントの頭脳層（オーケストレーション＋Agent Skills＋将来の能動性）は **opshub と同一リポ内で層を分けて**配置（別リポのサイトル製品にはしない）。①コア（記憶＋コネクタ＋CLI＋MCP）はクリーンに保ち、②の常駐・能動・状態持ちのコードを混ぜない。
3b. **秘書の正体 = 形A**: opshub は **MCP サーバ（口）と Agent Skills（手順書）だけ**を提供する。頭脳（LLM 推論ループ）は Claude Code 等の **外部エージェントホストが担い、opshub 自身はエージェント runtime を持たない**。→ Sub-issue D は「Skills を書く＋MCP を出す」に縮退し、エージェント runtime 実装は不要。
4. **緊張点① 本文保持**: ADR-0005 を見直し、**本文をローカルに保持する**。検索も本文ベースに。安全策をセットで作る = (a) 取り込み除外設定（excludes）、(b) 保存時の暗号化、(c) 認証情報の本文からの分離。
5. **緊張点② 能動性**: 現時点は**能動機能を作らない＝リクエスト駆動のみ**（ユーザーがセッションで聞いた時に応答）。常駐・定期実行（cron / systemd timer / launchd / Win タスク / 常駐プロセス / filewatch / webhook）は将来フェーズ。ただし将来に備え、タスク/受信箱に「期限」を持たせるのは安価で有効（データは①コア）。
6. **緊張点③ 返信案・書き戻し**: **返信下書きの生成は作る**（`propose` に reply-draft 種類を追加、①コア内で完結・外送信なしで低リスク）。**外部サービスへの書き戻し/投稿は当面作らない**（秘書は下書きを見せるまで、ユーザーが手で送る）。
7. **初期サポート対象コネクタ**: Slack / Box（Box Drive 含む）/ GitHub / MS Office（Teams・Outlook・Word・Excel・PowerPoint）。将来拡張前提。MS365 コネクタ（Calendar/OneDrive/Outlook）と Box Drive の FS-scan パターンが Office 深掘りの土台。
8. **Phase 番号**: Phase 10（top-level）。新 ADR 群 + projection schema 変更 + 新レイヤ（MCP / agent）のため Phase 9.x 枠にしない。

---

## 2. 新規 / 改訂 ADR

> ADR 番号は採番時に確定（現時点の最新は ADR-0019）。下記は forecast。各 ADR の論点を整理する。

**新規 ADR は3本（0020 / 0021 / 0022）に絞る**。返信下書きと形A は既存 ADR が拡張を想定済みのため、新 ADR を立てず改訂で吸収する。

| ADR (forecast) | 種別 | タイトル | 主な論点 |
|---|---|---|---|
| ADR-0020 | **新規**（ADR-0005 を Supersede） | Full Local Content Retention | 本文をローカル保持する決定。保持範囲（本文・添付の扱い）、検索インデックス化、event store と本文ストアの関係（event に本文を載せるか別 content store か）、容量見積り、ADR-0005 の「保持しない」表を置換。**Negative に明記（調査ベース）**: 本文→embedding/projection 経路は OWASP ASI06「memory/context poisoning」の新規攻撃面。緩和＝`sources` に **provenance タグ**（外部由来・信頼度）を持たせ低信頼本文を agent context へ渡す際に明示、かつ rebuild=rollback 経路がある点 |
| ADR-0021 | **新規** | Encryption at Rest | 本文を含む機密データの保存時暗号化。**推奨＝SQLCipher で DB 丸ごと AES-256**（本文＋本文ベース embedding＋FTS が全部 DB に入るため、列単位暗号化は検索で平文化が要り意味が薄れる→却下）。**鍵は keyring 再利用（ADR-0014）**＝SaaS トークンと同じ鍵管理経路、新機構不要。ADR-0001（純正 SQLite 前提）への依存追加を Negative 明記。鍵不在時はエラー |
| ADR-0022 | **新規** | MCP Server Surface | ①コアをエージェントに露出する MCP サーバ。**stdio 一択・ネットワーク listen 禁止**（MCP spec の local-server 推奨と一致。これで confused deputy / SSRF / セッション乗っ取りが構造的に non-applicable）。**Token Passthrough 禁止**（SaaS トークンを tool 引数で受けず keyring から内部解決、MCP 面に非露出）。**read/write tool 分離＝scope minimization**（read 自律 / 書き込み＝task 作成・下書き保存・connector sync は人確認）を **policy-as-data**（YAML＋annotation、MS Agent Governance Toolkit の発想のみ流用、Agent Mesh/DID/trust score は単一ホストにつき却下）で表現。**write=人確認の根拠**: tool poisoning 研究で auto-approve 攻撃成功率 84% vs human-in-loop <5%。**context 効率**（要約・関連抽出で返す）＝LLM context 圧縮＋data exfiltration 面縮小の二利益。MCP tool 呼び出しは **OpenTelemetry GenAI naming（`execute_tool` 等）準拠の event** で記録（フル計装はせず opt-in exporter） |
| ADR-0004 改訂 | 改訂 | Agent Runtime Boundary | **形A** を吸収。MCP を「エージェントが書き込んでよい認可経路」として CLI/service と並べて追記。頭脳（runtime）は opshub 外部・opshub は MCP＋Agent Skills のみ提供する方針、①②境界（②＝外部ホストは①を MCP 経由で叩く）。**Agent Skills は `ozzy-labs/skills` preset 配布**（opshub 本体同梱でなく＝skill はホストの `.claude/skills/` に置く資産でコアとライフサイクルが違う）と確定 |
| ADR-0016 改訂 | 改訂 | Action Loop and Structured Output | **返信下書きを吸収**。`schema_version` v1→v2 で `ReplyDraftCandidatePayload`（`kind="reply_draft"`、**返信元 `reply_to_source_id/type` 必須**）を追加。外部書き戻しを **しない** 境界、apply 先（下書き保存先）を明記。**triage 3分類（EAIA）を吸収**: respond=reply_draft 候補 / notify=inbox_item / ignore=no-op（auto-apply 禁止は継続、triage は generate の枝刈りのみ）。**文体は静的プロンプトでなく recall した「自分が author の過去送信 event」を `<style_example>` 注入**（Inbox Zero の弱点回避、要 Sub-issue A/B）。文脈は `--expand-graph`（ADR-0017）で生成＝Read AI Ada の自前グラフ相当を既存機構で代替 |
| ADR-0017 改訂 | 改訂 | Knowledge Graph | reply_draft の provenance 用に link_type を2種追加（`reply_draft_replies_to` = reply_draft→source、`referenced_in_reply_draft` = reply_draft→context entities）。純派生 projection パターン（新 event 非発行）を踏襲。将来の bi-temporal 化・graph hybrid recall（#5）は別 Phase（§9） |
| ADR-0010 改訂 | 改訂 | Connector Contract | write-back を当面 scope 外と明記（Phase 10）／本文抽出を含む取り込み契約の拡張・Teams コネクタ追加（**Phase 11**） |
| ADR-0025 | 新規（**Phase 11**） | Office Document Content Extraction | Word/Excel/PowerPoint からのテキスト抽出。抽出ライブラリ選定（markitdown 等）、抽出失敗・巨大ファイルの扱い、`source_type` 設計、ADR-0020 の本文保持方針との接続 |

> ADR-0005 は **Superseded by ADR-0020** とし、本文を残したまま Status を更新（pre-userbase につき移行措置・dual-read は記載しない。正しい end-state を直接書く）。

---

## 3. Commit 順序（Sub-issue 骨子）

> 各 sub-issue を 1〜複数 PR に割る。詳細 PR 分割と DoD は着手前に確定。依存順に並べる。

### Sub-issue A: 本文保持 foundation（ADR-0020 / 0021）

- ADR-0020（本文保持）+ ADR-0021（暗号化）Accepted
- 本文ストアの schema 設計（event に本文を載せるか別 content store か）+ migration
- excludes 設定の共通機構化（ADR-0005 で言及されていた `~/.config/opshub/excludes.yaml`、Phase 9 で box_drive inline だったものを横断統合）
- 保存時暗号化の実装 + 鍵管理（OS keychain）
- 既存 5 connector の取り込み経路を「要約のみ」→「本文＋要約」に拡張（backward-compat: 既存 source 行は本文 NULL 許容）

### Sub-issue B: 横断検索の本文ベース化

- embedding を本文ベースに（ADR-0012 / Alternative #4 の実装）+ `embeddings rebuild` 再計算経路
- 全文検索（SQLite FTS）の追加 + `recall` / 新 `search` コマンドへの統合
- 検索が複数コネクタ（Slack/Box/GitHub/Office）横断で効くことの確認

### Sub-issue C: MCP サーバ面（ADR-0022）

- MCP サーバ実装（stdio / local transport、ネットワーク非公開）
- CLI と同等操作の tool schema 化（read 系優先、書き込み系は明示分離）
- 認証情報を tool に露出しない境界
- エージェント（Claude Code 等）からの接続手順

### Sub-issue D: 秘書 Agent Skills（ADR-0004 改訂、形A＝runtime なし）

- **SKILL.md 標準（Anthropic Agent Skills 形式）を採用**（デファクト、独自形式を発明しない）。本文は「どの MCP tool をどの順で呼ぶか」の薄い手順書（≤5k tokens）、詳細は `references/` へ。progressive disclosure: L1 description 常時 / L2 本文発火時 / **L3＝MCP tool・CLI 呼び出し**
- 秘書 5 Skill と MCP tool マッピング（**エージェント runtime は実装しない**、外部ホストが頭脳）:

  | skill | description トリガー | 使う MCP tool | 自律範囲 |
  |---|---|---|---|
  | daily-brief | 「今日のまとめ/状況」 | `brief`(read) | 自律OK |
  | next-actions | 「次に何を/やること」 | `task list`+`brief` | 自律OK |
  | reply-draft | 「返信案/下書き」 | `propose --kind reply_draft`(外送信なし) | 下書き保存=確認 |
  | pr-review | 「PR レビューして」 | `recall`/GitHub source query(read) | 自律OK |
  | file-lookup | 「Box/ファイル確認」 | `search`(本文横断) | 自律OK |

- **skill security scan（新規・QwenPaw 由来）**: `ozzy-labs/skills` の CI lint に4カテゴリ検出（プロンプトインジェクション/コマンドインジェクション/ハードコード鍵/データ持ち出し）＋ frontmatter の隠しユニコード・「ignore previous instructions」類のパターン検出を追加。skill は外部ネットワークを持たず opshub MCP/CLI のみ使う規約
- **配布＝`ozzy-labs/skills` preset**（opshub 本体同梱でなく、ADR-0004 改訂で確定）。MCP セットアップは `docs/mcp-setup.md`。skill は単一の opshub MCP サーバを使う（skill ごとに別 MCP を立てない）
- 人格・常駐は opshub に持たせない（ホスト側の責務）

### Sub-issue E: 返信下書き生成（ADR-0016 改訂 / ADR-0017 改訂）

新規に要るのは4点に集約（文体・文脈・HITL は既存機構で賄える）:

- (a) **`ReplyDraftCandidatePayload`**（`kind="reply_draft"`、`reply_to_source_id/type` 必須、schema v1→v2 で両 version 読み分け）
- (b) **`reply_drafts/prompts.py`**: do-not-follow preamble（ADR-0015 §決定 f 継承）＋薄い静的 About（署名/役割）＋ `<style_example>`（recall: author=self を返信先 channel/相手で絞る）＋ `<context_source>`（`--expand-graph`）
- (c) **link_type 2種**（`reply_draft_replies_to` / `referenced_in_reply_draft`、ADR-0017 enum 拡張・純派生）
- (d) **外部書き戻しを しない 境界の test pin**（write-back 経路が存在しないこと）
- triage（respond/notify/ignore）は `propose generate` の前段 or structured field。auto-apply はしない（durable state 変更は operator の apply のみ）
- 依存: Sub-issue A（本文保持）＋ B（本文 embedding）— style_example を引くため

### Sub-issue F: MS Office 深掘り（ADR-0025 / 0010 改訂）→ **Phase 11 に分離**

- Teams コネクタ（Graph API、既存 MS365 コネクタ延長）
- Outlook 本文取り込みの深掘り（既存 ms365 outlook の本文化）
- Word/Excel/PowerPoint 本文抽出（OneDrive/SharePoint 経由 or Box Drive 同様の FS-scan、markitdown 等で抽出）
- 詳細は Phase 11 plan（Phase 10 完了後に起票）で確定。本 plan には依存関係の記録としてのみ残す。

### Sub-issue G: Phase 10 closeout

- ドキュメント一式更新（§5 / §6。Office 関連 docs は Phase 11 へ）
- e2e lifecycle test（§7）
- M6 cold-start guard / `opshub --help` ≤ 300ms 維持の確認
- AGENTS.md の Status 行更新（Phase 10 complete）

---

## 4. 各 Sub-issue の Definition of Done（骨子）

> 着手前に各項目を具体化。ここでは代表項目のみ。

### A — 本文保持 foundation

- [ ] ADR-0020 / ADR-0021 Accepted + decisions-log.md entry
- [ ] 本文ストア migration が `upgrade head` / `downgrade -1` 可能
- [ ] 既存 source 行は本文 NULL で挙動変化なし（backward-compat）
- [ ] excludes 設定（チャンネル/送信者/repo/path）で取り込み除外が効く
- [ ] 保存時暗号化が有効、鍵は OS keychain 管理、未暗号化での平文保存をテストで検出
- [ ] 認証情報・トークンが本文ストアに混入しないことの test pin

### B — 横断検索

- [ ] embedding が本文から計算される（要約のみ時代の vector は rebuild で置換）
- [ ] SQLite FTS による全文検索が動作
- [ ] `recall` / `search` が Slack/Box/GitHub/Office 横断で hit

### C — MCP サーバ

- [ ] MCP サーバが stdio で起動し、エージェントから tool 呼び出し可能
- [ ] read 系 / 書き込み系 tool が分離され、認証情報は露出しない
- [ ] ネットワークに listen しない（ローカルファースト invariant）

### D — 秘書エージェント層

- [ ] 秘書 Skill が SKILL.md 標準（frontmatter＋本文 ≤5k tokens）に準拠
- [ ] ②が①を MCP 経由でのみ呼ぶ（直接 import しない）境界の確認
- [ ] 代表 Agent Skill（daily-brief / reply-draft / pr-review / file-lookup）が動作
- [ ] **skill security scan を `ozzy-labs/skills` CI に追加**（4カテゴリ＋frontmatter 隠し命令検出）
- [ ] ①コアに②の常駐/能動コードが混入しないことの確認

### E — 返信下書き生成

- [ ] `propose` が `reply_draft` 候補を生成、返信元 source を参照
- [ ] 下書きが apply で保存される（外送信しない）
- [ ] 外部書き戻し経路が存在しないことの test pin

### F — MS Office 深掘り

- [ ] Teams コネクタが Graph 経由で同期
- [ ] Outlook 本文が取り込まれる
- [ ] Word/Excel/PowerPoint からテキスト抽出 → source 化、抽出失敗・巨大ファイルの escape hatch

### G — closeout

- [ ] docs 一式更新（§5 / §6）
- [ ] e2e lifecycle test pass（§7）
- [ ] M6 guard / `opshub --help` ≤ 300ms 維持
- [ ] AGENTS.md Status 行更新

---

## 5. 設計ドキュメント更新計画

- **`docs/principles.md`**:
  - §1 Local-first — 「本文をローカル保持する方が真の local-first（オフライン・上流削除耐性）」を反映
  - §6 External Content Minimization — **全面改訂**（ADR-0020 に合わせ「本文を保持する＋安全策」へ。ADR-0005 を supersede した旨を明記）
  - §4 Agent Runtime Boundary — 三層モデル・②層を反映
  - §9 Phased Delivery — Phase 10 追記
  - §Open Questions — 該当項目（能動性・multi-machine sync）の状態更新
- **`docs/architecture.md`**:
  - 新 §2.x MCP Server Layer
  - 新 §2.x Secretary Agent Layer
  - §8.1 External Content Retention — 全面改訂
  - §2.1 Connector Layer — Office 深掘り / Teams 追記
  - §9 Phased Delivery — Phase 10 追記
- **`docs/repository-structure.md`**: 新モジュール（mcp / agent / content store / office 抽出）
- **`docs/decisions-log.md`**: ADR-0020〜0025 + ADR-0005/0010 改訂の entry
- **`AGENTS.md`** / **`CLAUDE.md`**: Status 行、Available Skills（秘書 Skills 追加なら）

---

## 6. ユーザー向けドキュメント更新計画

- **`README.md` / `README.ja.md`**:
  - 製品定義を「秘書エージェント・プラットフォーム」に再フレーム（冒頭文）
  - 「ユーザー目線でできること」を *秘書への依頼例*（「次に何を?」「今日やること?」「返信案考えて」「PR レビューして」「Box のファイル確認して」）で書き直す
  - エージェント接続（MCP）のセットアップ手順
  - 新コマンド（`search` / reply-draft / MCP サーバ起動）
  - オプション依存関係の更新（Office 抽出・暗号化・MCP 用 extras）
  - 「OpsHub に今あるもの」表に Phase 10 行追加
- **`docs/upgrading.md`**: 本文保持への挙動変更（pre-userbase でも挙動変化として記載）、暗号化の有効化手順
- **`SECURITY.md`**: 本文ローカル保持の含意、保存時暗号化、機密データ取扱いのユーザー責任範囲
- **新規 `docs/secretary-agent.md`**: 秘書エージェントの使い方（依頼例・Skills 一覧・できること/できないこと）
- **新規 `docs/mcp-setup.md`**: エージェント（Claude Code 等）から MCP 経由で opshub を使う手順
- **既存 setup docs**: `docs/box-drive-setup.md` / MS Office 関連 setup の追補

---

## 7. テスト計画

### 7.1 単体テスト（unit）

- 本文ストア / excludes フィルタ / 暗号化（平文非保存・鍵不在時のエラー）
- MCP tool schema の入出力 validation、認証情報 redaction
- reply-draft candidate schema、返信元 source 解決
- Office 抽出器（各形式 / 抽出失敗 / 巨大ファイル / 破損ファイル）

### 7.2 結合テスト（integration）

- connector → 本文＋要約取り込み → 本文ストア persist → embedding（本文ベース）→ recall hit
- 既存 5 connector の backward-compat（本文 NULL で従来挙動）
- MCP サーバ起動 → tool 呼び出し → ①コア操作 → 結果返却
- 暗号化 DB の round-trip

### 7.3 e2e lifecycle テスト（`tests/integration/test_phase10_secretary_lifecycle.py`）

- 一連の「人間 → 秘書（MCP）→ コマンド」フローを pin。**形A** につき opshub 内に頭脳はないので、e2e は **MCP クライアントがエージェントのツール呼び出し列を台本どおり再現**して MCP 面と①コアを検証する（実エージェント・実 LLM は不要）:
  1. 複数コネクタからサンプル本文を取り込み（tmp dir / fixture）
  2. MCP 経由で「今日やること」を要求 → brief / next-actions が生成される
  3. MCP 経由で reply-draft 要求 → 返信下書きが生成され、apply で保存（外送信なしを確認）
  4. MCP 経由で横断検索 → Slack/Box/GitHub/Office をまたいで hit
  5. 外部書き戻し経路が呼べないこと（write-back 非存在）を確認
- LLM backend は決定的なスタブを使用（実 API を叩かない）

### 7.4 持続検証 / guard

- M6 cold-start guard（mcp / agent / 新 connector の module-level import が whitelist 内）
- `time opshub --help` ≤ 300ms 維持
- 暗号化の平文リーク検出を CI に常駐

---

## 8. Open Questions

> Phase 10 着手時点で未確定、本 plan 内で確定すべきもの。

1. **本文の格納先** — event に本文を載せる（event-sourced の純粋性）か、別 content store（event は参照のみ、本文は再構築可能な別表）か。ADR-0020 で確定。
2. **暗号化方式** — SQLCipher（DB 丸ごと）か、アプリ層で本文列のみ暗号化か。鍵は OS keychain。性能とのトレードオフ。ADR-0021 で確定。
3. **MCP tool の粒度** — CLI コマンド 1:1 で tool 化するか、秘書ユースケース単位（「今日やること」等）の粗粒度 tool にするか、両建てか。ADR-0022 で確定。
3b. **秘書の自律範囲** — 読み取り系ツールは自律実行 OK、書き込み系（task 作成 / 下書き保存 / connector sync）は人の確認を促す、という境界をどう MCP tool に表現するか（tool の annotation / 命名規約 / 確認フラグ）。ADR-0022 で確定。
4. **Agent Skills の配布** — opshub 同梱か `ozzy-labs/skills` 配布（handbook ADR-0016 の skills repo 機構）か。ADR-0004 改訂で確定。
5. **Office 抽出ライブラリ** — markitdown 等。ライセンス・依存サイズ・抽出品質。ADR-0025 で確定。
6. **reply-draft の apply 先** — 下書きをどこに保存するか（新 entity か、task の付随物か、workspace ファイルか）。ADR-0016 改訂で確定。
7. **Sub-issue の Phase 分割** — **確定（2026-05-30）**: Phase 10 = A〜E（秘書コア）、Phase 11 = F（MS Office 深掘り）。closeout(G) は Phase 10 の A〜E に対して行う。

---

## 9. Phase 11 / 以降 outlook

- **Phase 11 = MS Office 深掘り（直後に続ける）**: Teams 新コネクタ（Graph）＋ Outlook 本文深掘り ＋ Word/Excel/PowerPoint 文書抽出（ADR-0025 / ADR-0010 改訂）。Sub-issue F の内容。Phase 10 完了後に Phase 11 plan を起票。
- **将来 Phase（仮 12）= 統合・検索融合レイヤ（調査ベースの最大の収穫）**: opshub の中核差別化（記憶の質）を一段引き上げる。新 projection・新 event を伴うため独立 top-level Phase。
  - **RRF 多チャネル検索**（Cloudflare Agent Memory）: sqlite-vec ＋ SQLite FTS5 ＋ graph traversal を RRF(k=60) で融合 → ADR-0017 の既知欠落 #5「graph hybrid recall 不在」を充足。SQLite 内完結
  - **dreaming 型 記憶キュレーション**（Anthropic dreaming / Letta sleep-time）: `opshub memory consolidate`（仮）で要約・重複検出・関連付けを **新 event＋純派生 projection** として出力（raw event 不変、ADR-0017 の純派生パターン踏襲）。トリガは OS cron 委譲で core に常駐 runtime 不要
  - **bi-temporal な事実無効化**（Zep/Graphiti）: links に valid time＋transaction time、`LinkInvalidated` で soft-invalidate。opshub の event `occurred_at`/`recorded_at` 二軸が素地
  - **忘却＝物理削除でなく recall スコアの時間減衰**
- **能動性**（緊張点②の将来対応）— 段階案（形A維持＝トリガと常駐は core 外、生成ロジックと派生 artifact は core 内、承認は既存 inbox/proposals）:
  - 段階0: task/inbox に「期限」（core 内データのみ）
  - 段階1: **cron 委譲の冪等コマンド**（`briefing generate` / `memory consolidate`）。トリガは OS cron / systemd timer / launchd / Win タスクに外出し、core に常駐 daemon を作らない
  - 段階2: 記憶キュレーション（上記 dreaming 型）
  - 段階3: 通知（「通知済み記録」projection ＋ 通知経路。送信処理は connector/skill 側、core は「何を通知すべきか」の派生 state のみ）
  - 段階4（将来・慎重に）: 常駐 + filewatch（inotify/FSEvents）→ webhook。**core でなく sidecar/skill に**置き書き込みは MCP/CLI 経由
  - **アンチパターン（採らない）**: always-on クラウド VM 常駐（Gemini Spark 型）＝ローカルファースト・形A・緊張点③に反する
- **外部書き戻し**（緊張点③の将来対応）: 「下書き生成」と「投稿実行」を別機能に分離、投稿は毎回明示承認必須。
- **reflection memory（EAIA）**: reply_draft の apply 履歴（original vs 人が直した最終版の diff）は opshub では event として暗黙に蓄積され recall 経由で次回 few-shot に流れる。明示的な diff→ルール化は将来の propose 拡張候補
- **追加コネクタ**: 初期 4 系（Slack/Box/GitHub/Office）の後、Google Workspace / Notion / Jira 等。
- **Multi-machine sync**（principles.md §Open Q #5）: 本文ローカル保持と暗号化が入った後の sync 設計（litestream / Turso / event export-import）。
- **rename 安定 identity**（Phase 9.x から継続）: Box Drive 等の xattr / ADS ベース安定 ID。

---

## 10. 外部設計調査の反映（2026-05-30、関連プロダクト並列調査）

秘書エージェント領域の主要プロダクトを並列調査し、market 活性度でなく**設計・コンセプトの取り込み**観点で抽出した結論。

### positioning（中核の打ち出し）

- **形A（runtime を持たず MCP＋Agent Skills を出す）は業界の主流構成と一致**し validated。runtime レイヤ（LangGraph / OpenAI Agents SDK / Claude Agent SDK / Microsoft Agent Framework / CrewAI / AgentScope）は資本と本番実績で固まりつつあり後発が勝つ領域ではない。MCP は Linux Foundation 配下に移管され事実上の標準。調査した全フレームワークが MCP クライアント＝opshub の MCP サーバは競合でなく補完。
- **最大の差別化＝イベントソース型 operational memory（append-only・監査可能・決定的再構築・本文ローカル保持）**。競合の記憶は素朴な Markdown ファイル / lossy な reflection memory / vector-graph の agent-memory API のいずれかで、**event-sourced＋監査可能＋ローカルは空白**。中核 positioning を「lossy な agent memory ではなく監査可能な system of record」に据える（README/principles 改訂方針に反映）。
- **記憶レイヤ境界**（LangGraph の checkpointer/Store 分離に学ぶ）: 短期の実行・会話 state は外部ホスト（runtime）に委ね、長期 operational memory（真実の源）が opshub の責務。形Aの境界を補強（ADR-0004 改訂）。
- **名乗りは「operational memory / 秘書」を維持**、「Chief of Staff / Work OS」マーケ語は採らない。

### 明確に採らないもの（アンチパターンの言語化）

- always-on クラウド VM 常駐 / 自前エージェント runtime / CrewAI・AutoGen のエージェント定義形式
- Agent Mesh・DID・trust score（単一ユーザー・単一ホストにつき不要、ADR-0022 Alternatives で却下明記）
- フル OpenTelemetry 計装（opt-in exporter に留める）／ 列単位暗号化（検索と両立しない）

### 知的誠実性（過大主張の回避）

- **append-only は memory poisoning（OWASP ASI06）を「防止」しない**。append-only が直撃するのは ASI09（改ざん不能な監査ログ）・ASI03（attribution）・snapshot/rollback。本文ローカル保持（ADR-0020）はむしろ「汚染本文→汚染 projection/embedding」の新規攻撃面を作るため、provenance タグ＋rebuild=rollback を緩和策として明記する（ADR-0020 Negative）。
- 前回調査の訂正: Read AI Ada は「MCP 不使用」ではなく内部＝自前グラフ／対外＝MCP サーバの補完関係。

### 主要参照（一次情報）

- Skills: Anthropic Agent Skills docs（SKILL.md 標準）, OpenHands microagents（SKILL.md 収束）, QwenPaw（skill security scan）
- Memory: mem0 / Letta(MemGPT) / Zep・Graphiti(bi-temporal) / LangGraph persistence / Cloudflare Agent Memory(RRF) / Anthropic dreaming・Letta sleep-time
- Security: OWASP Top 10 for Agentic Applications(2025-12), MS Agent Governance Toolkit, OpenTelemetry GenAI semconv, MCP security best practices, SQLCipher
- Reply-draft: Inbox Zero, Executive AI Assistant(LangGraph), Read AI Ada
- 能動性: OpenClaw heartbeat, Anthropic dreaming, ChatGPT Pulse, Gemini Spark(アンチパターン例)

---

## 関連

- principles.md §1 (Local-first) / §4 (Agent Runtime Boundary) / §6 (External Content Minimization、本 phase で改訂) / §9 (Phased Delivery)
- architecture.md §2.1 (Connector Layer) / §2.13 (Agent Runtime Boundary) / §8.1 (External Content Retention、本 phase で改訂) / §9 (Phased Delivery)
- ADR-0004 (Agent Runtime Boundary、本 phase で改訂＝MCP 経路＋形A) / ADR-0005 (External Content Minimization、ADR-0020 で supersede) / ADR-0010 (Connector Contract、本 phase で改訂) / ADR-0012 (Embedding Strategy、本文ベース化) / ADR-0014 (SaaS Token Storage) / ADR-0015 (LLM Usage) / ADR-0016 (Action Loop、reply-draft) / ADR-0017 (Knowledge Graph、本 phase で reply_draft link_type 追加・expand-graph を文脈源に再利用) / ADR-0019 (Local-filesystem-backed Connector、Office FS-scan の土台)
- Phase 1 #3 / Phase 2 #23 / Phase 3 #43 / Phase 4 #62 / Phase 5 #81 / Phase 6 #99 / Phase 7 #113 / Phase 8 #128 / Phase 9（closed）— Phase 10 tracking issue は本 plan 確定後に起票
