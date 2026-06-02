"""Create the ``sources_fts`` virtual table + sync triggers.

Revision ID: 0019_create_sources_fts
Revises: 0018_add_body_provenance_to_sources
Create Date: 2026-05-30

Phase 10 step B2 (Sub-issue B, ADR-0012 改訂版 §4 + ADR-0020) adds a
SQLite FTS5 index over ``sources.body`` so the new ``opshub search``
CLI (and future MCP / Skill tools) can answer literal / boolean full-
text queries that pure vector recall cannot (exact phrase, file path,
ticket id, code fragment).

Why FTS5 and not just vector recall
-----------------------------------

The Phase 10 Plan §3 Sub-issue B + ADR-0012 改訂版 §4 explicitly call
for **hybrid search** (vector + FTS) because each side has a recall
gap the other side covers:

* Vector recall finds semantic neighbours but is weak on rare strings
  the embedder did not see (function names, URLs, channel IDs).
* FTS5 finds exact tokens / phrases but is blind to paraphrase.

We pick FTS5 over `LIKE` because it is built into SQLite (no extra
extension load), tokenises with `unicode61` (mixed JA/EN works after
``remove_diacritics 2`` normalisation), supports BM25 ranking via the
``bm25()`` auxiliary function, and the index lives in the same DB so
SQLCipher (ADR-0021) covers it transparently.

External-content FTS5 vs contentless
------------------------------------

We use **external-content** FTS5 (``content='sources'``) so the FTS
table mirrors ``sources.body`` without duplicating the text on every
write. SQLite assigns every regular table an implicit ``INTEGER
rowid`` even when the declared PK is ``TEXT`` (the ULID), and FTS5's
``content_rowid`` defaults to that hidden ``rowid`` — exactly what we
need for the JOIN back to ``sources`` rows.

Three triggers keep the index in sync with ``sources`` writes:

* ``AFTER INSERT`` — index the new body.
* ``AFTER DELETE`` — remove the index entry (uses the FTS5
  ``'delete'`` command sentinel).
* ``AFTER UPDATE OF body`` — delete-then-insert to refresh.

Triggers fire on every projection write, including the
``ON CONFLICT DO UPDATE`` re-observation path in
:class:`opshub.projections.sources.SourcesProjection` (the UPDATE
clause includes ``body`` so the trigger picks up the change).

NULL body handling
------------------

Phase 3-9 historic rows and ``box_drive`` rows always carry
``body = NULL`` (ADR-0019 §不変条件 (b) / ADR-0020 §(d)
backward-compat). FTS5 accepts NULL on insert — the row gets an empty
document, returned by no MATCH query. This keeps the index aligned
1:1 with ``sources`` rowids without special-casing NULL in the
triggers.

Tokeniser choice
----------------

``unicode61 remove_diacritics 2 categories 'L* N* Co'`` accepts
letters, numbers, and a Unicode private-use range so CJK / emoji-ish
text still tokenises sensibly. ``remove_diacritics 2`` is the
NFC-aware variant introduced in SQLite 3.27 (2019). The opshub `mise`
toolchain ships a SQLite ≥ 3.38, so the option is always available.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.

.. note::
   Superseded by migration ``0028_rebuild_sources_fts_trigram``
   (Phase 15, ADR-0028 §Decision (a)). 0028 physically rebuilds
   ``sources_fts`` with ``tokenize='trigram'`` so Japanese natural-
   text queries hit by substring rather than only by exact
   token / prefix. Migration 0019 itself stays immutable per the
   Phase 1 onward 規範; the tokenizer switch happens in a fresh
   revision so operator DBs converge by running new migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_create_sources_fts"
down_revision: str | Sequence[str] | None = "0018_add_body_provenance_to_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tokeniser pragma stays in one place so a future migration that
# rebuilds the index for tokeniser tuning can refer to the same string.
_TOKENIZE = "unicode61 remove_diacritics 2"


def upgrade() -> None:
    """Create ``sources_fts`` + the three sync triggers."""
    # External-content FTS5 — ``content='sources'`` tells SQLite the
    # canonical text lives in the ``sources`` table; FTS5 only stores
    # the inverted index. ``content_rowid`` defaults to ``rowid``.
    op.execute(
        "CREATE VIRTUAL TABLE sources_fts USING fts5("
        "body, "
        "content='sources', "
        "content_rowid='rowid', "
        f"tokenize='{_TOKENIZE}'"
        ")"
    )

    # Backfill any pre-existing rows. ``INSERT INTO sources_fts(rowid,
    # body) SELECT rowid, body FROM sources`` is the documented FTS5
    # rebuild idiom for external-content tables. ``body`` can be NULL —
    # FTS5 stores the row with an empty document, which is fine
    # (we just want the index entry to exist 1:1 with sources rowids).
    op.execute("INSERT INTO sources_fts(rowid, body) SELECT rowid, body FROM sources")

    # AFTER INSERT — index the new body. The trigger fires per-row so
    # bulk INSERTs land cleanly. ``new.body`` may be NULL (Phase 3-9 /
    # box_drive rows) — FTS5 stores the empty document, no MATCH hit.
    op.execute(
        "CREATE TRIGGER sources_fts_ai AFTER INSERT ON sources BEGIN "
        "INSERT INTO sources_fts(rowid, body) VALUES (new.rowid, new.body); "
        "END"
    )

    # AFTER DELETE — emit the FTS5 ``'delete'`` command sentinel so
    # the inverted index drops the old tokens. Without this, replaying
    # the event log (which can re-observe with a new body) leaves
    # stale tokens behind.
    op.execute(
        "CREATE TRIGGER sources_fts_ad AFTER DELETE ON sources BEGIN "
        "INSERT INTO sources_fts(sources_fts, rowid, body) "
        "VALUES ('delete', old.rowid, old.body); "
        "END"
    )

    # AFTER UPDATE OF body — delete the old document, insert the new.
    # Scoping to ``OF body`` keeps the trigger from firing on
    # ``updated_at`` / ``fingerprint`` / ``provenance_*`` no-op writes
    # the re-observation path emits (the UPDATE clause refreshes
    # several columns; only body matters to FTS).
    op.execute(
        "CREATE TRIGGER sources_fts_au AFTER UPDATE OF body ON sources BEGIN "
        "INSERT INTO sources_fts(sources_fts, rowid, body) "
        "VALUES ('delete', old.rowid, old.body); "
        "INSERT INTO sources_fts(rowid, body) VALUES (new.rowid, new.body); "
        "END"
    )


def downgrade() -> None:
    """Drop the triggers + the FTS5 virtual table."""
    op.execute("DROP TRIGGER IF EXISTS sources_fts_au")
    op.execute("DROP TRIGGER IF EXISTS sources_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS sources_fts_ai")
    op.execute("DROP TABLE IF EXISTS sources_fts")
