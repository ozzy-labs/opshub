# Phase 12 Implementation Plan

> Status: planning. Last reviewed: 2026-05-31. Scope: **Secretary Skills 拡張** = 14 skills 体制（既存 5 + 新規 9）+ 既存 5 のうち 2 件 rename（daily-brief → personal-brief / file-lookup → find-document）+ 4 新 MCP tools 露出（search FTS5 / propose.apply / 既存 4 read tools の時間フィルタ）+ 既存 5 SKILL.md の MCP 直接呼び化（CLI fallback 廃止）。Phase 10 で築いた MCP + Skills の枠の上に、Phase 11 で取り込んだ Office + Teams + Outlook データを「秘書らしい体感価値」に変換する。形A（runtime なし）・能動性なし・外部書き戻しなしを Phase 10/11 から継承。
>
> Sub-issue は **H1〜H6 の6つ**（親 epic #253、子 #254〜#259）。新規 ADR ゼロ、改訂 3本（ADR-0004 / ADR-0022 / ADR-0016）に縮退（Phase 10 の 3 新規 + 4 改訂 → Phase 11 の 1 新規 + 2 改訂 → Phase 12 の 0 新規 + 3 改訂、と縮退継続）。本 plan が SSOT であり、各 sub-issue body は要点抜粋。
>
> 本ドキュメントは **planning skeleton** であり、各 sub-issue の詳細設計・不変条件・最終 DoD は着手前に本 plan 内で確定する。実装契約（uow_factory / `EventStore.append` / `Projector.apply` / registry SSOT / cold-start guard / `core/sanitise.sanitise_error_message` / Pluggable backend Protocol freeze / Connector framework / 7 link_type + reply_draft / `tests/_secrets.py` 連結ビルド規範 等、Phase 1-11 で確立）は Phase 12 も全て継承する。

Phase 12 の目的は、Phase 10 で完成させた「動く秘書の枠組み」と Phase 11 で広げた「MS Office データの取り込み」を踏まえ、**秘書として使える Skill レパートリーを 5 → 14 に拡張**することにある。同時に既存 5 Skill の `opshub` CLI fallback を MCP 直接呼びに統一し、`search`(FTS5) と `propose.apply` を MCP に露出することで、host LLM が opshub の能力を一貫した surface で叩けるようにする。

Phase 11 で取り込んだ Teams / Outlook / Word / Excel / PowerPoint 由来の context は、新 skills（meeting-prep / research / meeting-followup / source-extract 等）が直接消費する経路に乗る。Phase 12 はデータ層を増やさず、Skill 層で「秘書らしさ」を一段階引き上げる phase。

---

## 1. 確定済み事項

### Open Question 決定（2026-05-31 設計セッション）

| # | 論点 | 決定 |
|---|---|---|
| OQ1 | 追加 Skills の選定 | **9 新規 skills**：meeting-prep / research / inbox-triage / external-brief / decision-rationale / handoff-draft / announcement-draft / meeting-followup / source-extract。**+ 既存 5 を 2 件 rename**（daily-brief → personal-brief / file-lookup → find-document）= **計 14 skills** |
| OQ2 | HITL 境界・draft 系永続化 | **B**: reply-draft は既存通り `propose.generate` + `propose.apply`、handoff-draft / announcement-draft は **text-only**（persist しない）。新 candidate kind 追加なし、schema 拡張なし |
| OQ3 | 既存 5 Skills MCP 整合化 | **A**: 全面 MCP 直接呼び化（CLI fallback 廃止）+ `search`(FTS5) MCP tool 追加 + personal-brief / next-actions の description 拡張で期間指定対応 |
| OQ4 | MCP tool 追加範囲 | **A 包括的（4 項目）**：`search`(FTS5) + `propose.apply` + 既存 4 read tools（task / decision / source / inbox.list）に時間フィルタ追加（`occurred_after` / `occurred_before` / `recorded_after` / `recorded_before`） |
| OQ5 | ozzy-labs/skills 配布 | **C**: 配布せず、opshub `docs/skills/` を SSOT、host install は README 手動手順、配信機構（ozzy-labs/skills CI + Renovate preset）は Phase 13+ で別途検討 |
| OQ6 | 新 Skills e2e 品質確認 | **A**: 統合 1 本（`test_phase12_secretary_lifecycle.py`）+ per-skill MCP dispatch pin（`tests/unit/skills/test_skill_specs.py` 拡張で skill 内 MCP tool 名・引数 schema を pin） |
| OQ7 | ADR 構成 | **B**: 新規 ADR ゼロ、**改訂 3 本**（ADR-0004 / ADR-0022 / ADR-0016）。Skill catalog（14 skills 責務マップ）は ADR ではなく `docs/secretary-agent.md` を SSOT |

### Phase 10/11 から継承する不変方針

1. **形A**: opshub は MCP server + Agent Skills のみ提供、頭脳（runtime）は外部ホスト。
2. **能動性なし**: リクエスト駆動のみ。常駐・定期実行は Phase 13+。
3. **外部書き戻しなし**: 取り込み + ローカル context 生成のみ、書き戻しは ADR-0010 で明示禁止（緊張点③）。draft 系 skill は text 生成のみ、user が手動で SaaS に転記。
4. **本文ローカル保持** + `provenance_origin="external"` / `provenance_trust="untrusted"` 付き（ADR-0020）。
5. **SQLCipher 丸ごと暗号化 opt-in**（ADR-0021、keyring 経由）。
6. **HITL boundary**: read tools は host LLM 自律 OK、write tools は host LLM が user 確認必須（ADR-0022 annotation policy）。

### Phase 番号

**Phase 12**（top-level）。Skill 拡張 9 + 既存 5 rename + 4 新 MCP tools 追加を伴うが、新 ADR ゼロ・新 connector ゼロ・新 projection ゼロのため Phase 11.x 枠でも理論上可能。しかし「秘書としての使用感を体感価値に引き上げる」という方針転換に近い量の変更（14 skills 体制、既存 rename を伴う）であり、メモリ方針 [[phase-numbering-new-arch-pattern]] に整合させ Phase 12 として独立。

### 14 Skills 最終命名一覧

```text
read（自律 OK・10 件）:
  personal-brief, next-actions, pr-review, find-document,
  meeting-prep, research, external-brief, decision-rationale,
  handoff-draft, announcement-draft

HITL write（propose.generate + apply・4 件）:
  reply-draft, inbox-triage, meeting-followup, source-extract
```

### pair structure（host LLM の routing 精度向上）

- **personal-brief ↔ external-brief**: 自分向け / 外向き
- **meeting-prep ↔ meeting-followup**: 会議前 / 会議後
- **inbox-triage ↔ source-extract**: 集合（inbox 全体）/ 個別（source 1件）
- **reply-draft / handoff-draft / announcement-draft**: draft family（返信 / 引き継ぎ / 告知）

---

## 2. 改訂 ADR

> 新規 ADR ゼロ。改訂 3本のみ。Skill catalog（14 skills 責務マップ）は `docs/secretary-agent.md` を SSOT として扱う（ADR 化しない理由：skill 追加・rename で頻繁更新される性質、ADR の「決定の根拠」性と相性悪い）。

| ADR | 種別 | タイトル | 主な改訂内容 |
|---|---|---|---|
| **ADR-0004** | 改訂 | Agent Runtime Boundary | §決定 (c) の「Agent Skills は `ozzy-labs/skills` preset 配布」を「**Skills は opshub `docs/skills/` を SSOT、配信機構（ozzy-labs/skills CI + Renovate preset）は Phase 13+ で別途検討**」に修正（OQ5=C 反映）。新規 §決定：Skill catalog は `docs/secretary-agent.md` を SSOT として 14 skills 責務マップ・HITL boundary・MCP tool 依存マップ・pair structure を維持。Phase 10 改訂時の前提（ozzy-labs/skills 配布完成）を意図的に backout する形で文書化 |
| **ADR-0022** | 改訂 | MCP Server Surface | 既存 7（Phase 10 Sub C）+ Step 1 widening 8（PR #231）に **Phase 12 で 4 追加** を明記：(a) `search`(FTS5、`ReadCategory.SEARCH` 新設) / (b) `propose.apply`(`WriteCategory.PROPOSE_APPLY`、`read_only=false, destructive=false, idempotent=true`) / (c)(d)(e)(f) 既存 4 read tools (task / decision / source / inbox.list) 入力 schema 拡張で時間フィルタ追加（`occurred_after` / `occurred_before` / `recorded_after` / `recorded_before`、ISO 8601 optional）。annotation policy 維持（read 自律 / write 確認） |
| **ADR-0016** | 改訂 | Action Loop and Structured Output | §決定 (i)/(j)/(k) の reply_draft（Phase 10 改訂）に加え、**draft 系統一方針** を追記：(a) reply-draft は `propose.generate` + `propose.apply` で persist / (b) handoff-draft / announcement-draft は **text 返却のみ persist しない**（OQ2=B 反映）/ (c) 理由：使用頻度の現実主義 + schema 拡張コスト回避、将来 persist 需要顕在化時に `DraftCandidatePayload` 拡張で対応可能と forecast。新 candidate kind 追加なし |

---

## 3. Commit 順序（Sub-issue 骨子）

> 各 sub-issue を 1〜複数 PR に割る。詳細 PR 分割と DoD は着手前に確定。依存順に並べる。Skill catalog（14 skills 責務マップ）は H6 closeout で `docs/secretary-agent.md` に集約。

### Sub-issue H1: foundation (#254)

ADR 改訂 3 本 + 既存 5 rename + MCP 整合化 + 4 新 MCP tools 露出 + skill_scan 強化。後続 H2-H5 が依拠。

**PR H1-a** `docs(adr): adr-0004 + 0022 + 0016 改訂 (phase 12)`

- ADR-0004 改訂（Skills SSOT in opshub docs/skills/、配信機構 Phase 13+ defer + Skill catalog refers to docs/secretary-agent.md）
- ADR-0022 改訂（4 新 MCP tools 契約化、annotation policy 維持）
- ADR-0016 改訂（draft 系統一方針：reply persist / handoff,announcement text-only）
- `docs/decisions-log.md` entries（3 件）

**PR H1-b** `feat(mcp): search(fts5) + propose.apply + time filter on list tools`

- `src/opshub/mcp/server.py`：`ReadCategory.SEARCH` 新規 + `build_search_handler` 追加 / `WriteCategory.PROPOSE_APPLY` 新規 + `build_propose_apply_handler` 追加（既存 `opshub propose apply` ロジック再利用）/ 既存 4 read tools の入力 schema に時間フィルタ追加
- `src/opshub/mcp/registry.py`：新 tool 登録
- 既存テスト更新 + 新 tools tests（dispatch + schema reject unknown fields + annotation 確認 + 時間フィルタ動作）

**PR H1-c** `feat(skills): rename existing 5 + mcp direct call + description expansion`

- ファイル rename：`docs/skills/daily-brief/` → `docs/skills/personal-brief/`、`docs/skills/file-lookup/` → `docs/skills/find-document/`
- 既存 5 SKILL.md を MCP 直接呼びに書き換え（`opshub brief --json` 等 CLI fallback の記述を MCP `brief` tool 呼びに変更）
- personal-brief / next-actions description 拡張で期間指定対応
- find-document description 拡張で `search`(FTS5) MCP tool 利用に変更
- `tools/skill_scan.py` 改修（必要に応じて）
- `tests/unit/skills/test_skill_specs.py` 拡張：14 skills 全てに per-skill MCP dispatch pin（skill 内 MCP tool 名・引数 schema が opshub の MCP surface と整合するか grep + jsonschema validation）

### Sub-issue H2: info gathering skills (#255)

**依存: H1**

- `docs/skills/meeting-prep/SKILL.md`：calendar event 起点 + recall + graph.related で会議準備（read-only、persist なし）
- `docs/skills/research/SKILL.md`：トピック起点 + recall + search(FTS5) + graph.related/expand + brief で横断調査
- skill_scan pass + per-skill MCP dispatch pin tests

**PR H2** `feat(skills): meeting-prep + research`

### Sub-issue H3: analysis skills (#256)

**依存: H1**

- `docs/skills/external-brief/SKILL.md`：task.list (state=completed, completed_after=last_week) + decision.list (recorded_after=last_week) + brief（外向き tone）。personal-brief との pair。
- `docs/skills/decision-rationale/SKILL.md`：decision.list (filter by topic) + graph.trace + recall.search で決定経緯
- skill_scan pass + per-skill MCP dispatch pin tests

**PR H3** `feat(skills): external-brief + decision-rationale`

### Sub-issue H4: HITL write skills (#257)

**依存: H1**（特に `propose.apply` MCP tool が露出されていること）

- `docs/skills/inbox-triage/SKILL.md`：inbox.list (state=open) + propose.generate (mode=inbox_triage) + propose.apply (HITL)
- `docs/skills/source-extract/SKILL.md`：source.get + propose.generate (mode=source_extract) + propose.apply (HITL)
- `docs/skills/meeting-followup/SKILL.md`：source.list (calendar_event, 過去) + source.get + recall.search + propose.generate (mode=meeting_followup) + propose.apply (HITL)
- 3 skill とも HITL boundary 厳守：generate は read 自律 OK、apply は host LLM の user 確認必須
- skill_scan pass + per-skill MCP dispatch pin tests + HITL boundary test pin

**PR H4** `feat(skills): inbox-triage + source-extract + meeting-followup`

### Sub-issue H5: draft skills (#258)

**依存: H1**

- `docs/skills/handoff-draft/SKILL.md`：task.list (in_progress) + decision.list + recall.search + graph.related で引き継ぎ書 text 返却（persist なし）
- `docs/skills/announcement-draft/SKILL.md`：recall.search + decision.list (recorded_after=last_release) + brief で告知文 text 返却（persist なし）
- 両 skill とも persist しない（OQ2=B、ADR-0016 改訂と整合）
- skill_scan pass + per-skill MCP dispatch pin tests + text-only boundary test pin

**PR H5** `feat(skills): handoff-draft + announcement-draft`

### Sub-issue H6: Phase 12 closeout (#259)

**依存: H1 + H2 + H3 + H4 + H5 全てマージ済み**

- 設計 docs 一括（§5）
- ユーザー docs 一括（§6）
- `docs/secretary-agent.md` 大幅更新（Skill catalog SSOT として 14 skills 責務マップ・HITL boundary・MCP tool 依存マップ・pair structure を集約）
- e2e lifecycle test（§7.3）
- guard 確認（§7.4）
- AGENTS.md / CLAUDE.md Status 行 Phase 12 complete
- `docs/phase-12-plan.md` Status header 更新（Phase 11 audit R2-CROSS-06 教訓継承）

**PR H6** `docs: phase 12 closeout + e2e`

### Wave 配置（依存 DAG）

```text
Wave 1: H1                              ← entry
Wave 2: H2 / H3 / H4 / H5（4 並列）    ← H1
Wave 3: H6                              ← H2-H5
```

drive 例: `/drive --merge #254 -> #255,#256,#257,#258 -> #259`（Wave 2 で H2/H3/H4/H5 が 4 並列、Phase 11 の 3 並列より一段速い）

**並列性の根拠**：H2 (info gathering) / H3 (analysis) / H4 (HITL write) / H5 (draft) は各 Skill 群が独立しており、SKILL.md ファイル単位で衝突しない。共通の前提は H1 で完了済み（4 新 MCP tools + 既存 5 rename + skill_scan 強化）。`tests/unit/skills/test_skill_specs.py` だけは複数 Wave 2 sub-issue が同一ファイルを編集する merge conflict 潜在リスクがあるため、各 PR は test 追加部分を別関数化して conflict 面積を最小化する。

---

## 4. 各 Sub-issue の Definition of Done（骨子）

> 着手前に各項目を具体化。ここでは代表項目のみ。

### H1 — foundation

- [ ] PR H1-a / H1-b / H1-c が merged
- [ ] 改訂 ADR-0004 / ADR-0022 / ADR-0016 Accepted + decisions-log.md entries
- [ ] 既存 5 rename が docs / tests / 設定 / examples で完全反映（grep `daily-brief` / `file-lookup` で hit ゼロ）
- [ ] 4 新 MCP tools 露出、`opshub mcp tools` 出力に表示、annotation policy 整合
- [ ] 既存 5 SKILL.md が MCP 直接呼びに統一（CLI fallback 削除）
- [ ] personal-brief / next-actions description 拡張で期間指定対応
- [ ] find-document description 拡張で search(FTS5) 利用
- [ ] skill_scan が 14 skills 全てに対して pass
- [ ] per-skill MCP dispatch pin tests pass（既存 5 含む）

### H2 — info gathering skills

- [ ] meeting-prep / research の SKILL.md が `docs/skills/` 配下に追加
- [ ] meeting-prep が source.list (calendar_event, 時間フィルタ) + recall + graph.related を組み立てる
- [ ] research が recall + search(FTS5) + graph.related/expand + brief を組み立てる
- [ ] skill_scan pass + per-skill MCP dispatch pin tests pass
- [ ] MCP tool 利用が H1 で追加した surface と整合（time filter / search tool 名）

### H3 — analysis skills

- [ ] external-brief / decision-rationale の SKILL.md が追加
- [ ] external-brief が task.list / decision.list の時間フィルタを利用
- [ ] decision-rationale が graph.trace を利用
- [ ] skill_scan pass + per-skill MCP dispatch pin tests pass

### H4 — HITL write skills

- [ ] inbox-triage / source-extract / meeting-followup の SKILL.md が追加
- [ ] 3 skill とも `propose.generate` + `propose.apply` 経路で HITL boundary
- [ ] skill_scan pass + per-skill MCP dispatch pin tests pass
- [ ] HITL boundary test pin pass（propose.apply の annotation = read_only=false 確認）

### H5 — draft skills

- [ ] handoff-draft / announcement-draft の SKILL.md が追加
- [ ] 両 skill とも persist しない（候補保存 / apply 経路非存在）
- [ ] skill_scan pass + per-skill MCP dispatch pin tests pass
- [ ] text-only boundary test pin pass
- [ ] ADR-0016 改訂で「draft 系統一方針」記載済み（H1 で確定済み）と整合

### H6 — closeout

- [ ] 設計 docs（principles / architecture / repository-structure / decisions-log）更新済み
- [ ] ユーザー docs（README ja/en / mcp-setup / secretary-agent）更新済み + host への手動 install 手順記載
- [ ] **`docs/secretary-agent.md` が Skill catalog SSOT として 14 skills 責務マップを集約**
- [ ] e2e lifecycle test pass（`test_phase12_secretary_lifecycle.py`）
- [ ] M6 guard / `opshub --help` ≤ 300ms 維持、暗号化平文リーク検出 CI 常駐継続
- [ ] AGENTS.md / CLAUDE.md Status 行 Phase 12 complete
- [ ] **`docs/phase-12-plan.md` Status header を `Phase 12 complete (YYYY-MM-DD)` に更新**（Phase 11 audit R2-CROSS-06 教訓継承）
- [ ] **H6 マージ後、main の CI workflow が green であることを確認**（Phase 10 #221 hotfix 経験を踏まえた事前確認）

---

## 5. 設計ドキュメント更新計画（H6）

- **`docs/principles.md`**:
  - §4 Skills 数 5 → 14 + 区分（read 自律 OK 10 / HITL write 4）追記
  - §9 Phase 12 行追加
- **`docs/architecture.md`**:
  - §Secretary Agent Layer 拡張：14 skills 全リスト + HITL boundary + MCP tool 依存マップ + pair structure
  - §MCP Server Surface：新 4 tools（search / propose.apply / 時間フィルタ）追記
  - §9 Phase 12 行追加
- **`docs/repository-structure.md`**: `docs/skills/` 配下の新規追加 9 + rename 2 を反映、`tools/skill_scan.py` 強化分も記載
- **`docs/decisions-log.md`**: ADR-0004 改訂 + ADR-0022 改訂 + ADR-0016 改訂 entry（3 件）

---

## 6. ユーザー向けドキュメント更新計画（H6）

- **`README.md` / `README.ja.md`**:
  - Phase 12 行追記
  - 14 skills 表（区分別：自分向け / 外向き / draft / HITL write）
  - 依頼例の追加：「今週まとめて」「上司向け週次報告作って」「来週の会議準備」「あの決定はなぜ」「引き継ぎ書作って」「リリース告知文書いて」等
  - **host への手動 install 手順**（OQ5=C 対応、`cp -r opshub/docs/skills/* ~/.claude/skills/` などの最小手順）
- **`docs/mcp-setup.md`**:
  - 新 4 MCP tools（search / propose.apply / 時間フィルタ）追記
  - host への docs/skills/ コピー手順 + 各 host (Claude Code / Codex CLI / Copilot CLI) での Skills 取り込み方法
- **`docs/secretary-agent.md` 大幅更新（Skill catalog SSOT）**:
  - 14 skills 責務マップ表（name / 区分 / 発火条件サマリ / 使用 MCP tools / 出力形式）
  - HITL boundary 一覧
  - pair structure
  - MCP tool 依存マップ（どの skill がどの tool を呼ぶか）

---

## 7. テスト計画

### 7.1 単体テスト（unit）

- **`mcp/server`**: 新 4 tools（search / propose.apply / 時間フィルタ拡張済み task.list / decision.list / source.list / inbox.list）の dispatch + schema reject unknown fields + annotation 確認
- **`mcp/registry`**: 新 tool 登録、`opshub mcp tools` 出力
- **`tools/skill_scan`**: 14 skills 全てに対して pass、CLI fallback の記述検出（既存 5 が MCP 直接呼びに移行していること）
- **`tests/unit/skills/test_skill_specs`**: per-skill MCP dispatch pin（skill 内 MCP tool 名 / 引数 schema が opshub MCP surface と整合）。HITL boundary pin（write tools annotation）、text-only boundary pin（handoff/announcement が persist 経路非存在）

### 7.2 結合テスト（integration）

- **`tests/integration/test_phase12_mcp_search.py`**: 新 search(FTS5) MCP tool が既存 FTS5 と同等の hit を返す
- **`tests/integration/test_phase12_propose_apply_mcp.py`**: propose.apply MCP tool が既存 CLI と同等の persist + idempotency を持つ
- **`tests/integration/test_phase12_time_filter.py`**: 時間フィルタの境界動作（含む / 含まない、ISO 8601 timezone）

### 7.3 e2e lifecycle テスト

- **`tests/integration/test_phase12_secretary_lifecycle.py`**: **14 skills 統合シナリオ** を台本 MCP クライアントで再現。形A につき opshub 内に頭脳はないので **MCP クライアントが台本どおりツール呼び出し列を再現** して MCP 面と ①コアを検証（実エージェント・実 LLM は不要）
  1. 複数コネクタから sample 取り込み（Phase 11 e2e fixture 再利用：Teams + Outlook + Office docs）
  2. MCP 経由で 14 skill の代表的呼び出し列を実行：
     - **read 自律 OK 10 件**: personal-brief / next-actions / pr-review / find-document / meeting-prep / research / external-brief / decision-rationale / handoff-draft / announcement-draft
     - **HITL write 4 件**: reply-draft / inbox-triage / meeting-followup / source-extract（generate + apply 経路）
  3. 4 新 MCP tools 動作確認（search / propose.apply / 時間フィルタ × 4）
  4. HITL boundary 動作確認（write tools が annotation 経由で user confirmation を要求）
  5. write-back 経路が呼べないこと（外部 SaaS への投稿経路非存在）を確認
  6. handoff-draft / announcement-draft が persist 経路を持たない（候補保存・apply 不可）ことを確認

### 7.4 持続検証 / guard

- M6 cold-start guard 維持（新 MCP handler の module-level import が whitelist 内）
- `time opshub --help` ≤ 300ms 維持
- 暗号化平文リーク検出 CI 常駐継続
- gitleaks / secret scanner 対策: テストフィクスチャは `tests/_secrets.py` から import（連結ビルド規範を Phase 12 でも継続）

---

## 8. Open Questions

> Phase 12 着手時点で **全て解消済み**（2026-05-31 設計セッション）。OQ1〜OQ7 は §1 確定済み事項参照。

着手中に新たな OQ が発生した場合は本節を更新。

### 着手中に追加で詰める実装詳細（forecast）

- 時間フィルタの field 名統一規約（`occurred_after/before` / `recorded_after/before` / `completed_after/before`）。Phase 10 Round 2 で発見した projection 間の `created_at` / `recorded_at` / `occurred_at` 揺らぎを念頭に H1 で決定
- `propose.apply` MCP tool の冪等性確保（`idempotency_key` の渡し方、既存 `opshub propose apply` の挙動を MCP annotation `idempotent=true` の規約に揃える）
- search(FTS5) MCP tool の query syntax（既存 CLI `opshub search` と同じ MATCH 構文を素直に通すか、MCP 層で sanitise するか）
- per-skill MCP dispatch pin の実装方針（grep ベース vs MCP server を起動して dry-run する vs jsonschema only）

---

## 9. Phase 13 / 以降 outlook

- **Phase 13 候補（確定）= データ拡張系**: 画像 OCR（PPT 画像 + Office 図表、tesseract）/ Google Workspace コネクタ（Docs / Slides / Sheets、markitdown 経路再利用）/ Notion / Jira / Linear / Confluence。
- **Phase 12 から繰り越し**:
  - **ozzy-labs/skills 配布完成**（OQ5=C で defer、Renovate preset + skills repo CI 整備、handbook ADR-0016 機構の正規利用）
  - **削除候補 skills の需要顕在化時の追加**: agenda-builder / retrospective / weekly-plan / options-compare / risk-assessment（OQ1 で 14 採用 + これら除外を確定、需要が見えたら個別追加）
  - **draft 系 persist 需要顕在化時の対応**: handoff-draft / announcement-draft が persist 不要との判断は使用頻度の現実主義に基づく。実際の運用で persist 要求が出たら ADR-0016 を再改訂し `DraftCandidatePayload` 拡張で対応
- **Phase 14+ 候補**:
  - 統合・検索融合レイヤ（RRF + dreaming + bi-temporal links、Phase 10 §9 / 11 §9 forecast を継続）
  - **能動性**（緊張点②）: 段階 0 期限データ → 1 cron 委譲の冪等コマンド → 2 dreaming 型キュレーション → 3 通知 → 4 filewatch（sidecar/skill のみ、core 不可）。always-on VM はアンチパターン
  - **外部書き戻し**（緊張点③）: 「下書き生成」と「投稿実行」を別機能に分離、投稿は毎回明示承認必須。reply / handoff / announcement → SaaS 投稿が最有力候補
  - **Codex CLI / Gemini CLI / Copilot Skills 横展開**（C3、ホスト中立性検証）

---

## 関連

- principles.md §4 (Skills 体系) / §9 (Phased Delivery)
- architecture.md §Secretary Agent Layer / §MCP Server Surface / §9 (Phased Delivery)
- ADR-0004 (Agent Runtime Boundary、本 phase で改訂)
- ADR-0010 (Connector Contract、Phase 11 で改訂、本 phase での変更なし)
- ADR-0014 (SaaS Token Storage)
- ADR-0016 (Action Loop and Structured Output、本 phase で改訂)
- ADR-0017 (Knowledge Graph)
- ADR-0020 (Full Local Content Retention)
- ADR-0021 (Encryption at Rest)
- ADR-0022 (MCP Server Surface、本 phase で改訂＝4 新 tools + 時間フィルタ)
- ADR-0025 (Office Document Content Extraction、Phase 11 で新規、本 phase での変更なし)
- Phase 10 plan §9 outlook（Phase 11 = MS Office 深掘り → 完了）
- Phase 11 plan §9 outlook（Phase 12 候補に「Skills 追加: meeting-prep / research / inbox-triage / status-update / decision-context / dedupe-check」と forecast 済、本 phase で具体化）
- Phase 12 epic #253、子 sub-issue #254-#259
- Phase 1 #3 / Phase 2 #23 / Phase 3 #43 / Phase 4 #62 / Phase 5 #81 / Phase 6 #99 / Phase 7 #113 / Phase 8 #128 / Phase 9 #187 / Phase 10 #203 / Phase 11 #233
- Step 1 MCP widening: PR #231（brief / graph.* / source.* / propose.generate / embeddings.find_duplicates）
- handbook ADR-0016（skills repo 機構、Phase 13+ で配布完成予定）
