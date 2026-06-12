"""``slack_demand_digest`` read-model projection (Phase 18-B, ADR-0033).

Materialises a per-channel x per-demand-kind digest of the most recent
Slack message that **demands** the operator's attention:

* a body that mentions the operator by ``<@self_user_id>`` literal
  (``demand_kind = "mention"``), or
* a DM the operator participates in (``demand_kind = "dm"``).

The projection consumes the existing :class:`SourceObserved` event
stream emitted by the Phase 7 Slack connector — no new fetcher /
mapper / event is introduced (ADR-0033 §Decision (a), event-sourced
architecture remains the single source of truth, ADR-0002).

Self user id resolution (per workspace)
---------------------------------------

The mention path needs the operator's Slack ``U...`` id to spot the
``<@U...>`` literal in the message body. ``U...`` ids are
**workspace-local** — the same human has a different id in every
workspace — so Phase 24-C ([ADR-0041](
../../../docs/adr/0041-slack-multi-workspace.md) §(g)) replaces the
former install-wide single id with a ``{team_id: self_user_id}`` map.
The map is resolved lazily on the first Slack event and memoised for
the projection lifetime (ADR-0033 §Decision (f) — never hit Slack's
``auth.test`` per event): for each configured
``[connectors.slack.workspaces.<alias>]`` table the resolver consults,
in order,

1. the per-alias env override
   ``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>`` (alias upper-cased; the
   alias grammar bans ``-`` so the mapping is injective). The value
   must be ``"T...:U..."`` — **team-qualified** — because the env path
   exists precisely for hosts where Slack is unreachable (CI /
   headless docker), and without a live ``auth.test`` there is nothing
   else to bind the alias to its ``team_id``. A bare ``U...`` value is
   rejected with a warning naming the expected shape.
2. :meth:`opshub.connectors.slack.auth.SlackAuth.test_token` for the
   alias (production path; one call per alias per projection
   lifetime), which yields both the ``team_id`` and the ``user_id``.

Tests / embedded callers can bypass the cascade entirely by passing an
explicit ``self_user_ids`` mapping (``{team_id: user_id}``) to the
constructor.

Per-alias failures are fail-soft: the alias is skipped with a warning
and the remaining workspaces still resolve. When **no** workspace
resolves, the projection logs a single warning and skips mention
detection (DM detection still works) — the rebuild driver fans every
event out to every projection and we must not crash the whole replay
just because the Slack projection cannot find a self id.

Demand detection
----------------

* **DM** (``demand_kind = "dm"``) is keyed off the channel id prefix:
  Slack DM ids begin with ``"D"``. The cheaper alternative
  (consulting ``raw["channel_type"]`` on the source event) is not
  available because :class:`SourceObserved` only carries the
  normalised connector fields (title / body / external_id / ...),
  not the raw Slack payload. The prefix rule is well-documented and
  Slack-stable; see :mod:`opshub.connectors.slack.conversations` for
  the discovery-time equivalent (``_TYPE_FROM_ROW_ORDER``).
* **Mention** (``demand_kind = "mention"``) is keyed off a literal
  ``<@<self_user_id>>`` substring search in the message body. Slack
  serialises every operator mention as this literal regardless of
  the surface (web / mobile / API) so a single string match is
  sufficient. ADR-0033 §Consequences §Negative §"mention parse の
  脆さ" carries the future-proofing note.

For a public-channel message that mentions self, only the
``"mention"`` row is upserted. For a DM that mentions self, both
``"mention"`` AND ``"dm"`` rows are upserted (the channel demand and
the self-mention are independent signals and the operator might
filter on either). The two rows share ``channel_id`` but differ on
``demand_kind``, so the natural-key PK keeps them distinct.

Self-authored suppression (Phase 23-D, issue #534)
--------------------------------------------------

A DM or mention the **operator themselves** authored is not a demand
on them: surfacing a DM the operator already replied to as "next to
read" is a corrosive false positive (an end-user who sees a stale
self-reply at the top of ``next-actions`` learns the digest is
unreliable). The Slack mapper now threads the message author's
``U...`` id onto :attr:`SourceObserved.author_id`; when it equals the
resolved operator self id the projection drops the event entirely, so
the digest row always reflects the *peer's* last ping. Because the
upsert is high-water on ``last_demand_ts``, suppressing self-authored
events also stops the operator's own reply from overwriting the
excerpt / permalink of the peer's actionable message.

Demand kinds (Phase 23-D revision)
----------------------------------

The projection writes exactly two ``demand_kind`` values —
``"mention"`` and ``"dm"``. The historical ``"mpim"`` placeholder was
removed in Phase 23-D (issue #534): the apply path never produced it
(group-DM ``<@self>`` messages land in the ``"mention"`` row), so it
was a structurally-unreachable enum value across the CHECK constraint,
the CLI filter, and the MCP schema.

Idempotency / replay
--------------------

Every apply is an INSERT-ON-CONFLICT-DO-UPDATE keyed on
``(channel_id, demand_kind)``. The UPDATE arm is guarded by a
``WHERE last_demand_ts < excluded.last_demand_ts`` clause so a replay
that re-encounters an older message never overwrites the latest one
— the projection is therefore replay-order-independent (two
:func:`opshub.projections.rebuild.rebuild_all` runs converge to the
same row set regardless of how events are interleaved).

The projection deliberately does **not** consume :class:`SourceObserved`
events from non-Slack connectors. The reducer filters by
``connector_name == "slack"`` + ``source_type == "slack_message"`` so
the rebuild driver's fan-out is cheap and a future Gmail mention
analogue lives in a separate projection (ADR-0033 §Consequences
§Positive).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    delete,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.connectors.slack.mapper import SOURCE_TYPE as SLACK_SOURCE_TYPE
from opshub.connectors.slack.mapper import SUMMARY_MAX_CHARS
from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, SourceObserved

__all__ = [
    "CHANNEL_TYPES",
    "DEMAND_KINDS",
    "SELF_USER_ID_ENV_PREFIX",
    "SlackDemandDigestProjection",
    "slack_demand_digest_table",
]


_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------- public enums

#: Permitted ``demand_kind`` values; mirrors the migration's CHECK
#: constraint so a mismatch surfaces at type-check time rather than as
#: an opaque ``IntegrityError`` at runtime.
#:
#: Phase 23-D (issue #534) dropped the dead ``"mpim"`` value: the apply
#: path never wrote it (group-DM ``<@self>`` messages land in the
#: ``"mention"`` row), so the enum / CHECK constraint / MCP schema
#: carried a structurally-unreachable third value. Pre-userbase posture
#: (no installed base) lets us tighten the enum to exactly the two
#: values the projection produces. A future MPIM-specific refinement, if
#: ever needed, re-adds the value with its own migration + apply branch.
DEMAND_KINDS: tuple[str, ...] = ("mention", "dm")

#: Permitted ``channel_type`` values; mirrors the migration's CHECK
#: constraint and the discovery-time
#: :data:`opshub.connectors.slack.conversations.CONVERSATION_TYPES`
#: enum. The projection records the type so the CLI can filter
#: (``opshub slack mentions list --types im,mpim``) without joining
#: a hypothetical Slack conversations projection (which does not
#: exist — discovery is a CLI-only feature per Phase 17).
CHANNEL_TYPES: tuple[str, ...] = ("im", "mpim", "private", "public")


#: Per-alias environment override prefix consulted by
#: :class:`SlackDemandDigestProjection` (Phase 24-C, ADR-0041 §(g)).
#: Operators export ``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>=T0123ABC:U123ABC``
#: (one per configured workspace alias, upper-cased; the value is
#: team-qualified — see the module docstring) to drive
#: ``opshub projections rebuild`` in environments where the keyring /
#: Slack API is not available (CI, headless docker, ...). The
#: pre-Phase-24 install-wide ``OPSHUB_SLACK_SELF_USER_ID`` variable is
#: gone (hard flip — a single id cannot be correct across N
#: workspaces). Documented in ``docs/troubleshooting.md`` §Slack
#: demand digest.
SELF_USER_ID_ENV_PREFIX = "OPSHUB_SLACK_SELF_USER_ID__"


# ---------------------------------------------------------------- table shape


slack_demand_digest_table: Table = Table(
    "slack_demand_digest",
    metadata,
    Column("channel_id", Text(), primary_key=True),
    Column("channel_type", Text(), nullable=False),
    Column("channel_name", Text(), nullable=True),
    Column("demand_kind", Text(), primary_key=True),
    Column("last_demand_ts", Float(), nullable=False),
    Column("last_demand_user_id", Text(), nullable=True),
    Column("last_demand_excerpt", Text(), nullable=True),
    Column("last_demand_permalink", Text(), nullable=True),
    Column(
        "last_source_id",
        String(length=26),
        ForeignKey(
            "sources.id",
            name="fk_slack_demand_digest_last_source_id_sources",
            ondelete="SET NULL",
        ),
        nullable=True,
    ),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "channel_type IN ('im', 'mpim', 'private', 'public')",
        name="ck_slack_demand_digest_channel_type_valid",
    ),
    CheckConstraint(
        "demand_kind IN ('mention', 'dm')",
        name="ck_slack_demand_digest_demand_kind_valid",
    ),
    Index(
        "ix_slack_demand_digest_last_demand_ts",
        "last_demand_ts",
    ),
    Index(
        "ix_slack_demand_digest_type_ts",
        "channel_type",
        "last_demand_ts",
    ),
)
"""SQLAlchemy ``Table`` mirroring the ``slack_demand_digest`` physical schema.

Created by migration ``0029_create_slack_demand_digest``; the
``demand_kind`` CHECK constraint was tightened to ``('mention', 'dm')``
by migration ``0032_drop_mpim_demand_kind`` (Phase 23-D, issue #534),
so this metadata-only ``Table`` reflects the post-0032 2-value enum.

The index ordering here intentionally drops the DESC qualifier the
migration uses on the physical index — SQLAlchemy 2.x does not surface
DESC index columns on a metadata-only :class:`Table` and the rebuild
driver only consults this object for the column shape, never for the
operational query plan. The migration retains the DESC ordering on
disk so ``ORDER BY last_demand_ts DESC LIMIT N`` reads the index in
forward sequence (no SQLite extra-sort pass).
"""


# ---------------------------------------------------------------- projection


class SlackDemandDigestProjection:
    """Reducer mapping Slack :class:`SourceObserved` events to digest rows.

    The reducer is a pure dispatch on event type + Slack source_type:
    it issues one INSERT-ON-CONFLICT-DO-UPDATE per qualifying event
    per detected demand kind. Each statement runs on the Connection
    passed in by the rebuild driver — the projection never opens its
    own transaction (see :class:`opshub.projections.base.Projection`).

    Event handling
    --------------

    * :class:`SourceObserved` with ``connector_name == "slack"`` AND
      ``source_type == "slack_message"`` — parse the ``external_id``
      (``"{team_id}:{channel_id}:{ts}"``, Phase 24-B) into its
      components and:

      - If :attr:`SourceObserved.author_id` equals the resolved
        operator self id, the event is dropped before any row is
        written (self-authored suppression, Phase 23-D / issue #534).
      - If the channel_id starts with ``"D"`` upsert the ``"dm"`` row
        (Slack DM channels always have a ``D...`` id; see module
        docstring for the prefix rule rationale).
      - If the body literal-contains ``"<@<self_user_id>>"`` upsert
        the ``"mention"`` row.
      - A DM that also mentions self produces both rows.

    * Any other event (or any Slack source without a body) — ignored.
      The rebuild driver fans every event out to every projection, so
      this reducer must remain a no-op for non-target events.

    Self user id resolution (per workspace)
    ---------------------------------------

    Phase 24-C ([ADR-0041](../../../docs/adr/0041-slack-multi-workspace.md)
    §(g)): the operator's ``U...`` id is workspace-local, so the
    projection holds a ``{team_id: self_user_id}`` map. The constructor
    accepts an explicit ``self_user_ids`` mapping (tests, embedded
    callers); otherwise the map is resolved lazily on the first Slack
    event, per configured workspace alias, via the per-alias env
    override (``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>=T...:U...``) falling
    back to a per-alias
    :meth:`opshub.connectors.slack.auth.SlackAuth.test_token` call —
    see the module docstring for the full cascade.

    If no workspace resolves, the projection logs a single WARNING and
    skips mention detection / self-suppression for the rest of its
    lifetime (DM detection still works). We deliberately do NOT raise —
    the rebuild driver applies every event to every projection, and a
    missing Slack token must not crash unrelated projection writes
    (tasks / inbox / sources / ...). Operators see the warning in the
    rebuild log and can re-run after fixing auth.
    """

    name = "slack_demand_digest"

    def __init__(self, *, self_user_ids: Mapping[str, str] | None = None) -> None:
        """Construct the projection with an optional explicit self-id map.

        Parameters
        ----------
        self_user_ids:
            ``{team_id: U... id}`` map of the operator's per-workspace
            Slack ids. ``None`` (default) defers resolution to the
            per-alias env-var → ``auth.test`` cascade described in the
            class docstring. When provided, the map is used verbatim
            (no env / auth consultation) so tests stay hermetic.
        """
        self._explicit_self_user_ids = dict(self_user_ids) if self_user_ids is not None else None
        # ``_resolved_self_user_ids`` is filled on first :meth:`apply`
        # so the constructor stays I/O-free (the registry materialises
        # the projection list at CLI cold start; calling Slack
        # auth.test up-front would inflate ``opshub --help`` past the
        # ADR-0001 300ms budget). ``None`` = cascade not yet run.
        self._resolved_self_user_ids: dict[str, str] | None = None
        # Pre-computed ``{team_id: "<@U...>"}`` literals we look for in
        # message bodies; the ``<@>`` framing is Slack-stable and
        # identical across surfaces. Filled in lock-step with the map.
        self._mention_literals: dict[str, str] = {}

    # ----------------------------------------------------- Projection protocol

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the demand digest table if it qualifies.

        Filters in order:

        1. event must be :class:`SourceObserved`,
        2. ``connector_name`` must be ``"slack"``,
        3. ``source_type`` must equal :data:`SLACK_SOURCE_TYPE`,
        4. ``external_id`` must parse as ``"<team_id>:<channel_id>:<ts>"``
           (Phase 24-B 3-token shape; legacy 2-token events drop),
        5. the ``last_demand_ts`` must compute (a malformed ts is
           treated as a non-event and skipped).

        Non-Slack events (tasks / inbox / sources from other
        connectors / ...) are silently dropped — the rebuild driver
        fans every event out to every projection, so this reducer
        must not crash on unrelated payloads.
        """
        if not isinstance(event, SourceObserved):
            return
        if event.connector_name != "slack":
            return
        if event.source_type != SLACK_SOURCE_TYPE:
            return

        parsed = _parse_slack_external_id(event.external_id)
        if parsed is None:
            return
        team_id, channel_id, ts_value = parsed

        self_user_id = self._self_user_id_for(team_id)
        # ``self_user_id`` is required for the mention path; the DM
        # path is independent of it (the channel id prefix is enough).
        # When the cascade resolves nothing for this event's workspace
        # (team_id), the mention path is silently skipped but DM
        # detection still works for operators who only care about that
        # signal. Phase 24-C: the lookup is **team-scoped** — workspace
        # A's self id never matches (or suppresses) workspace B's
        # messages, even when the raw ``U...`` strings collide across
        # workspaces (ADR-0041 §(g)).

        # Phase 23-D (issue #534): the message author's Slack ``U...`` id
        # now rides on :attr:`SourceObserved.author_id` (threaded by the
        # mapper). A DM / mention the operator *themselves* authored is
        # not a demand on them — surfacing a DM they already replied to as
        # "next to read" is the precise false positive #534 fixes. When
        # ``author_id`` matches the resolved self id we drop the event so
        # the digest row reflects the peer's last ping, not the
        # operator's own reply.
        last_demand_user_id = event.author_id
        if (
            self_user_id is not None
            and last_demand_user_id is not None
            and last_demand_user_id == self_user_id
        ):
            return

        is_dm = _is_dm_channel(channel_id)
        # ``_self_user_id_for`` resolution fills ``_mention_literals``
        # in lock-step, so the ``is not None`` narrowing here matches
        # the runtime invariant. The narrowed form keeps strict pyright
        # happy without an extra ``cast``.
        mention_literal = self._mention_literals.get(team_id)
        # epic #470 / issue #481: ``SourceObserved.body`` is required +
        # non-empty (``min_length=1``) so the previous ``event.body or
        # ""`` fallback is gone — read ``event.body`` directly.
        is_mention = (
            self_user_id is not None
            and mention_literal is not None
            and mention_literal in event.body
        )

        if not is_dm and not is_mention:
            return

        channel_type = _classify_channel_type(channel_id)
        # Phase 23-D (issue #534, あるべき #4): resolve a human label so
        # the MCP / CLI surface never leaks an opaque ``D...`` id. For a
        # DM the Slack fetcher has no channel ``name`` and falls back to
        # the channel id, so ``_extract_channel_name`` would return the
        # ``D...`` id; instead surface the peer's display name, which the
        # mapper bakes into the title prefix (``"{peer} in #..."``).
        # Channel / private rows keep the ``#name`` extraction.
        if is_dm:
            channel_name = _extract_dm_peer_name(event.title) or _extract_channel_name(event.title)
        else:
            channel_name = _extract_channel_name(event.title)
        excerpt = _build_excerpt(event)
        updated_at = event.occurred_at.astimezone(UTC)

        if is_dm:
            self._upsert_row(
                conn,
                channel_id=channel_id,
                channel_type=channel_type,
                channel_name=channel_name,
                demand_kind="dm",
                last_demand_ts=ts_value,
                last_demand_user_id=last_demand_user_id,
                last_demand_excerpt=excerpt,
                last_demand_permalink=event.url,
                last_source_id=event.aggregate_id,
                updated_at=updated_at,
            )
        if is_mention:
            self._upsert_row(
                conn,
                channel_id=channel_id,
                channel_type=channel_type,
                channel_name=channel_name,
                demand_kind="mention",
                last_demand_ts=ts_value,
                last_demand_user_id=last_demand_user_id,
                last_demand_excerpt=excerpt,
                last_demand_permalink=event.url,
                last_source_id=event.aggregate_id,
                updated_at=updated_at,
            )

    def reset(self, conn: Connection) -> None:
        """Empty the ``slack_demand_digest`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store.
        """
        conn.execute(delete(slack_demand_digest_table))

    # ------------------------------------------------------------------ helpers

    def _self_user_id_for(self, team_id: str) -> str | None:
        """Return the operator's self user id in workspace ``team_id``, or ``None``.

        Runs the per-workspace resolution cascade once (memoised across
        the projection lifetime — ADR-0033 §Decision (f)) and looks the
        event's ``team_id`` up in the resulting map. A successfully
        resolved workspace also fills its :attr:`_mention_literals`
        entry for the body-substring check.
        """
        ids = self._resolved_self_user_ids
        if ids is None:
            ids = self._resolve_self_user_ids()
            self._resolved_self_user_ids = ids
            self._mention_literals = {team: f"<@{uid}>" for team, uid in ids.items()}
            if not ids:
                _LOGGER.warning(
                    "slack_demand_digest: cannot resolve any workspace's "
                    "operator self user id (no explicit map, no %s<ALIAS> "
                    "env vars, no reachable Slack tokens). Mention "
                    "detection will be skipped for the duration of this "
                    "rebuild; DM detection still works.",
                    SELF_USER_ID_ENV_PREFIX,
                )
        return ids.get(team_id)

    def _resolve_self_user_ids(self) -> dict[str, str]:
        """Build the ``{team_id: self_user_id}`` map (Phase 24-C, ADR-0041 §(g)).

        Explicit constructor map wins verbatim. Otherwise each
        configured ``[connectors.slack.workspaces.<alias>]`` table is
        resolved independently: per-alias env override first
        (``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>``, team-qualified
        ``"T...:U..."`` value), then a per-alias
        :meth:`SlackAuth.test_token` call. Per-alias failures are
        fail-soft (warning + skip) so one unreachable workspace never
        blinds the others' mention detection.
        """
        if self._explicit_self_user_ids is not None:
            return dict(self._explicit_self_user_ids)

        resolved: dict[str, str] = {}
        for alias in _configured_workspace_aliases():
            entry = _self_id_from_env(alias)
            if entry is None:
                entry = _self_id_from_auth(alias)
            if entry is None:
                continue
            team_id, user_id = entry
            resolved[team_id] = user_id
        return resolved

    def _upsert_row(
        self,
        conn: Connection,
        *,
        channel_id: str,
        channel_type: str,
        channel_name: str | None,
        demand_kind: str,
        last_demand_ts: float,
        last_demand_user_id: str | None,
        last_demand_excerpt: str | None,
        last_demand_permalink: str | None,
        last_source_id: str,
        updated_at: datetime,
    ) -> None:
        """Upsert a single ``(channel_id, demand_kind)`` digest row.

        The UPDATE arm only fires when the inbound ``last_demand_ts``
        is **strictly newer** than the persisted one — without that
        guard a replay that re-encounters an older message would
        clobber the latest one and the projection would no longer be
        replay-order-independent.

        SQLite's ``ON CONFLICT(target) DO UPDATE SET ... WHERE ...``
        clause is exactly the right primitive here; SQLAlchemy
        surfaces it through ``stmt.on_conflict_do_update(...,
        where=...)``.
        """
        stmt = sqlite_insert(slack_demand_digest_table).values(
            channel_id=channel_id,
            channel_type=channel_type,
            channel_name=channel_name,
            demand_kind=demand_kind,
            last_demand_ts=last_demand_ts,
            last_demand_user_id=last_demand_user_id,
            last_demand_excerpt=last_demand_excerpt,
            last_demand_permalink=last_demand_permalink,
            last_source_id=last_source_id,
            updated_at=updated_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["channel_id", "demand_kind"],
            set_={
                "channel_type": stmt.excluded.channel_type,
                "channel_name": stmt.excluded.channel_name,
                "last_demand_ts": stmt.excluded.last_demand_ts,
                "last_demand_user_id": stmt.excluded.last_demand_user_id,
                "last_demand_excerpt": stmt.excluded.last_demand_excerpt,
                "last_demand_permalink": stmt.excluded.last_demand_permalink,
                "last_source_id": stmt.excluded.last_source_id,
                "updated_at": stmt.excluded.updated_at,
            },
            where=slack_demand_digest_table.c.last_demand_ts < stmt.excluded.last_demand_ts,
        )
        conn.execute(stmt)


# ---------------------------------------------------------------- module-level helpers


def _parse_slack_external_id(external_id: str) -> tuple[str, str, float] | None:
    """Split a Slack ``external_id`` (``"{team_id}:{channel_id}:{ts}"``).

    Returns ``(team_id, channel_id, ts_float)`` on success, ``None`` on
    any parse failure. Defensive parsing is justified by the rebuild
    driver: a malformed event must not crash the whole replay.

    Phase 24-B ([ADR-0041](docs/adr/0041-slack-multi-workspace.md)
    §(a) §(g)): the natural key gained a leading ``team_id`` token.
    Phase 24-C consumes it for the per-workspace self-id lookup; the
    digest row key stays ``(channel_id, demand_kind)`` until the Phase
    24-D ``(team_id, channel)`` re-key.

    Legacy 2-token events (``"{channel_id}:{ts}"``, pre-Phase-24
    ingest) deliberately return ``None`` — i.e. they are **dropped**
    from the digest. The sanctioned upgrade path is a DB re-init +
    full re-sync (ADR-0041 §(e)), after which every replayed event
    carries the 3-token shape; silently accepting both shapes would
    let one message appear under two natural keys and double-count
    demands. The drop is pinned explicitly by
    ``test_legacy_two_token_external_id_is_dropped``.
    """
    if not external_id:
        return None
    # Slack ``ts`` is a string like ``"1700000000.123456"`` — split on
    # the first two ``":"`` separators so a ``ts`` that hypothetically
    # contains a colon never bleeds into the channel token (defensive;
    # the current Slack format does not contain one).
    team_id, sep1, rest = external_id.partition(":")
    if not sep1 or not team_id:
        return None
    channel_id, sep2, tail = rest.partition(":")
    if not sep2 or not channel_id or not tail:
        return None
    try:
        ts_value = float(tail)
    except (TypeError, ValueError):
        return None
    return team_id, channel_id, ts_value


def _is_dm_channel(channel_id: str) -> bool:
    """Return ``True`` if ``channel_id`` is a Slack DM (``D...``).

    Slack channel id prefixes:

    * ``C`` — public channel
    * ``G`` — private channel / legacy mpim
    * ``D`` — direct message
    * ``CMPDM`` (rare) / future prefixes — treated as non-DM here
      because the operator-facing semantic of "DM" is "1:1 with the
      operator", which only ``D...`` ids represent in Slack's data
      model. MPIMs (group DMs) live under ``G`` ids and are picked
      up by the mention path when the body contains ``<@self>``.
    """
    return channel_id.startswith("D")


def _classify_channel_type(channel_id: str) -> str:
    """Map a Slack channel id prefix to a :data:`CHANNEL_TYPES` value.

    Best-effort: Slack does not currently expose ``private`` vs
    ``mpim`` from the ``G...`` prefix alone, so both collapse to
    ``"private"`` here. The discovery-time
    :func:`opshub.connectors.slack.conversations._classify_type` has
    the boolean flags (``is_mpim`` / ``is_private``) that distinguish
    them, but those are not available on :class:`SourceObserved` —
    threading them through would require a connector-side event
    schema bump that ADR-0033 explicitly forbids ("connector /
    fetcher / mapper / event schema には触れない").
    """
    if channel_id.startswith("D"):
        return "im"
    if channel_id.startswith("G"):
        # Phase 18-B accepts the (private, mpim) ambiguity per ADR-0033
        # §Decision (b) note above. Operators distinguish via
        # ``opshub slack conversations`` (discovery surface).
        return "private"
    # Default: any ``C...`` id, plus any unknown future prefix, is
    # treated as a public channel. The CLI ``--types`` filter can
    # narrow to ``public`` when this misclassifies a niche prefix.
    return "public"


def _extract_channel_name(title: str | None) -> str | None:
    """Best-effort recovery of the channel name from the source title.

    The Phase 7 Slack mapper (see :mod:`opshub.connectors.slack.mapper`)
    builds the title as ``"{user} in #{channel}: {excerpt}"`` for
    ordinary messages and ``"{user} joined #{channel}"`` /
    ``"{user} set #{channel} purpose: ..."`` for system subtypes. The
    common shape is a ``"#{channel}"`` token somewhere in the string.

    We extract it by searching for ``" in #"`` (the dominant arm)
    first, then for any standalone ``"#"`` token as a fallback. The
    extracted name is bounded to ``250`` characters as a defensive
    cap on the column write.

    Returns ``None`` when no ``#`` marker is present — the column is
    nullable to accommodate this case (and a future Slack source
    whose title shape diverges).
    """
    if not title:
        return None
    needle = " in #"
    idx = title.find(needle)
    if idx >= 0:
        rest = title[idx + len(needle) :]
        # The mapper's standard format ends the channel token with
        # ``": "``; anything after that is the body excerpt.
        cut = rest.find(":")
        if cut >= 0:
            rest = rest[:cut]
        return rest.strip()[:250] or None
    # Subtype variants: ``"{user} joined #channel"`` etc. Look for the
    # last ``#`` token.
    hash_idx = title.find("#")
    if hash_idx >= 0:
        rest = title[hash_idx + 1 :]
        # System messages end at first whitespace.
        cut = rest.find(" ")
        if cut >= 0:
            rest = rest[:cut]
        return rest.strip()[:250] or None
    return None


def _extract_dm_peer_name(title: str | None) -> str | None:
    """Best-effort recovery of a DM peer's display name from the title.

    Phase 23-D (issue #534, あるべき #4). A DM has no Slack channel
    ``name`` so the fetcher falls back to the channel id; the Phase 7
    mapper then builds the title as ``"{peer} in #{D...}: {excerpt}"``
    (the ``{peer}`` prefix is the message author's resolved display
    name). For DM rows we surface that peer name instead of the opaque
    ``D...`` id so ``slack.demand.list`` / ``opshub slack mentions list``
    show "alice" rather than "D04ABCXYZ".

    Extracts the substring **before** the dominant ``" in #"`` separator.
    Returns ``None`` when the separator is absent (system-message
    subtypes such as ``"{peer} joined #..."`` — those are not DM
    demands in practice, and the caller falls back to
    :func:`_extract_channel_name`). The result is bounded to ``250``
    characters as a defensive cap on the column write.
    """
    if not title:
        return None
    needle = " in #"
    idx = title.find(needle)
    if idx <= 0:
        return None
    peer = title[:idx].strip()
    return peer[:250] or None


def _build_excerpt(event: SourceObserved) -> str | None:
    """Return the short body excerpt persisted on the digest row.

    Prefers the Slack mapper's already-truncated ``summary``
    (≤ :data:`SUMMARY_MAX_CHARS` per
    :mod:`opshub.connectors.slack.mapper`) and falls back to a bounded
    slice of ``body`` for sources whose summary is missing (e.g. very
    old Phase 7 events written before the summary was populated for
    every subtype).
    """
    if event.summary:
        return event.summary[:SUMMARY_MAX_CHARS]
    body = event.body
    if not body:
        return None
    if len(body) <= SUMMARY_MAX_CHARS:
        return body
    return body[: SUMMARY_MAX_CHARS - 1] + "…"


def _configured_workspace_aliases() -> list[str]:
    """Return the configured Slack workspace aliases (fail-soft).

    Reads ``[connectors.slack.workspaces]`` from the settings. Any
    failure (config parse error, settings unavailable in an embedded
    context) degrades to an empty list — the projection must never
    crash the rebuild fan-out over a Slack config problem.
    """
    try:
        from opshub.core.config import OpsHubSettings

        return sorted(OpsHubSettings().connectors.slack.workspaces)
    except Exception:
        return []


def _self_id_from_env(alias: str) -> tuple[str, str] | None:
    """Parse the per-alias env override into ``(team_id, user_id)``.

    Phase 24-C (ADR-0041 §(g)): the value of
    ``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>`` must be team-qualified
    (``"T0123ABC:U0123456"``) because the env path exists for hosts
    where Slack is unreachable — without a live ``auth.test`` there is
    nothing else to bind the alias to its ``team_id`` (the digest map
    is keyed on the ``team_id`` each event's ``external_id`` carries).
    A bare ``U...`` value (the pre-Phase-24 single-workspace spelling)
    is rejected with a warning naming the expected shape; returning
    ``None`` lets the auth fallback try instead.
    """
    raw = os.environ.get(f"{SELF_USER_ID_ENV_PREFIX}{alias.upper()}")
    if not raw or not raw.strip():
        return None
    team_id, sep, user_id = raw.strip().partition(":")
    if not sep or not team_id or not user_id:
        _LOGGER.warning(
            "slack_demand_digest: %s%s=%r is not team-qualified; expected "
            "'<team_id>:<user_id>' (e.g. 'T0123ABC:U0123456'). Ignoring the "
            "override for workspace %r.",
            SELF_USER_ID_ENV_PREFIX,
            alias.upper(),
            raw,
            alias,
        )
        return None
    return team_id, user_id


def _self_id_from_auth(alias: str) -> tuple[str, str] | None:
    """Call ``SlackAuth(alias).test_token`` for ``(team_id, user_id)``.

    Returns ``None`` on any failure (no token stored for the alias, SDK
    extras missing, network error, Slack ``ok: false``). The caller
    treats ``None`` as "this workspace's self id is unavailable for
    this projection lifetime" and skips its mention detection while the
    other workspaces still resolve (per-alias fail-soft, ADR-0041
    §(g)).

    The import is deferred to avoid pulling :mod:`opshub.connectors.slack`
    onto the CLI cold-start path (ADR-0001) when the registry simply
    materialises the projection list without ever applying an event.
    """
    try:
        from opshub.connectors.slack.auth import SlackAuth
        from opshub.core.errors import ConfigError
    except ImportError:  # pragma: no cover — defensive against extras pruning
        return None

    try:
        auth = SlackAuth(alias)
        result: dict[str, Any] = dict(auth.test_token())
    except ConfigError:
        return None
    except Exception:  # last-resort fail-soft, see module docstring
        return None
    team_id = result.get("team_id") or ""
    user_id = result.get("user_id") or ""
    if not team_id or not user_id:
        return None
    return team_id, user_id
