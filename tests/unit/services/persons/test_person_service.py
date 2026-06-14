"""Tests for :class:`opshub.services.persons.PersonResolutionService` (25-B).

Pins the identity resolution policy (ADR-0043):

* exact handle bundling (same connector handle = one identity, idempotent);
* exact cross-connector email auto-merge (Gmail + Outlook same address);
* operator handles across connectors bundle onto a single operator person;
* fuzzy (display-name-only) matches are NOT auto-merged — the resolver
  over-splits, leaving HITL ``merge`` to the operator;
* ``merge`` / ``split`` writer paths + their error semantics;
* a read-only construction raises :class:`ConfigError` on writer methods.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError, NotFoundError, ValidationError
from opshub.core.ids import new_ulid
from opshub.db import SqlAlchemyEventStore
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, SourceObserved
from opshub.projections import all_projections
from opshub.projections.sources import SourcesProjection
from opshub.services.persons import PersonResolutionService

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_T0 = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)


class _AllProjectionsAdapter:
    """Adapter fanning events to every registered projection (Projector seam)."""

    def __init__(self) -> None:
        self._projections = all_projections()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        assert connection is not None
        for projection in self._projections:
            projection.apply(connection, event)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Engine with the full schema provisioned (events + every projection table)."""
    db_path = tmp_path / "persons_service.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    metadata.create_all(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _seed_source(
    engine: Engine,
    *,
    connector: str,
    external_id: str,
    handle: str,
    display: str | None = None,
) -> None:
    """Insert one ``sources`` row carrying an author handle via SourceObserved."""
    projection = SourcesProjection()
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=_T0,
        recorded_at=_T0,
        actor="connector:test",
        connector_name=connector,
        external_id=external_id,
        source_type="message",
        title="msg",
        body="hello",
        author_handle=handle,
        author_display=display,
    )
    with engine.begin() as conn:
        projection.apply(conn, event)


def _service(engine: Engine, **kw: object) -> PersonResolutionService:
    return PersonResolutionService(
        engine=engine,
        store=SqlAlchemyEventStore(engine),
        projector=_AllProjectionsAdapter(),
        uow_factory=engine.begin,
        **kw,  # type: ignore[arg-type]
    )


# ---- resolve: fresh persons + idempotency ---------------------------------


def test_resolve_mints_one_person_per_distinct_handle(engine: Engine) -> None:
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice")
    _seed_source(engine, connector="github", external_id="42", handle="bob", display="Bob")
    svc = _service(engine)

    summary = svc.resolve()
    assert summary.persons_created == 2
    assert summary.identities_linked == 2

    persons = svc.list_persons()
    names = sorted(p.display_name for p in persons)
    assert names == ["Alice", "Bob"]
    # Each person carries exactly its one identity.
    for p in persons:
        assert len(p.identities) == 1


def test_resolve_is_idempotent(engine: Engine) -> None:
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice")
    svc = _service(engine)

    first = svc.resolve()
    assert first.persons_created == 1
    second = svc.resolve()
    assert second.persons_created == 0
    assert second.identities_linked == 0
    assert len(svc.list_persons()) == 1


def test_resolve_same_handle_multiple_sources_bundles_once(engine: Engine) -> None:
    """Two messages from the same Slack handle yield one person, one identity."""
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alice")
    _seed_source(engine, connector="slack", external_id="T1:C1:2", handle="U_a", display="Alice")
    svc = _service(engine)

    summary = svc.resolve()
    assert summary.persons_created == 1
    persons = svc.list_persons()
    assert len(persons) == 1
    assert len(persons[0].identities) == 1


# ---- resolve: exact email auto-merge --------------------------------------


def test_resolve_auto_merges_same_email_across_connectors(engine: Engine) -> None:
    """Gmail + Outlook under the same email address bundle onto one person."""
    _seed_source(
        engine,
        connector="google_mail",
        external_id="g1",
        handle="alice@example.com",
        display="Alice",
    )
    _seed_source(
        engine,
        connector="ms365",
        external_id="o1",
        handle="alice@example.com",
        display="Alice O",
    )
    svc = _service(engine)

    summary = svc.resolve()
    # One fresh person; the second email identity auto-merges (link, no
    # new person).
    assert summary.persons_created == 1
    assert summary.identities_linked == 2

    persons = svc.list_persons()
    assert len(persons) == 1
    connectors = sorted(i.connector for i in persons[0].identities)
    assert connectors == ["google_mail", "ms365"]


# ---- resolve: fuzzy is NOT auto-merged ------------------------------------


def test_resolve_does_not_auto_merge_on_display_name_only(engine: Engine) -> None:
    """Same display name, different connectors/handles → two persons (HITL).

    Display-name similarity is a *fuzzy* signal; ADR-0043 keeps those out
    of the auto-merge path so the resolver over-splits rather than
    mis-merging. The operator confirms via ``merge``.
    """
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alex")
    _seed_source(engine, connector="github", external_id="9", handle="alex", display="Alex")
    svc = _service(engine)

    svc.resolve()
    persons = svc.list_persons()
    assert len(persons) == 2  # NOT merged on the shared display name


# ---- resolve: operator bundling -------------------------------------------


def test_resolve_bundles_operator_handles_onto_one_person(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All operator handles (across connectors) land on a single operator person."""
    monkeypatch.setenv("OPSHUB_CONNECTORS__GITHUB__OPERATOR_LOGIN", "me")
    monkeypatch.setenv("OPSHUB_CONNECTORS__GOOGLE_MAIL__OPERATOR_EMAIL", "me@example.com")

    _seed_source(engine, connector="github", external_id="1", handle="me", display="Me GH")
    _seed_source(
        engine, connector="google_mail", external_id="2", handle="me@example.com", display="Me Mail"
    )
    svc = _service(engine)

    svc.resolve()
    persons = svc.list_persons()
    operator_persons = [p for p in persons if p.is_operator]
    assert len(operator_persons) == 1
    op = operator_persons[0]
    assert sorted(i.connector for i in op.identities) == ["github", "google_mail"]


# ---- merge ----------------------------------------------------------------


def test_merge_reparents_onto_smaller_id(engine: Engine) -> None:
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alex")
    _seed_source(engine, connector="github", external_id="9", handle="alex", display="Alex")
    svc = _service(engine)
    svc.resolve()

    persons = svc.list_persons()
    a, b = persons[0].id, persons[1].id
    survivor = svc.merge(a, b)
    assert survivor == min(a, b)

    after = svc.list_persons()
    assert len(after) == 1
    assert after[0].id == survivor
    assert len(after[0].identities) == 2


def test_merge_self_raises(engine: Engine) -> None:
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a")
    svc = _service(engine)
    svc.resolve()
    pid = svc.list_persons()[0].id
    with pytest.raises(ValidationError):
        svc.merge(pid, pid)


def test_merge_missing_person_raises(engine: Engine) -> None:
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a")
    svc = _service(engine)
    svc.resolve()
    pid = svc.list_persons()[0].id
    with pytest.raises(NotFoundError):
        svc.merge(pid, new_ulid())


# ---- split ----------------------------------------------------------------


def test_split_detaches_identity_into_new_person(engine: Engine) -> None:
    _seed_source(engine, connector="slack", external_id="T1:C1:1", handle="U_a", display="Alex")
    _seed_source(engine, connector="github", external_id="9", handle="alex", display="Alex")
    svc = _service(engine)
    svc.resolve()
    a, b = (p.id for p in svc.list_persons())
    survivor = svc.merge(a, b)
    assert len(svc.list_persons()) == 1

    new_id = svc.split("github", "alex")
    after = svc.list_persons()
    assert len(after) == 2
    by_id = {p.id: p for p in after}
    assert new_id in by_id
    # The github identity is now on the new person; slack stays on the survivor.
    new_person = by_id[new_id]
    assert [i.connector for i in new_person.identities] == ["github"]
    survivor_person = by_id[survivor]
    assert [i.connector for i in survivor_person.identities] == ["slack"]


def test_split_missing_identity_raises(engine: Engine) -> None:
    svc = _service(engine)
    with pytest.raises(NotFoundError):
        svc.split("slack", "U_nope")


# ---- read-only construction guard -----------------------------------------


def test_writer_methods_require_writer_deps(engine: Engine) -> None:
    svc = PersonResolutionService(engine=engine)
    with pytest.raises(ConfigError):
        svc.resolve()
    with pytest.raises(ConfigError):
        svc.merge(new_ulid(), new_ulid())
    with pytest.raises(ConfigError):
        svc.split("slack", "U_a")
