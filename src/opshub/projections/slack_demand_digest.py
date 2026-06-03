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

Self user id resolution
-----------------------

The mention path needs the operator's Slack ``U...`` id to spot the
``<@U...>`` literal in the message body. We resolve it once at
construction time via the Phase 7 Slack auth helper
(:meth:`opshub.connectors.slack.auth.SlackAuth.test_token`) and cache
it for the lifetime of the projection instance (ADR-0033 §Decision
(f) — never hit Slack's ``auth.test`` per event).

Operators / tests that want to drive the projection without a real
Slack token can either:

* pass an explicit ``self_user_id`` to the constructor (preferred for
  unit tests), or
* set the ``OPSHUB_SLACK_SELF_USER_ID`` environment variable (handy
  for ``opshub projections rebuild`` in CI when the keyring is
  unavailable but the operator already knows their Slack id).

When neither is configured **and** ``auth.test`` is not reachable,
the projection logs a single warning and silently skips every Slack
event — the rebuild driver fans every event out to every projection
and we must not crash the whole replay just because the Slack
projection cannot find a self id.

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
    "SELF_USER_ID_ENV_VAR",
    "SlackDemandDigestProjection",
    "slack_demand_digest_table",
]


_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------- public enums

#: Permitted ``demand_kind`` values; mirrors the migration's CHECK
#: constraint so a mismatch surfaces at type-check time rather than as
#: an opaque ``IntegrityError`` at runtime. ADR-0033 §Decision (b)
#: §不変条件 #2 pins the enum as 3 values (``mention`` / ``dm`` /
#: ``mpim``) — Phase 18-B writes ``mention`` and ``dm`` only (group-DM
#: messages with a ``<@self>`` literal land in the mention row), but
#: the CLI filter and the CHECK constraint admit all three so a
#: Phase 19+ MPIM-specific refinement can land without a schema bump.
DEMAND_KINDS: tuple[str, ...] = ("mention", "dm", "mpim")

#: Permitted ``channel_type`` values; mirrors the migration's CHECK
#: constraint and the discovery-time
#: :data:`opshub.connectors.slack.conversations.CONVERSATION_TYPES`
#: enum. The projection records the type so the CLI can filter
#: (``opshub slack mentions list --types im,mpim``) without joining
#: a hypothetical Slack conversations projection (which does not
#: exist — discovery is a CLI-only feature per Phase 17).
CHANNEL_TYPES: tuple[str, ...] = ("im", "mpim", "private", "public")


#: Environment variable consulted by :class:`SlackDemandDigestProjection`
#: when the constructor receives neither an explicit ``self_user_id``
#: nor a reachable Slack auth. Operators can export
#: ``OPSHUB_SLACK_SELF_USER_ID=U123ABC`` to drive
#: ``opshub projections rebuild`` in environments where the keyring is
#: not available (CI, headless docker, ...). Documented in
#: ``docs/troubleshooting.md`` §Slack demand digest.
SELF_USER_ID_ENV_VAR = "OPSHUB_SLACK_SELF_USER_ID"


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
        "demand_kind IN ('mention', 'dm', 'mpim')",
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
"""SQLAlchemy ``Table`` mirroring migration ``0029_create_slack_demand_digest``.

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
      (``"{channel_id}:{ts}"``) into its components and:

      - If the channel_id starts with ``"D"`` upsert the ``"dm"`` row
        (Slack DM channels always have a ``D...`` id; see module
        docstring for the prefix rule rationale).
      - If the body literal-contains ``"<@<self_user_id>>"`` upsert
        the ``"mention"`` row.
      - A DM that also mentions self produces both rows.

    * Any other event (or any Slack source without a body) — ignored.
      The rebuild driver fans every event out to every projection, so
      this reducer must remain a no-op for non-target events.

    Self user id resolution
    -----------------------

    The constructor accepts the self user id via three paths, checked
    in order (first non-empty wins):

    1. explicit ``self_user_id`` keyword argument (tests, embedded
       callers),
    2. ``OPSHUB_SLACK_SELF_USER_ID`` environment variable (CI / headless),
    3. lazy :meth:`opshub.connectors.slack.auth.SlackAuth.test_token`
       call (production path, requires a valid Slack token).

    If all three fail the projection logs a single WARNING and skips
    every Slack event for the rest of its lifetime. We deliberately
    do NOT raise — the rebuild driver applies every event to every
    projection, and a missing Slack token must not crash unrelated
    projection writes (tasks / inbox / sources / ...). Operators see
    the warning in the rebuild log and can re-run after fixing auth.
    """

    name = "slack_demand_digest"

    def __init__(self, *, self_user_id: str | None = None) -> None:
        """Construct the projection with an optional explicit self id.

        Parameters
        ----------
        self_user_id:
            Operator's Slack ``U...`` id. ``None`` (default) defers
            resolution to the env-var → ``auth.test`` cascade
            described in the class docstring.
        """
        self._explicit_self_user_id = self_user_id
        # ``_resolved_self_user_id`` is filled on first :meth:`apply`
        # so the constructor stays I/O-free (the registry materialises
        # the projection list at CLI cold start; calling Slack
        # auth.test up-front would inflate ``opshub --help`` past the
        # ADR-0001 300ms budget).
        self._resolved_self_user_id: str | None = None
        self._resolution_attempted: bool = False
        # Once True the projection swallows every Slack event for the
        # rest of its lifetime. Flipped only when the user id cascade
        # exhausts every option without success — see class docstring
        # for the fail-soft rationale.
        self._self_user_id_unavailable: bool = False
        # Pre-compute the literal we look for in message bodies; the
        # ``<@>`` framing is Slack-stable and identical across
        # surfaces.  Set when :meth:`_self_user_id` first resolves.
        self._mention_literal: str | None = None

    # ----------------------------------------------------- Projection protocol

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the demand digest table if it qualifies.

        Filters in order:

        1. event must be :class:`SourceObserved`,
        2. ``connector_name`` must be ``"slack"``,
        3. ``source_type`` must equal :data:`SLACK_SOURCE_TYPE`,
        4. ``external_id`` must parse as ``"<channel_id>:<ts>"``,
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
        channel_id, ts_value = parsed

        self_user_id = self._self_user_id()
        # ``self_user_id`` is required for the mention path; the DM
        # path is independent of it (the channel id prefix is enough).
        # When the cascade has exhausted every option, the mention
        # path is silently skipped but DM detection still works for
        # operators who only care about that signal.

        is_dm = _is_dm_channel(channel_id)
        # ``_self_user_id`` resolution fills ``_mention_literal`` in
        # lock-step (see :meth:`_self_user_id`), so the ``is not None``
        # narrowing here matches the runtime invariant. The narrowed
        # form keeps strict pyright happy without an extra ``cast``.
        mention_literal = self._mention_literal
        is_mention = (
            self_user_id is not None
            and mention_literal is not None
            and mention_literal in (event.body or "")
        )

        if not is_dm and not is_mention:
            return

        channel_type = _classify_channel_type(channel_id)
        channel_name = _extract_channel_name(event.title)
        excerpt = _build_excerpt(event)
        # The Slack mapper does not surface the message author id on
        # :class:`SourceObserved` (only the resolved display name
        # lands in ``title``). Recording ``None`` keeps the column's
        # nullability honest while leaving room for a future Slack
        # connector enhancement to thread the user id through.
        last_demand_user_id = None
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

    def _self_user_id(self) -> str | None:
        """Return the resolved operator self user id, or ``None``.

        Resolution cascade (first non-empty wins):

        1. explicit constructor argument,
        2. ``OPSHUB_SLACK_SELF_USER_ID`` env var,
        3. :meth:`SlackAuth.test_token` (Slack API call).

        Result is memoised across the projection lifetime. A
        successfully-resolved id also fills :attr:`_mention_literal`
        for the body-substring check.
        """
        if self._resolved_self_user_id is not None:
            return self._resolved_self_user_id
        if self._self_user_id_unavailable:
            return None
        if self._resolution_attempted:
            # ``_resolution_attempted`` is True but neither cache
            # branch fired — re-running auth.test on every event
            # would be wasteful, so we treat the second-attempt path
            # as unavailable.
            return None
        self._resolution_attempted = True

        candidate = self._explicit_self_user_id
        if not candidate:
            env_value = os.environ.get(SELF_USER_ID_ENV_VAR)
            candidate = env_value.strip() if env_value else None
        if not candidate:
            candidate = _resolve_self_user_id_from_auth()

        if not candidate:
            _LOGGER.warning(
                "slack_demand_digest: cannot resolve operator self user id "
                "(no explicit id, no %s env var, no reachable Slack token). "
                "Mention detection will be skipped for the duration of this "
                "rebuild; DM detection still works.",
                SELF_USER_ID_ENV_VAR,
            )
            self._self_user_id_unavailable = True
            return None

        self._resolved_self_user_id = candidate
        self._mention_literal = f"<@{candidate}>"
        return candidate

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
            where=slack_demand_digest_table.c.last_demand_ts
            < stmt.excluded.last_demand_ts,
        )
        conn.execute(stmt)


# ---------------------------------------------------------------- module-level helpers


def _parse_slack_external_id(external_id: str) -> tuple[str, float] | None:
    """Split a Slack ``external_id`` (``"{channel_id}:{ts}"``).

    Returns ``(channel_id, ts_float)`` on success, ``None`` on any
    parse failure. Defensive parsing is justified by the rebuild
    driver: a malformed event must not crash the whole replay.
    """
    if not external_id:
        return None
    # Slack ``ts`` is a string like ``"1700000000.123456"`` — split
    # on the **first** ``":"`` so a channel id never collides with a
    # ``ts`` that hypothetically contains a colon (defensive; the
    # current Slack format does not).
    head, sep, tail = external_id.partition(":")
    if not sep:
        return None
    if not head or not tail:
        return None
    try:
        ts_value = float(tail)
    except (TypeError, ValueError):
        return None
    return head, ts_value


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


def _resolve_self_user_id_from_auth() -> str | None:
    """Call ``SlackAuth.test_token`` to fetch the operator's self id.

    Returns ``None`` on any failure (no token configured, SDK extras
    missing, network error, Slack ``ok: false``). The callers treat
    ``None`` as "self id unavailable for this projection lifetime" and
    skip mention detection accordingly.

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
        auth = SlackAuth()
        result: dict[str, Any] = dict(auth.test_token())
    except ConfigError:
        return None
    except Exception:  # last-resort fail-soft, see module docstring
        return None
    value = result.get("user_id") or ""
    return value or None
