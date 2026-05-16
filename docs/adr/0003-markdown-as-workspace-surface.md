# 0003. Markdown as Workspace Surface

- Status: Accepted
- Date: 2026-05-16
- Deciders: ozzy

## Context

OpsHub は人間と AI エージェントが共有するワークスペースを必要とする。markdown は次の理由で第一候補になる。

1. **agent 可読性** — Claude Code / Codex / Gemini / Copilot CLI のいずれも markdown を `Read` tool で扱える
2. **人間可読性** — Obsidian / VS Code / `cat` でそのまま読める
3. **diff 性** — git で履歴管理しやすい
4. **検索性** — `ripgrep` で横断検索できる

しかし markdown を **source of truth** にすると以下の問題が発生する。

1. クエリ性が貧弱 — 「P1 で due が今週内のタスク」のような構造化クエリが困難
2. 整合性保証が弱い — 複数ファイル間の link / 状態同期が壊れやすい
3. event-sourced との衝突 — event store が authoritative なら、markdown はそれの projection でしかない
4. 編集競合 — 複数 agent が同時編集すると merge 衝突が頻発

## Decision

markdown を **Workspace Surface** として位置づける。**source of truth ではない**。

具体ルール:

1. **`generated/` 配下の markdown** は projection の純粋関数で **disposable**
   - `opshub workspace generate` で再生成される
   - ユーザーや agent が直接編集してはならない (上書きされる)
   - 直接編集を試みたら lefthook / CI で警告する
2. **`notes/` などの人手記述 markdown** は **CLI 経由で event 化** する
   - `opshub note save <path>` → `NoteRecorded` event を append → projection 更新
   - 直接ファイルを置いただけでは Operational Memory に取り込まれない
3. **`inbox/` の markdown** は人手 / connector 双方が起点になり得るが、必ず event 経由で `inbox_items` に projection 化される
4. **markdown は常に regenerable** — workspace ディレクトリを丸ごと削除して `opshub workspace generate --force` で復元できる

## Consequences

### Positive

1. **event store と markdown の役割分離** が明確 — 何が source of truth か迷わない
2. **projection 変更耐性** — markdown の表現を変更しても event は不変
3. **agent との相性** — agent は markdown を読むだけで context 把握でき、書き込みは CLI に集約
4. **検索性 + 構造性の両立** — projection で構造クエリ、markdown で人間可読

### Negative / Trade-offs

1. **「markdown を直接編集すると損する」モデルへの慣れが必要** — Obsidian 流の workflow をそのまま持ち込めない
2. **編集 → event 化のワンクッション** が必要 — UX 設計を慎重にやらないと面倒に感じる
3. **生成 markdown と人手 markdown の混在** に誤解の余地がある — ディレクトリ分離 (`generated/` vs `notes/`) で明示する

## 軽減策

1. **`opshub note new` でテンプレ作成 → 編集後 `opshub note save` で event 化** の 2 ステップを CLI で提供
2. **`generated/` ディレクトリに `_DO_NOT_EDIT.md` を置く** — 直編集を試みた agent / 人間に警告
3. **`opshub workspace doctor`** で「人手編集された generated ファイル」を検出
4. **VS Code 拡張 / Obsidian plugin** で「save = event 化」を吸収する案は将来検討

## Alternatives Considered

### 1. Markdown を source of truth にする (Obsidian 流)

却下理由:

- クエリ性 / 整合性 / event-sourced との衝突が解決できない
- 複数 agent 同時編集での競合制御がほぼ不可能
- 構造クエリ (priority / due / status での絞り込み) が遅い・脆い

### 2. Markdown を生成のみ、編集は CLI 専用

却下理由:

- 「ちょっとメモを書く」が CLI 操作になり摩擦が大きい
- 人手記述ノートの存在価値を失う
- 採用案 (notes/ で人手記述を許す) の方が UX が良い

### 3. Markdown + frontmatter を semi-source of truth に

却下理由:

- frontmatter で状態を持つと、結局 markdown 編集 = 状態変更になり、event-sourced と矛盾
- frontmatter の検証・正規化が runtime にずれ込み、データ品質が不安定
- 採用案 (markdown を projection 化、event 経由で state 変更) の方が clean

### 4. JSON / YAML を workspace surface にする

却下理由:

- agent / 人間ともに markdown より読みにくい
- 自由記述部分 (notes / decision の rationale 等) を構造化フィールドに押し込むと表現力が落ちる
- markdown の自由度を残しつつ projection で構造化、が最良のバランス

## 関連

- [Principles 3 (Markdown is a Workspace Surface)](../principles.md)
- [Architecture 7 (Workspace ディレクトリ構造)](../architecture.md)
- [ADR-0002: Event-Sourced Architecture](0002-event-sourced-architecture.md)
