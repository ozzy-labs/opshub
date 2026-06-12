"""End-to-end smoke for thread reply ingestion + search / recall surface.

Phase 20-E ([#478](https://github.com/ozzy-labs/opshub/issues/478)) audit
followup. Phase 20-A / 20-C shipped thread reply ingestion (parent +
``conversations.replies`` snapshot + Phase 2 late-reply polling) but no
end-to-end smoke pinned the load-bearing operator outcome: a thread
**child reply** must surface in:

* ``opshub search`` (FTS5 path) — operators / skills (``find-document``)
  reach this surface.
* ``recall.search`` (semantic path) — used by every brief-style skill
  (``personal-brief`` / ``next-actions`` / ``research``).
* The MCP ``search`` tool handler — the actual skill-layer entry point
  for ``find-document`` / ``research`` etc. (``raw_query`` hard-coded
  ``False`` per ADR-0022 改訂 §決定).

The Phase 20-C unit + integration suites pin the *connector-side*
behaviour (cursor advancement, polling cadence, prune semantics) but
none of them prove the resulting ``sources`` row actually lights up in
the search / recall surfaces that skills consume — that's the gap this
file closes.

Test fixtures here seed ``sources`` rows directly (mirroring
``test_phase15_search_japanese.py``) rather than driving the full
connector + Slack mock, because the contract under test is the
search-side observation: a thread reply row, no matter how it landed
in ``sources``, surfaces through the documented retrieval surfaces.
The connector → ``sources`` mapping is pinned in
``tests/unit/connectors/slack/test_mapper.py``; this module is a
deliberate seam between "the row exists" and "skills can find it".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.sources import sources_table

# ``sqlite_vec`` only matters for the recall path (migration 0013
# creates the ``embeddings_vec_*`` virtual table); the search-only
# tests would still pass without it. Skip the whole module rather
# than splitting markers so non-``[vector]`` environments don't see
# a half-running file. This mirrors ``test_recall_cli_lifecycle.py``.
pytest.importorskip("sqlite_vec")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"

_NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB at ``alembic upgrade head``.

    Mirrors :mod:`tests.integration.test_phase15_search_japanese` —
    keeps the test focused on the search / recall surface rather than
    the connector + CLI bracket. Disposes the engine on teardown so the
    tmp DB is properly closed before pytest tears the file down.
    """
    db_path = tmp_path / "phase20_search_thread_reply.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_slack_thread_pair(
    engine: Engine,
    *,
    channel_id: str = "C1",
    parent_ts: str = "1717000000.000100",
    reply_ts: str = "1717000600.000200",
    parent_body: str = "kickoff meeting tomorrow",
    reply_body: str = "deploy rollback action item assigned",
) -> tuple[str, str]:
    """Insert one parent + one reply ``sources`` row mirroring Slack ingestion.

    Returns ``(parent_source_id, reply_source_id)``. The reply row's
    ``external_id`` follows the ``f"{team_id}:{channel_id}:{ts}"``
    natural-key convention (Phase 24-B, ADR-0041 §(a); see
    :mod:`opshub.connectors.slack.mapper`). ``thread_ts``
    is intentionally **not** modelled in the projection — the connector
    stores it on the event's ``raw`` payload (per ADR-0030 §不変条件 #1)
    — the projection only carries the discriminator fields the search
    + recall paths consult.
    """
    parent_id = new_ulid()
    reply_id = new_ulid()
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=parent_id,
                connector_name="slack",
                external_id=f"T-int:{channel_id}:{parent_ts}",
                source_type="slack_message",
                title=f"alice in #general: {parent_body}",
                url=f"https://acme.slack.com/archives/{channel_id}/p{parent_ts.replace('.', '')}",
                summary=parent_body[:200],
                observed_at=_NOW,
                updated_at=_NOW,
                body=parent_body,
            )
        )
        conn.execute(
            insert(sources_table).values(
                id=reply_id,
                connector_name="slack",
                external_id=f"T-int:{channel_id}:{reply_ts}",
                source_type="slack_message",
                title=f"bob in #general: {reply_body}",
                url=f"https://acme.slack.com/archives/{channel_id}/p{reply_ts.replace('.', '')}",
                summary=reply_body[:200],
                observed_at=_NOW,
                updated_at=_NOW,
                body=reply_body,
            )
        )
    return parent_id, reply_id


# ---- G3.1: FTS5 surface ----------------------------------------------------


def test_opshub_search_fts5_finds_thread_reply_body(head_engine: Engine) -> None:
    """``SearchService`` returns the thread reply row when its body matches.

    The load-bearing assertion is that a *child* reply — distinct row
    from the thread parent, distinct ``body`` — surfaces under a body
    keyword that only appears in the reply. A regression that mapped
    thread replies into the parent's row (or dropped them entirely)
    would fail here because the reply-specific keyword would have no
    matching ``sources_fts`` entry.
    """
    from opshub.services.search_service import SearchService

    _, reply_id = _seed_slack_thread_pair(
        head_engine,
        parent_body="kickoff meeting tomorrow",
        reply_body="deploy rollback action item assigned to alice",
    )

    service = SearchService(engine=head_engine)

    hits = service.search("rollback", connector_name="slack")

    hit_ids = {hit.entity_id for hit in hits}
    assert reply_id in hit_ids, f"thread reply row not surfaced by FTS5 search (hits={hit_ids})"


def test_opshub_search_fts5_distinguishes_parent_and_reply_bodies(
    head_engine: Engine,
) -> None:
    """A parent-only keyword surfaces the parent; a reply-only keyword surfaces the reply.

    Defends against a regression that merged thread replies into the
    parent's body (an early-Phase-20 design alternative): the FTS5
    index would then index the merged text on the parent row alone,
    and the reply row would have no ``body`` to match against. This
    test pins that the two are independent ``sources`` rows with
    independent indexed bodies.
    """
    from opshub.services.search_service import SearchService

    parent_id, reply_id = _seed_slack_thread_pair(
        head_engine,
        parent_body="kickoff meeting tomorrow",
        reply_body="deploy rollback action item",
    )

    service = SearchService(engine=head_engine)

    # Parent-only keyword surfaces the parent row alone.
    parent_hits = {hit.entity_id for hit in service.search("kickoff")}
    assert parent_id in parent_hits
    assert reply_id not in parent_hits

    # Reply-only keyword surfaces the reply row alone.
    reply_hits = {hit.entity_id for hit in service.search("rollback")}
    assert reply_id in reply_hits
    assert parent_id not in reply_hits


# ---- G3.2: semantic recall surface ----------------------------------------


def test_recall_search_semantic_finds_thread_reply(head_engine: Engine) -> None:
    """``RecallService`` returns the thread reply hit when the vector store names it.

    Stubs out the embedder + vector store the way
    :mod:`tests.unit.services.test_recall_service` does so the test
    runs without a real backend. The contract under test is that the
    recall service's metadata-attachment path handles a ``source``
    entity hit pointing at the thread reply ``sources.id`` correctly —
    the title / snippet / score round-trip through the join, no
    silent drop. A regression that filtered out ``source`` rows whose
    ``thread_ts != ts`` (the way to *detect* a reply row) would fail
    here because the reply hit would be dropped from the result list.
    """
    from opshub.services.recall_service import RecallService
    from opshub.vectors.embedder import EmbeddingResult
    from opshub.vectors.store import RecallHit as VectorRecallHit
    from opshub.vectors.store import StoredEmbedding

    _, reply_id = _seed_slack_thread_pair(
        head_engine,
        reply_body="deploy rollback action item assigned to alice",
    )

    # Seed exactly one ``embeddings`` row so the active-model
    # existence check passes (any single row carrying the embedder's
    # identity is sufficient — see the unit suite docstring).
    with head_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO embeddings "
                "(entity_type, entity_id, model_id, model_version, "
                " dim, created_at) VALUES "
                "(:et, :eid, :mid, :mv, :dim, :ts)"
            ),
            {
                "et": "source",
                "eid": reply_id,
                "mid": "stub-embedder",
                "mv": "v1",
                "dim": 4,
                "ts": _NOW,
            },
        )

    class _StubEmbedder:
        model_id = "stub-embedder"
        model_version = "v1"
        dim = 4

        def embed(self, texts: list[str]) -> list[EmbeddingResult]:
            return [self.embed_one(t) for t in texts]

        def embed_one(self, text: str) -> EmbeddingResult:
            # Parameter name matches the :class:`~opshub.vectors.embedder.Embedder`
            # protocol signature (pyright strict checks positional-only
            # parameter name compatibility for Protocol conformance).
            del text
            return EmbeddingResult(
                vector=(0.1, 0.2, 0.3, 0.4),
                model_id=self.model_id,
                model_version=self.model_version,
                dim=self.dim,
            )

    class _StubVectorStore:
        def __init__(self, reply_entity_id: str) -> None:
            self._reply_entity_id = reply_entity_id

        def upsert(self, embeddings: list[StoredEmbedding]) -> None:
            del embeddings  # pragma: no cover

        def recall(
            self,
            query: tuple[float, ...],
            *,
            k: int,
            entity_types: list[str] | None = None,
        ) -> list[VectorRecallHit]:
            del query, k, entity_types
            return [
                VectorRecallHit(
                    entity_type="source",
                    entity_id=self._reply_entity_id,
                    score=0.95,
                    vector=(0.0, 0.0, 0.0, 0.0),
                )
            ]

        def recall_by_rowid(
            self,
            entity_type: str,
            entity_id: str,
            *,
            k: int,
            entity_types: list[str] | None = None,
        ) -> list[VectorRecallHit]:  # pragma: no cover - unused here
            del entity_type, entity_id, k, entity_types
            return []

        def count(self, *, entity_type: str | None = None) -> int:  # pragma: no cover
            del entity_type
            return 0

        def delete(self, *, entity_type: str, entity_id: str) -> int:  # pragma: no cover
            del entity_type, entity_id
            return 0

    service = RecallService(
        embedder=_StubEmbedder(),
        vector_store=_StubVectorStore(reply_id),
        engine=head_engine,
    )

    hits = service.recall("rollback action item", entity_type="source")

    assert len(hits) == 1
    assert hits[0].entity_id == reply_id
    # The metadata attachment round-trips the seeded title (reply body
    # excerpt + author/channel prefix).
    assert "rollback" in hits[0].title


# ---- G3.3: MCP ``search`` tool smoke --------------------------------------


def test_find_document_skill_returns_thread_reply(
    head_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP ``search`` tool handler surfaces the thread reply row.

    The ``find-document`` skill's MCP tool dependency is ``search``
    ([ADR-0022](docs/adr/0022-mcp-server-surface.md) §決定 (f)). The
    tool builds a :class:`SearchService` via
    ``opshub.cli._wiring.build_search_service`` and runs it with
    ``raw_query=False`` hard-coded (so the skill layer never has to
    escape FTS5 syntax). We monkey-patch ``build_search_service`` to
    point at our seeded ``head_engine`` instead of the operator's real
    DB; the rest of the path (handler → service → FTS5 → result
    formatting) runs unmodified so a regression in any of those
    layers surfaces here.
    """
    import asyncio

    from opshub.mcp._tools import build_search_handler
    from opshub.services.search_service import SearchService

    _, reply_id = _seed_slack_thread_pair(
        head_engine,
        reply_body="deploy rollback action item assigned to alice",
    )

    # ``build_search_service`` resolves an engine through the CLI
    # wiring — point it at our migrated tmp engine so the handler
    # reads the seeded rows. ``build_search_handler`` itself takes an
    # engine argument but only forwards it to ``build_search_service``
    # via discarded path; we patch the wiring helper for safety.
    def _fake_build_search_service() -> SearchService:
        return SearchService(engine=head_engine)

    monkeypatch.setattr(
        "opshub.cli._wiring.build_search_service",
        _fake_build_search_service,
    )

    handler = build_search_handler(head_engine)

    async def _invoke() -> str:
        # Wrap the handler call in a typed coroutine so mypy strict
        # sees ``Coroutine[Any, Any, str]`` (the ``ToolHandler`` type
        # alias only promises ``Awaitable[str]``, which ``asyncio.run``
        # does not accept directly under mypy strict — it expects a
        # ``Coroutine``).
        return await handler(
            {
                "query": "rollback",
                "connector_name": "slack",
                "limit": 10,
            }
        )

    payload_json: str = asyncio.run(_invoke())

    import json

    payload: dict[str, Any] = json.loads(payload_json)
    item_ids = {item["entity_id"] for item in payload["items"]}
    assert reply_id in item_ids, (
        f"MCP search handler did not surface thread reply row (items={payload['items']})"
    )
    # The skill layer keys on ``connector_name`` to bucket results by
    # SaaS — pin that the reply row carries the slack discriminator.
    reply_item = next(item for item in payload["items"] if item["entity_id"] == reply_id)
    assert reply_item["connector_name"] == "slack"
    assert reply_item["source_type"] == "slack_message"
