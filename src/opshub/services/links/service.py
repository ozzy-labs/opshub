"""LinkService traversal queries (Phase 8 step C1 + D1, ADR-0017).

Read-only service over the ``links`` projection. Three traversal
operations:

* :meth:`LinkService.related` — 1-hop neighbours, optionally
  direction-filtered (outgoing / incoming / both)
* :meth:`LinkService.trace` — recursive backward (incoming) traversal
  for provenance queries; returns chains of links
* :meth:`LinkService.list_links` — projection table scan with optional
  ``from`` / ``to`` / ``link_type`` filters (Phase 8 D1)

All three methods include cycle detection (visited set) and depth
limits per ADR-0017 §決定 (e). :meth:`LinkService.find_link_id`
lookup helper is provided for the manual ``opshub link remove`` CLI
path (Phase 8 D1) when the operator wants to remove by natural-key
tuple rather than by id.

Writer-side methods (Phase 8 step D1)
-------------------------------------

In addition to the read-only traversal API, the service grew two
writer-side methods to back the ``opshub link add`` / ``opshub link
remove`` CLI subcommands per Phase 8 plan §3 Sub-issue D DoD:

* :meth:`LinkService.create_link` — mints a fresh ULID, emits
  :class:`LinkCreated`, and applies the :class:`LinksProjector` in one
  UoW. Returns the new link id.
* :meth:`LinkService.delete_link` — emits :class:`LinkDeleted` and
  applies the projector (hard-DELETE) in one UoW. Returns ``True`` if
  a row was actually deleted, ``False`` for a no-op (id not found).

The constructor accepts these writer dependencies as keyword-only
``store`` / ``projector`` / ``uow_factory`` / ``actor`` arguments with
default ``None``. When ``None``, the writer methods raise
:class:`ConfigError` so a read-only wiring (``LinkService(engine)``)
remains valid for Phase 8 C1's traversal-only callers; the existing
C1 unit tests construct the service this way and continue to pass.

Cold-start guard
----------------

Module-level imports are restricted to ``__future__`` / stdlib /
SQLAlchemy primitives + :mod:`opshub.core.errors` +
:mod:`opshub.projections.links`. No LLM / SDK / pydantic-heavy
imports at top level — the service is registered through
``opshub.services.__init__`` and pulled in by other services as the
graph expansion path (Phase 8 / epic #470 で常時実行) materialises.

Engine binding pattern
----------------------

The service follows the same engine-bound pattern as
:class:`~opshub.services.recall_service.RecallService` and
:class:`~opshub.services.duplicate_service.DuplicateService` for the
read path: a single :class:`~sqlalchemy.engine.Engine` is wired at
construction time and every traversal opens a short-lived
``engine.connect()``. The writer methods (added in Phase 8 D1) use
the optional ``uow_factory`` to share a single transaction between
``store.append`` and ``projector.apply`` — same shape as
:class:`~opshub.services.task_service.TaskService` and friends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from opshub.core.errors import ConfigError
from opshub.projections.links import links_table

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from datetime import datetime

    from sqlalchemy.engine import Connection, Engine, Row

    from opshub.services.event_store import EventStore
    from opshub.services.projector import Projector


__all__ = ["GraphSubset", "Link", "LinkPath", "LinkService"]


# ADR-0017 §決定 (e): ``trace(entity, depth=N)`` defaults to 3-hop
# backward traversal with a hard ceiling at 10. Centralised as
# module-level constants so the CLI / tests can reference the same
# values without re-deriving them from the dataclass defaults.
_TRACE_DEFAULT_DEPTH = 3
_TRACE_MAX_DEPTH = 10

# ADR-0017 §決定 (e): ``expand(entity, depth=N)`` defaults to 2-hop
# bidirectional traversal with a hard ceiling at 5. ``expand`` walks
# both directions so the fan-out grows roughly as ``branching^depth``
# — a tighter ceiling than ``trace`` (which is uni-directional) keeps
# CLI rendering tractable for typical operational graphs.
_EXPAND_DEFAULT_DEPTH = 2
_EXPAND_MAX_DEPTH = 5


_Direction = Literal["outgoing", "incoming", "both"]


@dataclass(frozen=True, slots=True)
class Link:
    """One link as stored in the ``links`` projection.

    Mirrors the columns of :data:`opshub.projections.links.links_table`
    (Phase 8 A2). ``slots=True`` keeps per-instance memory low for
    large traversal results; ``frozen=True`` lets the service return
    links as set members for visited-tracking inside the recursion.

    Attributes
    ----------
    id:
        ULID of the link row.
    from_entity_type / from_entity_id:
        Source side of the directed link.
    to_entity_type / to_entity_id:
        Target side of the directed link.
    link_type:
        One of :data:`opshub.projections.links.LINK_TYPES_MVP` for
        auto-extracted links, or a free-form string for manual links
        (ADR-0017 §決定 (b) — manual link_type is intentionally
        unconstrained).
    created_at:
        Business-time stamp the link was first observed.
    source_event_id:
        ULID of the event that emitted or derived this link
        (``LinkCreated`` for manual links; the derived event id for
        auto-extracted ones). ``None`` only when the link was seeded
        manually without an event (test fixtures).
    metadata:
        Optional JSON blob for link-type specific extras
        (e.g. recall score on ``referenced_in_briefing`` links). The
        service treats the value as opaque.
    """

    id: str
    from_entity_type: str
    from_entity_id: str
    to_entity_type: str
    to_entity_id: str
    link_type: str
    created_at: datetime
    source_event_id: str | None
    metadata: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class LinkPath:
    """A chain of links representing a traversal path.

    ``links`` is the ordered list of links from the starting entity
    outward (in :meth:`LinkService.trace`'s backward semantics, that
    means each successive link's ``to_*`` side equals the prior
    link's ``from_*`` side — i.e. the path is ordered nearest-to-
    farthest from the traversal root). ``depth`` is the number of
    hops (= ``len(links)``) and is provided as a convenience so
    callers don't need to recompute it.
    """

    links: tuple[Link, ...]
    depth: int


@dataclass(frozen=True, slots=True)
class GraphSubset:
    """A bidirectional N-hop graph slice rooted at an entity.

    Returned by :meth:`LinkService.expand`. ``nodes`` is the set of
    entities reached (the root + everything within ``depth`` hops in
    either direction). ``edges`` is the set of links between those
    nodes — sufficient to render a graph (e.g. via Graphviz DOT or
    JSON).

    Sets-of-tuples (rather than lists) because expand may visit the
    same node via multiple paths; we want unique nodes / edges.

    Attributes
    ----------
    root:
        The ``(entity_type, entity_id)`` tuple the expansion started
        from. CLI renderers highlight this node to anchor the graph.
    nodes:
        All reachable entities within ``depth`` hops in either
        direction (the root is always included). ``frozenset`` so
        callers cannot mutate the result and so equality is set-based
        rather than order-sensitive.
    edges:
        Links between visited nodes, deduplicated by ``id`` and
        sorted by ``created_at`` ascending for deterministic CLI
        output. A tuple (rather than ``frozenset``) is used because
        :class:`Link` carries a ``metadata`` dict which is not
        hashable — sorting + dedup-by-id gives the same uniqueness
        guarantee without requiring hashable values.
    depth:
        The depth limit that was applied. Echoed back from the input
        so CLI renderers can label the graph (e.g. "2-hop expansion
        of task:abc") without re-passing the value.
    """

    root: tuple[str, str]
    nodes: frozenset[tuple[str, str]]
    edges: tuple[Link, ...]
    depth: int


class LinkService:
    """Read-only graph traversal over the ``links`` projection.

    The service is stateless beyond the :class:`Engine` reference;
    every public method opens its own short-lived connection.

    Writer dependencies (``store`` / ``projector`` / ``uow_factory`` /
    ``actor``) are optional. When ``None`` (the C1 read-only default),
    :meth:`create_link` / :meth:`delete_link` raise
    :class:`ConfigError` — keeping the existing C1 traversal-only
    callers / tests intact. The Phase 8 D1 wiring in
    :mod:`opshub.cli._wiring` passes all four arguments so the CLI
    surface can mutate state through a single UoW.

    See module docstring for the engine-binding rationale and the
    cycle-detection / depth-limit contract pinned by ADR-0017
    §決定 (e).
    """

    def __init__(
        self,
        engine: Engine,
        *,
        store: EventStore | None = None,
        projector: Projector | None = None,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = "service:links",
    ) -> None:
        self._engine = engine
        self._store = store
        self._projector = projector
        self._uow_factory = uow_factory
        self._actor = actor

    # ------------------------------------------------------------------ commands

    def related(
        self,
        entity_type: str,
        entity_id: str,
        *,
        direction: _Direction = "both",
        link_types: list[str] | None = None,
        limit: int = 100,
    ) -> list[Link]:
        """Return 1-hop links to/from ``(entity_type, entity_id)``.

        Parameters
        ----------
        entity_type, entity_id:
            The entity whose neighbours we want.
        direction:
            * ``"outgoing"`` — only links where this entity is the
              ``from_*`` side
            * ``"incoming"`` — only links where this entity is the
              ``to_*`` side
            * ``"both"`` — union of incoming + outgoing (default)
        link_types:
            If set, restrict to links whose ``link_type`` is in the
            list. ``None`` (default) means all types.
        limit:
            Cap on returned rows (default 100). Applied at the SQL
            layer so large fan-outs don't materialise.

        Returns
        -------
        list[Link]
            Sorted by ``created_at`` ascending so callers see
            oldest-first (provenance order). Empty list when no
            matching link rows exist.
        """
        conditions: list[ColumnElement[bool]] = []
        if direction in ("outgoing", "both"):
            conditions.append(
                (links_table.c.from_entity_type == entity_type)
                & (links_table.c.from_entity_id == entity_id)
            )
        if direction in ("incoming", "both"):
            conditions.append(
                (links_table.c.to_entity_type == entity_type)
                & (links_table.c.to_entity_id == entity_id)
            )

        stmt = select(links_table).where(or_(*conditions))
        if link_types is not None:
            stmt = stmt.where(links_table.c.link_type.in_(link_types))
        stmt = stmt.order_by(links_table.c.created_at).limit(limit)

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_row_to_link(row) for row in rows]

    def trace(
        self,
        entity_type: str,
        entity_id: str,
        *,
        depth: int = _TRACE_DEFAULT_DEPTH,
        link_types: list[str] | None = None,
    ) -> list[LinkPath]:
        """Return backward (incoming) traversal chains up to ``depth`` hops.

        Provenance query: "what led to this entity?". At each level we
        follow links where the current entity is the ``to_*`` side
        (incoming direction), recursing into the resulting ``from_*``
        entities until ``depth`` is exhausted or no more incoming
        links exist.

        Parameters
        ----------
        entity_type, entity_id:
            The terminal entity whose provenance we want.
        depth:
            Maximum number of hops to follow backward. Defaults to
            ``3`` (ADR-0017 §決定 (e)). Must be ``<= 10`` —
            higher values raise :class:`ConfigError` rather than
            silently producing exponential fan-out.
        link_types:
            If set, restrict the recursion to links whose
            ``link_type`` is in the list. ``None`` (default) means
            all types.

        Returns
        -------
        list[LinkPath]
            One :class:`LinkPath` per terminal path found. An entity
            with no incoming links returns an empty list. Each path's
            ``links`` tuple is ordered nearest-to-root → farthest:
            ``path.links[0]`` is the direct incoming edge to the
            starting entity, ``path.links[-1]`` is the deepest
            upstream edge reached.

        Raises
        ------
        ConfigError
            When ``depth > 10`` (the ADR-0017 max). Issued before any
            DB query so the operator sees the error immediately.

        Notes
        -----
        Cycle detection: a ``visited`` set of
        ``(entity_type, entity_id)`` tuples prevents infinite
        recursion in graphs with cycles. A cycle terminates the
        current path; the partial path (including the closing edge)
        is still returned so the operator can see *where* the cycle
        closes.
        """
        if depth > _TRACE_MAX_DEPTH:
            raise ConfigError(f"depth {depth} exceeds max {_TRACE_MAX_DEPTH}")
        # depth <= 0 yields an empty result (we have no edges to
        # report and no recursion to perform). This matches the
        # "entity with no incoming links" behaviour and keeps the
        # CLI happy when a caller passes ``--depth 0`` to mean
        # "just show me what you have already".
        if depth <= 0:
            return []

        # Each ``trace()`` invocation starts with a fresh visited
        # set so traversal state cannot leak across calls.
        visited: set[tuple[str, str]] = {(entity_type, entity_id)}
        paths: list[LinkPath] = []
        self._trace_recurse(
            entity_type=entity_type,
            entity_id=entity_id,
            current_path=[],
            remaining_depth=depth,
            visited=visited,
            link_types=link_types,
            out_paths=paths,
        )
        return paths

    def expand(
        self,
        entity_type: str,
        entity_id: str,
        *,
        depth: int = _EXPAND_DEFAULT_DEPTH,
        link_types: list[str] | None = None,
    ) -> GraphSubset:
        """Bidirectional N-hop graph expansion rooted at ``(entity_type, entity_id)``.

        Walks both outgoing and incoming links from the root,
        recursively up to ``depth`` hops. BFS-style frontier
        expansion with visited tracking prevents revisiting the
        same node (cycle detection) and dedupes edges via the
        ``link.id`` key (so diamond shapes produce distinct edges
        without double-counting).

        Parameters
        ----------
        entity_type, entity_id:
            The root entity. Always included in ``nodes`` even when
            it has no links.
        depth:
            Number of hops to expand. Defaults to ``2`` (ADR-0017
            §決定 (e)). Must be in ``[0, _EXPAND_MAX_DEPTH]``;
            outside that range raises :class:`ConfigError`. ``depth=0``
            returns just the root node with no edges (useful as a
            "describe this entity" call).
        link_types:
            If set, restrict expansion to links whose ``link_type``
            is in the list — applied at *every* hop, not just the
            first. ``None`` (default) means all types.

        Returns
        -------
        GraphSubset
            * ``root`` — the starting node tuple
            * ``nodes`` — all reachable nodes within ``depth`` hops
              (a ``frozenset`` of ``(entity_type, entity_id)`` tuples)
            * ``edges`` — links between visited nodes, deduplicated
              by ``id`` and sorted by ``created_at`` ascending
            * ``depth`` — the applied depth (echoes the input for
              CLI rendering)

        Raises
        ------
        ConfigError
            When ``depth > _EXPAND_MAX_DEPTH`` (the ADR-0017 max of
            ``5``) or ``depth < 0``. Issued before any DB query so
            the operator sees the error immediately.

        Notes
        -----
        Cycle detection: visited nodes are tracked in a set; when an
        outgoing/incoming neighbour resolves to an already-visited
        node we record the edge but do not re-enqueue the node.
        This is stricter than ``trace`` (which backtracks for
        diamond branches) because ``expand`` only needs the *set*
        of reachable nodes — not every path to each node.
        """
        if depth > _EXPAND_MAX_DEPTH:
            raise ConfigError(f"expand depth {depth} exceeds maximum {_EXPAND_MAX_DEPTH}")
        if depth < 0:
            raise ConfigError(f"expand depth must be >= 0, got {depth}")

        root = (entity_type, entity_id)
        nodes: set[tuple[str, str]] = {root}
        edges_by_id: dict[str, Link] = {}

        # BFS by hop: ``frontier`` is the set of nodes whose
        # neighbours we'll inspect next. After visiting them we
        # collect any newly discovered nodes into
        # ``next_frontier`` for the following hop. The loop runs
        # at most ``depth`` times, so total node visits are bounded
        # by ``branching^depth`` — protected at the high end by
        # _EXPAND_MAX_DEPTH.
        frontier: set[tuple[str, str]] = {root}
        for _hop in range(depth):
            if not frontier:
                break
            next_frontier: set[tuple[str, str]] = set()
            for current_type, current_id in frontier:
                # ``related`` already merges outgoing + incoming
                # via direction="both" and applies the link_types
                # filter at the SQL layer. limit=10_000 is a
                # generous safety net; the real bound is the
                # depth cap above.
                links = self.related(
                    current_type,
                    current_id,
                    direction="both",
                    link_types=link_types,
                    limit=10_000,
                )
                for link in links:
                    # Dedupe by link.id so a link encountered from
                    # both endpoints of an edge counts once.
                    edges_by_id.setdefault(link.id, link)
                    outgoing = (link.to_entity_type, link.to_entity_id)
                    incoming = (link.from_entity_type, link.from_entity_id)
                    for candidate in (outgoing, incoming):
                        # Skip self (the endpoint we're expanding
                        # from) and any already-visited node.
                        if candidate == (current_type, current_id):
                            continue
                        if candidate in nodes:
                            continue
                        nodes.add(candidate)
                        next_frontier.add(candidate)
            frontier = next_frontier

        sorted_edges = tuple(sorted(edges_by_id.values(), key=lambda link: link.created_at))
        return GraphSubset(
            root=root,
            nodes=frozenset(nodes),
            edges=sorted_edges,
            depth=depth,
        )

    def find_link_id(
        self,
        *,
        from_entity_type: str,
        from_entity_id: str,
        to_entity_type: str,
        to_entity_id: str,
        link_type: str,
    ) -> str | None:
        """Look up a link's ``id`` by its natural-key tuple.

        Used by the manual ``opshub link remove <link-id>`` CLI path
        (Phase 8 PR D1) when the operator wants to remove by
        natural-key tuple rather than by id directly. Returns
        ``None`` if no matching link exists — callers surface that
        as a user-facing "no such link" message.
        """
        stmt = (
            select(links_table.c.id)
            .where(
                (links_table.c.from_entity_type == from_entity_type)
                & (links_table.c.from_entity_id == from_entity_id)
                & (links_table.c.to_entity_type == to_entity_type)
                & (links_table.c.to_entity_id == to_entity_id)
                & (links_table.c.link_type == link_type)
            )
            .limit(1)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        return str(row[0])

    def list_links(
        self,
        *,
        from_entity_type: str | None = None,
        from_entity_id: str | None = None,
        to_entity_type: str | None = None,
        to_entity_id: str | None = None,
        link_type: str | None = None,
        limit: int = 50,
    ) -> list[Link]:
        """Return links matching the supplied filters (Phase 8 D1).

        Used by ``opshub link list``. All filters are optional; an
        unfiltered call returns the ``limit`` most-recently-created
        links. The result is sorted by ``created_at`` ascending so
        operators see provenance order (oldest-first) — matches the
        :meth:`related` / :meth:`trace` convention.

        ``from_entity_type`` / ``from_entity_id`` filter the source
        side. ``to_entity_type`` / ``to_entity_id`` filter the target
        side. ``link_type`` is an equality filter (single value, not a
        list — the CLI exposes one ``--type`` flag per invocation; the
        traversal-side helpers take a list because they assemble
        multi-type criteria internally).
        """
        conditions: list[ColumnElement[bool]] = []
        if from_entity_type is not None:
            conditions.append(links_table.c.from_entity_type == from_entity_type)
        if from_entity_id is not None:
            conditions.append(links_table.c.from_entity_id == from_entity_id)
        if to_entity_type is not None:
            conditions.append(links_table.c.to_entity_type == to_entity_type)
        if to_entity_id is not None:
            conditions.append(links_table.c.to_entity_id == to_entity_id)
        if link_type is not None:
            conditions.append(links_table.c.link_type == link_type)

        stmt = select(links_table)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(links_table.c.created_at).limit(limit)

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_row_to_link(row) for row in rows]

    # ------------------------------------------------------------------ commands (writer)

    def create_link(
        self,
        *,
        from_entity_type: str,
        from_entity_id: str,
        to_entity_type: str,
        to_entity_id: str,
        link_type: str = "manual",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Emit :class:`LinkCreated` and apply the projector in one UoW.

        Mints a fresh ULID for the link ``aggregate_id``; the projector
        UPSERTs the row through :func:`_upsert_link` so a manual
        ``LinkCreated`` over an auto-extracted natural-key tuple
        collapses onto the existing row (the first-seen ``id`` wins —
        see :mod:`opshub.projections.links` module docstring for the
        collision contract).

        Raises
        ------
        ConfigError
            When the service was constructed without writer
            dependencies (``store`` / ``projector`` / ``uow_factory``).
            Used by the CLI wiring to fail loud if a future refactor
            forgets to thread the dependencies through
            :func:`opshub.cli._wiring.build_link_service`.
        """
        self._require_writer_deps()
        # Lazy imports keep the module-level cold-start surface intact
        # — see module docstring's cold-start guard section. The
        # writer path is rarely hit (manual CRUD only) so paying the
        # import on first call is acceptable.
        from opshub.core.ids import new_ulid
        from opshub.domain.events import LinkCreated

        link_id = new_ulid()
        event = LinkCreated(
            aggregate_id=link_id,
            actor=self._actor,
            from_entity_type=from_entity_type,
            from_entity_id=from_entity_id,
            to_entity_type=to_entity_type,
            to_entity_id=to_entity_id,
            link_type=link_type,
            metadata=metadata,
            created_by=self._actor,
        )
        self._commit(event)
        return link_id

    def delete_link(
        self,
        link_id: str,
        *,
        reason: str | None = None,
    ) -> bool:
        """Emit :class:`LinkDeleted` and apply the projector in one UoW.

        Returns ``True`` when a row was actually deleted, ``False``
        when ``link_id`` did not exist (no-op). The event is appended
        either way — ADR-0017 §決定 (h) keeps the audit trail of
        delete attempts even when the row is already gone.

        ``reason`` is sanitised via
        :func:`opshub.core.sanitise.sanitise_error_message` before
        being stamped on the event (ADR-0017 §決定 (d) / Phase 5 B1
        contract — events are pure value objects so the service
        layer owns the scrub).

        Raises
        ------
        ConfigError
            When the service was constructed without writer
            dependencies.
        """
        self._require_writer_deps()
        from opshub.core.sanitise import sanitise_error_message
        from opshub.domain.events import LinkDeleted

        # Pre-flight existence check so the CLI can show "no-op"
        # message when the id doesn't match a row. The event is still
        # appended afterwards — operators expect the audit trail even
        # for a missed delete attempt.
        existed = self._link_exists(link_id)

        sanitised_reason = None if reason is None else sanitise_error_message(reason)
        event = LinkDeleted(
            aggregate_id=link_id,
            actor=self._actor,
            deleted_by=self._actor,
            reason=sanitised_reason,
        )
        self._commit(event)
        return existed

    # ------------------------------------------------------------------ helpers

    def _require_writer_deps(self) -> None:
        """Guard the writer methods against a read-only construction.

        Phase 8 C1 ships a read-only :class:`LinkService` constructed
        as ``LinkService(engine)``. Phase 8 D1 adds writer methods
        that require ``store`` + ``projector`` + ``uow_factory`` to
        also be set. If a future caller hits the writer path with the
        old shape, we raise :class:`ConfigError` rather than silently
        :class:`AttributeError`-ing on ``self._store.append(...)``.
        """
        if self._store is None or self._projector is None or self._uow_factory is None:
            raise ConfigError(
                "LinkService writer methods require store + projector + uow_factory"
                " — construct via opshub.cli._wiring.build_link_service or pass the"
                " dependencies explicitly."
            )

    def _link_exists(self, link_id: str) -> bool:
        """Return True iff a ``links`` row with ``id == link_id`` exists.

        Run inside a fresh connection (separate from the writer UoW)
        so we can tell the operator whether their ``link remove``
        addressed a real row. The follow-up :class:`LinkDeleted`
        event + projector apply happens in a fresh UoW; the small
        race window between this read and the write is acceptable for
        a single-process CLI (no other writers in scope per ADR-0017).
        """
        stmt = select(links_table.c.id).where(links_table.c.id == link_id).limit(1)
        with self._engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def _commit(self, event: Any) -> None:
        """Append + project ``event`` in one UoW.

        Mirrors :meth:`opshub.services.task_service.TaskService._commit`
        — the writer path is identical in shape (store.append +
        projector.apply on the same connection inside the UoW context
        manager). The factory is mandatory for the writer methods so
        :meth:`_require_writer_deps` is called first.
        """
        # ``cast`` is unnecessary here because _require_writer_deps
        # narrows None away; pyright still wants a runtime assertion
        # to track the narrow through the method body.
        assert self._uow_factory is not None
        assert self._store is not None
        assert self._projector is not None
        with self._uow_factory() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)

    def _trace_recurse(
        self,
        *,
        entity_type: str,
        entity_id: str,
        current_path: list[Link],
        remaining_depth: int,
        visited: set[tuple[str, str]],
        link_types: list[str] | None,
        out_paths: list[LinkPath],
    ) -> None:
        """One level of backward traversal.

        Fetches incoming links to ``(entity_type, entity_id)`` and
        recurses into each upstream entity. Branching: an entity with
        multiple incoming links emits one path per branch. Terminals
        (no more incoming links, or ``remaining_depth`` exhausted, or
        cycle) flush the accumulated ``current_path`` into
        ``out_paths`` if non-empty.

        Recursion / iteration choice: depth is bounded by
        :data:`_TRACE_MAX_DEPTH` (= 10) so Python's default recursion
        limit (1000) is never threatened; an iterative rewrite is not
        worth the readability cost at this depth ceiling.
        """
        if remaining_depth == 0:
            if current_path:
                out_paths.append(
                    LinkPath(
                        links=tuple(current_path),
                        depth=len(current_path),
                    )
                )
            return

        stmt = select(links_table).where(
            (links_table.c.to_entity_type == entity_type)
            & (links_table.c.to_entity_id == entity_id)
        )
        if link_types is not None:
            stmt = stmt.where(links_table.c.link_type.in_(link_types))
        stmt = stmt.order_by(links_table.c.created_at)

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()

        if not rows:
            # Terminal: no more incoming links. Emit the partial
            # path if we've actually traversed at least one edge.
            if current_path:
                out_paths.append(
                    LinkPath(
                        links=tuple(current_path),
                        depth=len(current_path),
                    )
                )
            return

        for row in rows:
            link = _row_to_link(row)
            upstream = (link.from_entity_type, link.from_entity_id)
            if upstream in visited:
                # Cycle: emit the path with this closing edge
                # appended, then stop following this branch. The
                # closing edge is included so the operator can see
                # which link closes the cycle.
                extended = (*current_path, link)
                out_paths.append(
                    LinkPath(
                        links=extended,
                        depth=len(extended),
                    )
                )
                continue
            visited.add(upstream)
            try:
                self._trace_recurse(
                    entity_type=link.from_entity_type,
                    entity_id=link.from_entity_id,
                    current_path=[*current_path, link],
                    remaining_depth=remaining_depth - 1,
                    visited=visited,
                    link_types=link_types,
                    out_paths=out_paths,
                )
            finally:
                # Backtrack so sibling branches can re-visit the
                # node via a different path. Without this, a diamond
                # graph (A → B, A → C, B → D, C → D) would only
                # surface one of the two paths to D.
                visited.discard(upstream)


def _row_to_link(row: Row[tuple[object, ...]]) -> Link:
    """Materialise one ``links`` row into a :class:`Link`.

    Centralises the row → dataclass conversion so the two query
    sites (:meth:`LinkService.related` and the inner recursion in
    :meth:`LinkService._trace_recurse`) stay in sync. The ``metadata``
    column comes through as either ``None`` or a dict (SQLAlchemy's
    JSON column adapter handles the SQLite codec); we cast for the
    benefit of static checkers.
    """
    raw_metadata = row.metadata
    metadata_value: dict[str, object] | None
    if raw_metadata is None:
        metadata_value = None
    elif isinstance(raw_metadata, dict):
        # SQLAlchemy JSON adapter returns ``dict[str, Any]``; the
        # ``cast`` is a no-op at runtime but lets static checkers
        # see the expected shape without inferring ``Unknown``.
        metadata_value = cast("dict[str, Any]", raw_metadata)
    else:
        # Defensive: the projection writes dicts via SQLAlchemy
        # JSON, so anything else means a future caller bypassed
        # the projector. Treat as opaque-None rather than raise so
        # one malformed row doesn't poison an entire traversal.
        metadata_value = None
    return Link(
        id=str(row.id),
        from_entity_type=str(row.from_entity_type),
        from_entity_id=str(row.from_entity_id),
        to_entity_type=str(row.to_entity_type),
        to_entity_id=str(row.to_entity_id),
        link_type=str(row.link_type),
        created_at=row.created_at,
        source_event_id=None if row.source_event_id is None else str(row.source_event_id),
        metadata=metadata_value,
    )
