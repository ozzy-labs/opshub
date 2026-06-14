"""PersonResolutionService (Phase 25-B, ADR-0043).

Reduces the normalised ``author_handle`` / ``author_connector`` columns
the ``sources`` projection stores (Phase 25-A) into a **person** graph:
one node per human, bundling the connector-native handles they appear
under (Slack ``U...`` / email / GitHub ``login`` / ...).

Three operator-facing operations plus one incremental resolver:

* :meth:`resolve` — the incremental binding pass. Scans the ``sources``
  author columns, and for every ``(connector, handle)`` not yet bound,
  decides which person it belongs to per ADR-0043's policy:

  - **exact auto-merge** — an email handle that already maps to a person
    under a *different* email connector bundles onto that person (the
    same human's Gmail + Outlook address). The operator's own handles
    (resolved via :func:`opshub.services.operator_identity.is_authored_by_operator`)
    all bundle onto the single operator person ("operator も 1 person").
  - **fresh person** — everything else mints a new person. Display-name
    similarity (*fuzzy* matching) is **never** auto-merged; the operator
    confirms those via :meth:`merge` (HITL). The resolver therefore
    over-splits rather than mis-merging — the safe direction.

  ``resolve`` is idempotent: a second pass over the same ``sources``
  binds nothing new (every handle is already an identity), so
  ``opshub person list`` (which resolves first) is safe to re-run.

* :meth:`list_persons` — read the ``persons`` + ``person_identities``
  projections into :class:`Person` value objects (identities nested).

* :meth:`merge` — operator HITL merge of two persons into one
  (``IdentityMerged``). The lexicographically-smaller person id survives
  so the operation is deterministic regardless of argument order.

* :meth:`split` — operator HITL detach of one identity into a fresh
  person (``IdentitySplit``), undoing an over-eager merge.

Determinism (ADR-0002)
----------------------
The fuzzy / exact *decision* lives here, in the service, and is recorded
as :class:`~opshub.domain.events.PersonIdentified` /
:class:`~opshub.domain.events.IdentityLinked` /
:class:`~opshub.domain.events.IdentityMerged` events. The
:mod:`opshub.projections.persons` /
:mod:`opshub.projections.person_identities` reducers are pure functions
of those events, so ``projections rebuild`` replays into a byte-identical
person graph.

Engine binding follows :class:`~opshub.services.links.service.LinkService`:
a read-only construction (``PersonResolutionService(engine=...)``) powers
:meth:`list_persons`; the writer methods require ``store`` /
``projector`` / ``uow_factory`` and raise :class:`ConfigError` when
absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from opshub.core.errors import ConfigError, NotFoundError, ValidationError
from opshub.projections.person_identities import person_identities_table
from opshub.projections.persons import persons_table
from opshub.projections.sources import sources_table

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from datetime import datetime

    from sqlalchemy.engine import Connection, Engine

    from opshub.domain.events import DomainEvent
    from opshub.services.event_store import EventStore
    from opshub.services.projector import Projector

__all__ = [
    "Person",
    "PersonIdentity",
    "PersonResolutionService",
    "ResolveSummary",
]


# Connectors whose ``author_handle`` is an email address. Identities from
# any two of these that share the same (already lower-cased) handle are
# the same human — the exact auto-merge bundling key (ADR-0043). Mirrors
# the email-connector set the operator-identity resolver uses.
_EMAIL_CONNECTORS: frozenset[str] = frozenset(
    {"google_mail", "ms365", "google_calendar", "google_workspace"}
)


@dataclass(frozen=True, slots=True)
class PersonIdentity:
    """One connector-native identity bound to a person."""

    connector: str
    handle: str
    display: str | None
    confidence: str


@dataclass(frozen=True, slots=True)
class Person:
    """A resolved person and the identities bundled onto it."""

    id: str
    display_name: str
    is_operator: bool
    created_at: datetime
    identities: tuple[PersonIdentity, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ResolveSummary:
    """Outcome counts of a :meth:`PersonResolutionService.resolve` pass.

    ``identities_merged`` is reserved and stays ``0`` for the v1 resolver:
    exact email / operator bundling is modelled as an
    :class:`~opshub.domain.events.IdentityLinked` onto the *existing*
    person (counted under ``identities_linked``), not an
    :class:`~opshub.domain.events.IdentityMerged`. ``IdentityMerged`` is
    only emitted by the operator-driven :meth:`merge` path, never by
    ``resolve``.
    """

    persons_created: int
    identities_linked: int
    identities_merged: int


class PersonResolutionService:
    """Resolve ``sources`` author handles into a person graph (ADR-0043)."""

    def __init__(
        self,
        engine: Engine,
        *,
        store: EventStore | None = None,
        projector: Projector | None = None,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = "cli:person",
    ) -> None:
        self._engine = engine
        self._store = store
        self._projector = projector
        self._uow_factory = uow_factory
        self._actor = actor

    # ------------------------------------------------------------------ resolve

    def resolve(self) -> ResolveSummary:
        """Bind every not-yet-known ``sources`` author handle to a person.

        Idempotent: handles already present in ``person_identities`` are
        skipped, so a second pass binds nothing. Processing order is a
        deterministic sort on ``(connector, handle)`` so the person ids
        minted on a fresh DB are stable across runs of the same input.
        """
        self._require_writer_deps()

        known = self._known_identities()
        # ``email -> person_id`` map of already-bound email identities,
        # for the exact cross-connector email auto-merge.
        email_owner: dict[str, str] = {
            handle: pid
            for (connector, handle), pid in known.items()
            if connector in _EMAIL_CONNECTORS
        }
        operator_person_id: str | None = self._existing_operator_person_id()

        created = linked = merged = 0
        for connector, handle, display in self._unbound_author_handles(known):
            person_id, did_create, did_link = self._bind_handle(
                connector=connector,
                handle=handle,
                display=display,
                email_owner=email_owner,
                operator_person_id=operator_person_id,
            )
            if did_create:
                created += 1
            if did_link:
                linked += 1
            known[(connector, handle)] = person_id
            if connector in _EMAIL_CONNECTORS:
                email_owner.setdefault(handle, person_id)
            if self._is_operator_handle(connector, handle):
                operator_person_id = person_id

        return ResolveSummary(
            persons_created=created,
            identities_linked=linked,
            identities_merged=merged,
        )

    def _bind_handle(
        self,
        *,
        connector: str,
        handle: str,
        display: str | None,
        email_owner: dict[str, str],
        operator_person_id: str | None,
    ) -> tuple[str, bool, bool]:
        """Resolve one handle to a person, emitting the events. Returns (pid, created, linked)."""
        from opshub.core.ids import new_ulid
        from opshub.domain.events import IdentityLinked, PersonIdentified

        is_operator = self._is_operator_handle(connector, handle)

        # Exact bundling targets (no fresh person): operator handle → the
        # operator person; email handle → an existing same-email person.
        target: str | None = None
        if is_operator and operator_person_id is not None:
            target = operator_person_id
        elif connector in _EMAIL_CONNECTORS and handle in email_owner:
            target = email_owner[handle]

        if target is not None:
            self._commit(
                IdentityLinked(
                    aggregate_id=target,
                    actor=self._actor,
                    connector=connector,
                    handle=handle,
                    display=display,
                    confidence="exact",
                )
            )
            return target, False, True

        # Fresh person + its first identity, committed in one UoW so the
        # ``person_identities`` FK never references a not-yet-projected
        # person row.
        person_id = new_ulid()
        self._commit(
            PersonIdentified(
                aggregate_id=person_id,
                actor=self._actor,
                display_name=display or handle,
                is_operator=is_operator,
            ),
            IdentityLinked(
                aggregate_id=person_id,
                actor=self._actor,
                connector=connector,
                handle=handle,
                display=display,
                confidence="exact",
            ),
        )
        return person_id, True, True

    # ------------------------------------------------------------------ list

    def list_persons(self, *, limit: int = 200) -> list[Person]:
        """Return resolved persons (identities nested), newest first."""
        with self._engine.connect() as conn:
            person_rows = conn.execute(
                select(persons_table).order_by(persons_table.c.created_at.desc()).limit(limit)
            ).all()
            ident_rows = conn.execute(
                select(person_identities_table).order_by(
                    person_identities_table.c.connector,
                    person_identities_table.c.handle,
                )
            ).all()

        by_person: dict[str, list[PersonIdentity]] = {}
        for row in ident_rows:
            by_person.setdefault(row.person_id, []).append(
                PersonIdentity(
                    connector=row.connector,
                    handle=row.handle,
                    display=row.display,
                    confidence=row.confidence,
                )
            )

        return [
            Person(
                id=row.id,
                display_name=row.display_name,
                is_operator=bool(row.is_operator),
                created_at=row.created_at,
                identities=tuple(by_person.get(row.id, [])),
            )
            for row in person_rows
        ]

    # ------------------------------------------------------------------ merge

    def merge(self, person_a: str, person_b: str) -> str:
        """Merge two persons into one (operator HITL). Returns the survivor id.

        The lexicographically-smaller id survives so the operation is
        deterministic regardless of argument order. Raises
        :class:`ValidationError` when the two ids are equal and
        :class:`NotFoundError` when either person does not exist.
        """
        from opshub.domain.events import IdentityMerged

        self._require_writer_deps()
        if person_a == person_b:
            raise ValidationError("cannot merge a person with itself")
        survivor, merged = sorted((person_a, person_b))
        existing = self._existing_person_ids()
        for pid in (survivor, merged):
            if pid not in existing:
                raise NotFoundError(f"person {pid!r} not found")

        self._commit(
            IdentityMerged(
                aggregate_id=survivor,
                actor=self._actor,
                merged_person_id=merged,
                reason="manual",
            )
        )
        return survivor

    # ------------------------------------------------------------------ split

    def split(self, connector: str, handle: str) -> str:
        """Detach one identity into a fresh person (operator HITL). Returns new id.

        Raises :class:`NotFoundError` when the ``(connector, handle)``
        identity is not currently bound.
        """
        from opshub.core.ids import new_ulid
        from opshub.domain.events import IdentitySplit

        self._require_writer_deps()
        owner = self._identity_owner(connector, handle)
        if owner is None:
            raise NotFoundError(f"identity {connector}:{handle} not found")

        new_person_id = new_ulid()
        self._commit(
            IdentitySplit(
                aggregate_id=owner,
                actor=self._actor,
                new_person_id=new_person_id,
                identity_connector=connector,
                identity_handle=handle,
            )
        )
        return new_person_id

    # ------------------------------------------------------------------ reads

    def _known_identities(self) -> dict[tuple[str, str], str]:
        """Map every bound ``(connector, handle)`` to its person id."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    person_identities_table.c.connector,
                    person_identities_table.c.handle,
                    person_identities_table.c.person_id,
                )
            ).all()
        return {(row.connector, row.handle): row.person_id for row in rows}

    def _identity_owner(self, connector: str, handle: str) -> str | None:
        """Return the person id currently owning ``(connector, handle)`` or None."""
        stmt = select(person_identities_table.c.person_id).where(
            (person_identities_table.c.connector == connector)
            & (person_identities_table.c.handle == handle)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else str(row[0])

    def _existing_person_ids(self) -> set[str]:
        """Return the set of all current ``persons.id`` values."""
        with self._engine.connect() as conn:
            rows = conn.execute(select(persons_table.c.id)).all()
        return {str(row[0]) for row in rows}

    def _existing_operator_person_id(self) -> str | None:
        """Return the existing operator person id (``is_operator = 1``) or None."""
        stmt = select(persons_table.c.id).where(persons_table.c.is_operator == 1).limit(1)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else str(row[0])

    def _unbound_author_handles(
        self, known: dict[tuple[str, str], str]
    ) -> list[tuple[str, str, str | None]]:
        """Distinct ``sources`` author handles not yet in ``person_identities``.

        Returns ``(connector, handle, display)`` sorted deterministically.
        The display is the first one seen for the handle in the sort
        order (stable across runs). Handles already in ``known`` are
        filtered out so a re-resolve binds nothing.
        """
        stmt = (
            select(
                sources_table.c.author_connector,
                sources_table.c.author_handle,
                sources_table.c.author_display,
            )
            .where(sources_table.c.author_handle.is_not(None))
            .where(sources_table.c.author_connector.is_not(None))
            .order_by(
                sources_table.c.author_connector,
                sources_table.c.author_handle,
                sources_table.c.id,
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()

        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str, str | None]] = []
        for row in rows:
            connector = str(row.author_connector)
            handle = str(row.author_handle)
            key = (connector, handle)
            if key in known or key in seen:
                continue
            seen.add(key)
            display: str | None = row.author_display
            out.append((connector, handle, display))
        return out

    def _is_operator_handle(self, connector: str, handle: str) -> bool:
        """Return whether ``(connector, handle)`` identifies the operator.

        Delegates to the Phase 25-A operator-identity resolver. Slack
        needs the source ``external_id`` (its ``team_id`` prefix selects
        the workspace self id) — we look up a representative
        ``external_id`` for the handle so the per-workspace comparison
        works; non-Slack connectors ignore it.
        """
        from opshub.services.operator_identity import (
            SourceAuthor,
            is_authored_by_operator,
        )

        external_id = self._representative_external_id(connector, handle)
        return is_authored_by_operator(
            SourceAuthor(
                connector_name=connector,
                author_handle=handle,
                external_id=external_id,
            )
        )

    def _representative_external_id(self, connector: str, handle: str) -> str | None:
        """One ``sources.external_id`` for ``(connector, handle)`` (Slack team_id source)."""
        stmt = (
            select(sources_table.c.external_id)
            .where(sources_table.c.author_connector == connector)
            .where(sources_table.c.author_handle == handle)
            .order_by(sources_table.c.id)
            .limit(1)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else row[0]

    # ------------------------------------------------------------------ writer

    def _require_writer_deps(self) -> None:
        """Guard the writer methods against a read-only construction."""
        if self._store is None or self._projector is None or self._uow_factory is None:
            raise ConfigError(
                "PersonResolutionService writer methods require store + projector +"
                " uow_factory — construct via opshub.cli._wiring.build_person_service"
                " or pass the dependencies explicitly."
            )

    def _commit(self, *events: DomainEvent | Any) -> None:
        """Append + project one or more events in a single UoW.

        Multiple events (e.g. ``PersonIdentified`` + ``IdentityLinked``
        for a fresh person) share one transaction so the
        ``person_identities`` FK never references a not-yet-projected
        ``persons`` row.
        """
        assert self._uow_factory is not None
        assert self._store is not None
        assert self._projector is not None
        with self._uow_factory() as connection:
            for event in events:
                self._store.append(event, connection)
                self._projector.apply(event, connection)
