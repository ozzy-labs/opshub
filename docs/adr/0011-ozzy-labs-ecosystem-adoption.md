# 0011. Ozzy-Labs Ecosystem Adoption

- Status: Accepted
- Date: 2026-05-16
- Deciders: ozzy

## Context

ozzy-labs 組織には新規リポを「Phase 0 完了状態」に持ち込むための成熟したエコシステムが既に存在する。

| リポ | 役割 |
|---|---|
| `ozzy-labs/handbook` | 組織横断方針の SSOT (ADR + conventions + roadmap) |
| `ozzy-labs/commons` | 共有設定の SSOT + sync 機構 (`sync.sh` / `setup-repo.sh` / `init-templates.sh` / `sync-skills.sh`) |
| `ozzy-labs/skills` | Multi-agent 共通 skill の SSOT (adapter 出力 + Renovate sync) |
| `ozzy-labs/.github` | 共通 Renovate / Dependabot preset |

opshub は ozzy-labs 配下の新規リポとして、上記エコシステムに乗るか独自に振る舞うかを決める必要があった。

加えて opshub は **ozzy-labs 初の Python 主体リポ** になる予定 (ADR-0001)。既存のエコシステムは TypeScript-npm 前提で整備されており、Python 固有の調整が必要 (handbook の `conventions/new-project-bootstrap.md` も第 1 版は typescript-npm のみ対応)。

## Decision

opshub は **ozzy-labs エコシステムにフル参加** する。具体内容:

1. **bootstrap runbook を完全実行** — `conventions/new-project-bootstrap.md` の手順 (3.1 から 3.7) をすべて適用
   - `setup-repo.sh` で ruleset / security / labels / Renovate App 設定
   - `gh repo edit --add-topic` で 8 件の topic 付与
   - `sync.sh` で commons の 33 ファイルを配布
   - `init-templates.sh` で AGENTS.md / CLAUDE.md 生成
   - `.commons/sync.yaml` で commons / skills SHA 追跡
   - `sync-skills.sh` で skills の adapter 別配信
2. **`.commons/sync.yaml` の `skills_adapters` で 4 vendor opt-in** — `[claude-code, codex-cli, gemini-cli, copilot]` をすべて有効化 (ADR-0009 と整合)
3. **handbook ADR を上位として参照** — opshub の `docs/adr/` は project ADR として独立連番だが、handbook ADR と矛盾する判断はしない (`conventions/project-docs-layout.md` 準拠)
4. **GitHub Projects "OzzyLabs Platform" に opshub を登録** — opshub は単一リポだが長期 Phase 駆動のため (handbook ADR-0007 / `conventions/task-management.md` の条件「2 リポ以上または 2 週間超のタスク」に該当)
5. **handbook ADR 0001 命名規則準拠** — GitHub `ozzy-labs/opshub`、PyPI 配布 (npm scope `@ozzylabs` には属さない)
6. **Phase tracking convention 準拠** — Phase 0 (bootstrap) / Phase 1-4 を `phase-issue` skill で epic 化 (`conventions/phase-tracking.md`)
7. **Python pioneer としての還元** — opshub の bootstrap で得た知見を `conventions/bootstrap-python-uv.md` として handbook に PR 提案する (将来作業)

## Consequences

### Positive

1. **bootstrap が standardize される** — 1 コマンドで Phase 0 完了状態に近づける
2. **継続的な設定同期** — commons / skills の更新が Renovate 経由で自動取り込み
3. **org 横断方針との一貫性** — handbook ADR を引き継ぎ、独自判断の積み重ねを最小化
4. **multi-agent エコシステムの即時利用** — 13 個の skill が `.claude/skills/` / `.agents/skills/` / `.gemini/settings.json` / `.github/copilot-instructions.md` に自動配信される
5. **Python pioneer 知見が org 資産になる** — `conventions/bootstrap-python-uv.md` 提案で将来の Python リポを下支え

### Negative / Trade-offs

1. **Node 寄り設定の流入** — `biome.json` / `.mise.toml` の `node` / `pnpm` / `.devcontainer/Dockerfile` (Node 24 base) / `.vscode/extensions.json` の biome 拡張など。Python 適合のため初期 commit で置換が必要 (`phase-1-plan.md` 1 参照)
2. **commons sync で再混入する Node 寄り設定の対応** — `.commons/sync.yaml` の `pinned:` で防御する必要 (`biome.json` / `.mise.toml` / `.vscode/extensions.json` / `.devcontainer/Dockerfile` 等)
3. **handbook ADR との整合確認コスト** — opshub の判断が handbook ADR と衝突しないか継続チェックが必要
4. **`conventions/new-project-bootstrap.md` の typescript-npm 偏重** — Python リポでは一部手順を読み替える必要 (将来は Python 版 convention で解消)

## 軽減策

1. **`.commons/sync.yaml` の `pinned:`** に Python 固有設定を明示し、commons sync で上書きされないようにする
2. **`phase-1-plan.md` の TODO 7 項目** で Node 寄り設定を Python 用に置換 (Phase 1 着手時に解消)
3. **handbook ADR 監視** — opshub に影響する handbook ADR (例: 0018 / 0019 / 0023) の更新を週次でチェック
4. **Python bootstrap convention 提案** — `conventions/bootstrap-python-uv.md` を handbook に PR 提案 (Phase 1 完了後の振り返り時)

## Alternatives Considered

### 1. ozzy-labs エコシステムを使わず standalone リポとして運営

却下理由:

- bootstrap を手作業で組み立てる労力
- 多 agent 連携 / skill 配信 / git workflow 規約をすべて自前で整備するコスト
- ozzy-labs 配下の他リポと不整合 (運用ノウハウが共有できない)

### 2. handbook ADR + commons sync は採用、skills は採用しない

却下理由:

- 4 agent (Claude / Codex / Gemini / Copilot) を vendor-neutral に扱う方針 (ADR-0009) と矛盾
- 13 skill (commit / pr / review / implement / drive / 等) を自前で書く実装コスト
- skills の Renovate sync が落ちることで継続更新の利益を失う

### 3. commons の Node 寄り設定を opshub 用に手動 fork

却下理由:

- 継続同期の利益を失う
- 将来 commons が更新されたとき、手動 merge が必要
- pinned + Phase 1 TODO の方式 (採用案) で同等の柔軟性を得つつ sync の利益を保つ

### 4. Python リポ用に別の commons-python リポを作成

却下理由:

- maintain するリポが増える
- 共通部分 (.editorconfig / .gitattributes / .markdownlint / lefthook-base / GitHub Actions 等) は言語非依存なので分割の利益が薄い
- 必要に応じて commons 自体を多言語対応に進化させる方が build-once

## 関連

- [Principles 5 (Multi-Agent Neutral)](../principles.md)
- [Principles 9 (Phased Delivery)](../principles.md)
- [Repository Structure 1 (トップレベル構成)](../repository-structure.md)
- [Phase 1 Plan](../phase-1-plan.md)
- handbook: `conventions/new-project-bootstrap.md`
- handbook: `conventions/project-docs-layout.md`
- handbook ADR-0005 / 0007 / 0018 / 0019 / 0023
