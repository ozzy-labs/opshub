# 0000. Use Architecture Decision Records

- Status: Accepted
- Date: 2026-05-16
- Deciders: ozzy

## Context

OpsHub は設計フェーズで多くの判断 (言語選定 / アーキテクチャ / agent boundary / 機密保持方針 等) を積み上げている。これらは:

1. 後から覆すと高コスト
2. 直感に反する判断が含まれる (例: markdown が source of truth ではない、agent は DB を直接触らない)
3. AI エージェントを含む複数の参加者に共有する必要がある (Claude Code / Codex / Gemini / Copilot / 将来の協力者)

判断の **背景と代替案** を残さないと、3 ヶ月後に「なぜこうなっているのか」を再構築するコストが発生し、しばしば誤った reversion につながる。

## Decision

設計判断を **Architecture Decision Record (ADR)** として `docs/adr/` に記録する。

- フォーマット: Michael Nygard 形式 (簡易版)。詳細は [`docs/adr/README.md`](README.md)
- ファイル命名: `NNNN-kebab-case-title.md` (4 桁番号、小文字、ハイフン)
- ライフサイクル: `Proposed` → `Accepted` → (任意) `Deprecated` / `Superseded by ADR-NNNN`
- ADR は不変。決定変更時は新 ADR を起票し既存に supersedes リンクを張る
- 1 ADR = 1 決定

## Consequences

### Positive

1. **再現性**: 3 ヶ月後でも判断理由をたどれる
2. **オンボーディング**: 新規参加者 (人 / agent) が短時間で文脈把握できる
3. **議論の質**: ADR 起票が「決定 vs 雑感」の境界を強制する
4. **agent への文脈提供**: Claude Code / Codex 等が `docs/adr/` を参照することで、誤った reversion を回避できる

### Negative / Trade-offs

1. **記述コスト**: ADR 1 本あたり 30 分〜1 時間
2. **古い ADR の認知負荷**: 多数蓄積すると superseded 関係の追跡コストが増える
3. **Proposed の運用**: 暫定状態のまま放置されるリスク。Phase 1 着手時に一括レビューする運用で対処

## Alternatives Considered

### 1. ADR を採用しない (README / docs に都度書く)

却下理由: 判断の構造 (Context / Decision / Alternatives) が失われ、後から「なぜそう決めたか」が再構成しにくい。OSS 公開時の外部参加者にも不親切。

### 2. ADR の代わりに RFC 形式を採用

却下理由: RFC は提案・議論段階に強いが、確定後の参照性は ADR の方が優れる。両者は補完的だが、まず ADR から始めるのが標準的なプラクティス。RFC は将来必要に応じて `docs/rfc/` で追加可能。

### 3. ADR を `docs/decisions/` に置く

却下理由: `docs/adr/` の方が SEO / 検索ヒット率が高く、`adr-tools` のデフォルトと一致する。`docs/decisions/` は MADR で使われるが、より新しい慣習で広く認知されているとは言えない。
