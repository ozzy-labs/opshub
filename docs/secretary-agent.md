# Secretary Agent — opshub の秘書層を使う

opshub は Phase 10 (秘書エージェント・プラットフォーム化) で「人間 → 秘書エージェント → opshub コマンド」の三層モデルへ拡張され、Phase 12 (Secretary Skills 拡張) で秘書 Skill レパートリーを **5 → 14** に拡張した。本 doc は秘書エージェントの使い方を、Skill catalog SSOT として 10 § 構成で集約する。

本 doc は [ADR-0004 §決定 (c-2)](adr/0004-agent-runtime-boundary.md) で **Skill catalog SSOT** として明示された (Phase 12 H1)。14 skills 体制の責務マップ / HITL boundary / MCP tool 依存マップ / pair structure をここで一元管理する。ADR-0004 改訂のうち Skill 配信機構 (`ozzy-labs/skills` CI + Renovate preset) は Phase 14+ に defer され (Phase 13 では Google Workspace コネクタが優先された)、Skill 本体 (SKILL.md) は opshub `docs/skills/<name>/SKILL.md` を SSOT として保持する。

設計の根拠は [ADR-0004 Agent Runtime Boundary](adr/0004-agent-runtime-boundary.md) (形A: opshub は MCP + Agent Skills のみ提供、runtime は外部ホスト) と [ADR-0022 MCP Server Surface](adr/0022-mcp-server-surface.md) (MCP tool 面) と [ADR-0016 §決定 (l)](adr/0016-action-loop-and-structured-output.md) (Phase 12 H1 で追加された draft 系統一方針: persist 境界 / `mode` 引数射程 / triage 射程 / Candidate union freeze)。

## 1. 形A: 何を opshub が持ち、何を外部ホストが担うか

opshub 本体が提供するもの:

1. **operational memory (①コア)** — events / projections / connectors / recall / propose / brief / graph
2. **MCP サーバ (口)** — `opshub mcp serve` (stdio)。エージェント host が ①コアを叩く経路。Phase 12 H1 で `search` (FTS5) と `propose.apply` (HITL idempotent) と既存 4 read tools の physical column ベース時間フィルタを追加し、計 17 tools (read 12 + write 5) を公開
3. **Agent Skills (手順書)** — SKILL.md 標準。本 doc が catalog する **14 Skill** (Phase 12 H2-H5 で 9 新規 + Phase 12 H1 で rename 2 を含む)。`docs/skills/<name>/SKILL.md` を opshub SSOT、配信機構 (`ozzy-labs/skills` CI + Renovate preset) は Phase 14+ に defer (Phase 13 では Google Workspace コネクタを優先)
4. **skill security scan** — `tools/skill_scan.py` (4 カテゴリ + frontmatter 隠し命令検出)、`tests/unit/skills/test_skill_specs.py` が 14 skills 全てに対して per-skill MCP dispatch pin + scan を実行

opshub 本体が **持たない** もの:

- LLM 推論ループ / ReAct ループ / LangGraph state machine — 外部ホスト (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI) の責務
- 秘書の人格 / 常駐プロセス / 能動的な push / cron 内包 — 外部ホスト or OS-level scheduler の責務
- 外部 SaaS への書き戻し (返信送信 / コメント投稿 / ファイル upload) — Phase 12 でも実装しない (ADR-0010 §禁止事項 7、緊張点③)
- handoff/announcement-draft の persist 経路 — ADR-0016 §決定 (l)(a) で「返信元 source の有無」で persist 境界を切る方針が pin された。reply-draft は persist (`reply_to_source_id` が natural key)、handoff/announcement は text-only で persist しない

## 2. 秘書への依頼例

外部ホスト (Claude Code 等) に対して以下のように頼むと、対応する skill が発火する。Phase 12 で 14 行に拡張。

| ユーザー入力 | 発火する skill | 結果 |
|---|---|---|
| 「今日のまとめ」「今週どうなってる」「先月の振り返り」「最近どうなってる」「状況教えて」「自分の状況」 | [personal-brief](skills/personal-brief/SKILL.md) | 指定期間（今日 / 今週 / 今月 / 先週 / 先月）の主要シグナル + active task + 未処理 inbox + 直近 decision |
| 「次に何やる?」「やること教えて」「今週やること」「優先度高いのは?」 | [next-actions](skills/next-actions/SKILL.md) | 優先度順の next-actions リスト。新規 task 追加は人確認付き (`task.create` HITL) |
| 「これに返信案考えて」「下書き作って」 | [reply-draft](skills/reply-draft/SKILL.md) | 返信下書き候補。送信は行わずユーザーが手で貼り付け (HITL apply、idempotent) |
| 「PR #N レビューして」「この差分どう?」 | [pr-review](skills/pr-review/SKILL.md) | 関連 decision / task / 過去議論を引いてレビュー観点を提示 |
| 「Box にあったあの資料」「<キーワード>含むファイル」「あの Word ドキュメント」「Teams で誰かが言ってた〜」「あの Google Doc」「Sheets の <X>」「Google Slides で説明したやつ」 | [find-document](skills/find-document/SKILL.md) | 本文ベース横断検索で Box / Slack / GitHub / MS365 (Outlook / Calendar / OneDrive) / Teams / Box Drive / OneDrive Drive / Google Workspace (Docs / Slides / Sheets、Phase 13) / Office 文書 (Word / Excel / PowerPoint、Phase 11 ADR-0025) から該当 source を返す (Phase 12 H1 で `search` FTS5 MCP tool を直接呼ぶよう変更) |
| 「来週の会議準備」「明日のミーティング前確認」「<会議名> の準備して」「打ち合わせ前に状況教えて」 | [meeting-prep](skills/meeting-prep/SKILL.md) | 対象 calendar event の目的 / 過去関連やりとり / 関連 decisions / 参考 sources を会議前に集約 (Phase 12 H2、read-only、pair = meeting-followup) |
| 「<X> について調べて」「<Y> の経緯」「<トピック> 網羅的に教えて」 | [research](skills/research/SKILL.md) | トピック横断調査 (semantic recall + FTS5 + graph 拡張 + LLM 統合要約) を実行し、sources 一覧 / 関連 entities / 経緯サマリを返す (Phase 12 H2) |
| 「上司向け週次報告」「クライアント向け進捗まとめ」「外向きステータス」「マネージャーに送る report」 | [external-brief](skills/external-brief/SKILL.md) | 対象期間の完了 task + 確定 decision を外向き tone で集約 (Phase 12 H3、persist なし、pair = personal-brief) |
| 「あの決定はなぜ」「X を選んだ理由」「Y の決定経緯」「なんで A じゃなくて B にしたんだっけ」 | [decision-rationale](skills/decision-rationale/SKILL.md) | 決定 + 直接の根拠 source + 先行 decision + 関連 context を `graph.trace` で provenance を遡って提示 (Phase 12 H3) |
| 「受信箱整理して」「inbox 仕分けて」「未処理アイテム捌いて」「pending を片付けて」 | [inbox-triage](skills/inbox-triage/SKILL.md) | 未処理 inbox を集めて `propose.generate (mode=inbox_triage)` で各 item の action 候補を生成、HITL apply で承認分のみ保存 (Phase 12 H4、pair = source-extract) |
| 「この資料から task 抽出」「これに含まれる decisions 教えて」「<source_id> から候補を」「このドキュメントから ToDo 拾って」 | [source-extract](skills/source-extract/SKILL.md) | 1 source の本文を取得し `propose.generate (mode=source_extract)` で task / decision / reply_draft 候補を生成、HITL apply (Phase 12 H4、pair = inbox-triage) |
| 「会議後の action items」「ミーティングのフォローアップ」「議事録から task 抽出」「昨日の会議どうだった」 | [meeting-followup](skills/meeting-followup/SKILL.md) | 直近の `ms365_calendar` を集め `source.get` + `recall.search` で context 化、`propose.generate (mode=meeting_followup)` で候補生成、HITL apply (Phase 12 H4、pair = meeting-prep) |
| 「引き継ぎ書作って」「handoff 書く」「後任向け資料まとめて」「業務引継メモほしい」 | [handoff-draft](skills/handoff-draft/SKILL.md) | task.list (state=in_progress) + decision.list + recall.search + graph.related から引き継ぎ書 text を構成して返す (Phase 12 H5、persist なし、text-only) |
| 「リリース告知文書いて」「announcement 作って」「アナウンス文章まとめて」「release notes 草案」 | [announcement-draft](skills/announcement-draft/SKILL.md) | recall.search + decision.list (`recorded_after=last_release`) + brief で告知文 text を構成して返す (Phase 12 H5、persist なし、text-only) |

## 3. Skill catalog

14 skills を read / HITL write の 2 ブロックに分割。各行は name / pair / 発火条件 / 使用 MCP tools / 出力形式。

### 3.1 read 自律 OK (10 件)

host LLM が auto-approve できる read 系。MCP annotation = `readOnlyHint=true, destructiveHint=false`。

| skill | pair | 発火条件 | 使用 MCP tools | 出力形式 |
|---|---|---|---|---|
| [personal-brief](skills/personal-brief/SKILL.md) | ↔ external-brief | 「今日 / 今週 / 今月 / 先週 / 先月 のまとめ」「最近どうなってる」「状況教えて」 | `brief` または `recall.search` + `task.list` (`updated_after/before`) + `inbox.list` (`created_after/before`) + `decision.list` (`recorded_after/before`) | 散文サマリ + signals (期間 + 主要動き + active task + 未処理 inbox) |
| [next-actions](skills/next-actions/SKILL.md) | stand-alone | 「次に何やる?」「やること教えて」「今週やること」「優先度高いのは?」 | `task.list` (`updated_after/before`) + `recall.search` (+ HITL `task.create`) | 優先度順リスト (state + due + 関連 source) |
| [pr-review](skills/pr-review/SKILL.md) | stand-alone | 「PR #N レビューして」「この差分どう?」 | `recall.search` + `decision.list` (`recorded_after/before`) + `task.list` + `graph.related` / `graph.trace` | レビュー観点リスト (関連 decision + 過去 review + 関連 task) |
| [find-document](skills/find-document/SKILL.md) | stand-alone | 「Box にあったあの資料」「<キーワード>含むファイル」「あの Word ドキュメント」「Teams で誰かが言ってた〜」「あの Google Doc」「Sheets の <X>」「Google Slides で説明したやつ」 | `search` (FTS5、Phase 12 H1) + 補助 `recall.search` / `source.list` (`observed_after/before`) / `source.get` | source 一覧 (source_type 別 / 新しい順、snippet 200 字) |
| [meeting-prep](skills/meeting-prep/SKILL.md) | ↔ meeting-followup | 「来週の会議準備」「明日のミーティング前確認」「<会議名> の準備して」 | `source.list` (`source_type=ms365_calendar` + `observed_after/before`) + `recall.search` + `graph.related` | 会議ごとに「目的 / 過去関連やりとり / 関連 decisions / 参考 sources」 |
| [research](skills/research/SKILL.md) | stand-alone | 「<X> について調べて」「<Y> の経緯」「<トピック> 網羅的に教えて」 | `recall.search` (semantic) + `search` (FTS5) + `graph.related` / `graph.expand` + `brief` (+ `graph.trace` / `source.get` 任意) | sources 一覧 + 関連 entities + 経緯サマリ |
| [external-brief](skills/external-brief/SKILL.md) | ↔ personal-brief | 「上司向け週次報告」「クライアント向け進捗まとめ」「外向きステータス」 | `task.list` (`state=completed` + `updated_after`) + `decision.list` (`recorded_after`) + `brief` (外向き tone) | 外向き report (要点先出し / 進捗 + 確定事項 / 主観抑制) |
| [decision-rationale](skills/decision-rationale/SKILL.md) | stand-alone | 「あの決定はなぜ」「X を選んだ理由」「Y の決定経緯」 | `decision.list` (topic 絞り) + `graph.trace` + `recall.search` | 決定 + 直接の根拠 source + 先行 decision + 関連 context |
| [handoff-draft](skills/handoff-draft/SKILL.md) | draft family | 「引き継ぎ書作って」「handoff 書く」「後任向け資料まとめて」 | `task.list` (`state=in_progress`) + `decision.list` + `recall.search` + `graph.related` | 引き継ぎ書 text (Markdown、persist なし、ADR-0016 §決定 (l)(a)) |
| [announcement-draft](skills/announcement-draft/SKILL.md) | draft family | 「リリース告知文書いて」「announcement 作って」「お知らせ案ほしい」 | `recall.search` + `decision.list` (`recorded_after=last_release`) + `brief` (announcement tone) | 告知文 text (Markdown、persist なし、ADR-0016 §決定 (l)(a)) |

### 3.2 HITL write (4 件)

host LLM が user 確認必須 (`propose.generate` で候補生成 → user 確認 → `propose.apply` で保存)。MCP annotation = `readOnlyHint=false, destructiveHint=false` (`propose.apply` のみ `destructive=false` + `idempotent=true` の carve-out、その他 write は `destructiveHint=true`)。auto-apply 経路は構造的に存在しない (ADR-0016 §決定 (c))。

| skill | pair | 発火条件 | 使用 MCP tools | 出力形式 |
|---|---|---|---|---|
| [reply-draft](skills/reply-draft/SKILL.md) | draft family | 「返信案考えて」「下書き作って」「これに返信したい」 | `recall.search` + `propose.generate` (`reply_to_source_id`) + `propose.apply` (HITL、idempotent) | 返信下書き候補 (persist = `ReplyDraftCandidatePayload`、`reply_to_source_id` が natural key、user が手で SaaS に貼り付け) |
| [inbox-triage](skills/inbox-triage/SKILL.md) | ↔ source-extract | 「受信箱整理して」「inbox 仕分けて」「未処理アイテム捌いて」 | `inbox.list` (`state=open`) + `propose.generate` (`mode=inbox_triage`) + `propose.apply` (HITL) | 各 inbox item への action 候補 (task / decision)、user 個別承認分のみ保存 |
| [source-extract](skills/source-extract/SKILL.md) | ↔ inbox-triage | 「この資料から task 抽出」「これに含まれる decisions 教えて」「<source_id> から候補を」 | `source.get` + `propose.generate` (`mode=source_extract`) + `propose.apply` (HITL) | 1 source から抽出された task / decision / reply_draft 候補、user 個別承認分のみ保存 |
| [meeting-followup](skills/meeting-followup/SKILL.md) | ↔ meeting-prep | 「会議後の action items」「ミーティングのフォローアップ」「議事録から task 抽出」 | `source.list` (`source_type=ms365_calendar` + `observed_after/before`) + `source.get` + `recall.search` + `propose.generate` (`mode=meeting_followup`) + `propose.apply` (HITL) | 会議からの task / decision 候補、user 個別承認分のみ保存 |

skill 本体 (SKILL.md) は `docs/skills/<name>/SKILL.md` に置く reference 仕様。実際の配布は opshub からの **手動 install** ([§8 セットアップ](#8-セットアップ) 参照)。`@ozzylabs/skills` Renovate preset 経由の配布は Phase 14+ に defer (Phase 13 では Google Workspace コネクタを優先、ADR-0004 §決定 (c) backout)。

## 4. Pair structure

host LLM の routing 精度向上のため、14 skills のうち 9 件を 4 pair として対称軸で配置 (draft family のみ 1:2 で reply-draft / handoff-draft / announcement-draft の 3 件)。pair の向き / タイミング / 粒度の軸で区別する。

| pair | A 側 | B 側 | 軸 |
|---|---|---|---|
| 自分向け ↔ 外向き | personal-brief (粒度細かめ、進行中タスクも含む、雑多 OK) | external-brief (完了 + 確定 decision 中心、tone 制御、要点先出し) | 向き |
| 会議前 ↔ 会議後 | meeting-prep (read-only、preparation context、目的 / 過去関連 / 関連 decisions / 参考 sources) | meeting-followup (HITL write、action items 抽出、`propose.generate` + `apply`) | タイミング |
| 集合 ↔ 個別 | inbox-triage (集合、inbox 全体を一気に仕分け、複数 item の action 候補を batch 生成) | source-extract (個別、1 source から候補抽出、source 本文に依拠) | 粒度 |
| draft family | reply-draft (persist、`reply_to_source_id` が natural key) | handoff-draft / announcement-draft (text-only、persist なし、自発生成で natural key なし、ADR-0016 §決定 (l)(a)) | persist 境界 |

stand-alone (pair なし、5 件): `next-actions` / `pr-review` / `find-document` / `research` / `decision-rationale`。14 - pair 9 = stand-alone 5。stand-alone は用途が直交しているため pair 化せず単独で機能する (例: `find-document` は特定 1 ファイルを引く、`research` はトピック網羅。両者は用途が異なるが routing は別軸で発火する)。

## 5. HITL boundary

ADR-0022 §決定 (c) annotation policy + ADR-0016 §決定 (c) auto-apply 禁止 + ADR-0010 §禁止事項 7 write-back 禁止の 3 層で構成。

### 5.1 read 自律 OK (10 skill)

host LLM が auto-approve できる。MCP read tools (12) を組み合わせる。durable state を変えず、外部 SaaS にも書き込まない。

- personal-brief / next-actions (read 部分のみ) / pr-review / find-document / meeting-prep / research / external-brief / decision-rationale / handoff-draft / announcement-draft

ただし `next-actions` は `task.create` (write tool) を呼ぶ可能性があり、その場合は host LLM が user 確認を入れる (ADR-0022 §決定 (c))。

### 5.2 HITL write (4 skill)

host LLM が user 確認必須。**2 段ゲート** (generate / apply の両方で人確認):

1. **第 1 ゲート (`propose.generate` 呼び出し時)** — `ProposalGenerated` event を durable log に書く (persist は generate 時点で発生)。LLM コスト + durable state 変更があるため、host LLM が user に「候補を生成しますか?」を確認
2. **第 2 ゲート (`propose.apply` 呼び出し時)** — 各 candidate を `TaskCreated` / `DecisionRecorded` / `ReplyDraftSaved` に変換。user が個別承認した candidate のみ apply (`propose.apply` は idempotent、2 回目呼び出しは `{ok:true, already_applied:true}` を返す)

- reply-draft / inbox-triage / source-extract / meeting-followup

auto-apply 経路は構造的に存在しない (ADR-0016 §決定 (c))。`opshub propose apply` CLI も `propose.apply` MCP tool も必ず明示的に operator (CLI) / user (MCP HITL) が叩く必要がある。

### 5.3 外部書き戻し非存在 (構造的禁止)

- すべての connector package で `send` / `post` / `write` / `comment_create` callable を持たない (ADR-0010 §禁止事項 7、`tests/integration/test_phase11_office_lifecycle.py` + Phase 12 H6 e2e test (`tests/integration/test_phase12_secretary_lifecycle.py`) で構造的に pin)
- reply-draft / handoff-draft / announcement-draft は draft text を生成するだけで、SaaS への送信経路を持たない。ユーザーが手で SaaS に貼り付ける
- 将来 SaaS 書き戻しが必要になっても、新 ADR + ADR-0004 revisit + ADR-0016 §決定 (c) 整合の 3 要件すべてを要求する (ADR-0010 §禁止事項 7 改訂は引き続き保持)

## 6. MCP tool 依存マップ

14 skills × 17 MCP tools (read 12 + write 5) のマトリクス。各 skill が呼び出す MCP tool を列挙。✓ = primary 経路、(✓) = 補助経路 (Step 2 以降の optional 呼び出し)。

### 6.1 Read tools (12)

| MCP tool | personal-brief | next-actions | reply-draft | pr-review | find-document | meeting-prep | research | external-brief | decision-rationale | handoff-draft | announcement-draft | inbox-triage | source-extract | meeting-followup |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `recall.search` | ✓ | ✓ | ✓ | ✓ | (✓) | ✓ | ✓ |  | ✓ | ✓ | ✓ |  | (✓) | ✓ |
| `task.list` | ✓ | ✓ |  | ✓ |  |  |  | ✓ |  | ✓ |  |  |  |  |
| `inbox.list` | ✓ |  |  |  |  |  |  |  |  |  |  | ✓ |  |  |
| `decision.list` | ✓ |  |  | ✓ |  |  |  | ✓ | ✓ | ✓ | ✓ |  |  |  |
| `brief` | ✓ |  |  |  |  |  | ✓ | ✓ |  |  | ✓ |  |  |  |
| `graph.related` |  |  |  | ✓ |  | ✓ | ✓ |  |  | ✓ |  |  |  |  |
| `graph.trace` |  |  |  | ✓ |  |  | (✓) |  | ✓ |  |  |  |  |  |
| `graph.expand` |  |  |  |  |  |  | ✓ |  |  |  |  |  |  |  |
| `source.list` |  |  |  |  | (✓) | ✓ |  |  |  |  |  |  | (✓) | ✓ |
| `source.get` |  |  |  |  | (✓) | (✓) | (✓) |  | (✓) |  |  |  | ✓ | ✓ |
| `embeddings.find_duplicates` |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| `search` (FTS5、Phase 12 H1) |  |  |  |  | ✓ |  | ✓ |  |  |  |  |  | (✓) |  |

### 6.2 Write tools (5、HITL)

| MCP tool | personal-brief | next-actions | reply-draft | pr-review | find-document | meeting-prep | research | external-brief | decision-rationale | handoff-draft | announcement-draft | inbox-triage | source-extract | meeting-followup |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `task.create` |  | ✓ (HITL) |  |  |  |  |  |  |  |  |  |  |  |  |
| `inbox.add` |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| `connector.sync` |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| `propose.generate` |  |  | ✓ (`reply_to_source_id`) |  |  |  |  |  |  |  |  | ✓ (`mode=inbox_triage`) | ✓ (`mode=source_extract`) | ✓ (`mode=meeting_followup`) |
| `propose.apply` (Phase 12 H1) |  |  | ✓ (HITL、idempotent) |  |  |  |  |  |  |  |  | ✓ (HITL) | ✓ (HITL) | ✓ (HITL) |

`inbox.add` と `connector.sync` は 14 skills のいずれからも primary 経路として呼ばれない (host LLM の自律判断で呼ぶ余地は残す)。`embeddings.find_duplicates` も同様 (現状は CLI / operator が直接叩く用途)。

### 6.3 Phase 11 / Phase 13 source_type 列挙

Phase 11 で追加された source_type (`teams_message` / `ms365_outlook` (body deep retention) / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck`) と Phase 13 で追加された source_type (`google_doc` / `google_slides` / `google_sheets` / `google_workspace_file` catch-all) は、14 skills 全てから `recall.search` / `search` / `source.list` / `source.get` 経由で透過的に利用可能。mapper が `sources.body` に persist する限り skill 側に追加の変更は不要 (Phase 11 plan §7.3 step 1 / Phase 13 plan §7.3 step 4)。

find-document が利用できる本文系 source_type は計 6 種 (Phase 11 office 3 種 + Phase 13 Google Workspace native 3 種) + その他 metadata + body 各 connector で 1 つ MCP / 1 つ search だけで横断可能。`google_workspace_file` (catch-all、非 native = Drive にアップロードされた PDF / 画像 / フォルダ等) は metadata-only (`body=None`) で persist されるため、find-document の対象になるのは title / URL / observed_at のみ。

### 6.4 Phase 12 H1 で追加された physical column ベース時間フィルタ

| MCP tool | argument | projection 列 |
|---|---|---|
| `task.list` | `updated_after` / `updated_before` | `tasks.updated_at` |
| `inbox.list` | `created_after` / `created_before` | `inbox_items.created_at` |
| `decision.list` | `recorded_after` / `recorded_before` | `decisions.recorded_at` |
| `source.list` | `observed_after` / `observed_before` | `sources.observed_at` |

すべて ISO 8601 string、optional、`>= after` / `< before` 半開区間。physical column 命名により business 概念 (`completed_after` 等) と物理列の混線を回避 (ADR-0022 改訂 §決定 (f-3))。

## 7. できること / できないこと

### 7.1 できること

- opshub に蓄積された **本文ベースの operational memory** (Phase 10 Sub A: 本文保持 + 暗号化、Sub B: 本文 embedding + FTS5) を横断検索 / 要約 / 関連抽出する
- 過去の decision / task / proposal / event を踏まえた **文脈付き** の応答 (`--expand-graph` で知識グラフ拡張、ADR-0017)
- 返信下書きを「自分の過去送信 event」の文体を recall して再現 (ADR-0016 §決定 (k))
- 複数 agent host (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI) から **同一の MCP 面** を叩いて同じ記憶を共有
- Phase 11 で追加された **MS Office 由来の文脈**を秘書の素材として使う (14 skills 全てが `source.body` ベースで透過的に対応):
  - **Teams chat 本文** (`teams_message`、Microsoft Graph delta query 経由、[Teams setup](teams-setup.md))
  - **Outlook 本文 deep retention** (Phase 10 で取り込み始めた summary に加え、Phase 11 で本文も `sources.body` に persist)
  - **Office 文書本文** (`.docx`/`.xlsx`/`.pptx`、markitdown 経由、`box_drive` / `onedrive_drive` の `content_extraction = true` opt-in、[ADR-0025](adr/0025-office-document-content-extraction.md))
- Phase 13 で追加された **Google Workspace 由来の文脈**を秘書の素材として使う (14 skills 全てが `source.body` ベースで透過的に対応):
  - **Google Docs 本文** (`google_doc`、Drive API `files.export(fileId, mimeType=docx)` → markitdown、`google_workspace` の `content_extraction = true` opt-in、[Google Workspace setup](google-workspace-setup.md))
  - **Google Slides 本文** (`google_slides`、Drive API export → pptx → markitdown)
  - **Google Sheets 本文** (`google_sheets`、Drive API export → xlsx → markitdown)
  - **Google Workspace metadata only** (`google_workspace_file` catch-all = Workspace 非 native ファイル / フォルダ、metadata のみ persist。Phase 13 G3 default 挙動)
- Phase 12 で追加された **秘書らしいユースケース** に対応 (5 → 14 skills 拡張):
  - 「会議準備 / 会議後フォロー」(`meeting-prep` ↔ `meeting-followup`)
  - 「トピック横断調査」(`research`、recall + FTS5 + graph 拡張 + brief 統合)
  - 「外向きまとめ」(`external-brief`、外向き tone)
  - 「決定の経緯を遡る」(`decision-rationale`、`graph.trace` で provenance walk)
  - 「inbox を一気に仕分け」(`inbox-triage`、HITL batch)
  - 「1 source から候補抽出」(`source-extract`、HITL)
  - 「引き継ぎ書 / 告知文 text 生成」(`handoff-draft` / `announcement-draft`、text-only)

### 7.2 できないこと (構造的な禁止)

- **外部 SaaS への書き戻し** — Slack / Box / GitHub / MS365 / Teams に返信送信 / コメント投稿 / ファイル upload しない (ADR-0010 §禁止事項 7 + Phase 10 Sub E + Phase 11/12 H6 e2e test の経路非存在 pin)
- **能動的な push / 通知** — 「3 時に reminder 送る」「inbox を 1 時間ごとにチェック」のような常駐 runtime は持たない (ADR-0004 §(a) 形A、Phase 12 でも継続)
- **LLM 推論の opshub 内蔵** — opshub は推論ループを実行しない。LLM 呼び出し (Anthropic / OpenAI / Ollama) は `opshub propose` / `opshub brief` のようなコマンド経路でユーザーが明示的に起動したときのみ発生 (ADR-0015)
- **auto-apply** — `opshub propose apply` も `propose.apply` MCP tool も必ず人が叩く (ADR-0016 §決定 (c))。`propose.apply` は idempotent (2 回目は `{ok:true, already_applied:true}`) だが、最初の apply は user 確認必須
- **handoff-draft / announcement-draft の persist** — ADR-0016 §決定 (l)(a) で「返信元 source の有無」で persist 境界を切る方針。自発生成で natural key を持たない handoff/announcement は text-only で persist しない。将来 persist 需要が顕在化したら ADR-0016 §決定 (f) versioning パターンで対応 (Phase 14+)

## 8. セットアップ

### 8.1 opshub MCP server をホストから起動できるようにする

詳細は [`docs/mcp-setup.md`](mcp-setup.md) を参照。最小限は次の通り。

```bash
# 環境準備
uv tool install ozzylabs-opshub[mcp]
opshub init   # 初回のみ

# MCP server を stdio で起動 (host が subprocess として spawn する想定)
opshub mcp serve
```

### 8.2 秘書 Skills 14 件をホストに配布する (Phase 12: 手動 install)

Phase 12 では `ozzy-labs/skills` 経由の配布機構が未整備のため、opshub リポから host の skill loader 配下に手動 copy する。

Claude Code (`.claude/skills/`):

```bash
# opshub リポを clone 済の前提
cp -r opshub/docs/skills/* ~/.claude/skills/
```

Codex CLI / GitHub Copilot CLI (`.agents/skills/`):

```bash
cp -r opshub/docs/skills/* ~/.agents/skills/
```

プロジェクト単位で導入する場合は project root の `.claude/skills/` / `.agents/skills/` に同じパターンで copy する。`@ozzylabs/skills` Renovate preset 経由の自動配布は Phase 14+ に defer (ADR-0004 §決定 (c) backout)。

opshub 本体リポでは `docs/skills/<name>/SKILL.md` が **SSOT** として置かれている (Phase 12 H1 で確定、ADR-0004 §決定 (c))。

### 8.3 ホストから skill を呼ぶ

各ホスト固有の skill loader 経路 (Claude Code は `Skill(skill="...")` ツール、Codex CLI は AGENTS.md 経由、等) で skill を発火する。ユーザーは「今日のまとめ」のような自然文で頼むだけでよい (skill description が日本語トリガを含むため)。

## 9. skill security について

`tools/skill_scan.py` で 4 カテゴリ (プロンプトインジェクション / コマンドインジェクション / ハードコード鍵 / データ持ち出し) + frontmatter の隠しユニコード / 「ignore previous instructions」類のパターン検出を行う。

- 本リポ内 (`docs/skills/<name>/SKILL.md`) の 14 skills 全てに test (`tests/unit/skills/test_skill_specs.py`) で適用済
- 14 skills 全てに per-skill MCP dispatch pin (skill 内 MCP tool 名・引数 schema が opshub MCP surface と整合するか grep + JSON schema validation)
- HITL boundary test pin (HITL write skill の `propose.apply` annotation = `read_only=false, destructive=false, idempotent=true`)
- text-only boundary test pin (handoff/announcement-draft が persist 経路を持たない)
- `ozzy-labs/skills` 側 CI への組み込みは Phase 14+ で配布機構整備時に対応 (ADR-0004 §決定 (c) backout)
- 検出ルールは scope 縮小設計 (高 precision / 中 recall)。誤検出は `# skill-scan: allow <category>` コメントで局所的に suppress 可能

## 10. 関連

- [ADR-0004 Agent Runtime Boundary (形A + Phase 12 H1 改訂 = Skills SSOT 移管 + §決定 (c-2) Skill catalog SSOT)](adr/0004-agent-runtime-boundary.md)
- [ADR-0010 Connector Contract (write-back 禁止)](adr/0010-connector-contract.md)
- [ADR-0016 Action Loop (reply-draft / auto-apply 禁止 / Phase 12 H1 改訂 = §決定 (l) draft 系統一方針)](adr/0016-action-loop-and-structured-output.md)
- [ADR-0020 Full Local Content Retention](adr/0020-full-local-content-retention.md)
- [ADR-0022 MCP Server Surface (Phase 12 H1 改訂 = §決定 (f) 4 新 MCP tools = `search` + `propose.apply` + 物理列ベース時間フィルタ)](adr/0022-mcp-server-surface.md)
- [docs/mcp-setup.md](mcp-setup.md)
- [Phase 10 Implementation Plan](phase-10-plan.md)
- [Phase 12 Implementation Plan](phase-12-plan.md)
- handbook ADR-0016 (skills repo `ozzy-labs/skills` 配布機構、Phase 14+ で配布完成予定)
