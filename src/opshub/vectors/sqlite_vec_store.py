"""sqlite-vec backed VectorStore (Phase 4 step A2, ADR-0012).

Implements the Phase 1 :class:`~opshub.vectors.store.VectorStore`
Protocol using the three backend-specific ``embeddings_vec_<backend>``
virtual tables that migration 0013 (PR #64) created. The matching
``embeddings`` metadata table (``entity_type`` / ``entity_id`` /
``model_id`` / ``model_version`` / ``dim`` / ``created_at``) is JOINed
via SQLite's implicit ``rowid``.

Per phase-4-plan §1 #5 + §4 Open Q resolution, the store routes
``upsert`` / ``recall`` / ``delete`` to the vec0 table whose dimension
matches the embedding. Choosing the right vec0 by ``dim`` keeps the
code backend-agnostic — the caller picks the Embedder, the store
picks the matching vec0 from the dim it sees on the wire.

Requires the ``[vector]`` extras (sqlite-vec + numpy). The engine
factory (:func:`opshub.db.engine.create_engine_for_sqlite`) loads the
extension on every pooled connection at connect time, so this module
itself does not need to import ``sqlite_vec``; we only rely on the
``vec0`` virtual table being present.

Implementation notes
--------------------

* Migration 0002 (Phase 1) defines ``embeddings`` *without* an explicit
  primary-key column. SQLite auto-provides an ``INTEGER PRIMARY KEY``
  alias via ``rowid``, which is exactly what vec0 wants for JOINs.
  We therefore read / write ``rowid`` explicitly via raw SQL rather
  than declaring a Core ``Table`` (there is no ``id`` column to bind
  to and ``schema.py`` does not register ``embeddings``).
* vec0 virtual tables do **not** support ``UPDATE``; replacements
  go through ``DELETE`` + ``INSERT``. ``upsert`` performs the natural-key
  delete on both tables before inserting.
* :class:`~opshub.vectors.store.StoredEmbedding` carries no explicit
  ``dim`` field — the dimension is derived from ``len(vector)`` and used
  to route to the matching vec0 table.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from sqlalchemy import text

from opshub.core.errors import ConfigError
from opshub.vectors.store import RecallHit, StoredEmbedding

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = ["VEC_TABLES_BY_DIM", "SqliteVecStore"]


# Phase 4 backend → vec0 table mapping. Keys are the dimensions
# (1024 covers both local bge-m3 and voyage-3; 1536 covers OpenAI
# text-embedding-3-small). When a 4th backend with a new dim lands,
# add (a) a migration creating embeddings_vec_<name>, (b) an entry
# here.
#
# NOTE: local + voyage share dim=1024 *and* share the same vec0 table
# (``embeddings_vec_local``). The ``model_id`` / ``model_version``
# columns on the metadata table disambiguate which backend produced
# the row — the dimension alone is not sufficient to tell them apart.
# Migration 0013 creates a separate ``embeddings_vec_voyage`` table
# precisely so that operators *can* segregate them per-backend later;
# wiring that segregation into the store is deferred to Phase 4.x when
# parallel multi-backend retention lands. For now, all dim=1024 traffic
# lands in ``embeddings_vec_local`` to keep the routing decision
# dimension-only.
VEC_TABLES_BY_DIM: dict[int, str] = {
    1024: "embeddings_vec_local",  # shared by local (bge-m3) + voyage (voyage-3)
    1536: "embeddings_vec_openai",  # text-embedding-3-small
}


class SqliteVecStore:
    """VectorStore Protocol implementation backed by sqlite-vec.

    The instance is stateless beyond the bound :class:`Engine`; pooled
    connections inherit the sqlite-vec extension via the engine's
    ``connect`` listener so individual method calls don't need to
    re-load the extension.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- VectorStore Protocol --------------------------------------------

    def upsert(self, embeddings: list[StoredEmbedding]) -> None:
        """Insert or replace each embedding into metadata + matching vec0 table.

        vec0 virtual tables do not support ``UPDATE``, so we DELETE the
        prior natural-key row from both tables and re-INSERT. Each
        embedding is routed by ``len(vector)`` to the vec0 table whose
        declared dimension matches.

        Empty input is a no-op (matches the Phase 1 Protocol semantics
        for empty batches in :class:`Embedder.embed`).
        """
        if not embeddings:
            return
        with self._engine.begin() as conn:
            for emb in embeddings:
                vec_table = self._resolve_vec_table(len(emb.vector))
                # Drop any existing row keyed by
                # (entity_type, entity_id, model_id, model_version) so
                # vec0's lack of UPDATE support is invisible to callers.
                self._delete_by_natural_key(
                    conn,
                    entity_type=emb.entity_type,
                    entity_id=emb.entity_id,
                    model_id=emb.model_id,
                    model_version=emb.model_version,
                )

                # Insert the metadata row first; SQLite returns the
                # auto-allocated rowid via ``last_insert_rowid()``. We
                # capture it explicitly via SQLAlchemy's
                # ``cursor.lastrowid`` because the raw text INSERT
                # doesn't go through Core's ``insert().returning(...)``.
                insert_result = conn.execute(
                    text(
                        "INSERT INTO embeddings "
                        "(entity_type, entity_id, model_id, model_version, "
                        " dim, created_at) "
                        "VALUES (:entity_type, :entity_id, :model_id, "
                        "        :model_version, :dim, :created_at)"
                    ),
                    {
                        "entity_type": emb.entity_type,
                        "entity_id": emb.entity_id,
                        "model_id": emb.model_id,
                        "model_version": emb.model_version,
                        "dim": len(emb.vector),
                        "created_at": emb.created_at,
                    },
                )
                # SQLAlchemy's ``CursorResult.lastrowid`` returns the
                # ``last_insert_rowid()`` value from SQLite for plain
                # INSERTs (it's typed as ``int`` and is always
                # populated when the cursor executed a single-row
                # INSERT, which is exactly our case). We pin the vec0
                # row to that same rowid so the JOIN in :meth:`recall`
                # reconstructs (entity_type, entity_id).
                rowid = insert_result.lastrowid
                # vec0 INSERT pegged to the same rowid so the JOIN
                # works in :meth:`recall`.
                conn.execute(
                    text(f"INSERT INTO {vec_table}(rowid, embedding) VALUES (:rowid, :embedding)"),
                    {"rowid": rowid, "embedding": _vec_to_blob(emb.vector)},
                )

    def recall(
        self,
        query: tuple[float, ...],
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:
        """Return the ``k`` nearest hits to ``query``.

        Routes the query to the vec0 table whose declared dimension
        matches ``len(query)``. vec0's ``MATCH`` operator returns rows
        ordered by ascending distance; we transform distance into a
        ``score`` where higher = more similar (``score = -distance``)
        so the returned list is ordered ``score`` descending, matching
        the Protocol contract.

        ``entity_types`` filtering happens **post-query**: we ask vec0
        for ``k`` neighbours, then drop hits whose ``entity_type`` is
        not in the allow-list. This matches the Phase 4 plan §2.1 spec.
        Callers that want exactly ``k`` results after a type filter
        should over-request and slice client-side.
        """
        if k <= 0:
            return []
        vec_table = self._resolve_vec_table(len(query))
        stmt = text(
            f"""
            SELECT e.entity_type AS entity_type,
                   e.entity_id   AS entity_id,
                   v.distance    AS distance,
                   v.embedding   AS embedding
              FROM {vec_table} AS v
              JOIN embeddings  AS e ON e.rowid = v.rowid
             WHERE v.embedding MATCH :q AND k = :k
             ORDER BY v.distance
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(
                stmt,
                {"q": _vec_to_blob(query), "k": k},
            ).all()

        results: list[RecallHit] = []
        for row in rows:
            entity_type = str(row.entity_type)
            if entity_types is not None and entity_type not in entity_types:
                continue
            results.append(
                RecallHit(
                    entity_type=entity_type,
                    entity_id=str(row.entity_id),
                    # Negate so the Protocol's "higher = more similar"
                    # invariant holds while preserving vec0's native
                    # distance ordering.
                    score=-float(row.distance),
                    vector=_blob_to_vec(row.embedding),
                )
            )
        return results

    def recall_by_rowid(
        self,
        entity_type: str,
        entity_id: str,
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:
        """Return the ``k`` nearest hits to the embedding stored at ``(entity_type, entity_id)``.

        Avoids a re-embed round-trip when the caller already has the
        entity persisted (e.g. offline duplicate detection). The lookup
        chain is:

        1. ``embeddings`` metadata → most-recent ``rowid`` + ``dim``
           for ``(entity_type, entity_id)``; "most recent" = largest
           ``rowid`` so the latest insertion wins when an entity holds
           multiple ``model_version`` rows.
        2. ``dim`` → vec0 table via :data:`VEC_TABLES_BY_DIM`.
        3. Fetch the stored vector blob from that vec0 table at the
           captured ``rowid``.
        4. Issue the same vec0 ``MATCH`` query as :meth:`recall` using
           the fetched blob as the query vector.

        If the entity has no metadata row (never embedded) the result
        is an empty list — consistent with "no hits" rather than
        raising. Self-match is **not** filtered; the caller decides.
        """
        if k <= 0:
            return []
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT rowid AS rowid, dim AS dim "
                    "FROM embeddings "
                    "WHERE entity_type = :et AND entity_id = :eid "
                    "ORDER BY rowid DESC LIMIT 1"
                ),
                {"et": entity_type, "eid": entity_id},
            ).first()
            if row is None:
                return []
            vec_table = self._resolve_vec_table(int(row.dim))
            blob = conn.execute(
                text(f"SELECT embedding FROM {vec_table} WHERE rowid = :rowid"),
                {"rowid": int(row.rowid)},
            ).scalar_one()
            stmt = text(
                f"""
                SELECT e.entity_type AS entity_type,
                       e.entity_id   AS entity_id,
                       v.distance    AS distance,
                       v.embedding   AS embedding
                  FROM {vec_table} AS v
                  JOIN embeddings  AS e ON e.rowid = v.rowid
                 WHERE v.embedding MATCH :q AND k = :k
                 ORDER BY v.distance
                """
            )
            rows = conn.execute(stmt, {"q": blob, "k": k}).all()

        results: list[RecallHit] = []
        for hit in rows:
            hit_type = str(hit.entity_type)
            if entity_types is not None and hit_type not in entity_types:
                continue
            results.append(
                RecallHit(
                    entity_type=hit_type,
                    entity_id=str(hit.entity_id),
                    score=-float(hit.distance),
                    vector=_blob_to_vec(hit.embedding),
                )
            )
        return results

    def count(self, *, entity_type: str | None = None) -> int:
        """Return the number of metadata rows, optionally filtered.

        Counts rows in the ``embeddings`` metadata table, not the vec0
        virtual tables — they are kept in lock-step by :meth:`upsert`
        and :meth:`delete`, and ``embeddings`` is the canonical source
        of truth for "how many entities have an embedding".
        """
        if entity_type is None:
            stmt = text("SELECT COUNT(*) FROM embeddings")
            params: dict[str, str] = {}
        else:
            stmt = text("SELECT COUNT(*) FROM embeddings WHERE entity_type = :et")
            params = {"et": entity_type}
        with self._engine.connect() as conn:
            return int(conn.execute(stmt, params).scalar_one())

    def delete(self, *, entity_type: str, entity_id: str) -> int:
        """Remove every embedding (across all ``model_id`` / ``model_version``).

        Deletes from both the metadata table *and* every vec0 table
        that holds a matching row — orphaned vec0 rows would survive
        future recalls otherwise. Returns the number of metadata rows
        deleted so callers can detect no-op deletes (consistent with
        Phase 1's Protocol docstring).
        """
        with self._engine.begin() as conn:
            # Find every (rowid, dim) so we can route each vec0 delete
            # to the correct backend table in lock-step.
            rows = conn.execute(
                text(
                    "SELECT rowid AS rowid, dim AS dim "
                    "FROM embeddings "
                    "WHERE entity_type = :et AND entity_id = :eid"
                ),
                {"et": entity_type, "eid": entity_id},
            ).all()
            for row in rows:
                vec_table = self._resolve_vec_table(int(row.dim))
                conn.execute(
                    text(f"DELETE FROM {vec_table} WHERE rowid = :rowid"),
                    {"rowid": int(row.rowid)},
                )
            result = conn.execute(
                text("DELETE FROM embeddings WHERE entity_type = :et AND entity_id = :eid"),
                {"et": entity_type, "eid": entity_id},
            )
            # ``rowcount`` is well-defined for SQLite DELETEs.
            return int(result.rowcount)

    # ---- internals -------------------------------------------------------

    def _resolve_vec_table(self, dim: int) -> str:
        """Map an embedding dimension to its vec0 table.

        Raises :class:`ConfigError` with the supported set so operators
        get an actionable error when a new backend lands without the
        accompanying migration + :data:`VEC_TABLES_BY_DIM` entry.
        """
        try:
            return VEC_TABLES_BY_DIM[dim]
        except KeyError as exc:
            supported = ", ".join(str(d) for d in sorted(VEC_TABLES_BY_DIM))
            raise ConfigError(
                f"SqliteVecStore: no vec0 table for dim={dim}. "
                f"Supported dimensions: {supported}. Add a migration + "
                f"VEC_TABLES_BY_DIM entry to support a new backend."
            ) from exc

    def _delete_by_natural_key(
        self,
        conn: Connection,
        *,
        entity_type: str,
        entity_id: str,
        model_id: str,
        model_version: str,
    ) -> None:
        """Delete every row matching the natural key from both tables.

        Used by :meth:`upsert` to clear the prior row before re-inserting.
        Honours the UNIQUE ``(entity_type, entity_id, model_id,
        model_version)`` invariant declared by migration 0002.
        """
        rows = conn.execute(
            text(
                "SELECT rowid AS rowid, dim AS dim "
                "FROM embeddings "
                "WHERE entity_type = :et AND entity_id = :eid "
                "  AND model_id = :mid AND model_version = :mv"
            ),
            {
                "et": entity_type,
                "eid": entity_id,
                "mid": model_id,
                "mv": model_version,
            },
        ).all()
        for row in rows:
            vec_table = self._resolve_vec_table(int(row.dim))
            conn.execute(
                text(f"DELETE FROM {vec_table} WHERE rowid = :rowid"),
                {"rowid": int(row.rowid)},
            )
        conn.execute(
            text(
                "DELETE FROM embeddings "
                "WHERE entity_type = :et AND entity_id = :eid "
                "  AND model_id = :mid AND model_version = :mv"
            ),
            {
                "et": entity_type,
                "eid": entity_id,
                "mid": model_id,
                "mv": model_version,
            },
        )


def _vec_to_blob(vector: tuple[float, ...]) -> bytes:
    """Pack a float tuple into the bytes format sqlite-vec consumes.

    sqlite-vec's vec0 virtual table accepts vectors either as JSON
    arrays or as raw little-endian float32 blobs. We use the blob
    encoding — it is the on-the-wire format pinned by the Phase 4
    migration integration tests (see
    ``tests/integration/test_phase4_migrations.py::_vector_blob``)
    and avoids per-element string parsing on the SQLite side.
    """
    return struct.pack(f"<{len(vector)}f", *vector)


def _blob_to_vec(blob: bytes) -> tuple[float, ...]:
    """Inverse of :func:`_vec_to_blob`.

    Used by :meth:`SqliteVecStore.recall` to materialise the vector
    column back into the ``tuple[float, ...]`` shape declared by
    :class:`~opshub.vectors.store.RecallHit`.
    """
    count = len(blob) // 4
    return struct.unpack(f"<{count}f", blob)
