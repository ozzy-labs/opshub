"""Rebuild ``sources_fts`` with the FTS5 ``trigram`` tokenizer.

Revision ID: 0028_rebuild_sources_fts_trigram
Revises: 0019_create_sources_fts
Create Date: 2026-06-02

Phase 15 Sub-issue S2 (epic #338 / issue #358) physically supersedes
migration ``0019_create_sources_fts``'s tokenizer choice. ADR-0028
§Decision (a) replaces ``unicode61 remove_diacritics 2`` with the
SQLite FTS5 built-in ``trigram`` tokenizer so Japanese natural-text
queries (e.g. ``"boxの権限"``, ``"進捗記入"``, ``"CDKの"``) hit by
substring instead of requiring exact-token-or-prefix match.

Why this is a fresh migration, not an in-place patch to 0019
-------------------------------------------------------------

Migrations under ``src/opshub/db/migrations/versions/`` are
immutable once shipped (Phase 1 onward — `docs/phase-15-plan.md`
§1 inherited invariant #7). Operator DBs that already ran 0019
must converge by running a new revision, not by re-evaluating the
old one. We therefore add 0028 to drop and recreate ``sources_fts``
with the new tokenizer, back-fill from ``sources.body``, and
re-attach the three sync triggers in the same shape 0019 created
them.

Upgrade path
------------

1. Drop the three sync triggers (``sources_fts_ai`` /
   ``sources_fts_ad`` / ``sources_fts_au``). FTS5 vtables refuse a
   ``DROP TABLE`` while triggers reference them on some SQLite
   builds, so we tear triggers down first regardless.
2. ``DROP TABLE sources_fts`` removes both the inverted index and
   the shadow tables (``sources_fts_data``, ``sources_fts_idx``,
   ``sources_fts_docsize``, ``sources_fts_config``).
3. Recreate the external-content FTS5 vtable with ``tokenize='trigram'``.
4. Back-fill from ``sources.body`` using the same documented FTS5
   rebuild idiom 0019 uses
   (``INSERT INTO sources_fts(rowid, body) SELECT ...``).
   ``body`` may be NULL (Phase 3-9 / ``box_drive`` historic rows,
   ADR-0019 §不変条件 (b) / ADR-0020 §(d)); FTS5 stores those rows
   with an empty document so the 1:1 rowid alignment with
   ``sources`` is preserved.
5. Recreate the three triggers byte-for-byte from 0019. We
   intentionally do not factor the trigger DDL into a shared
   helper — the migration must stay self-contained and the trigger
   bodies are the contract between ``sources`` writes and the FTS
   index, so duplication is the safer trade-off.

Downgrade path
--------------

Symmetric: drop the three triggers, drop ``sources_fts``, recreate
the vtable with ``tokenize='unicode61 remove_diacritics 2'`` (the
0019 choice), back-fill from ``sources.body``, and re-create the
three triggers. ``alembic downgrade base`` then re-runs cleanly so
operators can roll back the tokenizer choice without losing the
``sources`` projection itself.

What this migration does not touch
----------------------------------

* ``sources`` projection schema / rows — only the derived FTS
  index. ADR-0020 keeps ``sources.body`` as the canonical store.
* ``SearchService`` query routing — the 1-2 character LIKE fallback
  for ``trigram``'s 3-char minimum lives in Phase 15 S3
  (`src/opshub/services/search_service.py`改修, ADR-0028 §Decision (b)).
* migration 0019 itself — immutable by Phase 1 規範, only
  superseded by this revision's physical rebuild.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028_rebuild_sources_fts_trigram"
down_revision: str | Sequence[str] | None = "0019_create_sources_fts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tokeniser strings live as module constants so the upgrade and
# downgrade halves stay in sync and a future migration that revisits
# the choice (e.g. a morphological tokenizer per ADR-0028
# §Alternatives (b)) can reference the same literal.
_TRIGRAM_TOKENIZE = "trigram"
_UNICODE61_TOKENIZE = "unicode61 remove_diacritics 2"

# Trigger DDL is intentionally identical to migration 0019 so the
# write-path contract (``sources`` INSERT / UPDATE OF body / DELETE
# stays mirrored into the FTS index) survives the tokenizer swap.
# We duplicate the SQL here rather than importing 0019 to keep this
# migration self-contained per the project-wide convention.
_TRIGGER_AI = (
    "CREATE TRIGGER sources_fts_ai AFTER INSERT ON sources BEGIN "
    "INSERT INTO sources_fts(rowid, body) VALUES (new.rowid, new.body); "
    "END"
)
_TRIGGER_AD = (
    "CREATE TRIGGER sources_fts_ad AFTER DELETE ON sources BEGIN "
    "INSERT INTO sources_fts(sources_fts, rowid, body) "
    "VALUES ('delete', old.rowid, old.body); "
    "END"
)
_TRIGGER_AU = (
    "CREATE TRIGGER sources_fts_au AFTER UPDATE OF body ON sources BEGIN "
    "INSERT INTO sources_fts(sources_fts, rowid, body) "
    "VALUES ('delete', old.rowid, old.body); "
    "INSERT INTO sources_fts(rowid, body) VALUES (new.rowid, new.body); "
    "END"
)


def _create_fts_table(tokenize: str) -> None:
    """Create the ``sources_fts`` external-content FTS5 vtable.

    ``content='sources'`` tells SQLite the canonical text lives in
    the ``sources`` projection; FTS5 only stores the inverted index.
    ``content_rowid`` defaults to ``rowid`` (the hidden integer
    rowid SQLite assigns even when the declared PK is the TEXT
    ULID), so the JOIN back to ``sources`` rows still works.
    """
    op.execute(
        "CREATE VIRTUAL TABLE sources_fts USING fts5("
        "body, "
        "content='sources', "
        "content_rowid='rowid', "
        f"tokenize='{tokenize}'"
        ")"
    )


def _backfill_from_sources() -> None:
    """Back-fill the FTS index from existing ``sources`` rows.

    Standard FTS5 external-content rebuild idiom. ``body`` may be
    NULL (Phase 3-9 / ``box_drive`` historic rows); FTS5 accepts
    NULL and stores an empty document, which keeps the 1:1 rowid
    alignment with ``sources`` rowids intact.
    """
    op.execute("INSERT INTO sources_fts(rowid, body) SELECT rowid, body FROM sources")


def _drop_triggers() -> None:
    """Drop the three sync triggers if present."""
    op.execute("DROP TRIGGER IF EXISTS sources_fts_au")
    op.execute("DROP TRIGGER IF EXISTS sources_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS sources_fts_ai")


def _create_triggers() -> None:
    """Create the three sync triggers (identical to migration 0019)."""
    op.execute(_TRIGGER_AI)
    op.execute(_TRIGGER_AD)
    op.execute(_TRIGGER_AU)


def upgrade() -> None:
    """Swap ``sources_fts`` to the ``trigram`` tokenizer.

    See module docstring for the rationale. Steps:

    1. drop triggers, 2. drop FTS vtable, 3. create with trigram,
    4. back-fill from ``sources.body``, 5. re-create triggers.
    """
    _drop_triggers()
    op.execute("DROP TABLE IF EXISTS sources_fts")
    _create_fts_table(_TRIGRAM_TOKENIZE)
    _backfill_from_sources()
    _create_triggers()


def downgrade() -> None:
    """Restore the ``unicode61 remove_diacritics 2`` tokenizer.

    Symmetric to :func:`upgrade`; back-fill is included so the
    inverse round-trip leaves operator DBs with a populated FTS
    index identical in shape to the post-0019 state.
    """
    _drop_triggers()
    op.execute("DROP TABLE IF EXISTS sources_fts")
    _create_fts_table(_UNICODE61_TOKENIZE)
    _backfill_from_sources()
    _create_triggers()
