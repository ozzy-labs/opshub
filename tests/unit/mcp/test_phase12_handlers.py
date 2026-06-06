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
                # epic #470 / issue #481: ``sources.body`` is NOT NULL.
                body=f"src-{source_id[-3:]}",
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
# 1b. Time-filter boundary regression — M6 audit cluster B
# ---------------------------------------------------------------------------
#
# Phase 12 audit Cluster B (M6) calls out an asymmetric coverage gap in
# the four list handlers: each tool only had partial after/before
# coverage (``task.list`` had both, ``inbox.list`` after-only,
# ``decision.list`` before-only, ``source.list`` after-only) plus
# limited tz parsing coverage (only ``Z`` suffix). The block below
# pins the half-open ``>= after`` / ``< before`` semantics symmetrically
# across all four tools plus the empty-result edge cases (after == before
# and after > before — neither should raise, both should return empty)
# and the explicit-offset / naive / malformed tz parsing paths.

# Tool descriptor: (handler builder, seeder, after-arg, before-arg).
_LIST_TOOL_TIME_FILTERS: tuple[tuple[str, Any, Any, str, str], ...] = (
    (
        "task.list",
        build_task_list_handler,
        _seed_task_at,
        "updated_after",
        "updated_before",
    ),
    (
        "inbox.list",
        build_inbox_list_handler,
        _seed_inbox_at,
        "created_after",
        "created_before",
    ),
    (
        "decision.list",
        build_decision_list_handler,
        _seed_decision_at,
        "recorded_after",
        "recorded_before",
    ),
    (
        "source.list",
        build_source_list_handler,
        _seed_source_at,
        "observed_after",
        "observed_before",
    ),
)


def _seed_for(
    tool_name: str, seeder: Any, engine: Engine, *, entity_id: str, when: datetime
) -> None:
    """Tool-specific kwarg name shim: each seeder uses a different id kwarg."""
    kwarg = {
        "task.list": "task_id",
        "inbox.list": "item_id",
        "decision.list": "decision_id",
        "source.list": "source_id",
    }[tool_name]
    seeder(engine, **{kwarg: entity_id}, when=when)


def _entity_id_for(tool_name: str, suffix: str) -> str:
    """Build a deterministic 26-char ULID-shaped id keyed by tool + suffix."""
    prefix_map = {
        "task.list": "01HTASKBOUND",
        "inbox.list": "01HINBOXBOUND",
        "decision.list": "01HDECBOUND",
        "source.list": "01HSRCBOUND",
    }
    base = prefix_map[tool_name] + suffix
    return base.ljust(26, "Z")[:26]


@pytest.mark.parametrize(
    "tool_name,build_handler,seeder,after_arg,before_arg",
    _LIST_TOOL_TIME_FILTERS,
    ids=[t[0] for t in _LIST_TOOL_TIME_FILTERS],
)
async def test_list_after_and_before_window_is_half_open(
    engine: Engine,
    tool_name: str,
    build_handler: Any,
    seeder: Any,
    after_arg: str,
    before_arg: str,
) -> None:
    """Half-open window ``[after, before)`` symmetric across all 4 tools.

    M6 audit gap pin (Phase 12 Cluster B): every list handler's
    physical-column time filter must implement ``>= after`` / ``< before``.
    A row planted exactly at ``after`` IS included; a row planted
    exactly at ``before`` is NOT. Rows strictly outside the window
    drop out on both sides. Pins the inequality direction so a
    regression that swaps ``<`` for ``<=`` (or vice-versa) is caught
    for every tool, not just the one with the most coverage.
    """
    # Seed four rows: outside-low, exactly-at-after, exactly-at-before, outside-high.
    boundary_lo = _BASE
    boundary_hi = _BASE + timedelta(days=2)
    outside_lo_id = _entity_id_for(tool_name, "OL")
    at_after_id = _entity_id_for(tool_name, "AA")
    at_before_id = _entity_id_for(tool_name, "AB")
    outside_hi_id = _entity_id_for(tool_name, "OH")

    _seed_for(
        tool_name, seeder, engine, entity_id=outside_lo_id, when=boundary_lo - timedelta(hours=1)
    )
    _seed_for(tool_name, seeder, engine, entity_id=at_after_id, when=boundary_lo)
    _seed_for(tool_name, seeder, engine, entity_id=at_before_id, when=boundary_hi)
    _seed_for(
        tool_name, seeder, engine, entity_id=outside_hi_id, when=boundary_hi + timedelta(hours=1)
    )

    handler = build_handler(engine)
    args: dict[str, Any] = {
        after_arg: boundary_lo.isoformat(),
        before_arg: boundary_hi.isoformat(),
        "limit": 50,
    }
    payload = _parse(await handler(args))
    ids = {row["id"] for row in cast("list[dict[str, Any]]", payload["items"])}

    assert at_after_id in ids, (
        f"{tool_name!r}: row at exactly ``after`` boundary must be included"
        " (half-open lower bound is inclusive: ``>= after``)"
    )
    assert at_before_id not in ids, (
        f"{tool_name!r}: row at exactly ``before`` boundary must be excluded"
        " (half-open upper bound is exclusive: ``< before``)"
    )
    assert outside_lo_id not in ids, (
        f"{tool_name!r}: row strictly before the window must be excluded"
    )
    assert outside_hi_id not in ids, (
        f"{tool_name!r}: row strictly after the window must be excluded"
    )


@pytest.mark.parametrize(
    "tool_name,build_handler,seeder,after_arg,before_arg",
    _LIST_TOOL_TIME_FILTERS,
    ids=[t[0] for t in _LIST_TOOL_TIME_FILTERS],
)
async def test_list_after_equals_before_returns_empty(
    engine: Engine,
    tool_name: str,
    build_handler: Any,
    seeder: Any,
    after_arg: str,
    before_arg: str,
) -> None:
    """``after == before`` produces an empty set (degenerate half-open).

    M6 audit pin: the half-open ``[t, t)`` interval is the empty set
    by definition. The handler must NOT raise on this degenerate
    boundary — it must return zero rows so a caller passing the same
    instant for both bounds gets a clean empty list rather than an
    error.
    """
    _seed_for(tool_name, seeder, engine, entity_id=_entity_id_for(tool_name, "EQ"), when=_BASE)

    handler = build_handler(engine)
    boundary = _BASE.isoformat()
    args: dict[str, Any] = {
        after_arg: boundary,
        before_arg: boundary,
        "limit": 50,
    }
    payload = _parse(await handler(args))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert items == [], (
        f"{tool_name!r}: ``after == before`` is the empty half-open interval"
        f" — handler must return [] (no error). Got {items!r}"
    )


@pytest.mark.parametrize(
    "tool_name,build_handler,seeder,after_arg,before_arg",
    _LIST_TOOL_TIME_FILTERS,
    ids=[t[0] for t in _LIST_TOOL_TIME_FILTERS],
)
async def test_list_after_greater_than_before_returns_empty(
    engine: Engine,
    tool_name: str,
    build_handler: Any,
    seeder: Any,
    after_arg: str,
    before_arg: str,
) -> None:
    """``after > before`` returns empty without raising (defensive contract).

    M6 audit pin: callers may construct windows from natural-language
    inputs ("between yesterday and tomorrow") that parse to a
    backwards range. The handler MUST treat this as an empty result
    rather than a hard error — agent hosts retry on errors and would
    burn turns on the malformed range. Empty result + caller-side
    sanity check is the documented contract.
    """
    _seed_for(tool_name, seeder, engine, entity_id=_entity_id_for(tool_name, "GT"), when=_BASE)

    handler = build_handler(engine)
    args: dict[str, Any] = {
        after_arg: (_BASE + timedelta(days=2)).isoformat(),
        before_arg: _BASE.isoformat(),
        "limit": 50,
    }
    payload = _parse(await handler(args))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert items == [], (
        f"{tool_name!r}: ``after > before`` must return [] (not raise). Got {items!r}"
    )


# ----- tz parsing edge cases -----


async def test_time_filter_accepts_explicit_offset(engine: Engine) -> None:
    """Explicit ``+09:00`` (JST) offset parses without raising.

    M6 audit pin: ``_parse_iso`` must accept arbitrary explicit
    offsets — not just ``Z`` / ``+00:00``. The handler relies on
    :py:meth:`datetime.datetime.fromisoformat` which honours every
    ISO 8601 offset since Python 3.13; the pin guards against a
    future refactor that re-introduces a naive ``Z``-only shortcut
    that would reject explicit-offset strings outright.

    Comparison semantics across tz offsets vs. SQLite's tz-naive
    storage are the caller's responsibility (the canonical opshub
    pattern is to normalise to UTC before serialising). What this
    pin guards is the **parse path**: the handler must not raise
    on a well-formed ``+09:00`` cutoff.
    """
    _seed_task_at(engine, task_id="01HTASKTZJST0000000000000Z", when=_BASE + timedelta(days=1))
    handler = build_task_list_handler(engine)
    # The handler must accept the explicit offset and complete normally
    # — we assert only on the envelope shape, not on row inclusion,
    # because SQLite stores DateTime(timezone=True) as a tz-naive
    # ISO string and the cross-tz comparison is lex-string against the
    # caller's serialised offset (out of scope for this pin).
    payload = _parse(await handler({"updated_after": "2026-05-31T00:00:00+09:00", "limit": 50}))
    assert "items" in payload, payload


async def test_time_filter_accepts_naive_string(engine: Engine) -> None:
    """A tz-naive ISO 8601 string parses (no exception) — caller-side concern.

    M6 audit pin: the handler does not reject tz-naive input
    (``2026-05-30T12:00:00`` without an offset). SQLite's datetime
    comparison is string-lexicographic for naive values and the
    sqlalchemy DateTime(timezone=True) column accepts the comparison
    — what matters for the boundary contract is that the handler
    does not raise.
    """
    _seed_task_at(engine, task_id="01HTASKNAIVE000000000000Z", when=_BASE + timedelta(days=1))
    handler = build_task_list_handler(engine)
    # No assertion on rows — the documented contract is "does not raise".
    # If a future refactor decides to reject tz-naive input the test
    # below (malformed) keeps the strict error path covered.
    payload = _parse(await handler({"updated_after": "2026-05-30T00:00:00", "limit": 50}))
    assert "items" in payload


async def test_time_filter_rejects_malformed_string(engine: Engine) -> None:
    """Malformed ISO 8601 surfaces as ``ValueError`` (not silent NOOP).

    M6 audit pin: ``_parse_iso`` calls ``datetime.fromisoformat``
    directly, which raises ``ValueError`` on garbage input. The
    handler propagates that — the MCP server wrapper renders it as
    an ``isError`` response. A regression that swallows the parse
    error and silently drops the filter would expand the result set
    way past the caller's intent and is exactly what this guard
    catches.
    """
    handler = build_task_list_handler(engine)
    with pytest.raises(ValueError):
        await handler({"updated_after": "not-a-valid-iso-8601-string", "limit": 5})


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

    ``applied_entity_type`` / ``applied_entity_id`` are parametrised so
    the per-kind idempotency lookup (``_lookup_applied_entity`` in
    ``src/opshub/mcp/_writes.py``) is observably exercised for every
    Phase 12 H1 candidate kind — ``task`` / ``decision`` / ``reply_draft``
    each writes a distinct ``applied_entity_type`` payload and the
    handler MUST recover that string verbatim on the second call.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        applied_entity_type: str = "task",
        applied_entity_id: str = "01HTASKAPPLIED00000000000",
        event_id: str = "01HEVTAPPLIED0000000000000",
    ) -> None:
        self._engine = engine
        self._applied: dict[tuple[str, int], tuple[str, str]] = {}
        self._applied_entity_type = applied_entity_type
        self._applied_entity_id = applied_entity_id
        self._event_id = event_id

    def apply(self, proposal_id: str, candidate_index: int) -> tuple[str, str]:
        from opshub.core.errors import OpsHubError

        key = (proposal_id, candidate_index)
        if key in self._applied:
            raise OpsHubError(f"candidate {candidate_index} already applied")
        result = (self._applied_entity_type, self._applied_entity_id)
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
                    id=self._event_id,
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


async def test_propose_apply_second_call_returns_decision_entity(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-kind idempotency pin — ``decision`` candidate normalisation.

    The H3 contract (Phase 12 audit Cluster B): the handler's
    ``_lookup_applied_entity`` (`src/opshub/mcp/_writes.py`) walks the
    event log and reads ``applied_entity_type`` straight out of the
    ``proposal.applied`` payload. When the first apply created a
    ``decision`` (not a ``task``), the second call MUST return
    ``applied_entity_type="decision"`` verbatim — not the literal
    ``"task"`` default that the original stub hard-coded. A
    regression that hard-codes ``"task"`` somewhere in the lookup
    path (e.g. via a default in the SELECT clause) is exactly what
    this guard catches.
    """
    stub = _StubProposalService(
        engine,
        applied_entity_type="decision",
        applied_entity_id="01HDECAPPLIED0000000000000",
        event_id="01HEVTAPPLIEDDEC000000000",
    )

    def _factory(actor: str = "cli:propose") -> _StubProposalService:
        _ = actor
        return stub

    monkeypatch.setattr("opshub.cli._wiring.build_proposal_service", _factory, raising=True)
    handler = build_propose_apply_handler(engine)

    args: Mapping[str, Any] = {"proposal_id": "01HPROPDEC1", "candidate_index": 0}
    first = _parse(await handler(args))
    assert first["already_applied"] is False
    assert first["applied_entity_type"] == "decision"
    assert first["applied_entity_id"] == "01HDECAPPLIED0000000000000"

    second = _parse(await handler(args))
    assert second["ok"] is True
    assert second["already_applied"] is True
    assert second["applied_entity_type"] == "decision", (
        "second call must recover ``decision`` (not ``task``) from the"
        " event-log lookup — _lookup_applied_entity must read the"
        " applied_entity_type field from the persisted payload"
    )
    assert second["applied_entity_id"] == "01HDECAPPLIED0000000000000"


async def test_propose_apply_second_call_returns_reply_draft_entity(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-kind idempotency pin — ``reply_draft`` candidate normalisation.

    Phase 10 step E2 added the ``reply_draft`` candidate kind
    (ADR-0016 §決定 (i)). The H3 contract requires
    ``_lookup_applied_entity`` to recover ``reply_draft`` for the
    second call exactly like it does for ``task`` / ``decision`` —
    no kind-specific carve-out, no string-matching on ``"task"``.
    """
    stub = _StubProposalService(
        engine,
        applied_entity_type="reply_draft",
        applied_entity_id="01HREPLYAPPLIED000000000R",
        event_id="01HEVTAPPLIEDRPL000000000",
    )

    def _factory(actor: str = "cli:propose") -> _StubProposalService:
        _ = actor
        return stub

    monkeypatch.setattr("opshub.cli._wiring.build_proposal_service", _factory, raising=True)
    handler = build_propose_apply_handler(engine)

    args: Mapping[str, Any] = {"proposal_id": "01HPROPRPL1", "candidate_index": 0}
    first = _parse(await handler(args))
    assert first["already_applied"] is False
    assert first["applied_entity_type"] == "reply_draft"

    second = _parse(await handler(args))
    assert second["ok"] is True
    assert second["already_applied"] is True
    assert second["applied_entity_type"] == "reply_draft", (
        "second call must recover ``reply_draft`` from the event-log"
        " lookup; the handler must not assume a single canonical kind"
    )
    assert second["applied_entity_id"] == "01HREPLYAPPLIED000000000R"


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


# ---------------------------------------------------------------------------
# 4. search MCP tool — real FTS5 end-to-end (H4 audit Cluster B)
# ---------------------------------------------------------------------------
#
# The lifecycle integration test (``tests/integration/test_phase12_assistant_lifecycle``)
# covers ``search`` against a fully-migrated SQLite DB, but the
# unit-level link from the registry's ``build_search_handler`` factory
# down to the real :class:`SearchService` was only stubbed (see
# ``test_search_handler_hard_codes_raw_query_false`` above). H4 audit
# Cluster B asks for a unit-level pin that exercises the real
# SearchService against a seeded FTS5 index so a regression that
# (a) breaks the registry → SearchService wiring, (b) flips phrase
# quoting off, or (c) drops a row from sources_fts surfaces here.


def _bootstrap_fts_index(engine: Engine) -> None:
    """Create the ``sources_fts`` virtual table + sync triggers.

    Mirrors the head migration state (the alembic-only path is too
    heavy for a unit test that already created the projection tables
    via Table.create()). We bootstrap just enough for the
    SearchService MATCH query to land — the virtual table + the AFTER
    INSERT trigger so seeded rows show up. Phase 15 S3 (#359) syncs
    the tokenizer to ``trigram`` to match migration
    ``0028_rebuild_sources_fts_trigram`` so query semantics are
    identical to production after the Phase 15 S2 supersede; before
    the sync this helper still spun up the Phase 10 ``unicode61
    remove_diacritics 2`` tokenizer and would have silently diverged
    from production MATCH behaviour.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5("
                "body, content='sources', content_rowid='rowid',"
                " tokenize='trigram'"
                ")"
            )
        )
        # Sync trigger: every new ``sources`` row lands a matching FTS doc.
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS sources_fts_ai AFTER INSERT ON sources BEGIN "
                "INSERT INTO sources_fts(rowid, body) VALUES (new.rowid, new.body); "
                "END"
            )
        )


def _seed_source_with_body(
    engine: Engine,
    *,
    source_id: str,
    body: str,
    connector_name: str = "github",
    title: str = "seeded source",
) -> None:
    """Insert one fully-populated ``sources`` row (triggers fill FTS)."""
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name=connector_name,
                external_id=source_id,
                source_type="issue",
                title=title,
                url=None,
                summary=body[:200],
                body=body,
                provenance_origin="external",
                provenance_trust="untrusted",
                observed_at=_BASE,
                updated_at=_BASE,
            )
        )


async def test_search_handler_returns_hits_via_real_searchservice(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: registry → ``build_search_handler`` → real ``SearchService``.

    Phase 12 H4 audit Cluster B (high severity): the unit suite stubs
    ``SearchService`` everywhere, so a regression that breaks the
    registry → SearchService wiring (e.g. ``build_search_service``
    constructed against the wrong engine) would only surface in the
    lifecycle integration test. This pin runs the real ``SearchService``
    against a freshly-seeded FTS5 index so the entire vertical slice
    is covered by a fast unit test.

    The seeded body intentionally contains tokens that look like FTS5
    syntax glyphs (parentheses); phrase quoting MUST handle them so
    the call returns a hit instead of raising
    ``sqlite3.OperationalError: fts5: syntax error``.
    """
    _bootstrap_fts_index(engine)
    _seed_source_with_body(
        engine,
        source_id="01HSRCFTSA00000000000000A",
        body="Phase 12 plan covers find_document() and the FTS5 surface.",
        title="search-fts-a",
    )
    _seed_source_with_body(
        engine,
        source_id="01HSRCFTSB00000000000000B",
        body="Unrelated entry about workflow scheduling.",
        title="search-fts-b",
    )

    # Force the registry's lazy ``build_search_service`` import to wire
    # the real service against the test engine — without the override,
    # it would resolve a fresh engine from the user's settings.
    from opshub.services.search_service import SearchService

    def _factory() -> SearchService:
        return SearchService(engine=engine)

    monkeypatch.setattr("opshub.cli._wiring.build_search_service", _factory, raising=True)

    from opshub.mcp._tools import build_search_handler

    handler = build_search_handler(engine)
    # Free-form multi-token query — phrase quoting is the default per
    # ADR-0022 改訂 §決定 (f-1). The MATCH must succeed and return the
    # row whose body contains the literal phrase.
    payload = _parse(await handler({"query": "Phase 12 plan", "limit": 5}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert len(items) >= 1, payload
    ids = {row["entity_id"] for row in items}
    assert "01HSRCFTSA00000000000000A" in ids
    # The unrelated row stays out — confirms the MATCH filter is real,
    # not a fallback that returns every row.
    assert "01HSRCFTSB00000000000000B" not in ids


async def test_search_handler_phrase_quote_protects_fts5_syntax(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phrase quoting handles FTS5 syntax glyphs in the query.

    H4 audit Cluster B pin: an operator-supplied query like
    ``find_document(arg)`` contains parentheses which FTS5 parses as
    grouping in raw mode. The MCP boundary MUST phrase-quote the
    query before handing it to FTS5 so it lands as a literal token
    stream — no OperationalError, no escalation to FTS5 boolean
    syntax. A regression that flips ``raw_query=True`` here would
    raise ``sqlite3.OperationalError``.
    """
    _bootstrap_fts_index(engine)
    _seed_source_with_body(
        engine,
        source_id="01HSRCFTSC00000000000000C",
        body="The function find_document is exported from the public surface.",
        title="search-fts-c",
    )

    from opshub.services.search_service import SearchService

    def _factory() -> SearchService:
        return SearchService(engine=engine)

    monkeypatch.setattr("opshub.cli._wiring.build_search_service", _factory, raising=True)

    from opshub.mcp._tools import build_search_handler

    handler = build_search_handler(engine)
    payload = _parse(await handler({"query": "find_document is exported", "limit": 5}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert any(row["entity_id"] == "01HSRCFTSC00000000000000C" for row in items), payload


# ---------------------------------------------------------------------------
# 5. propose.generate unknown mode → MCP isError via dispatch_tool_call (M8)
# ---------------------------------------------------------------------------


async def test_dispatch_propose_generate_unknown_mode_surfaces_as_iserror(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown ``mode`` reaches ``dispatch_tool_call`` as a redacted ``OpsHubError``.

    M8 audit pin: the handler's defence-in-depth check
    (``mode_raw not in _PROPOSE_GENERATE_MODES``) raises ``OpsHubError``
    when an out-of-band caller bypasses schema validation. The
    dispatch wrapper (``opshub.mcp.server.dispatch_tool_call``) MUST
    re-raise it as ``OpsHubError`` so the MCP SDK lands the response
    on the ``isError=true`` branch instead of crashing the server. A
    regression that swallows the error and returns ``ok:true`` would
    let an agent host silently route past the dispatch guard.

    Note: this pin bypasses the JSON-schema validation layer (which
    would catch the value before reaching the handler) — the schema
    enum is the first line of defence, the handler check is the
    second, and this test exercises the second so a hypothetical SDK
    that skipped schema validation still cannot ship a bad mode
    through.
    """
    from opshub.core.errors import OpsHubError
    from opshub.mcp._registry import ToolPolicy, ToolSpec, WriteCategory
    from opshub.mcp._writes import build_propose_generate_handler
    from opshub.mcp.server import dispatch_tool_call

    # No-op ProposalService stub — the handler must raise BEFORE
    # reaching the service when ``mode`` is unknown, so any service
    # method called would itself be a regression.
    class _UnreachableService:
        def generate(self, *args: Any, **kwargs: Any) -> Any:
            _ = (args, kwargs)
            raise AssertionError("service.generate must not be called on unknown mode")

        def generate_reply_draft(self, *args: Any, **kwargs: Any) -> Any:
            _ = (args, kwargs)
            raise AssertionError("service.generate_reply_draft must not be called on unknown mode")

    def _factory(actor: str = "cli:propose") -> _UnreachableService:
        _ = actor
        return _UnreachableService()

    monkeypatch.setattr("opshub.cli._wiring.build_proposal_service", _factory, raising=True)

    handler = build_propose_generate_handler(engine)
    # Construct a minimal ToolSpec that wraps the handler so
    # ``dispatch_tool_call`` exercises the full server wrapper path
    # (OTel record + secret redaction + exception rewrap).
    spec = ToolSpec(
        name="propose.generate",
        title="propose.generate",
        description="propose.generate",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        policy=ToolPolicy(read_only=False, destructive=True, idempotent=False, open_world=True),
        # Category is informational; the dispatch wrapper does not
        # branch on it. WriteCategory.PROPOSE_GENERATE matches the
        # real registry entry so a reader recognises the spec shape.
        category=WriteCategory.PROPOSE_GENERATE,
        handler=handler,
    )

    with pytest.raises(OpsHubError) as excinfo:
        await dispatch_tool_call(
            {spec.name: spec},
            spec.name,
            {"topic": "x", "mode": "invalid"},
        )
    message = str(excinfo.value)
    assert "mode" in message, message
    # The dispatch wrapper re-wraps as ``OpsHubError`` via ``raise ...
    # from exc`` — confirm the original error survives in __cause__ so
    # server-side traceback still has context.
    assert isinstance(excinfo.value.__cause__, OpsHubError)
