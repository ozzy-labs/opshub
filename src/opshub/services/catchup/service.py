"""CatchupService (Phase 25-E, epic #566).

The 秘書化 v1 **catchup** flow answers "what changed since I last looked?"
— the non-resident, on-demand surfacing of the diff that accrued since the
operator last caught up. It is the read counterpart of the旗艦 commitment
ledger: where ``commitment scan`` mines new signal (LLM, write), catchup
*reads* the existing read models and bundles the diff into one digest.

Three responsibilities:

1. :meth:`catchup` — assemble the diff digest. Reads the
   ``seen_markers`` watermark (or treats the whole history as unseen on the
   first run), then bundles three sections from the existing read models:

   * **new sources** — ``sources`` rows observed after the watermark
     (count + the most-recent handful for context);
   * **open commitments** — the open commitment ledger (``commitments``
     where ``state = "open"``), with overdue ones (a ``due`` date in the
     past) surfaced first;
   * **open Slack demand** — ``slack_demand_digest`` rows whose last demand
     landed after the watermark (the operator's un-answered @mentions / DMs
     that are *new* since the last catchup).

   When ``advance=True`` (the default for ``opshub catchup
   --since-last-seen``), the run records a
   :class:`~opshub.domain.events.SeenMarkerAdvanced` so the next catchup
   resumes from this point. ``advance=False`` is a dry preview that leaves
   the marker untouched.

**No LLM call.** Like ``commitment list``, catchup is a pure read of the
projections (ADR-0015 §brief is the *host*-side summarisation layer — the
host LLM can summarise the returned digest, but the service itself just
gathers the deterministic diff so it works offline / with the backend
disabled). The host (Claude Code etc.) applies a ``brief``-style summary
on top of the structured digest the catchup skill returns.

Engine binding follows :class:`~opshub.services.persons.PersonResolutionService`:
a read-only construction powers a dry :meth:`catchup` (``advance=False``);
advancing the marker additionally needs the writer triplet (store +
projector + UoW factory).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from opshub.core.errors import ConfigError
from opshub.core.time import now_utc
from opshub.domain.events.seen_marker import SEEN_MARKER_KEY, SeenMarkerAdvanced
from opshub.projections.commitments import commitments_table
from opshub.projections.seen_markers import seen_markers_table
from opshub.projections.slack_demand_digest import slack_demand_digest_table
from opshub.projections.sources import sources_table

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

    from opshub.domain.events import DomainEvent
    from opshub.services.event_store import EventStore
    from opshub.services.projector import Projector

__all__ = [
    "CatchupCommitment",
    "CatchupDemand",
    "CatchupDigest",
    "CatchupService",
    "CatchupSource",
]


_DEFAULT_ACTOR = "cli:catchup"

# Per-section caps on the items materialised into the digest. The counts
# always reflect the full diff; the lists are truncated so a giant backlog
# does not produce an unreadable wall of text (the operator can drill in
# via ``opshub source list`` / ``commitment list`` / ``slack mentions``).
_DEFAULT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class CatchupSource:
    """One new ``sources`` row surfaced in the catchup digest."""

    id: str
    connector_name: str
    source_type: str
    title: str
    url: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CatchupCommitment:
    """One open commitment surfaced in the catchup digest."""

    id: str
    direction: str
    counterparty: str | None
    due: str | None
    text: str
    overdue: bool


@dataclass(frozen=True, slots=True)
class CatchupDemand:
    """One new Slack demand (un-answered @mention / DM) since the watermark.

    ``last_demand_at`` is the Slack message business time (the
    ``last_demand_ts`` epoch float converted to a UTC datetime for display).
    The "is this new since the last catchup?" filter runs against the
    digest row's ``updated_at`` (the projection wall-clock), not this
    message time — a late-arriving demand should surface on the next
    catchup even if its Slack ts predates the watermark.
    """

    team_id: str
    channel_id: str
    channel_name: str | None
    demand_kind: str
    last_demand_user_id: str | None
    last_demand_excerpt: str | None
    last_demand_permalink: str | None
    last_demand_at: datetime


@dataclass(frozen=True, slots=True)
class CatchupDigest:
    """The assembled "what changed since I last looked?" diff.

    ``since`` is the watermark the diff was computed against (``None`` on
    the first-ever catchup, when the whole history counts as unseen).
    ``advanced_to`` is the new watermark the run recorded (``None`` when
    ``advance=False`` left the marker untouched). The ``*_total`` counts
    reflect the full diff; the lists are truncated to the per-section cap.
    """

    since: datetime | None
    advanced_to: datetime | None
    new_sources_total: int
    new_sources: list[CatchupSource]
    open_commitments_total: int
    overdue_commitments_total: int
    open_commitments: list[CatchupCommitment]
    new_demand_total: int
    new_demand: list[CatchupDemand]


class CatchupService:
    """Assemble + advance the catchup diff digest (Phase 25-E)."""

    def __init__(
        self,
        engine: Engine,
        *,
        store: EventStore | None = None,
        projector: Projector | None = None,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> None:
        self._engine = engine
        self._store = store
        self._projector = projector
        self._uow_factory = uow_factory
        self._actor = actor

    # ------------------------------------------------------------------ catchup

    def catchup(self, *, advance: bool = True, limit: int = _DEFAULT_LIMIT) -> CatchupDigest:
        """Assemble the diff since the last catchup; optionally advance the marker.

        Reads the ``seen_markers`` watermark and bundles the new sources /
        open commitments / new Slack demand that accrued after it. When
        ``advance`` is true (the default), records a
        :class:`SeenMarkerAdvanced` stamping ``now`` as the new watermark so
        the next catchup resumes from here. The advance happens **after**
        the diff is gathered so the returned digest reflects the pre-advance
        window.

        Raises
        ------
        ConfigError
            When ``advance`` is true but the service was constructed
            read-only (no writer dependencies). A dry ``advance=False``
            catchup works against a read-only construction.
        """
        run_started_at = now_utc()
        since = self._current_marker()

        new_sources_total, new_sources = self._new_sources(since, limit)
        (
            open_total,
            overdue_total,
            open_commitments,
        ) = self._open_commitments(run_started_at, limit)
        new_demand_total, new_demand = self._new_demand(since, limit)

        advanced_to: datetime | None = None
        if advance:
            self._advance_marker(run_started_at)
            advanced_to = run_started_at

        return CatchupDigest(
            since=since,
            advanced_to=advanced_to,
            new_sources_total=new_sources_total,
            new_sources=new_sources,
            open_commitments_total=open_total,
            overdue_commitments_total=overdue_total,
            open_commitments=open_commitments,
            new_demand_total=new_demand_total,
            new_demand=new_demand,
        )

    # ------------------------------------------------------------------ reads

    def _current_marker(self) -> datetime | None:
        """Return the stored ``seen_at`` watermark, or ``None`` before any catchup."""
        stmt = select(seen_markers_table.c.seen_at).where(
            seen_markers_table.c.marker_key == SEEN_MARKER_KEY
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else _as_aware(row[0])

    def _new_sources(self, since: datetime | None, limit: int) -> tuple[int, list[CatchupSource]]:
        """Count + list ``sources`` observed after the watermark (newest first)."""
        count_stmt = select(sources_table.c.id)
        list_stmt = select(
            sources_table.c.id,
            sources_table.c.connector_name,
            sources_table.c.source_type,
            sources_table.c.title,
            sources_table.c.url,
            sources_table.c.observed_at,
        ).order_by(sources_table.c.observed_at.desc())
        if since is not None:
            count_stmt = count_stmt.where(sources_table.c.observed_at > since)
            list_stmt = list_stmt.where(sources_table.c.observed_at > since)
        list_stmt = list_stmt.limit(limit)
        with self._engine.connect() as conn:
            total = len(conn.execute(count_stmt).all())
            rows = conn.execute(list_stmt).all()
        items = [
            CatchupSource(
                id=str(row.id),
                connector_name=str(row.connector_name),
                source_type=str(row.source_type),
                title=str(row.title),
                url=row.url,
                observed_at=_as_aware(row.observed_at),
            )
            for row in rows
        ]
        return total, items

    def _open_commitments(
        self, as_of: datetime, limit: int
    ) -> tuple[int, int, list[CatchupCommitment]]:
        """Count + list the open commitment ledger, overdue surfaced first.

        Catchup surfaces the *current* open commitments (the standing "what
        do I owe / am I owed" backlog) rather than only those extracted
        since the watermark — an open commitment stays relevant every
        catchup until the operator resolves it. ``overdue`` is computed
        against ``as_of`` by comparing the ISO ``due`` string lexically
        (ISO-8601 sorts chronologically); a non-ISO / partial ``due`` is
        treated as not-overdue (we never have a false alarm on a string we
        cannot parse).
        """
        stmt = (
            select(
                commitments_table.c.id,
                commitments_table.c.direction,
                commitments_table.c.counterparty,
                commitments_table.c.due,
                commitments_table.c.text,
            )
            .where(commitments_table.c.state == "open")
            .order_by(commitments_table.c.extracted_at.desc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()

        as_of_iso = as_of.date().isoformat()
        all_open = [
            CatchupCommitment(
                id=str(row.id),
                direction=str(row.direction),
                counterparty=row.counterparty,
                due=row.due,
                text=str(row.text),
                overdue=_is_overdue(row.due, as_of_iso),
            )
            for row in rows
        ]
        overdue_total = sum(1 for c in all_open if c.overdue)
        # Overdue first (most pressing), then the rest in extraction order.
        ordered = sorted(all_open, key=lambda c: not c.overdue)
        return len(all_open), overdue_total, ordered[:limit]

    def _new_demand(self, since: datetime | None, limit: int) -> tuple[int, list[CatchupDemand]]:
        """Count + list Slack demand whose digest row updated after the watermark.

        The "new since last catchup" filter runs against the digest row's
        ``updated_at`` (the projection wall-clock), which aligns with the
        ``seen_at`` datetime watermark — the Slack ``last_demand_ts`` is a
        message-business-time epoch float on a different axis. Ordered by
        most-recent demand first (``last_demand_ts``).
        """
        count_stmt = select(slack_demand_digest_table.c.channel_id)
        list_stmt = select(
            slack_demand_digest_table.c.team_id,
            slack_demand_digest_table.c.channel_id,
            slack_demand_digest_table.c.channel_name,
            slack_demand_digest_table.c.demand_kind,
            slack_demand_digest_table.c.last_demand_user_id,
            slack_demand_digest_table.c.last_demand_excerpt,
            slack_demand_digest_table.c.last_demand_permalink,
            slack_demand_digest_table.c.last_demand_ts,
        ).order_by(slack_demand_digest_table.c.last_demand_ts.desc())
        if since is not None:
            count_stmt = count_stmt.where(slack_demand_digest_table.c.updated_at > since)
            list_stmt = list_stmt.where(slack_demand_digest_table.c.updated_at > since)
        list_stmt = list_stmt.limit(limit)
        with self._engine.connect() as conn:
            total = len(conn.execute(count_stmt).all())
            rows = conn.execute(list_stmt).all()
        items = [
            CatchupDemand(
                team_id=str(row.team_id),
                channel_id=str(row.channel_id),
                channel_name=row.channel_name,
                demand_kind=str(row.demand_kind),
                last_demand_user_id=row.last_demand_user_id,
                last_demand_excerpt=row.last_demand_excerpt,
                last_demand_permalink=row.last_demand_permalink,
                last_demand_at=_ts_to_datetime(float(row.last_demand_ts)),
            )
            for row in rows
        ]
        return total, items

    # ------------------------------------------------------------------ writer

    def _advance_marker(self, seen_at: datetime) -> None:
        """Record a :class:`SeenMarkerAdvanced` stamping ``seen_at`` as the watermark."""
        self._require_writer_deps()
        self._commit(
            SeenMarkerAdvanced(
                aggregate_id=SEEN_MARKER_KEY,
                actor=self._actor,
                seen_at=seen_at,
                occurred_at=seen_at,
                recorded_at=seen_at,
            )
        )

    def _require_writer_deps(self) -> None:
        """Guard the marker-advance path against a read-only construction."""
        if self._store is None or self._projector is None or self._uow_factory is None:
            raise ConfigError(
                "CatchupService.catchup(advance=True) requires store + projector +"
                " uow_factory — construct via opshub.cli._wiring.build_catchup_service,"
                " or call catchup(advance=False) for a dry preview."
            )

    def _commit(self, event: DomainEvent) -> None:
        """Append + project one event in a single UoW."""
        assert self._uow_factory is not None
        assert self._store is not None
        assert self._projector is not None
        with self._uow_factory() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)


def _as_aware(value: datetime) -> datetime:
    """Re-attach UTC to a naive datetime SQLite hands back (it drops tzinfo)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ts_to_datetime(ts: float) -> datetime:
    """Convert a Slack ``last_demand_ts`` epoch float to a UTC datetime."""
    return datetime.fromtimestamp(ts, tz=UTC)


def _is_overdue(due: str | None, as_of_iso: str) -> bool:
    """Return whether ``due`` (an ISO-ish date string) is before ``as_of_iso``.

    ISO-8601 date / datetime strings sort chronologically as plain text, so
    a lexical ``<`` comparison answers "is the due date in the past?"
    without parsing. A ``None`` or non-ISO / partial ``due`` (e.g. the LLM
    returned "next Friday") is treated as **not** overdue — catchup never
    raises a false alarm on a string it cannot interpret.
    """
    if not due:
        return False
    head = due.strip()[:10]
    # Require a YYYY-MM-DD prefix; anything else (free-form / partial) is
    # left as not-overdue rather than mis-sorted against the as-of date.
    if len(head) != 10 or head[4] != "-" or head[7] != "-":
        return False
    if not (head[:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit()):
        return False
    return head < as_of_iso


def digest_to_dict(digest: CatchupDigest) -> dict[str, object]:
    """Render a :class:`CatchupDigest` to a JSON-serialisable dict.

    Single source of truth for the catchup wire shape — both the
    ``opshub catchup --format json`` CLI and the MCP ``catchup`` read tool
    serialise through here so the two surfaces never drift (the kind of
    cross-surface gap the parallel Phase 25-D / 25-E split produced).
    """

    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "since": _iso(digest.since),
        "advanced_to": _iso(digest.advanced_to),
        "new_sources_total": digest.new_sources_total,
        "new_sources": [
            {
                "id": s.id,
                "connector_name": s.connector_name,
                "source_type": s.source_type,
                "title": s.title,
                "url": s.url,
                "observed_at": _iso(s.observed_at),
            }
            for s in digest.new_sources
        ],
        "open_commitments_total": digest.open_commitments_total,
        "overdue_commitments_total": digest.overdue_commitments_total,
        "open_commitments": [
            {
                "id": c.id,
                "direction": c.direction,
                "counterparty": c.counterparty,
                "due": c.due,
                "text": c.text,
                "overdue": c.overdue,
            }
            for c in digest.open_commitments
        ],
        "new_demand_total": digest.new_demand_total,
        "new_demand": [
            {
                "team_id": d.team_id,
                "channel_id": d.channel_id,
                "channel_name": d.channel_name,
                "demand_kind": d.demand_kind,
                "last_demand_user_id": d.last_demand_user_id,
                "last_demand_excerpt": d.last_demand_excerpt,
                "last_demand_permalink": d.last_demand_permalink,
                "last_demand_at": _iso(d.last_demand_at),
            }
            for d in digest.new_demand
        ],
    }
