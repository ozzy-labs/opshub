"""Unit tests for :class:`opshub.projections.slack_demand_digest.SlackDemandDigestProjection`.

These tests exercise the reducer directly against a live SQLite
connection — without going through Alembic or the event store — so we
can pin the demand-detection contract row-by-row:

* mention literal ``<@<self_user_id>>`` triggers a ``"mention"`` row.
* DM channels (``D...`` prefix) trigger a ``"dm"`` row.
* The two paths are independent: a DM that mentions self yields both
  rows; a public channel that just contains ``<@other>`` yields
  nothing.
* The upsert is replay-order-independent: applying an older event
  after a newer one must not clobber the latest demand timestamp.
* :meth:`reset` empties the table.

The ``slack_demand_digest`` table needs ``sources`` to exist for the
FK; the fixture creates both via :meth:`Table.create` so the unit
test stays isolated from migration drift (the integration test in
``tests/integration/test_slack_demand_digest_rebuild.py`` covers the
migration path explicitly).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events import DomainEvent, SourceObserved, TaskCreated
from opshub.projections.slack_demand_digest import (
    SELF_USER_ID_ENV_VAR,
    SlackDemandDigestProjection,
    slack_demand_digest_table,
)
from opshub.projections.sources import SourcesProjection, sources_table

_SELF_USER_ID = "U_SELF"
_OTHER_USER_ID = "U_OTHER"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite engine with the digest + sources tables."""
    db_path = tmp_path / "demand_digest.sqlite"
    db_engine = create_engine_for_sqlite(db_path)
    # ``sources`` first because the digest table FKs against it.
    sources_table.create(db_engine)
    slack_demand_digest_table.create(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _apply(
    projection: SlackDemandDigestProjection,
    conn: Connection,
    event: DomainEvent,
    *,
    seed_source: bool = True,
) -> None:
    """Apply ``event`` to the digest projection.

    The Phase 18-B projection writes a FK reference (``last_source_id``
    → ``sources.id``) on every upsert. Production keeps the FK honest
    because :class:`SourcesProjection` is registered ahead of the
    digest projection in
    :func:`opshub.projections.registry.all_projections`. The unit
    tests reproduce that ordering by applying both projections in
    the same connection — ``seed_source=False`` lets the few
    "this event must be ignored" cases skip the source seeding so
    the negative-path assertions stay focused.
    """
    if seed_source:
        SourcesProjection().apply(conn, event)
    projection.apply(conn, event)


def _slack_observed(
    *,
    channel_id: str,
    ts: str,
    team_id: str = "T-test",
    body: str,
    title: str = "alice in #general",
    summary: str | None = None,
    permalink: str = "https://example.slack.com/archives/C/p1",
    occurred_at: datetime | None = None,
    aggregate_id: str | None = None,
    author_id: str | None = _OTHER_USER_ID,
) -> SourceObserved:
    """Build a :class:`SourceObserved` mimicking a Phase 7 Slack mapper output.

    ``author_id`` defaults to :data:`_OTHER_USER_ID` so fixtures model a
    peer-authored message (the common demand case); pass
    ``author_id=_SELF_USER_ID`` to model a self-authored message and
    exercise the Phase 23-D suppression path, or ``author_id=None`` for a
    bot / system / historic (pre-Phase-23) event.
    """
    if occurred_at is None:
        occurred_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    if aggregate_id is None:
        aggregate_id = new_ulid()
    # epic #470 / issue #481: ``body`` is required + non-empty. The
    # Slack mapper falls back to the title when text is empty (see
    # :func:`opshub.connectors.slack.mapper.map_message`); the test
    # helper mirrors that so demand-digest fixtures stay valid.
    resolved_body = body if body else title
    return SourceObserved(
        aggregate_id=aggregate_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="connector:slack",
        connector_name="slack",
        # Phase 24-B (ADR-0041 §(a)): 3-token natural key.
        external_id=f"{team_id}:{channel_id}:{ts}",
        source_type="slack_message",
        title=title,
        url=permalink,
        summary=summary if summary is not None else body[:200] if body else None,
        body=resolved_body,
        provenance_origin="external",
        provenance_trust="untrusted",
        author_id=author_id,
    )


# ---- mention detection ----------------------------------------------------


def test_mention_in_public_channel_inserts_mention_row(engine: Engine) -> None:
    """A body that contains ``<@self>`` upserts a ``mention`` row."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="C123ABC",
        ts="1700000000.000100",
        body=f"hey <@{_SELF_USER_ID}> can you take a look?",
        title="bob in #general: hey ...",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["channel_id"] == "C123ABC"
    assert row["channel_type"] == "public"
    assert row["channel_name"] == "general"
    assert row["demand_kind"] == "mention"
    assert row["last_demand_ts"] == pytest.approx(1700000000.000100)  # pyright: ignore[reportUnknownMemberType]
    assert row["last_demand_excerpt"] == "hey <@U_SELF> can you take a look?"
    assert row["last_demand_permalink"] == "https://example.slack.com/archives/C/p1"
    assert row["last_source_id"] == event.aggregate_id
    # Phase 23-D (issue #534): the peer's author id is now recorded
    # (it used to be hard-coded NULL).
    assert row["last_demand_user_id"] == _OTHER_USER_ID


def test_mention_for_other_user_does_not_insert_row(engine: Engine) -> None:
    """A ``<@other>`` literal in a public channel must not produce a row."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="C123ABC",
        ts="1700000001.000100",
        body=f"hey <@{_OTHER_USER_ID}> wdyt?",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).all()
    assert rows == []


def test_mention_in_private_channel_records_private_type(engine: Engine) -> None:
    """``G...`` channel ids round-trip as ``private`` (ADR-0033 §Decision (b))."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="G987PRIV",
        ts="1700000002.000200",
        body=f"<@{_SELF_USER_ID}> please review",
        title="carol in #leadership: ...",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["channel_type"] == "private"
    assert row["demand_kind"] == "mention"


# ---- DM detection ---------------------------------------------------------


def test_dm_channel_inserts_dm_row(engine: Engine) -> None:
    """A ``D...`` channel id upserts a ``dm`` row, even without ``<@self>``."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="D111CDE",
        ts="1700000003.000300",
        body="hello!",
        title="alice in #alice: hello!",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["channel_id"] == "D111CDE"
    assert row["channel_type"] == "im"
    assert row["demand_kind"] == "dm"


def test_dm_with_self_mention_writes_both_rows(engine: Engine) -> None:
    """A DM body containing ``<@self>`` produces both ``dm`` and ``mention`` rows."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="D222EFG",
        ts="1700000004.000400",
        body=f"<@{_SELF_USER_ID}> ping",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(slack_demand_digest_table).order_by(slack_demand_digest_table.c.demand_kind)
            )
            .mappings()
            .all()
        )
    assert {row["demand_kind"] for row in rows} == {"dm", "mention"}
    # Both rows share the same channel id and channel_type.
    assert all(row["channel_id"] == "D222EFG" for row in rows)
    assert all(row["channel_type"] == "im" for row in rows)


def test_dm_path_works_without_self_user_id(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DM detection succeeds even when the self user id cascade returns ``None``.

    DM detection only needs the channel id prefix; only the mention
    path requires the self id. We monkeypatch the auth resolver to
    return ``None`` so the projection falls into the "self id
    unavailable" branch, then assert the DM row still lands.
    """
    monkeypatch.delenv(SELF_USER_ID_ENV_VAR, raising=False)
    # Force the auth.test cascade to fail so the projection treats
    # the self user id as unavailable. The module is loaded at import
    # time so we patch the resolver function the projection imports.
    monkeypatch.setattr(
        "opshub.projections.slack_demand_digest._resolve_self_user_id_from_auth",
        lambda: None,
    )

    projection = SlackDemandDigestProjection()  # no explicit id
    event = _slack_observed(
        channel_id="D333HIJ",
        ts="1700000005.000500",
        body="just a DM",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["demand_kind"] == "dm"


# ---- self-authored suppression (Phase 23-D, issue #534) -------------------


def test_self_authored_dm_is_suppressed(engine: Engine) -> None:
    """A DM the operator themselves authored must not produce a row.

    Issue #534 core fix: a DM the operator already replied to is not a
    demand on them. When ``author_id == self_user_id`` the event is
    dropped before any row is written.
    """
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="D444SELF",
        ts="1700000040.000400",
        body="ok, on it!",
        author_id=_SELF_USER_ID,
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).all()
    assert rows == []


def test_self_authored_mention_is_suppressed(engine: Engine) -> None:
    """A ``<@self>`` literal the operator wrote themselves is not a demand.

    Edge case: the operator pastes their own id into a public channel
    message. Without the author guard this would self-trigger a mention
    row.
    """
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="C444SELF",
        ts="1700000041.000400",
        body=f"reminder to <@{_SELF_USER_ID}> from myself",
        author_id=_SELF_USER_ID,
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).all()
    assert rows == []


def test_self_reply_does_not_overwrite_peer_demand(engine: Engine) -> None:
    """A later self-reply must not clobber the peer's demand excerpt.

    This is the precise mechanism #534 describes: the high-water upsert
    would otherwise let the operator's own reply (a strictly newer ts)
    replace the peer's actionable excerpt / permalink. Suppressing
    self-authored events keeps the row pinned to the peer's ping.
    """
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    peer_ping = _slack_observed(
        channel_id="D555PEER",
        ts="1700000050.000500",
        body="can you review this?",
        author_id=_OTHER_USER_ID,
    )
    self_reply = _slack_observed(
        channel_id="D555PEER",
        ts="1700000051.000600",
        body="sure, looking now",
        author_id=_SELF_USER_ID,
    )

    with engine.begin() as conn:
        _apply(projection, conn, peer_ping)
        _apply(projection, conn, self_reply)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    # The peer's ping survives — neither the ts nor the excerpt advanced.
    assert row["last_demand_ts"] == pytest.approx(1700000050.000500)  # pyright: ignore[reportUnknownMemberType]
    assert row["last_demand_excerpt"] == "can you review this?"
    assert row["last_demand_user_id"] == _OTHER_USER_ID


def test_dm_without_self_user_id_still_records_author(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Author suppression is skipped when self id is unavailable, but author is recorded.

    With no resolvable self id the projection cannot compare authors, so
    the suppression guard is a no-op (the DM still lands) — but the
    peer's author id is still persisted for the FROM column.
    """
    monkeypatch.delenv(SELF_USER_ID_ENV_VAR, raising=False)
    monkeypatch.setattr(
        "opshub.projections.slack_demand_digest._resolve_self_user_id_from_auth",
        lambda: None,
    )
    projection = SlackDemandDigestProjection()  # no explicit id
    event = _slack_observed(
        channel_id="D666NOID",
        ts="1700000060.000600",
        body="hello there",
        author_id="U_PEER",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["demand_kind"] == "dm"
    assert row["last_demand_user_id"] == "U_PEER"


# ---- DM peer name resolution (Phase 23-D, issue #534 §4) ------------------


def test_dm_channel_name_resolves_to_peer_display_name(engine: Engine) -> None:
    """A DM row surfaces the peer display name, not the opaque ``D...`` id.

    Issue #534 §4: for a DM the Slack fetcher has no channel name and the
    title degrades to ``"{peer} in #{D...}: ..."``; the projection lifts
    the peer name out of the title prefix so the operator sees "alice"
    rather than "D777PEER".
    """
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="D777PEER",
        ts="1700000070.000700",
        body="quick question",
        title="alice smith in #D777PEER: quick question",
        author_id=_OTHER_USER_ID,
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["demand_kind"] == "dm"
    assert row["channel_name"] == "alice smith"


# ---- idempotency / ordering -----------------------------------------------


def test_upsert_idempotent_same_ts(engine: Engine) -> None:
    """Applying the same event twice produces exactly one row, unchanged."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = _slack_observed(
        channel_id="C444KLM",
        ts="1700000006.000600",
        body=f"<@{_SELF_USER_ID}> first",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["last_demand_ts"] == pytest.approx(1700000006.000600)  # pyright: ignore[reportUnknownMemberType]


def test_newer_ts_overwrites_existing_row(engine: Engine) -> None:
    """A second mention with a strictly newer ts replaces the persisted row."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    older = _slack_observed(
        channel_id="C555MNO",
        ts="1700000007.000700",
        body=f"<@{_SELF_USER_ID}> earlier",
        occurred_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
    )
    newer = _slack_observed(
        channel_id="C555MNO",
        ts="1700000008.000800",
        body=f"<@{_SELF_USER_ID}> later",
        occurred_at=datetime(2026, 6, 1, 12, 5, 0, tzinfo=UTC),
    )

    with engine.begin() as conn:
        _apply(projection, conn, older)
        _apply(projection, conn, newer)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["last_demand_ts"] == pytest.approx(1700000008.000800)  # pyright: ignore[reportUnknownMemberType]
    assert row["last_demand_excerpt"] == f"<@{_SELF_USER_ID}> later"


def test_older_ts_does_not_overwrite_newer_row(engine: Engine) -> None:
    """A replayed older event must not clobber the latest demand timestamp."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    newer = _slack_observed(
        channel_id="C666PQR",
        ts="1700000010.001000",
        body=f"<@{_SELF_USER_ID}> later",
    )
    older = _slack_observed(
        channel_id="C666PQR",
        ts="1700000009.000900",
        body=f"<@{_SELF_USER_ID}> earlier",
    )

    with engine.begin() as conn:
        _apply(projection, conn, newer)
        _apply(projection, conn, older)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["last_demand_ts"] == pytest.approx(1700000010.001000)  # pyright: ignore[reportUnknownMemberType]
    assert row["last_demand_excerpt"] == f"<@{_SELF_USER_ID}> later"


# ---- env-var fallback for self user id ------------------------------------


def test_self_user_id_resolved_from_env_var(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OPSHUB_SLACK_SELF_USER_ID env var feeds the mention literal."""
    monkeypatch.setenv(SELF_USER_ID_ENV_VAR, "U_FROM_ENV")
    # Make sure the auth fallback is never consulted in this path.
    monkeypatch.setattr(
        "opshub.projections.slack_demand_digest._resolve_self_user_id_from_auth",
        lambda: None,
    )

    projection = SlackDemandDigestProjection()  # no explicit id
    event = _slack_observed(
        channel_id="C777STU",
        ts="1700000011.001100",
        body="hi <@U_FROM_ENV> please review",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        row = conn.execute(select(slack_demand_digest_table)).mappings().one()
    assert row["demand_kind"] == "mention"


# ---- non-Slack events / ignore path ---------------------------------------


def test_non_slack_source_observed_is_ignored(engine: Engine) -> None:
    """Source events from other connectors must not produce digest rows."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        actor="connector:github",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="not a slack source",
        body=f"<@{_SELF_USER_ID}> would not count",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).all()
    assert rows == []


def test_unrelated_event_types_are_ignored(engine: Engine) -> None:
    """Task / decision / inbox events must never touch the digest table."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = TaskCreated(
        aggregate_id=new_ulid(),
        occurred_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        actor="test",
        title="unrelated",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).all()
    assert rows == []


def test_malformed_external_id_is_ignored(engine: Engine) -> None:
    """An external_id without ``"<team>:<channel>:<ts>"`` shape is dropped silently."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        actor="connector:slack",
        connector_name="slack",
        external_id="malformed-no-colon",
        source_type="slack_message",
        title="alice in #general",
        body=f"<@{_SELF_USER_ID}> hi",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).all()
    assert rows == []


def test_legacy_two_token_external_id_is_dropped(engine: Engine) -> None:
    """A pre-Phase-24 2-token external_id (``"{channel_id}:{ts}"``) is dropped.

    Phase 24-B ([ADR-0041](docs/adr/0041-slack-multi-workspace.md) §(a))
    re-keyed the natural key to ``"{team_id}:{channel_id}:{ts}"``. The
    sanctioned upgrade path for pre-existing data is a DB re-init + full
    re-sync (ADR-0041 §(e)); accepting both shapes would let one message
    appear under two natural keys and double-count demands, so the parser
    deliberately returns ``None`` for the legacy shape. This pin makes the
    drop **explicit** — a future change that silently re-accepts 2-token
    events should have to flip this test consciously.
    """
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    event = SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        actor="connector:slack",
        connector_name="slack",
        # Would have produced a DM row under the pre-24 2-token parser
        # (D-prefixed channel + valid ts).
        external_id="D123ABC:1700000000.000100",
        source_type="slack_message",
        title="alice",
        body=f"<@{_SELF_USER_ID}> hi",
    )

    with engine.begin() as conn:
        _apply(projection, conn, event)

    with engine.connect() as conn:
        rows = conn.execute(select(slack_demand_digest_table)).all()
    assert rows == []


def test_parse_slack_external_id_three_token_shape() -> None:
    """``_parse_slack_external_id`` reads the channel from the middle token."""
    from opshub.projections.slack_demand_digest import (
        _parse_slack_external_id,  # pyright: ignore[reportPrivateUsage]
    )

    assert _parse_slack_external_id("T0ACME:C123ABC:1700000000.000100") == (
        "C123ABC",
        1700000000.000100,
    )
    # Legacy 2-token / malformed shapes all return None (dropped upstream).
    assert _parse_slack_external_id("C123ABC:1700000000.000100") is None
    assert _parse_slack_external_id("no-colon") is None
    assert _parse_slack_external_id("") is None
    assert _parse_slack_external_id(":C1:1.0") is None
    assert _parse_slack_external_id("T1::1.0") is None
    assert _parse_slack_external_id("T1:C1:") is None
    assert _parse_slack_external_id("T1:C1:not-a-ts") is None


# ---- reset ----------------------------------------------------------------


def test_reset_empties_table(engine: Engine) -> None:
    """``reset(conn)`` deletes every row so the rebuild driver can replay."""
    projection = SlackDemandDigestProjection(self_user_id=_SELF_USER_ID)
    with engine.begin() as conn:
        for i in range(3):
            event = _slack_observed(
                channel_id=f"C{i:03d}XYZ",
                ts=f"170000000{i}.000000",
                body=f"<@{_SELF_USER_ID}> row {i}",
            )
            _apply(projection, conn, event)

    with engine.begin() as conn:
        projection.reset(conn)

    with engine.connect() as conn:
        remaining = conn.execute(select(slack_demand_digest_table)).all()
    assert remaining == []
