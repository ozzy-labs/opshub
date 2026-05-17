# Performance baseline (v0.1.0)

Measured on a 2024-class developer workstation (8-core CPU, 16 GB RAM, NVMe
SSD, Python 3.13). Numbers are indicative — they will vary with hardware and
config.

## Cold-start budget

| Command | Median (3 runs) | Budget (ADR-0001) |
|---|---|---|
| `opshub --help` | ~135 ms | ≤ 300 ms |
| `opshub version` | ~75 ms | ≤ 300 ms |

The M6 cold-start guard (`tests/integration/test_cli_imports.py`) enforces a
module-level import whitelist on every `cli/*.py` module so heavy dependencies
(`sqlalchemy` / `pydantic-settings` / ML / LLM SDKs) stay out of the cold-start
path. Heavy imports happen inside command function bodies, only when actually
needed. The complementary tripwire `tests/integration/test_cold_start.py`
asserts that `opshub --help` stays under the ADR-0001 budget on every CI run.

## Test suite

| Configuration | Tests | Time |
|---|---|---|
| `uv run pytest` (default extras) | 1533 pass / 9 skip | ~85 s |
| `just ci` (lint + format + pyright + mypy + pytest, all extras) | 1533 pass / 9 skip | ~80-120 s |

The 9 skipped tests are gated by `pytest.importorskip` on optional extras
(`sentence-transformers`, `voyageai`, `keyring`) that aren't installed in
default CI environments.

## Recall + briefing latency

End-to-end measurements with mocked embedder + mocked LLM (no real API calls):

| Operation | Median |
|---|---|
| `opshub recall "<query>"` (10 entities embedded) | ~50 ms |
| `opshub brief "<topic>"` (5 recall hits) | ~80 ms + LLM round-trip |
| `opshub propose generate "<topic>"` (5 hits) | ~100 ms + LLM round-trip |

With real LLM backends the dominant cost is the network round-trip:

| Backend | Typical (no streaming, ~500 token response) |
|---|---|
| Anthropic claude-haiku-4-5 | ~2-4 s |
| OpenAI gpt-4o-mini | ~2-3 s |
| Ollama llama3.2:3b (local CPU) | ~5-15 s |
| Ollama llama3.2:3b (local GPU) | ~1-3 s |

## Database size

A typical operator generates ~10-50 events / day across all surfaces. Rough
sizing:

| Usage | DB size after 1 year |
|---|---|
| Solo dev, tasks + decisions only (no embeddings, no SaaS connectors) | ~5 MB |
| With embeddings (1024-dim float32, ~5000 entities) | ~25 MB |
| With connectors (GitHub + Slack high-volume) | ~100-500 MB |

The event log grows monotonically (ADR-0002). Phase 9 (Multi-machine sync)
will introduce options for archival / export.

## Performance regression detection

The `just ci` recipe runs every test on every PR. Performance regressions in
the cold-start path are caught by two layers:

- `tests/integration/test_cli_imports.py` — static whitelist check; fails if
  any new module-level import slips into a `cli/*.py` file.
- `tests/integration/test_cold_start.py` — runtime tripwire; fails if
  `opshub --help` exceeds the ADR-0001 300 ms budget.

Latency regressions in recall / brief / propose are not currently asserted by
a regression test — operators noticing a slowdown should file an issue.
