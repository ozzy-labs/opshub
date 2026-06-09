# CLAUDE.md

共通方針は AGENTS.md を参照（Phase 1-21 + epic #470 closeout 状態の記載含む）。以下は Claude Code 固有の設定。

Phase 21 (epic #504、[ADR-0037](docs/adr/0037-browser-read-layer-playwright.md)) で **Playwright ベースの browser read 層**を新設した: (1) `src/opshub/browser/core.py` browser core (Chromium / headless default / opshub 専用 user-data-dir / render 後の `page.inner_text("body")` を 500K char cap で抽出、`[browser]` config = `headless` / `channel` / `timeout` / `cdp_endpoint`、`browser` extras = `playwright>=1.50`、binary は `playwright install chromium` が operator 手順で不在時 `ConfigError`)、(2) `connectors/web/` web connector (`web_page` source_type、operator が `[connectors.web] pages` に明示登録した URL のみ取得 = **crawler 非該当**、delta API なしのため抽出後本文 SHA-256 fingerprint 変更検知、`opshub web sync` CLI、connector 数 10 → 11)、(3) MCP `browser.fetch` tool (ad-hoc Web ページ read、ネットワークに egress するため **write-category** = HITL per call、`asyncio.to_thread` bridge、MCP surface 18 → **19 tools = read 13 + write 6**)。**read 専用** — 操作系 (click / fill / submit) は ADR-0037 §決定 (f) で後続 Phase に defer。詳細は [`docs/upgrading.md`](docs/upgrading.md) §Phase 21 / [`docs/troubleshooting.md`](docs/troubleshooting.md) §3.13 (chromium 未 install / headless 切替 / timeout) を参照。

## 基本ルール

- ユーザーへの確認には `AskUserQuestion` を使用する

## Available Skills

スキル配信経路は 2 系統に分かれる:

- **アシスタント 14 Skill** (`personal-brief` / `next-actions` / ... / `meeting-followup`) は opshub Python package に **同梱** され、`opshub skills install` または `opshub init` 経由で `.claude/skills/` (Claude Code 用) と `.agents/skills/` (Codex CLI / Copilot CLI 用) に展開される（Phase 16-A〜D、[ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)）。SSOT は `docs/skills/<name>/SKILL.md` (ADR-0004 §決定 (c))。
- **ecosystem 共通スキル** (drive / lint / commit / ship / pr / review / health / implement / phase-issue / topics / commit-conventions / lint-rules / test) は [`ozzy-labs/skills`](https://github.com/ozzy-labs/skills) に置かれ、`@ozzylabs/skills` の Renovate preset 経由で配布される（handbook [ADR-0016](https://github.com/ozzy-labs/handbook/blob/main/adr/0016-create-skills-repo.md)）。両系統は名前空間 disjoint で、`opshub skills install` はアシスタント 14 skill のみを書き込み、ecosystem 共通 skill には触れない (test `test_skills_install_only_writes_14_assistant_skills` で pin)。

### 開発作業用スキル

- `/implement` — Issue または指示をもとに、ブランチ作成・実装
- `/lint` — 全リンターを自動修正付きで実行
- `/test` — ビルド・テスト・型チェックを実行
- `/commit` — 変更をステージし、Conventional Commits でコミット
- `/pr` — 変更を push し、PR を作成・更新
- `/review` — コード変更や PR をレビュー
- `/ship` — lint・コミット・PR 作成を一括実行
- `/drive` — implement + ship + review loop（Issue から merge-ready な PR まで自律駆動）

### アシスタントエージェント Skills (Phase 10 で 5 Skill 開始、Phase 12 で 14 Skill 体制に拡張、opshub MCP 経由)

opshub は形A (ADR-0004) に基づきアシスタント **14 Skill** を提供する。SKILL.md の SSOT は `docs/skills/<name>/SKILL.md` に置く (Phase 12 H1 で opshub を SSOT に確定、ADR-0004 §決定 (c))。配信経路は Phase 16-A ([ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md)) で **opshub package 同梱 + `opshub skills install`** に確定し、Phase 16-B ([#383](https://github.com/ozzy-labs/opshub/issues/383)) で CLI が着地した (`opshub skills install` / `opshub skills list`、`--host` / `--scope` / `--skip-existing` / `--dry-run` / `--print-paths` flag、詳細は [`docs/assistant-agent.md`](docs/assistant-agent.md) §8)。ecosystem 共通 skill (drive / lint / commit 等) は引き続き `@ozzylabs/skills` Renovate preset 経由で配布される (アシスタント 14 skill 経路と名前空間 disjoint、test `test_skills_install_only_writes_14_assistant_skills` で pin)。発火条件は自然文 (skill description) で表現されており、自分で叩く必要はない。

opshub repo 自身も Phase 16-D ([ADR-0029](docs/adr/0029-distribute-assistant-skills-via-opshub-package.md) §dogfood、[#385](https://github.com/ozzy-labs/opshub/issues/385)) で in-repo dogfood している。`.claude/skills/<assistant>/` (Claude Code 用) と `.agents/skills/<assistant>/` (Codex CLI / Copilot CLI 用) に 14 件分の SKILL.md が project scope で commit されており、opshub maintainer は worktree root で Claude Code / Codex CLI / Copilot CLI を起動するだけでアシスタント 14 Skill を発火できる (ecosystem 共通 skill 13 件 = `commit` / `commit-conventions` / `drive` / `health` / `implement` / `lint` / `lint-rules` / `phase-issue` / `pr` / `review` / `ship` / `test` / `topics` と名前空間 disjoint、合計 27 dir)。`docs/skills/<name>/SKILL.md` を編集したら `uv run opshub skills install --scope project` で `.claude/skills/<assistant>/` + `.agents/skills/<assistant>/` を再生成し、結果を commit すること。drift は `skills-sync-check` pre-commit lefthook hook (`lefthook.yaml`) が SSOT (`docs/skills/<name>/`) と mirror (`.claude/skills/<name>/` / `.agents/skills/<name>/`) を `diff -rq` で直接比較して検知する (`opshub skills` CLI を経由しない理由は `lefthook.yaml` のコメント参照: 直接比較で uv invocation round-trip を回避し hook を fast に保つ)。

**read 自律 OK (10 件)**:

- `personal-brief` — 「今日のまとめ」「今週どうなってる」「先月の振り返り」「最近どうなってる」「状況教えて」
- `next-actions` — 「次に何やる?」「やること教えて」「今週やること」「優先度高いのは?」
- `pr-review` — 「PR #N レビューして」「この差分どう?」
- `find-document` — 「Box にあったあの資料」「<キーワード>含むファイル」 (Phase 12 H1 で `search` FTS5 MCP tool 直接利用)
- `meeting-prep` (Phase 12 H2) — 「来週の会議準備」「明日のミーティング前確認」
- `research` (Phase 12 H2) — 「`<X>` について調べて」「`<トピック>` 網羅的に教えて」
- `external-brief` (Phase 12 H3) — 「上司向け週次報告」「クライアント向け進捗まとめ」 (pair = personal-brief)
- `decision-rationale` (Phase 12 H3) — 「あの決定はなぜ」「X を選んだ理由」
- `handoff-draft` (Phase 12 H5) — 「引き継ぎ書作って」「handoff 書く」 (text-only、persist なし)
- `announcement-draft` (Phase 12 H5) — 「リリース告知文書いて」「announcement 作って」 (text-only、persist なし)

**HITL write (4 件)** (外送信なし、apply は HITL):

- `reply-draft` — 「これに返信案考えて」「下書き作って」 (idempotent apply)
- `inbox-triage` (Phase 12 H4) — 「受信箱整理して」「inbox 仕分けて」 (pair = source-extract)
- `source-extract` (Phase 12 H4) — 「この資料から task 抽出」「<source_id> から候補を」
- `meeting-followup` (Phase 12 H4) — 「会議後の action items」「議事録から task 抽出」 (pair = meeting-prep)

詳細 (責務マップ / pair structure / MCP tool 依存マップ / HITL boundary) は [`docs/assistant-agent.md`](docs/assistant-agent.md) を参照。MCP セットアップは [`docs/mcp-setup.md`](docs/mcp-setup.md)。

## Skills の共通ルール

- スキル完了時のネクストアクション提案には `AskUserQuestion` を使用する
- ネクストアクションはユーザーの確認なく実行しない

## 長時間 CLI の進捗表示

長時間 CLI (`opshub <connector> sync` / `opshub slack conversations` / `opshub embeddings rebuild` / `opshub embeddings drain` / `opshub projections rebuild`) は TTY 時に進捗を自動表示し、`--progress` / `--no-progress` フラグまたは `OPSHUB_PROGRESS` 環境変数 (truthy = `1`/`true`/`yes`/`on`、falsy = `0`/`false`/`no`/`off`、case-insensitive) で上書きできる ([ADR-0026](docs/adr/0026-cli-progress-reporting.md))。`opshub slack conversations` は Phase 19-D ([ADR-0035](docs/adr/0035-slack-sort-axis-consolidation.md)) で `--activity={mine|any}` flag を廃止し `--sort=name|last_self_post|last_activity` に統合した。default は `--sort=name` (display_name 昇順) + `--format=toml` ([connectors.slack] channels に直接貼れる)。`--sort=last_self_post` は engagement 軸 (`search.messages` 経由、`search:read` User Token 必須)、`--sort=last_activity` は any-author 軸 (`conversations.history` per-row、`*:history` scope 必須) を起動する。`--sort=name` + `--since` を指定すると engagement 軸が implicit default で発火し、表示順は name のまま `last_self_post_ts` を populate する (ADR-0035 §(d))。spinner 説明文は engagement 軸経路で `"listing conversations + engagement"`、any 軸経路で `"listing conversations + activity"`、probe なしで `"listing conversations"`。table の追加列は engagement 軸で `LAST_POST` (`YYYY-MM-DD` UTC、`last_self_post_ts`)、any 軸で `LAST_ACTIVITY` (`last_activity_ts`); TOML コメントは engagement 軸で `"last post YYYY-MM-DD"`、any 軸で `"last YYYY-MM-DD"`。engagement 軸経路では 1 度だけ stderr に `notice: search.messages may lag by minutes; use --sort=last_activity for live activity.` が出る (indexing lag advisory、ADR-0034 §(i) 継承、`-q` / `OPSHUB_LOG_LEVEL` の影響を受けない、完全 suppress したい場合は `--sort=last_activity` を明示)。`--sort=last_self_post|last_activity` を `--since` なしで指定すると暗黙 `--since 90d` cutoff が当たり、stderr に `notice: --sort=<sort> defaulted to --since 90d to cap probe cost; pass --since explicitly to override.` が 1 度出る (ADR-0035 §(e))。engagement 軸経路 (`--sort=last_self_post` または `--sort=name` + `--since`) は Bot Token で `ConfigError` (`--sort=last_activity` への切替を誘導する)。`--all` + engagement 軸経路は非両立 (engagement 軸は self-member channel しか index しないため) で `ConfigError` exit 1 する; `--all + --sort=last_activity` は workspace-wide any-author 軸として受理される。JSON 出力は populated 軸の field のみ emit する (どちらの軸でも他方の field は drop される)。`opshub connector` group は Phase 17 で全廃止 (ADR-0031、`docs/upgrading.md` §Phase 17 / §Phase 19-D で旧 → 新コマンドの対応表を参照)。`opshub slack sync` は Phase 20 ([ADR-0036](docs/adr/0036-slack-sync-date-floor.md)) で **date floor** を追加した: `[connectors.slack] sync_since` (相対 `90d`/`4w` or ISO `2026-01-01`、default `None` = 全件、相対は sync 実行時点で評価) が connector-wide 既定 floor、per-channel `[[connectors.slack.channels]] since` が上書き (`since = "all"` で当該 channel だけ全件)。`channels` は従来の文字列配列 `["C0123"]` と新 table 形式 `[[connectors.slack.channels]] id=...` の両方を受理する (additive、`conversations --format=toml` 出力もそのまま有効)。floor は `oldest = _max_ts(cursor, floor)` で cursor を authoritative に保つため、既存 sync 済み channel に後から `sync_since` を効かせても再取得・削除は起きない。floor を下げたときの過去取り直しは Phase 22 ([ADR-0038](docs/adr/0038-slack-sync-gap-backfill.md)) の **自動 gap backfill** (compound cursor の per-channel low-water `backfill` 軸 + bounded fetch、feature 着地後に sync された channel で発火・相対 floor は ts 前進のため非発火) + 明示 `opshub slack cursor backfill --channel <id> --since <new> [--until <old>]` が担う。**`opshub projections rebuild` は Slack cursor をリセットしない** (replay で `ConnectorSyncCompleted` を流し直し同値復元するため、ADR-0038 §Context で旧記述を是正)。

`opshub slack sync` は Phase 20 ([ADR-0030](docs/adr/0030-slack-thread-reply-ingestion.md) revised + landed) で **thread reply (late reply 含む) も含む message 単位の全量取得** に拡張された (Gmail / Outlook と symmetric)。1 回の sync は Phase 1 (`conversations.history` で親 + `latest_reply` 持ち親について `conversations.replies` 即時 fetch、`messages[0]` は親自身なので skip) と Phase 2 (`threads` 軸 cursor を持つ既知 thread について `conversations.replies(oldest=threads_cursor, inclusive=False)` で late reply のみ追加 fetch) の 2 phase 構成。cursor schema は Phase 20-B で `{"channels": {...}, "threads": {...}}` の 2 軸 compound に拡張され、旧 flat-dict は silent migration せず `ConfigError` で reject する (pre-userbase posture)。Phase 23-A ([#531](https://github.com/ozzy-labs/opshub/issues/531)) でこの reject メッセージの誘導先を `opshub projections rebuild` (flat-dict event payload を replay して同形に戻す dead-end) から `opshub slack cursor reset --all` (cursor を parse せず空 compound で hard-drop = 唯一の working 回復経路) に是正した。late reply polling 対象を絞るため `[connectors.slack] thread_activity_window` (default `"30d"`、CLI `--thread-activity-window` / env `OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW` で上書き、`"all"` で prune 無効化) を設け、Phase 2 happy path 完了時に窓を超えた `threads` 軸 entry を prune する (mid-iteration crash は resume-safe のため prune しない)。窓経過後の cold thread に late reply が来ても本経路では追従せず (意図された limitation)。**`opshub projections rebuild` は cursor をリセットしない** (replay で同値復元するため) ので取り直しには使えず、実際に機能する cursor 操作は Phase 22-E ([ADR-0038](docs/adr/0038-slack-sync-gap-backfill.md) §(f)) で着地し、Phase 23-F ([#536](https://github.com/ozzy-labs/opshub/issues/536)) で **日常 = `opshub slack status`(読み取り) / 復旧 = `opshub slack cursor`(書き換え) の二層**に整理された。`opshub slack status` が 3 軸 cursor を人間語で表示 (前進取得済み=high-water / 過去取得下限=low-water(無記録なら「先頭まで」) / 追跡中スレッド数 + 「次回 sync で過去取り直し予定」=実効 floor < low-water。cursor は再開点であって被覆台帳ではないため high/low water を別事実として出し連続被覆は主張しない)、`opshub slack status --verbose` が生 3 軸 + raw ts の pretty-print (旧 `opshub slack cursor show` を昇格・置換)。`opshub slack cursor reset [--channel C1,C2 | --all]` が working cursor reset、`opshub slack cursor backfill --channel <id> --since <new> [--until <old>]` が明示 bounded backfill (pre-feature channel の救済主経路)。`cursor` group は help から隠さない (障害時の命綱、flat-dict エラーが `cursor reset --all` を案内する整合性)。

## トラブルシュート用オプション

全サブコマンドに共通の `-v` / `-q` / `--debug` / `--log-format` / `--log-file` フラグと `OPSHUB_LOG_LEVEL` / `OPSHUB_LOG_FORMAT` / `OPSHUB_DEBUG` / `OPSHUB_LOG_FILE` 環境変数で verbosity を制御できる。トークン / 鍵 / 既知形状の secret は全 verbosity で redaction される ([ADR-0027](docs/adr/0027-observability-and-troubleshooting-logging.md))。手順は [`docs/troubleshooting.md`](docs/troubleshooting.md)。

## `opshub search` の日本語クエリ

Phase 15 ([ADR-0028](docs/adr/0028-fts5-japanese-tokenizer.md)、epic #338) で `sources_fts` の tokenizer を FTS5 built-in `trigram` に張り替え + SearchService に短クエリ LIKE fallback を入れたため、日本語自然文 (`boxの権限` / `進捗記入` / `CDKの`) は default mode で 3 文字以上の substring を hit する。1-2 文字の短クエリ (`依頼` / `PR` / `Q4`) は `LOWER(body) LIKE LOWER(?)` 経路 (full scan、`raw_query=False` 時のみ) で hit する。`--raw` は FTS5 boolean / phrase / prefix を直接書きたい power-user 向けに維持 (例: `box* AND 権限*`)、短クエリ fallback は無効化される。MCP `search` tool ([ADR-0022](docs/adr/0022-mcp-server-surface.md) §決定 (f)) は `raw_query` hard-coded `false` のためアシスタント 14 Skill (`find-document` / `research` / etc.) も透過的に恩恵を受ける。発火しないときの調査は [`docs/troubleshooting.md`](docs/troubleshooting.md) §3.6 を参照。
