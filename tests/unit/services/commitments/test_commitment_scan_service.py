"""Tests for :class:`opshub.services.commitments.CommitmentScanService` (25-C).

Pins the旗艦 commitment-ledger contract (ADR-0042):

* deterministic extraction (same body + author → same row);
* direction from the Phase 25-A operator-self-id signal (self → ``i_owe``,
  other → ``owed_to_me``);
* counterparty resolved to a ``person:<id>`` ref from ``person_identities``;
* due passed through;
* scan cursor advances on completion, does NOT advance on mid-scan failure;
* LLM unset / disabled → ``scan`` raises ``ConfigError`` + records
  ``CommitmentScanFailed``; ``list`` works without an LLM;
* projection replay is idempotent (rebuild → byte-identical ledger);
* resolve / dismiss / reopen state-transition guards.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError, OpsHubError
from opshub.core.ids import new_ulid
from opshub.db import SqlAlchemyEventStore
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import metadata
from opshub.domain.events import (
    DomainEvent,
    IdentityLinked,
    PersonIdentified,
    SourceObserved,
)
from opshub.llm.client import LLMClient, LLMMessage, LLMResponse, StructuredResponse
from opshub.projections import all_projections
from opshub.projections.commitment_scan_cursor import commitment_scan_cursor_table
from opshub.projections.person_identities import PersonIdentitiesProjection
from opshub.projections.persons import PersonsProjection
from opshub.projections.sources import SourcesProjection
from opshub.services.commitments import CommitmentScanService
from opshub.services.commitments.service import ExtractedCommitment

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)


class _AllProjectionsAdapter:
    """Projector seam fanning events to every registered projection."""

    def __init__(self) -> None:
        self._projections = all_projections()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        assert connection is not None
        for projection in self._projections:
            projection.apply(connection, event)


class _StubLLMClient:
    """LLMClient stub returning canned extractions per source body.

    ``by_body`` maps a source body substring → list of
    :class:`ExtractedCommitment`. The default returns one commitment for
    any body. ``fail_with`` flips the stub into a failure path.
    """

    def __init__(
        self,
        *,
        by_body: dict[str, list[ExtractedCommitment]] | None = None,
        default: list[ExtractedCommitment] | None = None,
        fail_with: Exception | None = None,
        model_id: str = "stub-llm",
    ) -> None:
        self._by_body = by_body or {}
        self._default = default
        self._fail_with = fail_with
        self._model_id = model_id
        self.calls: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return "v1"

    def complete(self, messages: list[LLMMessage], **_: object) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        del temperature, max_tokens
        user = messages[-1].content
        self.calls.append(user)
        if self._fail_with is not None:
            raise self._fail_with
        chosen: list[ExtractedCommitment] | None = None
        for needle, extractions in self._by_body.items():
            if needle in user:
                chosen = extractions
                break
        if chosen is None:
            chosen = (
                self._default
                if self._default is not None
                else [ExtractedCommitment(text="default commitment")]
            )
        parsed = schema(commitments=list(chosen))
        return StructuredResponse(
            parsed=parsed,
            model_id=self._model_id,
            model_version="v1",
            tokens_in=10,
            tokens_out=5,
        )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "commitment_service.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    metadata.create_all(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _seed_source(
    engine: Engine,
    *,
    source_id: str,
    body: str,
    connector: str = "github",
    external_id: str | None = None,
    handle: str | None = None,
    occurred_at: datetime = _T0,
) -> None:
    # ``author_connector`` is not on the event — the projection mirrors
    # ``connector_name`` onto it (Phase 25-A). Only ``author_handle`` /
    # ``author_display`` flow through the event.
    event = SourceObserved(
        aggregate_id=source_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="connector:test",
        connector_name=connector,
        external_id=external_id or f"{connector}:{source_id}",
        source_type="message",
        title="msg",
        body=body,
        author_handle=handle,
    )
    with engine.begin() as conn:
        SourcesProjection().apply(conn, event)


def _seed_person_identity(engine: Engine, *, connector: str, handle: str, person_id: str) -> None:
    with engine.begin() as conn:
        PersonsProjection().apply(
            conn,
            PersonIdentified(
                aggregate_id=person_id,
                occurred_at=_T0,
                recorded_at=_T0,
                actor="x",
                display_name=handle,
            ),
        )
        PersonIdentitiesProjection().apply(
            conn,
            IdentityLinked(
                aggregate_id=person_id,
                occurred_at=_T0,
                recorded_at=_T0,
                actor="x",
                connector=connector,
                handle=handle,
            ),
        )


def _service(engine: Engine, llm: LLMClient | None) -> CommitmentScanService:
    return CommitmentScanService(
        engine=engine,
        llm_client=llm,
        store=SqlAlchemyEventStore(engine),
        projector=_AllProjectionsAdapter(),
        uow_factory=engine.begin,
    )


# ---- extraction + direction -----------------------------------------------


def test_scan_extracts_owed_to_me_for_other_author(engine: Engine) -> None:
    sid = new_ulid()
    _seed_source(engine, source_id=sid, body="can you review the PR by Friday?", handle="alice")
    svc = _service(engine, _StubLLMClient())

    summary = svc.scan()
    assert summary.sources_scanned == 1
    assert summary.commitments_extracted == 1

    rows = svc.list_commitments()
    assert len(rows) == 1
    assert rows[0].direction == "owed_to_me"
    assert rows[0].state == "open"
    assert rows[0].source_id == sid


def test_scan_extracts_i_owe_for_operator_author(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSHUB_CONNECTORS__GITHUB__OPERATOR_LOGIN", "me")
    _seed_source(engine, source_id=new_ulid(), body="I'll send the deck by Monday", handle="me")
    svc = _service(engine, _StubLLMClient())

    svc.scan()
    rows = svc.list_commitments()
    assert len(rows) == 1
    assert rows[0].direction == "i_owe"
    # Operator-authored → counterparty cannot be derived from authorship (v1).
    assert rows[0].counterparty is None


def test_scan_resolves_counterparty_person_ref(engine: Engine) -> None:
    pid = new_ulid()
    _seed_person_identity(engine, connector="github", handle="alice", person_id=pid)
    _seed_source(engine, source_id=new_ulid(), body="please ship it", handle="alice")
    svc = _service(engine, _StubLLMClient())

    svc.scan()
    rows = svc.list_commitments()
    assert rows[0].counterparty == f"person:{pid}"


def test_scan_passes_due_through(engine: Engine) -> None:
    _seed_source(engine, source_id=new_ulid(), body="deadline soon", handle="alice")
    svc = _service(
        engine,
        _StubLLMClient(
            default=[ExtractedCommitment(text="x", due="2026-06-20", confidence="high")]
        ),
    )
    svc.scan()
    rows = svc.list_commitments()
    assert rows[0].due == "2026-06-20"
    assert rows[0].confidence == "high"


def test_scan_is_deterministic_same_body(engine: Engine) -> None:
    sid = new_ulid()
    _seed_source(engine, source_id=sid, body="same body", handle="alice")
    svc = _service(engine, _StubLLMClient(default=[ExtractedCommitment(text="fixed text")]))
    svc.scan()
    first = svc.list_commitments()
    # A second scan from scratch (re-seed cursor) over the same source must
    # produce a row with identical content keyed on the source_ref.
    svc2 = _service(engine, _StubLLMClient(default=[ExtractedCommitment(text="fixed text")]))
    svc2.scan_from(None)
    second = svc2.list_commitments()
    assert len(second) == 1
    assert second[0].text == first[0].text == "fixed text"
    assert second[0].id == first[0].id  # source_ref UPSERT keeps the id


# ---- cursor advance / non-advance -----------------------------------------


def _cursor(engine: Engine) -> str | None:
    from sqlalchemy import select

    with engine.connect() as conn:
        r = conn.execute(select(commitment_scan_cursor_table.c.cursor_value)).first()
    return None if r is None else r[0]


def test_cursor_advances_on_completion(engine: Engine) -> None:
    s1, s2 = sorted((new_ulid(), new_ulid()))
    _seed_source(engine, source_id=s1, body="one", handle="alice")
    _seed_source(engine, source_id=s2, body="two", handle="alice")
    svc = _service(engine, _StubLLMClient())
    svc.scan()
    assert _cursor(engine) == s2  # watermark = highest source id


def test_cursor_does_not_advance_on_mid_scan_failure(engine: Engine) -> None:
    _seed_source(engine, source_id=new_ulid(), body="boom", handle="alice")
    svc = _service(engine, _StubLLMClient(fail_with=RuntimeError("llm exploded")))
    with pytest.raises(RuntimeError):
        svc.scan()
    # Cursor untouched (started recorded resume_from=None, failure is no-op).
    assert _cursor(engine) is None
    # A CommitmentScanFailed event was recorded for diagnosis.
    with engine.connect() as conn:
        from sqlalchemy import select

        from opshub.db.schema import events_table

        types = [r[0] for r in conn.execute(select(events_table.c.event_type)).all()]
    assert "commitment.scan_failed" in types


def test_incremental_scan_skips_already_extracted(engine: Engine) -> None:
    s1 = new_ulid()
    _seed_source(engine, source_id=s1, body="first", handle="alice")
    svc = _service(engine, _StubLLMClient())
    first = svc.scan()
    assert first.sources_scanned == 1

    # Second scan with no new sources reads nothing (cursor at s1).
    again = _service(engine, _StubLLMClient()).scan()
    assert again.sources_scanned == 0
    assert again.commitments_extracted == 0


# ---- LLM unset / disabled degrade -----------------------------------------


def test_scan_without_llm_raises_config_error(engine: Engine) -> None:
    _seed_source(engine, source_id=new_ulid(), body="x", handle="alice")
    svc = _service(engine, None)
    with pytest.raises(ConfigError):
        svc.scan()


def test_disabled_backend_records_scan_failed_and_raises(engine: Engine) -> None:
    from opshub.llm.factory import NoOpLLMClient

    _seed_source(engine, source_id=new_ulid(), body="x", handle="alice")
    svc = _service(engine, NoOpLLMClient())
    with pytest.raises(ConfigError):
        svc.scan()
    # The bracket recorded a CommitmentScanFailed so the attempt is durable.
    from sqlalchemy import select

    from opshub.db.schema import events_table

    with engine.connect() as conn:
        types = [r[0] for r in conn.execute(select(events_table.c.event_type)).all()]
    assert "commitment.scan_failed" in types


def test_list_works_without_llm(engine: Engine) -> None:
    # Populate via a real scan, then list through an LLM-less service.
    _seed_source(engine, source_id=new_ulid(), body="x", handle="alice")
    _service(engine, _StubLLMClient()).scan()
    reader = _service(engine, None)
    assert len(reader.list_commitments()) == 1


# ---- list filters ----------------------------------------------------------


def test_list_filters_by_direction_and_state(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSHUB_CONNECTORS__GITHUB__OPERATOR_LOGIN", "me")
    s_other, s_self = sorted((new_ulid(), new_ulid()))
    _seed_source(engine, source_id=s_other, body="please do X", handle="alice")
    _seed_source(engine, source_id=s_self, body="I'll do Y", handle="me")
    svc = _service(engine, _StubLLMClient())
    svc.scan()

    owed = svc.list_commitments(direction="owed_to_me")
    owes = svc.list_commitments(direction="i_owe")
    assert {c.direction for c in owed} == {"owed_to_me"}
    assert {c.direction for c in owes} == {"i_owe"}

    # Resolve one, then --open hides it.
    target = owed[0].id
    svc.resolve(target)
    open_only = svc.list_commitments(state="open")
    assert target not in {c.id for c in open_only}


# ---- state-transition guards ----------------------------------------------


def test_resolve_missing_commitment_raises(engine: Engine) -> None:
    svc = _service(engine, None)
    with pytest.raises(OpsHubError):
        svc.resolve(new_ulid())


def test_double_resolve_raises(engine: Engine) -> None:
    _seed_source(engine, source_id=new_ulid(), body="x", handle="alice")
    svc = _service(engine, _StubLLMClient())
    svc.scan()
    cid = svc.list_commitments()[0].id
    svc.resolve(cid)
    with pytest.raises(OpsHubError):
        svc.resolve(cid)


def test_reopen_open_commitment_raises(engine: Engine) -> None:
    _seed_source(engine, source_id=new_ulid(), body="x", handle="alice")
    svc = _service(engine, _StubLLMClient())
    svc.scan()
    cid = svc.list_commitments()[0].id
    with pytest.raises(OpsHubError):
        svc.reopen(cid)  # already open


# ---- replay idempotency ----------------------------------------------------


def test_projection_replay_is_idempotent(engine: Engine) -> None:
    """Rebuilding the projections from the event log yields the same ledger."""

    _seed_source(engine, source_id=new_ulid(), body="x", handle="alice")
    svc = _service(engine, _StubLLMClient())
    svc.scan()
    cid = svc.list_commitments()[0].id
    svc.resolve(cid)
    before = svc.list_commitments()

    # Replay every stored event through a fresh fan-out (reset first).
    projections = all_projections()
    store = SqlAlchemyEventStore(engine)
    with engine.begin() as conn:
        for proj in projections:
            proj.reset(conn)
        for event in store.iter_all():
            for proj in projections:
                proj.apply(conn, event)

    after = svc.list_commitments()
    assert len(after) == len(before) == 1
    assert after[0].id == before[0].id
    assert after[0].state == "resolved"
    assert after[0].direction == before[0].direction
