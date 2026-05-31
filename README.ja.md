# OpsHub

[![PyPI](https://img.shields.io/pypi/v/ozzylabs-opshub.svg)](https://pypi.org/project/ozzylabs-opshub/)
[![CI](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | 日本語

**ローカルファーストな秘書エージェント・プラットフォーム — 人間と AI エージェントが共有する監査可能な operational memory。**

OpsHub は **ローカルファーストな秘書エージェント・プラットフォーム** です。append-only な event log を基盤にした operational memory を持ち、外部のエージェント host (Claude Code / Codex CLI / Gemini CLI / GitHub Copilot CLI) があなたの代わりにそれを叩きます。「次に何をやる?」「あの Slack スレッドに返信案を書いて」と自然文で頼むと、agent が MCP 経由で OpsHub を呼び、recall / 要約 / 提案を行います。状態をクラウドに送らずに済みます。

三層モデル ([ADR-0004](docs/adr/0004-agent-runtime-boundary.md)):

1. **あなた (人間)** — 自然文で頼む。
2. **秘書エージェント** — エディタ / ターミナル (Claude Code 等) で動く。OpsHub は **MCP server (`opshub mcp serve`) + Agent Skills (SKILL.md)** だけを提供し、LLM 推論ループ自体は持ちません。頭脳はホスト側。
3. **OpsHub コア (CLI)** — append-only な event log + projection + 本文ストア + connector。同じ面を CLI から直接叩くことも、エージェント経由で叩くこともできます。

## インストール

OpsHub は PyPI にて **`ozzylabs-opshub`** という配布名で公開されています
（PyPI には namespace の概念がないため、PEP 423 の `<owner>-<package>` 規
約に従っています）。CLI コマンド名は `opshub` のままです。

```bash
uv tool install ozzylabs-opshub
# または
pipx install ozzylabs-opshub
```

オプション extras（必要なときだけ pull されます）:

```bash
uv tool install "ozzylabs-opshub[llm-anthropic,connectors-github]"
```

extras の一覧は下記 [オプション依存関係](#オプション依存関係) を参照。

### 別案: GitHub から直接インストール

タグ付き git ref からも install できます（PyPI 不要）:

```bash
uv tool install git+https://github.com/ozzy-labs/opshub.git@v0.1.0
```

pre-release タグ、`main` にだけある未リリース修正、または PyPI に到達でき
ない air-gapped 環境などで有用です。

## クイックスタート

```bash
opshub init                                           # 初回のみ: DB と workspace のセットアップ
opshub task create "OpsHub のブログ記事を書く"        # タスク作成
opshub task list                                       # オープン中のタスク一覧

# LLM backend を設定したあと（次セクション）:
opshub brief "current priorities"                      # LLM による briefing 要約
opshub propose generate "what's next?"                 # LLM による次アクション提案
opshub propose apply <proposal-id> 0                  # オペレーター承認 → entity 生成
```

状態はすべて XDG ディレクトリ配下に保存されます。`OPSHUB_*` 環境変数で上書
き可能です（例: `OPSHUB_STORAGE__DB_PATH=/custom/path.sqlite`）。

## 秘書に頼む

エージェント host を MCP 経由で繋ぐと（[エージェント host を接続する (MCP)](#エージェント-host-を接続する-mcp) を参照）、自然文で秘書に頼めるようになります。エージェントが裏で適切な OpsHub コマンドを呼びます。

Phase 12（2026-05-31）で秘書 Skill レパートリーを 5 → **14** に拡張しました（read 自律 OK 10 / HITL write 4）。下表は代表的な発火例で、責務マップ全体は [`docs/secretary-agent.md`](docs/secretary-agent.md) を参照。

| こう頼むと | 発火する Skill | 何をするか |
|---|---|---|
| 「今日のまとめ」「次に何やる?」「今週どうなってる」 | `personal-brief` / `next-actions` | 指定期間（今日 / 今週 / 今月 / 先週 / 先月）の主要シグナル + active task + 未処理 inbox |
| 「あの Slack スレッドに返信案考えて」 | `reply-draft` | LLM が過去の自分の送信文体を踏まえて下書きを生成（HITL apply、idempotent、OpsHub は外送信しない） |
| 「PR #123 をレビューして」 | `pr-review` | 関連 decision / task / 過去議論を引いてレビュー観点を提示 |
| 「Box にあった X の資料」「Word / Excel / PPT 探して」「あの Google Doc」「Sheets 探して」「あの Gmail」「Gmail に来てた件」「Google Calendar の予定」 | `find-document` | Slack / Box / GitHub / MS365 / Teams / Box Drive / OneDrive Drive / Google Workspace / Gmail / Google Calendar を本文ベースで横断検索（Phase 11 で Office 文書本文も対象、Phase 12 H1 で `search` FTS5 を MCP 経由で直接利用、Phase 13 で Google Docs / Slides / Sheets を Drive API export + markitdown 経由で取り込み、Phase 14 で Gmail (`gmail_message`) と Google Calendar (`google_calendar`) を Gmail API + Calendar API 経由で取り込み、Outlook / ms365_calendar と symmetric） |
| 「Teams スレッド要約して」 | `personal-brief` / `find-document` | Phase 11 で取り込んだ Teams chat 本文に対する横断 recall |
| 「明日の会議準備」「次のミーティング前に context」 | `meeting-prep` (Phase 12) | 対象 calendar event の目的 / 過去関連やりとり / 関連 decisions / 参考 sources を集約 |
| 「<X> について調べて」「<トピック> 網羅的に」 | `research` (Phase 12) | トピック横断調査（semantic recall + FTS5 + graph 拡張 + LLM 統合要約） |
| 「上司向け週次報告」「クライアント向け進捗まとめ」 | `external-brief` (Phase 12) | 外向き report（完了 task + 確定 decision 中心、tone 制御）— pair = personal-brief |
| 「あの決定はなぜ」「X を選んだ理由」 | `decision-rationale` (Phase 12) | 決定 + 直接の根拠 source + 先行 decision を `graph.trace` で provenance 遡って提示 |
| 「受信箱整理して」「inbox 仕分けて」 | `inbox-triage` (Phase 12、HITL) | 未処理 inbox を集めて action 候補を生成、user 個別承認分のみ保存 |
| 「この資料から task 抽出」「<source_id> から候補を」 | `source-extract` (Phase 12、HITL) | 1 source から task / decision / reply_draft 候補を抽出、HITL apply |
| 「会議後の action items」「議事録から task 抽出」 | `meeting-followup` (Phase 12、HITL) | 直近の calendar event から action items を抽出 — pair = meeting-prep |
| 「引き継ぎ書作って」「handoff 書く」 | `handoff-draft` (Phase 12) | task.list (in_progress) + decision.list + recall + graph から引き継ぎ書 text を構成（persist なし、text-only） |
| 「リリース告知文書いて」「announcement 作って」 | `announcement-draft` (Phase 12) | recall + decision + brief で告知文 text を構成（persist なし、text-only） |

14 件の秘書 Skill は [`docs/skills/<name>/SKILL.md`](docs/skills/) を SSOT として保持しています（Phase 12 H1 で opshub を SSOT に確定、ADR-0004 §決定 (c)）。既存 5 件のうち 2 件を rename（`daily-brief` → `personal-brief` / `file-lookup` → `find-document`）し、新規 9 件を Phase 12 H2-H5 で追加しました。`@ozzylabs/skills` Renovate preset 経由の配布は Phase 15+ に defer（ADR-0004 §決定 (c) backout、Phase 14 完了時点でも依然 defer）。Phase 14 まではホスト側に手動 install します（[秘書 Skill を install する](#秘書-skill-を-install-する) 参照）。Skills カタログ（責務マップ / MCP tool 依存マップ / pair structure / HITL boundary）と「できること / できないこと」（外部書き戻し / 常駐 / auto-apply はしない）の詳細は [`docs/secretary-agent.md`](docs/secretary-agent.md) を参照。

## エージェント host を接続する (MCP)

```bash
uv tool install "ozzylabs-opshub[mcp]"
opshub init
opshub db migrate
opshub mcp tools           # read / write tool 一覧を確認 (policy-as-data で監査可能)
```

エージェント host (Claude Code 等) から `opshub mcp serve` を stdio MCP サーバとして spawn する。Claude Code の例 (`~/.claude/mcp_servers.json`):

```json
{
  "mcpServers": {
    "opshub": { "command": "opshub", "args": ["mcp", "serve"] }
  }
}
```

詳細（他ホスト / 暗号化 / トラブルシュート）: [`docs/mcp-setup.md`](docs/mcp-setup.md)。

## 秘書 Skill を install する

Phase 12 で導入した 14 件の秘書 Skill は Phase 14 完了時点でも同じく [`docs/skills/<name>/SKILL.md`](docs/skills/) を opshub SSOT として保持しています（[ADR-0004 §決定 (c)](docs/adr/0004-agent-runtime-boundary.md)）。`@ozzylabs/skills` Renovate preset 経由の配布は引き続き Phase 15+ に defer されているため、当面はホスト側に手動 copy します:

```bash
# Claude Code（ユーザー単位）
cp -r path/to/opshub/docs/skills/* ~/.claude/skills/

# Codex CLI / GitHub Copilot CLI（ユーザー単位）
cp -r path/to/opshub/docs/skills/* ~/.agents/skills/

# プロジェクト単位（任意のホスト）
cp -r path/to/opshub/docs/skills/* ./.claude/skills/   # または ./.agents/skills/
```

Skill description に日本語トリガが含まれるため、「今日のまとめ」「会議準備」のような自然文でホストが該当 Skill を発火します。最新の install 手順と Phase 15+ の配布完成計画は [`docs/secretary-agent.md`](docs/secretary-agent.md) §8 (セットアップ) を参照。

## LLM backend の設定（任意）

OpsHub は LLM なしでも動作します — `task` / `decision` / `inbox` /
`connector sync` はすべてスタンドアロンで使えます。`brief` / `propose` を
有効化するには、いずれかを設定してください:

```bash
opshub connector auth set llm:anthropic       # Anthropic Claude（推奨）
opshub connector auth set llm:openai          # OpenAI
# または完全ローカルで:
ollama serve && ollama pull llama3.2:3b
# その後 ~/.config/opshub/config.toml で [llm] backend = "ollama" を設定
```

設計思想は [`docs/principles.md`](docs/principles.md) §1 (Local-first) を
参照。

## OpsHub に今あるもの

Phase 1–8 を出荷済み（2026-05-17, v0.1.0）。Phase 9 出荷 2026-05-23。Phase 10・Phase 11・Phase 12・Phase 13・Phase 14 出荷 2026-05-31:

| Phase | レイヤ | ハイライト |
|---|---|---|
| 1 | Foundation | イベントストア、タスク、CLI、Markdown ワークスペース |
| 2 | Coordination | inbox / decisions / sessions / locks / handoffs |
| 3 | Connectors framework | GitHub connector + ワークスペースファイル ingest |
| 4 | Semantic recall | Pluggable Embedder (local / OpenAI / Voyage) + sqlite-vec + `recall` + 重複検出 |
| 5 | Briefing | Pluggable LLM (Anthropic / OpenAI) + `brief` + prompt injection 対策 |
| 6 | Action loop | structured output + Ollama backend + `propose`（human-in-the-loop） |
| 7 | Connectors wave 2 | Slack + Microsoft 365 + Box |
| 8 | Knowledge graph | `links` projection + 自動抽出 + `graph` + `--expand-graph` |
| 9 | Local-FS connectors | `box_drive` (Box Drive デスクトップ → ローカル FS scan、ADR-0019) |
| 10 | Secretary agent platform | 本文ローカル保持 (ADR-0020) + 保存時暗号化 (ADR-0021) + MCP server (ADR-0022) + `opshub search` (FTS5) + `opshub mcp serve` + 秘書 5 Skills (Phase 12 H1 で `personal-brief` / `next-actions` / `reply-draft` / `pr-review` / `find-document` に rename 済) + reply-draft (ADR-0016 §決定 (i)) + ADR-0004 改訂 (形A: runtime をコアに持たない) + ADR-0010 改訂 (write-back 禁止) + ADR-0017 改訂 (reply_draft link types) |
| 11 | MS Office 深掘り | Office 文書本文抽出 (ADR-0025、markitdown 経路で `.docx`/`.xlsx`/`.pptx`、50 MB / 500K chars cap、fail-safe) + ADR-0019 改訂 (`content_extraction` opt-in 例外節 + `onedrive_drive` パターン汎化) + ADR-0010 改訂 (Teams connector + 本文抽出契約 + delta-link cursor + 失効時 full-pass fallback + Teams User Token principal) + 新 `teams` connector (Microsoft Graph chat delta + `Chat.Read`) + 新 `onedrive_drive` connector (FS scan、WSL2 `/mnt/onedrive` / macOS `~/OneDrive`) + `box_drive` の Office 抽出 hook + Outlook 本文 deep retention |
| 12 | Secretary Skills 拡張 | 秘書 Skill レパートリーを 5 → **14** に拡張 (read 自律 OK 10 / HITL write 4)。新規 9 = `meeting-prep` / `research` / `inbox-triage` / `external-brief` / `decision-rationale` / `handoff-draft` / `announcement-draft` / `meeting-followup` / `source-extract`、rename 2 = `daily-brief` → `personal-brief` / `file-lookup` → `find-document`。4 新 MCP tools (`search` (FTS5) + `propose.apply` (HITL idempotent) + 既存 4 read tools の物理列ベース時間フィルタ = `task.list` / `inbox.list` / `decision.list` / `source.list`)。既存 5 SKILL.md を MCP 直接呼びに統一 (CLI fallback 廃止)。ADR-0004 改訂 (Skills SSOT を opshub `docs/skills/` に移管、配布は Phase 15+ defer) + ADR-0022 改訂 (4 新 MCP tools 契約化) + ADR-0016 改訂 (draft 系統一方針: persist 境界 = 返信元 source の有無 / `mode` 引数射程 / triage 射程 / Candidate union freeze)。`docs/secretary-agent.md` を 14 skills 責務マップ SSOT に拡張（責務マップ / HITL boundary / MCP tool 依存マップ / pair structure） |
| 13 | Google Workspace コネクタ | 新規 `google_workspace` connector が Google Docs / Slides / Sheets を Drive API v3 (`changes.list` cursor + 失効時 full-pass fallback) + OAuth Refresh Token (offline access + 自前 refresh + rotation 書き戻し = MS365 / Box pattern、Teams pattern (verbatim user token) とは別系統である旨を ADR-0010 §Phase 13 改訂 (e)-(h) で明文化) で取り込み。Workspace native 本文 (`google_doc` / `google_slides` / `google_sheets`) は Drive API `files.export` で MS Office mediatype (`.docx` / `.pptx` / `.xlsx`) として取得し、Phase 11 で確立した markitdown 経路 (`core/document_extract.extract_workspace_export(bytes, source_type)`、ADR-0025 §決定 (d') + (j)) に流して本文抽出。ADR 改訂 3 本 (ADR-0010 + ADR-0014 + ADR-0025)、新 ADR ゼロ (Phase 11 → 12 → 13 で「1 新規 + 2 改訂 → 0 新規 + 3 改訂 → 0 新規 + 3 改訂」と縮退継続)。Drive `files.watch` push notification は禁止 (`changes.list` poll のみで形 A 整合 = 能動性なし)。新 extras `connectors-google-workspace` (httpx) + 新 setup doc `docs/google-workspace-setup.md` |
| 14 | Gmail + Google Calendar コネクタ | 新規 `google_mail` connector が Gmail API v1 (`users.messages.list` 初回 + `users.history.list` delta + 7 日 TTL 失効時 full-pass fallback) で、新規 `google_calendar` connector が Calendar API v3 (`events.list(syncToken=...)` delta + `410 GONE` 失効時 full-pass + `timeMin`/`timeMax` window fallback) で取り込み。両者は Phase 13 の Google OAuth principal を新規 shared `connectors/google_auth/` foundation 経由で共有 — 1 Google account = 1 principal が `drive.readonly + gmail.readonly + calendar.readonly` の 3-scope 固定 list を Drive + Gmail + Calendar の 3 connector で共有（1 回の再 consent で 3 connector 全てに反映）。Mapper は MS365 Outlook / Calendar mapper (Phase 7 + Phase 11) と**意図的に symmetric**: Gmail = message 単位 `gmail_message`、text/plain 優先 → text/html 生保持 / markitdown なし / 添付 retain なし / `[Labels: ...]` prepend / `[gmail body truncated]` tag / threadId field。Calendar = master event only `google_calendar`、override は別 record として emit / summary = `start_iso - end_iso (N attendees)` / RRULE field / attendee list を body 埋め込み。Push notification (`watch` / `events.watch`) は禁止 — poll のみで形 A 整合。Symmetry は `tests/unit/connectors/test_mapper_symmetry.py` で機械検証。ADR 改訂 2 本 (ADR-0010 + ADR-0014)、新 ADR ゼロ（単一改訂路線を継続）。新 extras なし — Gmail / Calendar は `connectors-google-workspace` (httpx) を流用 |

次は **Phase 15+ 候補** — multi-machine sync / 能動性段階 1-4 (cron 委譲 / 記憶キュレーション / 通知 / filewatch / Gmail push / Calendar push 再評価) / 画像 OCR (PPT 内画像 / Office 図表、Phase 13 → 14 から繰り越し) / Drive Comments / Suggestions 取り込み (Phase 13 follow-up) / Gmail / Calendar 添付の本文抽出 (markitdown 経路、ADR-0025 拡張) / 追加コネクタ (Notion / Jira / Linear / Confluence、Phase 13 から繰り越し) / 外部書き戻し (Teams / Drive / Gmail 返信送信 + Calendar event create + HITL、要 新 ADR) / Calendar instance 展開 projection (ms365 / google 両方同時) / `ozzy-labs/skills` 配布完成。phase ごとの詳細は [`docs/architecture.md`](docs/architecture.md) §9 (Phased Delivery) に。

## コマンド

```bash
# Foundation (Phase 1)
opshub task create "phase 2 計画をドラフト"
opshub task list --format md                          # format: table / md / json

# Coordination (Phase 2)
opshub inbox add "失敗してる nightly build を triage"
opshub inbox triage <id> --to-task "nightly build を修正"
opshub inbox list --format md
opshub decision record "Phase 4 に sqlite-vec を採用"
opshub decision list
opshub lock acquire task:<task-ulid>                  # ADR-0013 coordination lock
opshub lock release <lock-id>
opshub lock list
opshub session start --scope "phase-3 design"         # work session 区切り
opshub agent run begin claude                         # agent run 自動注入
opshub agent run end <run-id> --summary "ADR-0017 ドラフト"
opshub session end --summary "EOD wrap"
opshub handoff open --from agent:claude --to ozzy --topic "review"
opshub handoff close <handoff-id> --note "merged"

# Connectors (Phase 3 + Phase 7, ADR-0010 / ADR-0014)
opshub connector auth set github                      # GitHub PAT を OS keychain に保存
opshub connector sync github                          # 差分同期 (OPSHUB_CONNECTOR_GITHUB_REPO=owner/repo)
opshub connector auth set connector:slack             # Slack OAuth token を OS keychain に保存 (User Token 推奨、Bot Token も可 — ADR-0018)
opshub connector sync slack                           # 差分同期 ([connectors.slack] channels)
opshub connector auth set connector:ms365             # OAuth paste-code (Microsoft Graph Calendar / OneDrive / Outlook)
opshub connector sync ms365                           # endpoint ごとの差分同期
opshub connector auth set connector:box               # OAuth paste-code (Box Events API)
opshub connector sync box                             # 差分同期 (Box stream_position cursor)
opshub connector sync box_drive                       # Phase 9: ローカル Box Drive mount を scan (docs/box-drive-setup.md)
opshub connector sync onedrive_drive                  # Phase 11: ローカル OneDrive Desktop mount を scan (docs/onedrive-drive-setup.md)
opshub connector auth set connector:teams             # Phase 11: Microsoft Graph User Token を OS keychain に保存 (Chat.Read、docs/teams-setup.md)
opshub connector sync teams                           # Phase 11: Graph chat delta + 失効時 fallback
opshub connector auth set google_workspace            # Phase 14: Google OAuth paste-code、Drive + Gmail + Calendar の 3 connector で principal 共有 (drive.readonly + gmail.readonly + calendar.readonly、1 回の再 consent で 3 つ全て反映、docs/google-workspace-setup.md)
opshub connector sync google_workspace                # Phase 13: Drive API v3 changes.list cursor + 失効時 fallback (content_extraction = true で Workspace export → markitdown 経路)
opshub connector sync google_mail                     # Phase 14: Gmail API v1 users.history.list delta + 7 日 TTL fallback (message 単位、Outlook と symmetric な本文抽出)
opshub connector sync google_calendar                 # Phase 14: Calendar API v3 events.list(syncToken=...) + 410 GONE fallback (master event only + override 別 record、MS365 Calendar と symmetric)
opshub connector list                                 # 登録済 connector を表示

# Workspace + projections
opshub workspace ingest                               # workspace/inbox/*.md を ingest (Phase 3)
opshub workspace ingest --dry-run                     # scan のみ、書き込みなし
opshub workspace generate                             # projections から markdown workspace を再生成
opshub projections rebuild                            # イベントストアから projections を再構築 (idempotent)

# Semantic recall (Phase 4, ADR-0012)
opshub connector auth set embedder:openai             # OpenAI API key を OS keychain に保存
opshub embeddings rebuild                             # task/decision/inbox/source の本文を bulk embed (Phase 10 で本文ベースに、ADR-0020)
opshub embeddings status                              # backend + entity 種別ごとの embedded vs pending を表示
opshub embeddings drain                               # pending な embedding をリトライ (auto-embed hook の保険)
opshub embeddings find-duplicates -t 0.92             # オフライン near-duplicate スキャン
opshub recall "認証の最近の決定"                       # 全 entity に対するセマンティック検索

# 本文横断 FTS5 検索 (Phase 10, ADR-0012 改訂 §4 + ADR-0020)
opshub search "ticket-1234"                           # Slack / GitHub / Box / MS365 / Box Drive の本文を横断検索
opshub search "deploy AND failure" --raw              # FTS5 boolean / phrase / prefix 構文に opt-in
opshub search "channel ID" --connector slack          # connector を限定

# Briefing (Phase 5, ADR-0015)
opshub connector auth set llm:anthropic               # Anthropic API key を OS keychain に保存
opshub brief "phase 5 progress"                       # LLM-backed briefing (markdown を stdout へ)
opshub brief "phase 5 progress" --save                # <workspace>/briefings/ にも保存
opshub brief "phase 5 progress" --format json         # JSON 形式 (briefing_id / model / tokens / source_refs)
opshub brief "phase 8 review" --expand-graph          # 1-hop graph 展開で LLM コンテキストを広げる

# Action loop (Phase 6, ADR-0016, Phase 10 reply-draft)
opshub propose generate "next steps"                  # LLM が task/decision 候補を提案
opshub propose generate "next steps" --from-briefing <id>
opshub propose generate "next steps" --format json
opshub propose generate "next steps" --expand-graph   # proposal のために 1-hop graph 展開
opshub propose generate "" --reply-to <source-id>     # Phase 10: reply-draft モード (topic は無視される; ADR-0016 §決定 (i))
opshub propose list                                   # 最近の proposal (markdown table)
opshub propose list --state pending --limit 10        # フィルタ: pending / applied / rejected
opshub propose apply <proposal-id> <candidate-index>  # オペレーター承認 → entity 生成 (reply-draft はローカルに保存、外送信しない)
opshub propose reject <proposal-id> <candidate-index> --reason "out of scope"

# Knowledge graph (Phase 8, ADR-0017)
opshub link add task:<task-id> source:<src-id> --type references
opshub link list --from task:<task-id>                # --from / --to / --type でフィルタ
opshub link remove <link-id> --reason "wrong source"  # ハード削除 (LinkDeleted を発行)
opshub graph related task:<task-id> --direction both  # 1-hop 隣接 (md / json / dot)
opshub graph trace task:<task-id> --depth 3           # 後方 provenance walk (default 3, max 10)
opshub graph expand task:<task-id> --depth 2 --format dot

# MCP server surface (Phase 10, ADR-0022)
opshub mcp tools                                       # read / write tool 一覧を表示 (policy-as-data で監査可能)
opshub mcp tools -f json                               # diff / scripting 用 JSON
opshub mcp serve                                       # stdio MCP server — エージェント host が subprocess として spawn
```

## オプション依存関係

| Extras | 用途 | サイズ |
|---|---|---|
| `vector` | semantic recall 用 sqlite-vec | 小 |
| `local-embedding` | sentence-transformers (bge-m3, ~500MB) | 大 |
| `api-embedding-openai` / `api-embedding-voyage` | API embedder backend | 小 |
| `llm-anthropic` / `llm-openai` | API LLM backend | 小 |
| `llm-ollama` | Ollama daemon クライアント | 小 |
| `connectors-github` / `connectors-slack` / `connectors-ms365` / `connectors-box` | SaaS connector | 小 |
| `connectors-teams` | Microsoft Teams connector (Phase 11、msal + httpx) | 小 |
| `connectors-google-workspace` | Google Workspace connector (Phase 13、httpx)。`[office]` extras と併用すると `content_extraction = true` を有効化でき、`google_doc` / `google_slides` / `google_sheets` を Workspace export → markitdown 経路で本文取り込み。Phase 14 で Gmail (`google_mail`) と Google Calendar (`google_calendar`) connector も同じ extras を流用 (httpx 共有、新 extras なし) | 小 |
| `office` | Office 文書本文抽出 (Phase 11、ADR-0025)。`markitdown` を `[docx,xlsx,pptx]` sub-extras 付きで導入する (内訳は `mammoth` / `openpyxl` / `python-pptx`) ため、opshub が対応する 3 つの sub-format のみが取り込まれる | 小 |
| `secrets` | OS keyring backend | 小 |
| `encryption` | SQLCipher backed の保存時暗号化 (Phase 10、ADR-0021) | 小 |
| `mcp` | `opshub mcp serve` 用 MCP server SDK (Phase 10、ADR-0022) | 小 |
| `dev` | テスト + lint ツールチェイン | 中 |

## ドキュメント

- [`docs/principles.md`](docs/principles.md) — 設計原則 (local-first, event-sourced, 本文ローカル保持)
- [`docs/architecture.md`](docs/architecture.md) — 階層アーキテクチャの概観
- [`docs/secretary-agent.md`](docs/secretary-agent.md) — Phase 10 秘書エージェント層の使い方 (Skill カタログ・できること/できないこと)
- [`docs/mcp-setup.md`](docs/mcp-setup.md) — Phase 10 エージェント host 向け MCP セットアップ
- [`docs/adr/`](docs/adr/README.md) — Architecture Decision Records
- [`docs/box-drive-setup.md`](docs/box-drive-setup.md) — Phase 9 `box_drive` connector setup (WSL2 / macOS)
- [`docs/onedrive-drive-setup.md`](docs/onedrive-drive-setup.md) — Phase 11 `onedrive_drive` connector setup (WSL2 / macOS)
- [`docs/teams-setup.md`](docs/teams-setup.md) — Phase 11 `teams` connector setup (Azure app 登録 + User Token)
- [`docs/google-workspace-setup.md`](docs/google-workspace-setup.md) — Phase 13 `google_workspace` connector setup (GCP project + OAuth consent screen + paste-code Refresh Token)
- [`docs/upgrading.md`](docs/upgrading.md) — バージョン移行ノート (該当時のみ)
- [`docs/release-notes-v0.1.0.md`](docs/release-notes-v0.1.0.md) — v0.1.0 ナラティブリリースノート
- [`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md) — リリースの切り方 (maintainer 向け)
- [`CHANGELOG.md`](CHANGELOG.md) — リリース履歴
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — コントリビューションガイドライン
- [`SECURITY.md`](SECURITY.md) — 脆弱性開示 + Phase 10 本文保持脅威モデル

## ライセンス

MIT。[LICENSE](LICENSE) を参照。
