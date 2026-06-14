# 0008. Naming: opshub

- Status: Superseded by [ADR-0044](0044-rename-opshub-to-suasor.md) (2026-06-14 — 本 ADR §Positive #3「`opshub` という主要プロダクトは存在しない」の前提が `OpsHub, Inc.` (opshub.com、ALM/DevOps 統合ベンダー) の実在で覆り、製品名を **Suasor (スアソル)** に改名する判断に至った。実装は将来 Phase に defer)
- Date: 2026-05-16
- Deciders: ozzy

## Context

本プロダクト (人間と AI エージェントが共有する Operational Memory + 実行ハブ) の命名を決める必要があった。検討経緯:

1. 機能の本質は「複数 SaaS から業務シグナルを集約し、AI エージェントが triage / 下書き / 実行を行う中継・統合点」
2. ozzy-labs の既存ゼロベース命名 (2026-04-18 確定) は `forge` / `skills` / `presets` / `starlight` / `road` / `web` / `mcp-knowledge` / `bootstrap` の単語 1 語に揃っており、`agentic-*` 接頭辞は setup/scaffold シリーズ (`agentic-bootstrap` / `create-agentic-app` / `create-agentic-aws`) 限定
3. CLI bin として日常的に打つため短さも重要
4. `ozzy` を含むツール名は採用しない方針 (個人名由来、ブランドスケール不利)

## Decision

| 項目 | 値 |
|---|---|
| GitHub repo | `ozzy-labs/opshub` |
| Product displayName | `OpsHub` |
| CLI bin | `opshub` |
| Python パッケージ | `opshub` |
| カテゴリ | `workflow` |

理由:

1. **`Ops` で「業務オペレーション」の広さを表現** — task / inbox / triage より広い概念をカバー
2. **`Hub` で「集約点」を表現** — connector / agent / human の合流点
3. **6 文字、CLI として打ちやすい** — `forge` (5) / `skills` (6) と同帯
4. **ゼロベース命名と整合** — 単語 1 語、`agentic-*` 接頭辞なし
5. **個人名を含まない** — 命名ルール準拠

## Consequences

### Positive

1. ozzy-labs 既存命名と並列に置ける (`forge` / `skills` / `opshub`)
2. CLI として日常使用に耐える長さ
3. 検索衝突が限定的 (`opshub` という主要プロダクトは存在しない)
4. `workflow` カテゴリの初プロダクトとして位置づけが明確

### Negative / Trade-offs

1. `OpsHub` は DevOps / ITOps Hub 系のエンタープライズ命名と語感が近く、個人 OSS としてはやや没個性
2. 「Operational Memory」「Workspace Runtime」のような本質的価値が repo 名から直接読めない (description / README で補う)

## Alternatives Considered

### 1. `relay`

却下理由: 「中継機能」を示すが「何のための中継か」が伝わらない。ユーザー指摘により dropped。

### 2. `inbox`

却下理由: 機能の即解性は最高だが、「集約 + 行動 + 多 agent 協調」を含む本プロダクトのスコープより狭い。受信箱としての連想が強すぎる。

### 3. `triage` / `agenda` / `desk` / `dispatch`

却下理由:

- `triage`: 「捌く」核心は表すが、保存・横断検索・実行までを含む scope より狭い
- `agenda`: npm 上の cron ライブラリと衝突
- `desk`: Zendesk のブランド連想
- `dispatch`: Netflix Dispatch (incident management) との連想、能動的すぎる

### 4. `signal`

却下理由: 「業務シグナル」を直接表現できるが、Signal Messenger との SEO / ブランド衝突が大きい。

### 5. `agentic-opshub` (with `agentic-` prefix)

却下理由:

1. ゼロベース命名 (2026-04-18) は核ツール = 無接頭辞単語 1 語に揃っている。`agentic-*` 接頭辞は setup/scaffold 限定
2. 14 文字で既存 ozzy-labs リポの中で最長
3. `agentic-opshub` 採用すると命名規則ドキュメントに例外条項を追加する必要がある
4. SEO 上の衝突回避動機が弱い (Facebook Relay のような library 名と領域違い)

### 6. `Agentic OpsHub` displayName のみ採用

却下理由: repo 名と displayName が乖離すると agent / 人間ともに発見性が落ちる。揃える方が運用が安定。

## 関連

- [Principles 10 (Pythonic but Vendor-Neutral)](../principles.md)
- [Decisions Log](../decisions-log.md)
- memory: `project_ozzylabs_product_naming` (workflow カテゴリ追加)
