"""Phase 12 H1 regression tests for the new MCP tool handlers.

Pins three deliverables from ``docs/phase-12-plan.md`` §3 H1-b:

1. **Physical-column time filters** on the four list handlers
   (``task.list`` / ``inbox.list`` / ``decision.list`` / ``source.list``).
   The mapping table below is the SSOT — each tool uses its own
   ``*_after`` / ``*_before`` argument names so the MCP boundary
   matches the projection physical columns 1:1.

2. **``search`` (FTS5)** MCP tool exposes the body-level search
   surface without the CLI's ``--raw-query`` flag. Phrase quoting
   stays default so host LLMs can pass free-form token streams.

3. **``propose.apply`` idempotency normalisation** — a second call
   with the same ``(proposal_id, candidate_index)`` returns
   ``{ok: true, already_applied: true, applied_entity_type,
   applied_entity_id}`` instead of raising
   ``OpsHubError("already applied")``. This is the contract
   advertised by the registry's ``idempotent=true`` annotation
   (see ``_policy_for_propose_apply``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import insert
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import events_table
from opshub.mcp._tools import (
    build_decision_list_handler,
    build_inbox_list_handler,
    build_source_list_handler,
    build_task_list_handler,
)
from opshub.mcp._writes import build_propose_apply_handler
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table

_BASE = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB with the four projection tables + events."""
    db_path = tmp_path / "phase12.sqlite"
    eng = create_engine_for_sqlite(db_path)
    try:
        tasks_table.create(eng)
        inbox_items_table.create(eng)
        decisions_table.create(eng)
        sources_table.create(eng)
        events_table.create(eng)
        yield eng
    finally:
        eng.dispose()


def _parse(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# 1. Time-filter regression — task.list / inbox.list / decision.list / source.list
# ---------------------------------------------------------------------------


def _seed_task_at(engine: Engine, *, task_id: str, when: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(tasks_table).values(
                id=task_id,
                title=f"task-{task_id[-3:]}",
                body=None,
                state="active",
                result_note=None,
                created_at=when,
                updated_at=when,
            )
        )


def _seed_inbox_at(engine: Engine, *, item_id: str, when: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(inbox_items_table).values(
                id=item_id,
                summary=f"inbox-{item_id[-3:]}",
                source_ref=None,
                state="pending",
                disposition=None,
                target_id=None,
                reason=None,
                created_at=when,
                updated_at=when,
            )
        )


def _seed_decision_at(engine: Engine, *, decision_id: str, when: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(decisions_table).values(
                id=decision_id,
                text=f"decision-{decision_id[-3:]}",
                context=None,
                actor="test",
                recorded_at=when,
            )
        )


def _seed_source_at(engine: Engine, *, source_id: str, when: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name="probe",
                external_id=source_id,
                source_type="probe_event",
                title=f"src-{source_id[-3:]}",
                url=None,
                summary=None,
                body=None,
                provenance_origin=None,
                provenance_trust=None,
                observed_at=when,
                updated_at=when,
            )
        )


async def test_task_list_updated_after_excludes_older_rows(engine: Engine) -> None:
    """``updated_after`` half-open lower bound on ``tasks.updated_at``."""
    _seed_task_at(engine, task_id="01HTASKAAAAAAAAAAAAAAAAAA1", when=_BASE - timedelta(days=2))
    _seed_task_at(engine, task_id="01HTASKAAAAAAAAAAAAAAAAAA2", when=_BASE + timedelta(days=1))
    handler = build_task_list_handler(engine)
    cutoff = (_BASE - timedelta(hours=12)).isoformat()
    payload = _parse(await handler({"updated_after": cutoff, "limit": 50}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    ids = [row["id"] for row in rows]
    assert "01HTASKAAAAAAAAAAAAAAAAAA2" in ids
    assert "01HTASKAAAAAAAAAAAAAAAAAA1" not in ids


async def test_task_list_updated_before_excludes_newer_rows(engine: Engine) -> None:
    """``updated_before`` half-open upper bound on ``tasks.updated_at``."""
    _seed_task_at(engine, task_id="01HTASKBBBBBBBBBBBBBBBBBB1", when=_BASE - timedelta(days=2))
    _seed_task_at(engine, task_id="01HTASKBBBBBBBBBBBBBBBBBB2", when=_BASE + timedelta(days=1))
    handler = build_task_list_handler(engine)
    cutoff = _BASE.isoformat()
    payload = _parse(await handler({"updated_before": cutoff, "limit": 50}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    ids = [row["id"] for row in rows]
    assert "01HTASKBBBBBBBBBBBBBBBBBB1" in ids
    assert "01HTASKBBBBBBBBBBBBBBBBBB2" not in ids


async def test_inbox_list_created_after_filter(engine: Engine) -> None:
    _seed_inbox_at(engine, item_id="01HINBOXAAAAAAAAAAAAAAAAA1", when=_BASE - timedelta(days=2))
    _seed_inbox_at(engine, item_id="01HINBOXAAAAAAAAAAAAAAAAA2", when=_BASE + timedelta(hours=1))
    handler = build_inbox_list_handler(engine)
    cutoff = _BASE.isoformat()
    payload = _parse(await handler({"created_after": cutoff, "limit": 50}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    ids = [row["id"] for row in rows]
    assert ids == ["01HINBOXAAAAAAAAAAAAAAAAA2"]


async def test_decision_list_recorded_before_filter(engine: Engine) -> None:
    _seed_decision_at(
        engine, decision_id="01HDECAAAAAAAAAAAAAAAAAAAA", when=_BASE - timedelta(days=3)
    )
    _seed_decision_at(
        engine, decision_id="01HDECBBBBBBBBBBBBBBBBBBBB", when=_BASE + timedelta(days=3)
    )
    handler = build_decision_list_handler(engine)
    cutoff = _BASE.isoformat()
    payload = _parse(await handler({"recorded_before": cutoff, "limit": 50}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    ids = [row["id"] for row in rows]
    assert ids == ["01HDECAAAAAAAAAAAAAAAAAAAA"]


async def test_source_list_observed_after_filter(engine: Engine) -> None:
    _seed_source_at(engine, source_id="01HSRCAAAAAAAAAAAAAAAAAAAA", when=_BASE - timedelta(days=2))
    _seed_source_at(engine, source_id="01HSRCBBBBBBBBBBBBBBBBBBBB", when=_BASE + timedelta(hours=2))
    handler = build_source_list_handler(engine)
    cutoff = _BASE.isoformat()
    payload = _parse(await handler({"observed_after": cutoff, "limit": 50}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    ids = [row["id"] for row in rows]
    assert ids == ["01HSRCBBBBBBBBBBBBBBBBBBBB"]


async def test_time_filter_accepts_z_suffix(engine: Engine) -> None:
    """``Z`` suffix is accepted alongside explicit ``+00:00`` offset."""
    _seed_task_at(engine, task_id="01HTASKCCCCCCCCCCCCCCCCCC1", when=_BASE + timedelta(days=1))
    handler = build_task_list_handler(engine)
    payload = _parse(await handler({"updated_after": "2026-05-30T00:00:00Z", "limit": 5}))
    rows = cast("list[dict[str, Any]]", payload["items"])
    assert any(r["id"] == "01HTASKCCCCCCCCCCCCCCCCCC1" for r in rows)


# ---------------------------------------------------------------------------
# 2. propose.apply idempotency normalisation
# ---------------------------------------------------------------------------


class _StubProposalService:
    """Minimal :class:`ProposalService` stand-in for the apply handler.

    Mirrors the service's real behaviour for two paths:

    * first ``apply`` call → returns ``(applied_entity_type,
      applied_entity_id)`` and records a ``ProposalApplied`` event in
      the in-memory event log (the handler's idempotency lookup walks
      the event log to recover the tuple on the second call).
    * second ``apply`` call → raises
      ``OpsHubError("candidate 0 already applied")`` matching the
      service's literal wording so the handler's substring match
      triggers normalisation.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._applied: dict[tuple[str, int], tuple[str, str]] = {}

    def apply(self, proposal_id: str, candidate_index: int) -> tuple[str, str]:
        from opshub.core.errors import OpsHubError

        key = (proposal_id, candidate_index)
        if key in self._applied:
            raise OpsHubError(f"candidate {candidate_index} already applied")
        result = ("task", "01HTASKAPPLIED00000000000")
        self._applied[key] = result
        # Mimic SqlAlchemyEventStore.append by inserting a row into
        # ``events_table`` with the same shape ``_lookup_applied_entity``
        # expects. The discriminator must match the Pydantic event
        # class :class:`opshub.domain.events.proposal.ProposalApplied`
        # which declares ``event_type: Literal["proposal.applied"]``
        # (dot-notation, ADR-0002 event naming). A Phase 12 H6 e2e
        # test surfaced an earlier mismatch where this stub used
        # ``"ProposalApplied"`` and silently masked the lookup bug.
        with self._engine.begin() as conn:
            conn.execute(
                insert(events_table).values(
                    id="01HEVTAPPLIED0000000000000",
                    aggregate_id=proposal_id,
                    event_type="proposal.applied",
                    schema_version=1,
                    occurred_at=_BASE,
                    recorded_at=_BASE,
                    actor="test:apply",
                    payload=json.dumps(
                        {
                            "candidate_index": candidate_index,
                            "applied_entity_type": result[0],
                            "applied_entity_id": result[1],
                        }
                    ),
                )
            )
        return result


@pytest.fixture
def patched_apply_handler(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire ``build_propose_apply_handler`` against the stub service.

    The handler defers ``build_proposal_service`` to call-time so a
    monkeypatch on the wiring helper is enough — no need to touch
    the underlying service registry.
    """
    stub = _StubProposalService(engine)

    def _factory(actor: str = "cli:propose") -> _StubProposalService:
        _ = actor
        return stub

    # The handler imports ``build_proposal_service`` lazily from
    # ``opshub.cli._wiring`` so patch there.
    monkeypatch.setattr("opshub.cli._wiring.build_proposal_service", _factory, raising=True)
    return build_propose_apply_handler(engine)


async def test_propose_apply_first_call_returns_not_already_applied(
    patched_apply_handler: Any,
) -> None:
    """First apply: ``already_applied=false`` + entity tuple."""
    args: Mapping[str, Any] = {"proposal_id": "01HPROP1", "candidate_index": 0}
    payload = _parse(await patched_apply_handler(args))
    assert payload["ok"] is True
    assert payload["already_applied"] is False
    assert payload["applied_entity_type"] == "task"
    assert payload["applied_entity_id"] == "01HTASKAPPLIED00000000000"
    assert payload["proposal_id"] == "01HPROP1"
    assert payload["candidate_index"] == 0


async def test_propose_apply_second_call_normalises_to_already_applied(
    patched_apply_handler: Any,
) -> None:
    """Second call for the same ``(proposal_id, candidate_index)`` returns
    ``already_applied=true`` rather than raising ``OpsHubError``.

    This is the contract that makes ``annotations.idempotentHint=true``
    honest at the MCP boundary (ADR-0022 改訂 §決定).
    """
    args: Mapping[str, Any] = {"proposal_id": "01HPROP2", "candidate_index": 0}
    await patched_apply_handler(args)
    payload = _parse(await patched_apply_handler(args))
    assert payload["ok"] is True
    assert payload["already_applied"] is True
    assert payload["applied_entity_type"] == "task"
    assert payload["applied_entity_id"] == "01HTASKAPPLIED00000000000"


async def test_propose_apply_propagates_already_rejected(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``already rejected`` is NOT normalised — propagates as MCP isError."""
    from opshub.core.errors import OpsHubError

    class _RejectingService:
        def apply(self, proposal_id: str, candidate_index: int) -> tuple[str, str]:
            _ = proposal_id
            raise OpsHubError(f"candidate {candidate_index} already rejected")

    def _factory(actor: str = "cli:propose") -> _RejectingService:
        _ = actor
        return _RejectingService()

    monkeypatch.setattr("opshub.cli._wiring.build_proposal_service", _factory, raising=True)
    handler = build_propose_apply_handler(engine)
    with pytest.raises(OpsHubError, match="already rejected"):
        await handler({"proposal_id": "01HPROP3", "candidate_index": 0})


def test_lookup_applied_entity_event_type_matches_pydantic_literal() -> None:
    """Pin the ``event_type`` discriminator string used by ``_lookup_applied_entity``
    against the Pydantic event class literal.

    Phase 12 H6 surfaced an earlier mismatch where the lookup filtered
    on ``"ProposalApplied"`` while the
    :class:`opshub.domain.events.proposal.ProposalApplied` class
    declares ``event_type: Literal["proposal.applied"]`` (dot-notation,
    ADR-0002 event naming). The mismatch silently masked every
    historical apply lookup, causing the second ``propose.apply`` call
    to raise ``OpsHubError("already applied")`` instead of returning
    ``already_applied=true``. A symbolic source-grep guards the drift
    so a future rename of the Pydantic literal is caught immediately.
    """
    import re
    from pathlib import Path

    from opshub.domain.events.proposal import ProposalApplied

    # Default value of the Literal[...] field
    pydantic_literal = ProposalApplied.model_fields["event_type"].default
    assert pydantic_literal == "proposal.applied"

    writes_src = (
        Path(__file__).resolve().parents[3] / "src" / "opshub" / "mcp" / "_writes.py"
    ).read_text(encoding="utf-8")
    # Capture the event_type literal threaded into the lookup query.
    match = re.search(
        r'events_table\.c\.event_type\s*==\s*"([^"]+)"',
        writes_src,
    )
    assert match is not None, (
        'could not find `events_table.c.event_type == "..."` in mcp/_writes.py'
    )
    assert match.group(1) == pydantic_literal, (
        "mcp/_writes.py event_type filter must match the Pydantic"
        f" literal {pydantic_literal!r}; got {match.group(1)!r}"
    )


# ---------------------------------------------------------------------------
# 3. search handler — phrase quoting is on; raw_query is not exposed
# ---------------------------------------------------------------------------


async def test_search_handler_hard_codes_raw_query_false(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_search_handler`` must call SearchService with ``raw_query=False``.

    ADR-0022 改訂 §決定 pins this contract. We assert via a stub that
    records the ``raw_query`` argument and confirm the handler never
    threads ``raw_query=True`` through, regardless of what host
    arguments are passed (the schema does not accept ``raw_query``,
    but a future schema regression must still fail this test).
    """
    from opshub.mcp._tools import build_search_handler
    from opshub.services.search_service import SearchHit

    captured: dict[str, Any] = {}

    class _StubSearchService:
        def search(
            self,
            query_text: str,
            *,
            limit: int = 10,
            connector_name: str | None = None,
            raw_query: bool = False,
        ) -> list[SearchHit]:
            captured["query_text"] = query_text
            captured["limit"] = limit
            captured["connector_name"] = connector_name
            captured["raw_query"] = raw_query
            return [
                SearchHit(
                    entity_id="01HSRC0000000000000000000A",
                    connector_name="probe",
                    source_type="probe_event",
                    title="probe title",
                    url=None,
                    snippet="probe snippet",
                    score=1.0,
                )
            ]

    def _factory() -> _StubSearchService:
        return _StubSearchService()

    monkeypatch.setattr("opshub.cli._wiring.build_search_service", _factory, raising=True)

    handler = build_search_handler(engine)
    payload = _parse(
        await handler({"query": "phase 12 plan", "connector_name": "probe", "limit": 5})
    )
    assert captured["query_text"] == "phase 12 plan"
    assert captured["connector_name"] == "probe"
    assert captured["limit"] == 5
    assert captured["raw_query"] is False, (
        "search handler must hard-code raw_query=False at the MCP boundary"
    )
    assert payload["items"][0]["entity_id"] == "01HSRC0000000000000000000A"
    assert payload["connector_filter"] == "probe"


async def test_propose_apply_propagates_unknown_proposal(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrelated ``OpsHubError`` (not found / out of range) propagates."""
    from opshub.core.errors import OpsHubError

    class _UnknownService:
        def apply(self, proposal_id: str, candidate_index: int) -> tuple[str, str]:
            _ = candidate_index
            raise OpsHubError(f"proposal {proposal_id} not found")

    def _factory(actor: str = "cli:propose") -> _UnknownService:
        _ = actor
        return _UnknownService()

    monkeypatch.setattr("opshub.cli._wiring.build_proposal_service", _factory, raising=True)
    handler = build_propose_apply_handler(engine)
    with pytest.raises(OpsHubError, match="not found"):
        await handler({"proposal_id": "01HUNKNOWN", "candidate_index": 0})
