# Architecture Decision Records

> Status: Draft (in active design). Last reviewed: 2026-05-17.

このディレクトリには OpsHub の重要な設計判断を **ADR (Architecture Decision Record)** として記録する。

## 1. ADR とは

ADR は「なぜその設計を採用したか」を、決定時点の文脈・代替案・トレードオフとともに 1 ファイルに記録する慣習。Michael Nygard の [Documenting Architecture Decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions) に由来。

OpsHub では以下を ADR 化する。

- 言語 / スタックの選定
- アーキテクチャ全体に影響する設計判断 (event sourcing、agent boundary など)
- 後から覆すと高コストになる判断
- 直感に反するため、なぜそうしたかを残す必要のある判断

逆に、容易に覆せる実装詳細 (e.g., 個別ライブラリの選定) は ADR にしない。

## 2. フォーマット

[Michael Nygard 形式](https://github.com/joelparkerhenderson/architecture-decision-record/blob/main/locales/en/templates/decision-record-template-by-michael-nygard/index.md) を採用 (簡易版)。各 ADR は以下の見出しを持つ。

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Deprecated | Superseded by ADR-NNNN
- Date: YYYY-MM-DD
- Deciders: <人 / role>

## Context
<判断の背景・制約>

## Decision
<採用する判断>

## Consequences
### Positive
### Negative / Trade-offs

## Alternatives Considered
<比較した代替案と却下理由>
```

## 3. Status ライフサイクル

| Status | 意味 |
|---|---|
| `Proposed` | 議論中。実装方針として暫定採用しているが確定ではない |
| `Accepted` | 確定。原則として superseded されるまで遵守 |
| `Deprecated` | 採用を取り下げ。後継 ADR を必須としない |
| `Superseded by ADR-NNNN` | 新しい ADR で置き換え。新 ADR にリンク |

Phase 1 着手前に主要 ADR (0001-0007 + 0012) を一括 `Accepted` に昇格させた。Phase 2 着手前に ADR-0009 (Multi-Agent Neutrality) を、Phase 2 step 5 で ADR-0013 (Lock Granularity) を、Phase 3 step A6 で ADR-0014 (SaaS Token Storage) を、Phase 3 step D (closeout) で ADR-0010 (Connector Contract) を、それぞれ実装で検証した上で Accepted に昇格させた。Phase 5 step A1 で ADR-0015 (LLM Usage Strategy) を Accepted で起票し、principles.md §Open Q #1 (LLM 利用方針) を closeout する (closeout 反映自体は Phase 5 D1)。Phase 6 step A1 で ADR-0016 (Action Loop and Structured Output) を Accepted で起票し、ADR-0015 §決定 (a) deferred (Local LLM backend) を本 ADR §決定 (h) で closeout する。Phase 8 step A1 で ADR-0017 (Knowledge Graph) を Accepted で起票し、`links` projection schema / 自動抽出 vs manual link / traversal depth / `--expand-graph` flag / `LinkDeleted` hard delete 等の 8 決定を pin する (principles.md §Open Q #5 Multi-machine sync は本 ADR では closeout せず Phase 9 候補)。Phase 9 step A1 で ADR-0019 (Local-filesystem-backed Connector) を Accepted で起票し、`Connector` Protocol 再利用 + auth を OS 依存に置換 / `stat()` のみで `open()` 禁止 / Identity = rel_path / `sources.fingerprint` 列で diff detection / 削除追跡なし / `root_path` platform default / Excludes は `opshub.toml` inline / Operator precondition は opshub 範囲外 / Watch mode は Phase 9.x 持ち越し の 9 決定を pin する。Phase 11 Sub-issue F1 で ADR-0025 (Office Document Content Extraction) を Accepted で新規起票し markitdown 経路 / 50MB+500K chars 上限 / 抽出失敗 fail-safe / 形式別 source_type 3 種 / Excel cells 上限 / PPT 本文+ノート両方含む / 画像 OCR Phase 12+ defer の 9 決定を pin。同 PR で ADR-0019 を改訂 (`content_extraction = true` opt-in 例外節 + onedrive_drive 汎化) + ADR-0010 を改訂 (Teams 追加 + 本文抽出契約 + delta-link cursor fallback + Teams User Token principal) を Accepted で確定する。

## 4. ファイル命名

```text
NNNN-kebab-case-title.md
```

- `NNNN` は 4 桁ゼロ詰め番号 (`0001`, `0002`, ...)。連番。
- title は小文字 kebab-case。
- 削除しない (Deprecated / Superseded であっても履歴として残す)。

## 5. 追加ガイドライン

1. **ADR は不変** が原則。誤字修正・リンク追加など軽微な編集は許容するが、決定内容を変えるなら新規 ADR を起票する。
2. **小さく書く**。1 ADR = 1 決定。複数決定を 1 ファイルに詰めない。
3. **Context は決定時点の事実のみ**。後の状況は次の ADR に書く。
4. **Alternatives は最低 1 つ書く**。「他の選択肢を検討した」という事実を残す。
5. **新規 ADR を起票したら、関連する `principles.md` / `architecture.md` / 既存 ADR から相互リンクを張る**。

## 6. インデックス

| Number | Title | Status |
|---|---|---|
| 0000 | [Use Architecture Decision Records](0000-use-adrs.md) | Accepted |
| 0001 | [Python Stack](0001-python-stack.md) | Accepted |
| 0002 | [Event-Sourced Architecture](0002-event-sourced-architecture.md) | Accepted |
| 0003 | [Markdown as Workspace Surface](0003-markdown-as-workspace-surface.md) | Accepted |
| 0004 | [Agent Runtime Boundary](0004-agent-runtime-boundary.md) | Accepted |
| 0005 | [External Content Minimization](0005-external-content-minimization.md) | Accepted |
| 0006 | [CLI-first MVP, defer MCP](0006-cli-first-mvp.md) | Accepted |
| 0007 | [Single Python Package, defer Monorepo](0007-single-python-package.md) | Accepted |
| 0008 | [Naming: opshub](0008-naming-opshub.md) | Accepted |
| 0009 | [Multi-Agent Neutrality](0009-multi-agent-neutrality.md) | Accepted |
| 0010 | [Connector Contract](0010-connector-contract.md) | Accepted |
| 0011 | [Ozzy-Labs Ecosystem Adoption](0011-ozzy-labs-ecosystem-adoption.md) | Accepted |
| 0012 | [Embedding Strategy](0012-embedding-strategy.md) | Accepted |
| 0013 | [Lock Granularity](0013-lock-granularity.md) | Accepted |
| 0014 | [SaaS Token Storage](0014-saas-token-storage.md) | Accepted |
| 0015 | [LLM Usage Strategy](0015-llm-usage-strategy.md) | Accepted |
| 0016 | [Action Loop and Structured Output](0016-action-loop-and-structured-output.md) | Accepted |
| 0017 | [Knowledge Graph](0017-knowledge-graph.md) | Accepted |
| 0018 | [Slack Connector Token Principal](0018-slack-token-principal.md) | Accepted |
| 0019 | [Local-filesystem-backed Connector (box_drive)](0019-local-filesystem-backed-connector.md) | Accepted |
| 0020 | [Full Local Content Retention](0020-full-local-content-retention.md) | Accepted |
| 0021 | [Encryption at Rest](0021-encryption-at-rest.md) | Accepted |
| 0022 | [MCP Server Surface](0022-mcp-server-surface.md) | Accepted |
| 0025 | [Office Document Content Extraction](0025-office-document-content-extraction.md) | Accepted |
| 0026 | [CLI Progress Reporting for Long-Running Commands](0026-cli-progress-reporting.md) | Accepted |
| 0027 | [Observability & Troubleshooting Logging](0027-observability-and-troubleshooting-logging.md) | Accepted |
| 0028 | [FTS5 sources_fts tokenizer choice (trigram + short-query LIKE fallback)](0028-fts5-japanese-tokenizer.md) | Accepted |
| 0029 | [Distribute Assistant Skills via opshub Package Bundling](0029-distribute-assistant-skills-via-opshub-package.md) | Accepted |
| 0030 | [Slack Thread Reply Ingestion Policy](0030-slack-thread-reply-ingestion.md) | Accepted |

## Open Questions

1. ADR の言語: 当面日本語 + 英語見出し。OSS 公開時に英訳するか、最初から英語で書くかは未確定
2. ADR の review 運用 (PR で必須 review を要求するか)
3. ADR テンプレートを `0000-template.md` として別途置くか
