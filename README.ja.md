# OpsHub

[![PyPI](https://img.shields.io/pypi/v/ozzylabs-opshub.svg)](https://pypi.org/project/ozzylabs-opshub/)
[![CI](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/ozzy-labs/opshub/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | 日本語

**人間と AI エージェントのための、ローカルファーストな Operational Memory 兼 実行ハブ。**

OpsHub は作業状態 — タスク、決定、briefing、embedding、リンク — を、ホーム
ディレクトリ配下にある単一の SQLite イベントログに格納します。AI エージェ
ントは CLI を介して同じ surface を読み書きできるため、状態をクラウドに送る
ことなくセッション間でコンテキストを保持できます。

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

Phase 1–8 を出荷済み（2026-05-17, v0.1.0）:

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

次は **Phase 9 (Multi-machine sync)** — [`docs/principles.md`](docs/principles.md)
§Open Questions #5 を参照。phase ごとの詳細は
[`docs/architecture.md`](docs/architecture.md) §9 (Phased Delivery) に。

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
opshub connector list                                 # 登録済 connector を表示

# Workspace + projections
opshub workspace ingest                               # workspace/inbox/*.md を ingest (Phase 3)
opshub workspace ingest --dry-run                     # scan のみ、書き込みなし
opshub workspace generate                             # projections から markdown workspace を再生成
opshub projections rebuild                            # イベントストアから projections を再構築 (idempotent)

# Semantic recall (Phase 4, ADR-0012)
opshub connector auth set embedder:openai             # OpenAI API key を OS keychain に保存
opshub embeddings rebuild                             # task/decision/inbox/source の要約を bulk embed
opshub embeddings status                              # backend + entity 種別ごとの embedded vs pending を表示
opshub embeddings drain                               # pending な embedding をリトライ (auto-embed hook の保険)
opshub embeddings find-duplicates -t 0.92             # オフライン near-duplicate スキャン
opshub recall "認証の最近の決定"                       # 全 entity に対するセマンティック検索

# Briefing (Phase 5, ADR-0015)
opshub connector auth set llm:anthropic               # Anthropic API key を OS keychain に保存
opshub brief "phase 5 progress"                       # LLM-backed briefing (markdown を stdout へ)
opshub brief "phase 5 progress" --save                # <workspace>/briefings/ にも保存
opshub brief "phase 5 progress" --format json         # JSON 形式 (briefing_id / model / tokens / source_refs)
opshub brief "phase 8 review" --expand-graph          # 1-hop graph 展開で LLM コンテキストを広げる

# Action loop (Phase 6, ADR-0016)
opshub propose generate "next steps"                  # LLM が task/decision 候補を提案
opshub propose generate "next steps" --from-briefing <id>
opshub propose generate "next steps" --format json
opshub propose generate "next steps" --expand-graph   # proposal のために 1-hop graph 展開
opshub propose list                                   # 最近の proposal (markdown table)
opshub propose list --state pending --limit 10        # フィルタ: pending / applied / rejected
opshub propose apply <proposal-id> <candidate-index>  # オペレーター承認 → entity 生成
opshub propose reject <proposal-id> <candidate-index> --reason "out of scope"

# Knowledge graph (Phase 8, ADR-0017)
opshub link add task:<task-id> source:<src-id> --type references
opshub link list --from task:<task-id>                # --from / --to / --type でフィルタ
opshub link remove <link-id> --reason "wrong source"  # ハード削除 (LinkDeleted を発行)
opshub graph related task:<task-id> --direction both  # 1-hop 隣接 (md / json / dot)
opshub graph trace task:<task-id> --depth 3           # 後方 provenance walk (default 3, max 10)
opshub graph expand task:<task-id> --depth 2 --format dot
```

## オプション依存関係

| Extras | 用途 | サイズ |
|---|---|---|
| `vector` | semantic recall 用 sqlite-vec | 小 |
| `local-embedding` | sentence-transformers (bge-m3, ~500MB) | 大 |
| `api-embedding-openai` / `api-embedding-voyage` | API embedder backend | 小 |
| `llm-anthropic` / `llm-openai` | API LLM backend | 小 |
| `llm-ollama` | Ollama daemon クライアント | 小 |
| `connectors-github` / `connectors-slack` / `connectors-msgraph` / `connectors-box` | SaaS connector | 小 |
| `secrets` | OS keyring backend | 小 |
| `dev` | テスト + lint ツールチェイン | 中 |

## ドキュメント

- [`docs/principles.md`](docs/principles.md) — 設計原則 (local-first, event-sourced 等)
- [`docs/architecture.md`](docs/architecture.md) — 階層アーキテクチャの概観
- [`docs/adr/`](docs/adr/README.md) — Architecture Decision Records
- [`docs/upgrading.md`](docs/upgrading.md) — バージョン移行ノート (該当時のみ)
- [`docs/release-notes-v0.1.0.md`](docs/release-notes-v0.1.0.md) — v0.1.0 ナラティブリリースノート
- [`docs/RELEASE_RUNBOOK.md`](docs/RELEASE_RUNBOOK.md) — リリースの切り方 (maintainer 向け)
- [`CHANGELOG.md`](CHANGELOG.md) — リリース履歴
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — コントリビューションガイドライン
- [`SECURITY.md`](SECURITY.md) — 脆弱性開示

## ライセンス

MIT。[LICENSE](LICENSE) を参照。
