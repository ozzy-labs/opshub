# AGENTS.md

このファイルは AI エージェント向けの共通 instructions です。

## 基本方針

- 日本語で応答する
- 推奨案とその理由を提示する
- `.env` ファイルは読み取り・ステージングしない
- 破壊的な Git 操作を避ける

## 設計判断のスタンス

- 過去の決定（ADR など）やこれまでの実装量にとらわれず、「あるべき姿」を優先して提案する。既存 ADR と食い違う場合は、その ADR の見直し・転換を提案してよい。
- ただし、過去の決定や実装量を**まったく無視してよいわけではない**。改修にかかる手間や既存設計との兼ね合いは判断材料として示したうえで、それでも「あるべき姿」を推す。
- この方針は、opshub にまだ実ユーザーがいない現段階を前提とする。1.0 リリースや実ユーザーが付いた時点で見直す。

## プロジェクト概要

`opshub`: Local-first assistant agent platform — auditable operational memory for humans and AI agents.

**Status**: Phase 1-15 complete + Phase 16 complete (epic #381, 2026-06-02) + Phase 17 complete (CLI noun-first 再編、ADR-0031、PR #414) + Phase 18 complete (Slack mention / DM demand digest、ADR-0033、epic #426 = 18-A PR #431 docs + 18-B PR #433 projection + 18-C new MCP `slack.demand.list` + skill 拡張 + docs 一括更新). Phase 15 complete 2026-06-02 (epic #338); Phase 16-A pinned distribution-channel decision via ADR-0029 + ADR-0004 §決定 (c) revision (doc + test docstring realignment), Phase 16-B (#383) landed `opshub skills install` / `opshub skills list` CLI with `[tool.hatch.build.force-include]` bundle of `docs/skills` → `src/opshub/_skills`, Phase 16-C (#384) wired `opshub init` to the install path (TTY prompt + `--install-skills` / `--no-install-skills` flag, non-interactive default = install), Phase 16-D (#385) populated in-repo `.claude/skills/<assistant>/` + `.agents/skills/<assistant>/` dogfood mirrors + added `skills-sync-check` pre-commit lefthook guard (`diff -rq` against SSOT). Phase 1 (foundation) + Phase 2 (coordination) + Phase 3 (connector layer + workspace ingest、MVP = framework + GitHub) + Phase 4 (semantic recall layer、MVP = Pluggable Embedder + sqlite-vec + recall + 重複検出) + Phase 5 (briefing layer、MVP = ADR-0015 + Pluggable LLM (Anthropic + OpenAI) + Briefing 自動生成 + event-driven auto-embed) + Phase 6 (action loop layer、MVP = ADR-0016 + Pluggable LLM structured output (Anthropic + OpenAI + Ollama) + Proposal domain) + Phase 7 (connectors wave 2、MVP = Slack + Microsoft 365 + Box) + Phase 8 (knowledge graph layer、MVP = ADR-0017 + `links` projection + 4 自動抽出 + manual link CRUD + traversal + `--expand-graph`) complete (2026-05-17) + Phase 9 (local-filesystem-backed connector layer、MVP = ADR-0019 + `sources.fingerprint` (migration 0017) + `box_drive` connector + `core/platform.py`) complete (2026-05-23) + Phase 10 (Assistant Agent Platform、MVP = ADR-0020 (full local content retention、ADR-0005 supersede) + ADR-0021 (encryption at rest、SQLCipher + keyring) + ADR-0022 (MCP server surface、stdio + policy-as-data + redact + OTel naming) + ADR-0004 改訂 (形A) + ADR-0016 改訂 (reply_draft) + ADR-0017 改訂 (reply_draft link types) + ADR-0010 改訂 (write-back 明示禁止) + 本文ベース embedding (migration 0018) + SQLite FTS5 (migration 0019) + `opshub search` + `opshub mcp serve` + アシスタント 5 Skills (Phase 12 H1 で `personal-brief` / `next-actions` / `reply-draft` / `pr-review` / `find-document` に rename 済) + `tools/skill_scan.py`) complete (2026-05-31) + Phase 11 (MS Office 深掘り、MVP = ADR-0025 (Office Document Content Extraction、markitdown 経路 + 50 MB / 500K chars cap + source_type 3 種 `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` + fail-safe) + ADR-0019 改訂 (`content_extraction = true` opt-in 例外節 + `onedrive_drive` パターン汎化、§決定 (b') + (j)) + ADR-0010 改訂 (Teams connector 追加 + 本文抽出契約 + delta-link cursor + 失効時 full-pass fallback + Teams User Token principal、§改訂 (a)/(b)/(c)/(d)) + `src/opshub/core/document_extract.py` + `connectors/teams/` (Microsoft Graph chat delta + User Token) + `connectors/onedrive_drive/` (FS scan、WSL2 `/mnt/onedrive` / macOS `~/OneDrive` platform default) + `connectors/box_drive` の Office 抽出 hook + `connectors/ms365/mapper` の Outlook body deep retention、epic #233) complete (2026-05-31) + **Phase 12 (Assistant Skills 拡張、MVP = 14 skills 体制 (新規 9 = meeting-prep / research / inbox-triage / external-brief / decision-rationale / handoff-draft / announcement-draft / meeting-followup / source-extract + 既存 5 のうち rename 2 = daily-brief → personal-brief / file-lookup → find-document、read 自律 OK 10 / HITL write 4) + 4 新 MCP tools (`search` (FTS5、`ReadCategory.SEARCH`) + `propose.apply` (HITL idempotent、`WriteCategory.PROPOSE_APPLY`、`destructive=false`) + 既存 4 read tools の physical column ベース時間フィルタ (`task.list.updated_after/before` / `inbox.list.created_after/before` / `decision.list.recorded_after/before` / `source.list.observed_after/before`)) + 既存 5 SKILL.md を MCP 直接呼びに統一 + `propose.generate` の `mode` 引数追加 (`inbox_triage` / `source_extract` / `meeting_followup`) + ADR 改訂 3本 (ADR-0004 改訂 (Skills SSOT を opshub `docs/skills/` に移管 + Skill catalog SSOT = `docs/assistant-agent.md` 独立条文化) + ADR-0022 改訂 (4 新 MCP tools 契約化) + ADR-0016 改訂 (draft 系統一方針 §決定 (l): persist 境界 = 返信元 source の有無 / `mode` 引数射程 / triage = reply_draft 専用 / Candidate union freeze)) + `docs/assistant-agent.md` を 14 skills 責務マップ SSOT に拡張 (10 § 構成: §形A 責務分担 / §アシスタントへの依頼例 / §Skill catalog / §Pair structure / §HITL boundary / §MCP tool 依存マップ / §できること・できないこと / §セットアップ / §skill security / §関連)、epic #253) complete (2026-05-31) + **Phase 13 (Google Workspace コネクタ、MVP = ADR-0010 改訂 (Phase 13 改訂 (e)-(h) = Google Workspace 追加 / Drive `files.watch` 禁止 / Workspace export 経路の本文抽出契約 / Drive `changes.list` cursor + TTL fallback 義務 / Refresh Token principal = MS365 / Box pattern、Teams pattern とは別系統) + ADR-0014 改訂 (§Phase 7 Validation rotation pin リストに `connector:google_workspace:refresh_token` を 3 件目として追加 + env override `OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN` + Phase 7 keyring keys 一覧に Phase 13 Google Workspace を追記) + ADR-0025 改訂 (§決定 (d') 新 source_type 3 種 `google_doc` / `google_slides` / `google_sheets` + §決定 (j) Workspace export 経路 = Drive API `files.export` → MS Office mediatype → markitdown 統一) + `connectors/google_workspace/` (5 module 構成 = `auth.py` paste-code OAuth + refresh token rotation 書き戻し / `client.py` Drive API v3 `changes.list` + httpx + rate-limit retry / `cursor.py` page token + TTL 失効時 full-pass fallback / `mapper.py` Google mimeType → source_type 分岐 + provenance / `connector.py` content_extraction opt-in wiring + files.export 経由 markitdown 抽出 + settings) + `core/document_extract.extract_workspace_export(bytes, source_type)` (G2 で API 表面拡張 = path / bytes 両方、Phase 11 path-only と backward-compat) + `[connectors-google-workspace]` extras (httpx)、epic #274) complete (2026-05-31)** + **Phase 14 (Gmail + Google Calendar コネクタ、MVP = ADR-0010 改訂 (§Phase 14 改訂 (i)-(m) = Gmail + Calendar 追加 / delta-cursor 型 connector 全般への TTL fallback 一般化 ((j) = Phase 11/13 SSOT 統合) / Outlook 流本文抽出 (text/plain 優先 → text/html 生保持 / markitdown なし / 添付 retain なし) を Gmail / Calendar に適用 / Gmail unit = message 単位 + Calendar unit = master event only + override 別 record + label / attendee は summary / body 埋め込みのみ (Outlook 流) / Google OAuth principal scope 拡張 + shared auth foundation `connectors/google_auth/` 抽出方針) + ADR-0014 改訂 (§Phase 7 Validation `connector:google_workspace:refresh_token` slot の scope を Drive → Drive + Gmail + Calendar に拡大、shared auth foundation `connectors/google_auth/auth.py` への抽出方針追記、新 slot 追加なし = 1 Google account = 1 principal で 3 connector 共有) + `connectors/google_auth/` (Phase 13 `connectors/google_workspace/auth.py` の物理移動、scope 引数化 + 3-scope 固定 list `drive.readonly + gmail.readonly + calendar.readonly`、token rotation pin test を shared 側に集約) + `connectors/google_mail/` (5 module 構成、httpx + Gmail API v1 `users.messages.list` initial sync + `users.history.list` delta + 7 日 TTL 失効時 full-pass fallback + WARN、message 単位 mapper `gmail_message` = Outlook と symmetric、`[Labels: ...]` prepend / `[gmail body truncated]` tag / threadId field、添付 retain なし) + `connectors/google_calendar/` (5 module 構成、httpx + Calendar API v3 `events.list(syncToken=...)` + 410 GONE 失効時 full-pass + `timeMin`/`timeMax` window + WARN、master event mapper `google_calendar` = MS365 Calendar と symmetric、summary = `start_iso - end_iso (N attendees)` / RRULE field / attendee body 埋め込み / override は別 record として emit) + mapper symmetry pin test (`tests/unit/connectors/test_mapper_symmetry.py`、Outlook ↔ Gmail / ms365_calendar ↔ google_calendar の field / summary / body フォーマット同形性を機械検証)、epic #292) complete (2026-05-31)** + **Phase 15 (Search 品質改善 = FTS5 日本語 tokenizer trigram 化 + 短クエリ LIKE fallback、MVP = ADR-0028 新規 (FTS5 sources_fts tokenizer choice) + migration `0028_rebuild_sources_fts_trigram` (`sources_fts` を `unicode61 remove_diacritics 2` から FTS5 built-in `trigram` に物理張り替え + `sources.body` から back-fill + trigger 3 本再作成、downgrade 経路は元 tokenizer に復元) + SearchService 短クエリ LIKE fallback (`src/opshub/services/search_service.py` の `_MIN_FTS_QUERY_CHARS = 3` 閾値で 1-2 文字 query を `LOWER(body) LIKE LOWER(?)` full scan に routing、NFC 正規化 + ASCII case-insensitive + LIKE wildcard escape、`raw_query=True` で fallback bypass) + cross-cutting fix (`tests/unit/mcp/test_phase12_handlers.py::_bootstrap_fts_index()` の seed tokenizer を `unicode61 remove_diacritics 2` から `trigram` に同期、production と MCP unit boundary 再整合) + `opshub search --help` の `--raw` 説明更新 + `docs/troubleshooting.md` §3.6 日本語 search 節追加。日本語自然文 (`boxの権限` / `進捗記入` / `CDKの`) は default で 3 文字以上の substring が hit、短クエリ (`依頼` / `PR` / `Q4`) は LIKE fallback で hit、`--raw` は FTS5 boolean / prefix の power-user 契約として維持。MCP `search` tool (ADR-0022 §決定 (f)) は `raw_query` hard-coded `false` のためアシスタント 14 Skill (`find-document` / `research` / etc.) も透過的に恩恵を受ける。形 A + 外部書き戻しなし + immutable migration 規範 (0019 は touch せず 0028 で supersede) を継承、新規 ADR 1 本 + 改訂ゼロ (Phase 11 流の単一トピック集中パターン継承、Phase 11 = 1 新規 + 2 改訂、Phase 12 = 0 新規 + 3 改訂、Phase 13 = 0 新規 + 3 改訂、Phase 14 = 0 新規 + 2 改訂、**Phase 15 = 1 新規 + 0 改訂**)、epic #338) complete (2026-06-02)** — event store、全 projection (tasks / inbox_items / decisions / work_sessions / agent_runs / locks / handoffs / sources (Phase 10 で `body` + `provenance_origin` + `provenance_trust` 追加、Phase 11 で `teams_message` / `word_document` / `excel_spreadsheet` / `powerpoint_slide_deck` source_type 追加、Phase 13 で `google_doc` / `google_slides` / `google_sheets` / `google_workspace_file` source_type 追加、Phase 14 で `gmail_message` / `google_calendar` source_type 追加) / connector_cursors / ingested_files / embeddings / briefings / proposals (Phase 10 で `reply_draft` candidate kind 追加) / links (Phase 10 で `reply_draft_replies_to` / `referenced_in_reply_draft` link types 追加))、CLI (`init` / `task` / `inbox` / `decision` / `lock` / `session` / `agent run` / `handoff` / `connector auth set` (Phase 13 で `google_workspace` Google OAuth paste-code flow 追加) / `connector sync` (Phase 13 で `google_workspace`) / `connector list` / `workspace ingest` / `workspace generate` / `projections rebuild` / `embeddings rebuild` / `embeddings drain` / `embeddings status` / `embeddings find-duplicates` / `recall` / `search` (Phase 10) / `brief` / `propose generate` (Phase 10 で `--reply-to` 対応、Phase 12 H4 で `mode` 対応) / `propose list` / `propose apply` / `propose reject` / `link add` / `link remove` / `link list` / `graph related` / `graph trace` / `graph expand` / `mcp serve` (Phase 10) / `mcp tools` (Phase 10) / `db migrate`)、markdown 生成、**10 connector** (GitHub + Slack + Microsoft 365 + Box + Box Drive (FS scan) + Teams (Phase 11、Microsoft Graph chat delta) + OneDrive Drive (Phase 11、FS scan) + Google Workspace (Phase 13、Drive API v3 `changes.list` + Refresh Token rotation + Workspace export 経路) + Gmail (Phase 14、Gmail API v1 `users.history.list` delta + message 単位 + 7 日 TTL fallback、Outlook と symmetric) + Google Calendar (Phase 14、Calendar API v3 `events.list(syncToken=...)` + master event only + override 別 record + 410 GONE fallback、MS365 Calendar と symmetric)、Google 3 connector は Phase 14 で `connectors/google_auth/` shared auth foundation = 1 Google account principal + 3-scope 固定 list `drive.readonly + gmail.readonly + calendar.readonly` を共有) で `source_type` discriminator 別に `sources` projection に persist (Phase 10 で本文 + provenance 取り込みに拡張、`box_drive` / `onedrive_drive` は ADR-0019 §不変条件 (b) で default `body=None`、Phase 11 で `content_extraction = true` opt-in 時のみ markitdown 経由で Office 文書 本文取り込み、Phase 13 で Google Workspace native (`google_doc` / `google_slides` / `google_sheets`) も同じ `content_extraction = true` opt-in で Workspace export → markitdown 経路を起動、Phase 14 で Gmail / Calendar 本文は Outlook 流 = text/plain 優先 → text/html 生保持で markitdown 経路を通さない)、workspace inbox file ingest、Pluggable Embedder + sqlite-vec backed VectorStore、Pluggable LLMClient (Anthropic + OpenAI + Ollama) + structured output + Briefing 自動生成 + Proposal domain (human-in-the-loop apply、Phase 10 で reply_draft mode 追加、Phase 12 で `mode` 引数経由の 3 dispatch key 追加) + Knowledge graph layer + event-driven auto-embed + MCP server (stdio one transport、policy-as-data registry、Phase 10 sub C + Step 1 widening PR #231 + Phase 12 H1 で計 17 tools = read 12 + write 5、Phase 18-C で `slack.demand.list` 追加 → 18 tools = read 13 + write 5) + **アシスタント 14 Skill** (`docs/skills/<name>/SKILL.md` を opshub SSOT として保持、Phase 12 H1 で確定、配信経路は Phase 16-A で opshub Python package 同梱 + `opshub skills install` に確定 = [ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)、Phase 16-B/C/D で着地済 = `opshub skills install` CLI / `opshub init` 連携 / in-repo dogfood) + 保存時暗号化 (`[storage] encryption = true` で opt-in、SQLCipher AES-256、Phase 10 sub A、ADR-0021) が end-to-end で動作。次の候補は Phase 16+ (形態素 tokenizer 採用 (Lindera / SudachiPy / MeCab、Phase 15 で defer、trigram で operator 体験が不足する場合に ADR-0028 改訂 + tokenizer 評価) / dual index (unicode61 + trigram、Phase 15 で却下、BM25 ranking 劣化が観測されたら再評価) / `opshub search rebuild-index` 専用 CLI (projection rebuild 経由で十分か、需要顕在化時に切る) / MCP `search` tool 契約改訂 (`raw_query` operator 露出、ADR-0022 改訂事項) / 検索結果の semantic re-rank (vector との hybrid score、別 ADR) / snippet() ハイライト / 検索クエリの NFKC 正規化 / 全角半角統一 / multi-machine sync / 能動性段階 1-4 = cron 委譲 / 記憶キュレーション / 通知 / filewatch / Gmail push / Calendar push 再評価 / 画像 OCR (PPT 内画像 + Office 図表、tesseract / pytesseract、Phase 13 → Phase 14 → Phase 15 から繰り越し) / Drive Comments + Suggestions (Phase 13 から繰り越し) / Gmail 添付・Calendar 添付の本文抽出 (markitdown 経路、ADR-0025 拡張) / 追加コネクタ Notion / Jira / Linear / Confluence (Phase 13 から繰り越し) / 外部書き戻し = Teams 返信送信 + Gmail send + Calendar event create + HITL / Calendar instance 展開 projection (master + RRULE → instance dynamic、ms365 / google 両 calendar 同時)、詳細は `docs/principles.md` §9)。

### Post-Phase 15 Maintenance

Phase 15 完了 (2026-06-02) 以降の改修は Phase 化されておらず、connector-level の UX 改善 / refactor / audit followup として進行する。新 ADR / 新 projection / 新 connector category を伴う作業は新 Phase で起票、それ以外は本節に追記する。時系列順:

- **Slack `channels` → `conversations` 刷新** ([#366](https://github.com/ozzy-labs/opshub/issues/366) → PR [#369](https://github.com/ozzy-labs/opshub/pull/369)) — `users.conversations` joined-only default + DM/MPIM 統合 + `--types` / `--all` / 進捗表示。`docs(slack)` 整合 PR [#371](https://github.com/ozzy-labs/opshub/pull/371) を追随。
- **Slack search title 改善** ([#367](https://github.com/ozzy-labs/opshub/issues/367) → PR [#368](https://github.com/ozzy-labs/opshub/pull/368)) — bot/system message fallback + body excerpt 80 字。
- **Slack `conversations` type 別固定ソート + `--since` activity filter** ([#374](https://github.com/ozzy-labs/opshub/issues/374) → PR [#375](https://github.com/ozzy-labs/opshub/pull/375)) — `public → private → mpim → im` 固定順、`--since 7d` / `--since 2026-05-01` で per-conv `conversations.history?limit=1` + `LAST_ACTIVITY` 列、不足 `*:history` scope は type 単位で skip + 1 warn (`exit 0`)。
- **Slack 429 retry / `Retry-After` backoff helper 集約** ([#377](https://github.com/ozzy-labs/opshub/issues/377) → PR [#378](https://github.com/ozzy-labs/opshub/pull/378)、[#379](https://github.com/ozzy-labs/opshub/issues/379) → PR [#380](https://github.com/ozzy-labs/opshub/pull/380)) — `opshub.connectors.slack._retry.retry_on_rate_limit` 1 helper に集約 (3 attempts、`Retry-After` honoured、fallback 1s / 2s / 4s exponential)、slack コネクタ内 3 call site (`SlackFetcher._call_history` + `conversations._call_history_oldest` + `conversations._call_list`) が share、policy 改修が 3 箇所同期 → 1 箇所更新で済む。
- **Audit followup: Slack docs drift + test coverage gap closure** (PR [#386](https://github.com/ozzy-labs/opshub/pull/386)) — `CLAUDE.md` / `docs/mcp-setup.md` / `AGENTS.md` / `docs/adr/0026-cli-progress-reporting.md` の `--since` 関連 doc 整合 4 件 + `parse_since` OverflowError / `_call_list all=True` exhaustion / `_render_json` mixed payload / `_retry` headers 防御 / `_fetch_last_activity_ts` 防御 (`ts` 非数値 / `messages` 非 list) / `_sort_rows` 未知 type fallback 計 7 件の test pin。
- **Slack thread ingestion 仕様 pin** ([ADR-0030](docs/adr/0030-slack-thread-reply-ingestion.md)) — 子返信は `conversations.replies` で ingest、`thread_ts` field 保持、`slack_thread` source_type 作らず Gmail / Outlook と symmetric (ADR-0010 §Phase 14 改訂 (k) 不変条件 3 と対称)。本 ADR は方針 pin のみで実装は後続 Phase に分離 (fetcher 拡張 / mapper の `thread_ts` field 追加 / `_retry.retry_on_rate_limit` share / integration test / `--max-threads-per-sync` cap 検討は別 issue)。

### Phase 16: Assistant Skill Distribution via opshub Package Bundling

新規 architectural pattern (新 ADR + 配信経路改訂) を伴うため新 Phase で起票。epic [#381](https://github.com/ozzy-labs/opshub/issues/381)。

- **Phase 16-A** ([#382](https://github.com/ozzy-labs/opshub/issues/382)) — ADR-0029 新規 + ADR-0004 §決定 (c) 改訂 + doc 一括更新 (architecture / assistant-agent / README / README.ja / mcp-setup / upgrading / CLAUDE / AGENTS の "cp -r" 手順を pointer 化) + test docstring realignment + `_skills/` payload `.py` 不在 pin (Phase 16-B 着地前は skip)。配信経路を `ozzy-labs/skills` Renovate preset から opshub Python package 同梱 + `opshub skills install` に切り替え (SSOT 位置は opshub `docs/skills/<name>/SKILL.md` で不変)。dogfood 採用 (Phase 16-D で実行)。
- **Phase 16-B** ([#383](https://github.com/ozzy-labs/opshub/issues/383)) — `[tool.hatch.build.force-include]` で `docs/skills` → `src/opshub/_skills` 同梱 + `opshub skills install` / `opshub skills list` CLI 実装 (`--scope {user,project}` / `--host {claude-code,codex,copilot,all}` / `--skip-existing` / `--dry-run` / `--print-paths`)。
- **Phase 16-C** ([#384](https://github.com/ozzy-labs/opshub/issues/384)) — `opshub init` 連携 (`--install-skills` / `--no-install-skills` flag、TTY 時は `rich.prompt.Confirm` で確認 (default = yes)、非対話 (`sys.stdin.isatty() == False`) は default = install で `uv tool install ozzylabs-opshub[mcp] && opshub init` 経路を救済 (ADR-0029 §決定 (d)))。`install_command` を `opshub.cli.skills` から `opshub.cli.init` が lazy import で呼び出す構造。
- **Phase 16-D** ([#385](https://github.com/ozzy-labs/opshub/issues/385)、ADR-0029 §dogfood 採用) — in-repo `.claude/skills/<assistant>/` populate (project scope dogfood)。

### Phase 17: CLI command surface reorganization (noun-first per-noun group)

新規 ADR (新 CLI 規範) を伴うため新 Phase で起票。`opshub connector <verb>` group を `opshub <noun> <verb>` (例: `opshub slack sync` / `opshub github sync`) に再編した BREAKING CHANGE ([ADR-0031](docs/adr/0031-cli-command-surface-organization.md))。

- **Phase 17-A** (PR [#412](https://github.com/ozzy-labs/opshub/pull/412)) — ADR-0031 新規 + 設計 pin。
- **Phase 17-B** (PR [#414](https://github.com/ozzy-labs/opshub/pull/414)) — `opshub connector` group 全廃 + per-noun group 実装 + tests + `docs/upgrading.md` §Phase 17 で旧 → 新コマンド対応表 (PR [#415](https://github.com/ozzy-labs/opshub/pull/415))。

### Phase 18: Slack mention / DM demand digest (operator 視点の「未処理 ping」signal)

新規 ADR + 新 projection を伴うため新 Phase で起票。epic [#426](https://github.com/ozzy-labs/opshub/issues/426)、根拠 [ADR-0033](docs/adr/0033-slack-mention-demand-digest.md)。

- **Phase 18-A** (PR [#431](https://github.com/ozzy-labs/opshub/pull/431)) — ADR-0033 新規 + 関連 ADR cross-reference (docs only)。`opshub slack sync` が既に append している `SourceObserved` event を消費する projection 経路 + 専用 MCP read tool で出荷する方針を pin。新 fetch / 新 scope (`search:read`) は採用しない。
- **Phase 18-B** (PR [#433](https://github.com/ozzy-labs/opshub/pull/433)) — `slack_demand_digest` projection 実装 + Alembic migration `0029_create_slack_demand_digest` + `opshub projections rebuild` 経路へ登録 + debug CLI `opshub slack mentions list` (`--types` / `--demand-kind` / `--format`)。`<@self>` mention literal + DM channel id prefix (`D...`) で demand 信号を検出し `(channel_id, demand_kind)` で upsert。self user_id は constructor / `OPSHUB_SLACK_SELF_USER_ID` env var / `SlackAuth.test_token()` cascade で解決し fail-soft。
- **Phase 18-C** ([#430](https://github.com/ozzy-labs/opshub/issues/430)) — 新 MCP tool `slack.demand.list` (`ReadCategory.SLACK_DEMAND_LIST`、`readOnlyHint=true, openWorldHint=false`、`types` / `demand_kinds` / `since_ts` / `limit` / `order` 引数) + skill 拡張 (`next-actions` / `personal-brief` / `inbox-triage` の SSOT に `slack.demand.list` 経路追記) + `opshub skills install --scope project` で dogfood mirror 再生成 + docs 一括更新 (mcp-setup / assistant-agent / upgrading §Phase 18-C)。MCP tool surface は 17 → 18 (read 12 → 13)、write 5 件は不変。

### Phase 19: Slack discovery engagement axis (`opshub slack conversations` の `--since` default 改訂)

新 ADR + CLI default semantics 切替を伴うため新 Phase で起票。epic [#438](https://github.com/ozzy-labs/opshub/issues/438)、根拠 [ADR-0034](docs/adr/0034-slack-engagement-axis.md)。

- **Phase 19-A** ([#440](https://github.com/ozzy-labs/opshub/issues/440)) — ADR-0034 新規 (docs only)。`opshub slack conversations --since` の default semantics を「any-author activity」 (Phase 17 #374) から「engagement axis = 自分が発言した channel」に切替えるための設計 pin、search.messages 経由 + `search:read` scope 要件、`--activity=mine|any` の disjoint axes 不変条件、`--all + --activity=mine` 非両立を確定。
- **Phase 19-B** ([#441](https://github.com/ozzy-labs/opshub/issues/441)) — Slack adapter `_fetch_self_post_index` (search.messages, `query=f"from:<@{self_user_id}>"`, cursor + legacy paging fallback, retry 共有, `_to_connector_failed_search` error mapper) + `SlackConversation.last_self_post_ts` 追加 + `list_conversations(activity=...)` 引数追加 + Bot Token 拒否 (ConfigError) + indexing-lag notice + `engagement_index_orphan` debug counter。CLI `--activity` flag (`mine` default、`any` accept、unknown は exit 2) + `--all + --activity=mine + --since` 非両立 (exit 1) + table `LAST_POST` / TOML `last post YYYY-MM-DD` / JSON disjoint axis field emit + spinner description 切替。docs 一括更新 (README / CLAUDE / AGENTS / mcp-setup §scope 表 `search:read` 追加 / troubleshooting `--activity=mine` 失敗時セクション / upgrading §Phase 19 default semantics 切替 + JSON consumer 注意 / architecture engagement 軸段落)。
- **Phase 19-C** ([#447](https://github.com/ozzy-labs/opshub/pull/447)) — ADR-0034 §(i) `_INDEXING_LAG_NOTICE` 文字列 pin + negative-path tests (Phase 19-B audit followup、docs+tests only)。
- **Phase 19-D** ([ADR-0035](docs/adr/0035-slack-sort-axis-consolidation.md), epic [#448](https://github.com/ozzy-labs/opshub/issues/448)) — `--activity={mine|any}` flag 完全削除 → `--sort=name|last_self_post|last_activity` に統合 (ADR-0035 §(c))、default `--format` を `table` → `toml` 切替 (§(a))、default `--sort=name` 固定で `--since` の有無による sort flip 廃止 (§(b))、`--sort=name + --since` で engagement 軸を implicit default に (§(d))、`--sort=last_self_post|last_activity` + `--since` なしで暗黙 `--since 90d` cutoff + stderr notice (§(e))。**19-D-1** ([#451](https://github.com/ozzy-labs/opshub/pull/451)) で ADR-0035 新規 + ADR-0034 supersede 注記、**19-D-2** ([#450](https://github.com/ozzy-labs/opshub/issues/450)) で adapter `SortKey` 型 + `list_conversations(sort=...)` + `_emit_implicit_cutoff_notice` + Bot Token / search-scope エラー文言書き換え + CLI `--sort` option + `--all` + engagement-axis reject 拡張 + docs 一括更新 (README / CLAUDE / AGENTS / mcp-setup / troubleshooting / upgrading / architecture)。BREAKING CHANGE: `--activity` 削除 / default `--format` 変更 / `--since` 単独で `search:read` 要件発生。pre-userbase で compat shim なし。

### Phase 20: `opshub slack sync` date floor (`sync_since` + per-channel `since`)

- **Phase 20** ([ADR-0036](docs/adr/0036-slack-sync-date-floor.md), epic [#457](https://github.com/ozzy-labs/opshub/issues/457)) — `opshub slack sync` に opt-in の date floor を追加し、`conversations.history` の `oldest` を bound して初回 / 新規 channel のバックフィルを抑制する。`[connectors.slack]` に connector-wide `sync_since` (相対 `90d`/`4w` or ISO、default `None` = 全件、sync 実行時点で評価) + per-channel `since` (継承 / `"all"` 全件 / 独自 floor) を追加。`channels` は文字列配列と `[[connectors.slack.channels]]` table 形式の両方を受理する **additive** 変更 (`conversations --format=toml` 出力・既存 config / env をそのまま維持)。floor 解決順 channel `since` → `sync_since` → なし。日付パースは `opshub.core.time.parse_since` (typer 非依存に core へ移設、CLI `--since` は薄い wrapper、#459) + `since_to_ts` に集約。floor は `oldest = _max_ts(cursor, floor)` で cursor を authoritative に保つ (既存 sync 済み channel は inert、再取得・削除なし、floor 下げの遡及 backfill なし)。サブ issue: [#458](https://github.com/ozzy-labs/opshub/issues/458) (ADR) / [#459](https://github.com/ozzy-labs/opshub/issues/459) (parse_since 移設) / [#460](https://github.com/ozzy-labs/opshub/issues/460) (実装)。本環境のブランチ制約により 3 sub-issue を 1 PR (`claude/opshub-slack-date-filter-v5pg8`) に統合実装。非破壊のため `feat(slack):` (minor bump)。

## Tech Stack

- Runtime: Python 3.13+ (ADR-0001)
- Package manager: uv
- Version management: mise (`.mise.toml`)
- CLI: Typer
- DB: SQLite + SQLAlchemy 2.x Core + Alembic
- Validation: Pydantic v2
- Vector (Phase 4): sqlite-vec (`[vector]` extras, ADR-0012)

## 主要コマンド

```bash
uv sync                       # 依存関係インストール (lockfile に従う)
uv sync --locked              # CI 用、lockfile からの逸脱を許さない
uv sync --extra dev           # 開発用ツール (ruff/mypy/pyright/pytest) を含める
uv run opshub --help          # CLI 実行
uv run pytest                 # テスト実行
uv run ruff check             # lint
uv run ruff format            # format
uv run pyright                # 高速型チェック (local)
uv run mypy src               # 厳密型チェック (CI)
uv run alembic upgrade head   # DB migration 適用
```

長時間 CLI (`opshub <connector> sync` / `opshub embeddings rebuild` / `opshub embeddings drain` / `opshub projections rebuild`) は stderr が TTY のとき進捗を自動表示し、ルートの `--progress` / `--no-progress` フラグまたは `OPSHUB_PROGRESS` 環境変数 (truthy = `1`/`true`/`yes`/`on`、falsy = `0`/`false`/`no`/`off`、case-insensitive) で上書きできる ([ADR-0026](docs/adr/0026-cli-progress-reporting.md))。Phase 17 ([ADR-0031](docs/adr/0031-cli-command-surface-organization.md)) で旧 `opshub connector <verb>` group は per-noun group (`opshub slack sync` / `opshub github sync` 等) に再編済 (`docs/upgrading.md` §Phase 17 で旧 → 新の対応表)。

トラブルシュート用にはルートに `-v` / `-q` / `--debug` / `--log-format` / `--log-file` フラグと対応する `OPSHUB_LOG_LEVEL` / `OPSHUB_LOG_FORMAT` / `OPSHUB_DEBUG` / `OPSHUB_LOG_FILE` 環境変数があり、トークン / 鍵 / 既知形状の secret は全 verbosity で redaction される ([ADR-0027](docs/adr/0027-observability-and-troubleshooting-logging.md))。手順は [`docs/troubleshooting.md`](docs/troubleshooting.md)。

## 検証（必須）

コード変更後、報告前に以下を通すこと:

1. `uv run ruff check` — lint 通過
2. `uv run ruff format --check` — フォーマット崩れなし
3. `uv run pyright` — 型チェック通過 (local 用、高速)
4. `uv run pytest` — テスト通過

CI では追加で `uv sync --locked` と `uv run mypy src` を実行する。

## コーディング規約

- Python: インデント 4 スペース (PEP 8)
- YAML / JSON / Markdown / TOML: インデント 2 スペース
- 改行コード: LF
- ファイル末尾: 改行あり
- 行長: 100 列 (Python `[tool.ruff] line-length = 100`)

## 規約

言語・コミット・ブランチ・PR のルールは README.md を参照すること。

<!-- begin: @ozzylabs/skills -->

## Available Skills

- `commit` — 変更をステージし、Conventional Commits でコミットする。プッシュや PR 作成は行わない。
- `commit-conventions` — Conventional Commits のメッセージ生成ルール（Type/Scope 判定表、フォーマット）。他スキルから参照される。
- `drive` — Issue または指示から実装・PR 作成・セルフレビュー・修正を自動で回し、merge-ready な PR を出す。単一/複数の Issue/PR と明示依存記法に対応。オプションでマージまで実行可能。
- `health` — リポジトリ改修中に意図せず残る状態（working tree, stash, branch, worktree, PR, issue, actions など）と skill catalog 整合性を一発で確認し、16 領域のステータス表で俯瞰しつつ各項目に固定語彙の推奨アクションを inline で付与して報告する。`--deep` 指定時は `要確認` 項目を read-only コマンドで追加調査し、機械判定可能な範囲でラベルを格上げする。検査と提示のみで、削除・close 等の実行は行わない。
- `implement` — Issue または指示をもとに、ブランチ作成・実装計画・コード変更を行う。Issue 番号またはテキスト指示を受け取る。
- `lint` — 全リンターを自動修正付きで実行し、結果を報告する。コード品質チェック、フォーマット、型チェック、セキュリティスキャンを含む。
- `lint-rules` — 拡張子別リンター・フォーマッターのコマンド対応表と型チェックルール。他スキルから参照される。
- `phase-issue` — Phase-N tracking issue を生成する。cross-session handoff context、決定事項表、PR ごとのタスク、DoD、Phase N+1 outlook を含む構造化された issue body を組み立てて gh issue create で起票する。引数で全項目を渡す非対話モードと、不足分を補う対話モード（Claude Code companion）に対応する。
- `pr` — コミット済みの変更をリモートにプッシュし、PR を作成・更新する。
- `review` — コード変更や PR を 11 観点（perspectives）でレビューし、JSON 構造化出力 + 人間可読レポートで報告する。quick / deep モードを切替可能。PR 番号またはワーキングツリー差分を入力に取る。
- `ship` — lint・コミット・PR 作成を一括実行する。変更に対して lint → コミット → PR 作成を順に実行する統合パイプライン。
- `test` — ビルド・テスト・型チェックを実行し、結果を報告する。
- `topics` — GitHub topics 候補を制約検証・人気度測定・broad+narrow / 単数複数比較・ozzy-labs 慣行ハードコードで選定し、`gh repo edit --add-topic` で適用する。スコープは ozzy-labs 内利用のみ。

<!-- end: @ozzylabs/skills -->

## Adapter Files

| Agent          | Configuration                         |
| -------------- | ------------------------------------- |
| Claude Code    | `CLAUDE.md`, `.claude/`               |
| Gemini CLI     | `.gemini/settings.json` → `AGENTS.md` |
| Codex CLI      | `AGENTS.md` + `.agents/skills/`       |
| GitHub Copilot | `AGENTS.md` + `.agents/skills/`       |

`.agents/skills/` and `.claude/skills/` are distributed from [`ozzy-labs/skills`](https://github.com/ozzy-labs/skills) via the `@ozzylabs/skills` Renovate preset, not from `commons` (see [ADR-0016](https://github.com/ozzy-labs/handbook/blob/main/adr/0016-create-skills-repo.md)).

アシスタント 14 Skill（Phase 12 で 5 → 14 拡張: read 自律 OK 10 = personal-brief / next-actions / pr-review / find-document / meeting-prep / research / external-brief / decision-rationale / handoff-draft / announcement-draft、HITL write 4 = reply-draft / inbox-triage / source-extract / meeting-followup）の catalog は [`docs/assistant-agent.md`](docs/assistant-agent.md) を参照（10 § 構成で責務マップ / pair structure / HITL boundary / MCP tool 依存マップを集約）。Codex / Copilot CLI も MCP 経由で同じ surface を叩ける。配信経路は Phase 16-A ([ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)) で **opshub package 同梱 + `opshub skills install`** に確定し、Phase 16-B ([#383](https://github.com/ozzy-labs/opshub/issues/383)) で CLI (`opshub skills install` / `opshub skills list`) が着地した。`@ozzylabs/skills` Renovate preset 経路は ecosystem 共通 skill (drive / lint / commit 等) を引き続き担当し、アシスタント 14 skill 経路から carve out される (名前空間 disjoint、ADR-0029 §決定 (h)、test `test_skills_install_only_writes_14_assistant_skills` で pin)。

opshub repo 自身も Phase 16-D ([ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md) §dogfood、[#385](https://github.com/ozzy-labs/opshub/issues/385)) で in-repo dogfood している。`.claude/skills/<assistant>/` (Claude Code 用) と `.agents/skills/<assistant>/` (Codex CLI / Copilot CLI 用) に 14 件分の SKILL.md が project scope で commit されているため、Codex CLI / Copilot CLI も worktree root で起動するだけでアシスタント 14 Skill を発火できる (ecosystem 共通 13 skill と名前空間 disjoint、合計 27 dir)。`docs/skills/<name>/SKILL.md` を編集したら `uv run opshub skills install --scope project` で `.claude/skills/<assistant>/` + `.agents/skills/<assistant>/` を再生成し、結果を commit すること。drift は `skills-sync-check` pre-commit lefthook hook (`lefthook.yaml`) が SSOT (`docs/skills/<name>/`) と mirror (`.claude/skills/<name>/` / `.agents/skills/<name>/`) を `diff -rq` で直接比較して検知する (`opshub skills` CLI を経由しない理由は `lefthook.yaml` のコメント参照: 直接比較で uv invocation round-trip を回避し hook を fast に保つ)。
