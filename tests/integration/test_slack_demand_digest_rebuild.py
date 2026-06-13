"""End-to-end rebuild test for ``slack_demand_digest`` (Phase 18-B, ADR-0033).

Provisions a migrated SQLite database (covers migration 0029),
appends a hand-crafted mix of Slack :class:`SourceObserved` events
directly to the event store, runs
:func:`opshub.projections.rebuild_all` with the full project
registry, and pins:

* the digest table is populated with one row per
  ``(team_id, channel_id, demand_kind)`` natural key (Phase 24-D,
  ADR-0041 §(g) — migration 0033),
* DM detection (``D...`` channel id) yields ``demand_kind="dm"``,
* mention detection (``<@self_user_id>`` body literal) yields
  ``demand_kind="mention"``,
* a second :func:`rebuild_all` call leaves the digest snapshot
  byte-identical (replay idempotency).

The test uses :func:`SlackDemandDigestProjection` constructed with
an explicit ``self_user_ids`` map so it does not need a Slack token to
run; the production code path resolves the same value via the
per-alias ``OPSHUB_SLACK_SELF_USER_ID__<ALIAS>`` env vars or per-alias
:meth:`SlackAuth.test_token` calls
(see the projection module docstring).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.domain.events import SourceObserved
from opshub.projections import rebuild_all
from opshub.projections.base import Projection
from opshub.projections.registry import all_projections
from opshub.projections.slack_demand_digest import (
    SlackDemandDigestProjection,
    slack_demand_digest_table,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"

_SELF_USER_ID = "U_SELF_INT"
_OTHER_USER_ID = "U_OTHER_INT"


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "demand_digest_rebuild.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _slack_observed(
    *,
    channel_id: str,
    ts: str,
    body: str,
    team_id: str = "T-int",
    title: str = "alice in #general: ...",
    occurred_at: datetime | None = None,
    author_id: str | None = None,
) -> SourceObserved:
    if occurred_at is None:
        occurred_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    # epic #470 / issue #481: ``body`` is required + non-empty. The
    # Slack mapper falls back to the title for text-less messages
    # (Slackbot pings, ``channel_join`` etc.); mirror that here so
    # the fixture stays valid under the new contract.
    resolved_body = body if body else title
    return SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        actor="connector:slack",
        connector_name="slack",
        # Phase 24-B (ADR-0041 §(a)): 3-token natural key.
        external_id=f"{team_id}:{channel_id}:{ts}",
        source_type="slack_message",
        title=title,
        url=f"https://example.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
        summary=body[:200] if body else None,
        body=resolved_body,
        provenance_origin="external",
        provenance_trust="untrusted",
        # Phase 23-D (issue #534): author id rides on the event so the
        # rebuild can reproduce self-authored suppression. Default
        # ``None`` models a historic (pre-Phase-23) event whose author
        # was never threaded.
        author_id=author_id,
    )


def _build_projections(self_user_id: str) -> list[Projection]:
    """Return the registry list with the demand-digest projection patched.

    The default :func:`all_projections` instantiates
    :class:`SlackDemandDigestProjection` without arguments, which
    would try to consult Slack auth at apply time. Tests run in a
    sandbox without a Slack token, so we replace that instance with
    one that already knows the self user id.
    """
    result: list[Projection] = []
    for projection in all_projections():
        if isinstance(projection, SlackDemandDigestProjection):
            result.append(SlackDemandDigestProjection(self_user_ids={"T-int": self_user_id}))
        else:
            result.append(projection)
    return result


def _read_digest_rows(engine: Engine) -> list[dict[str, Any]]:
    """Snapshot the digest table, sorted for stable comparison."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(slack_demand_digest_table).order_by(
                    slack_demand_digest_table.c.team_id.asc(),
                    slack_demand_digest_table.c.channel_id.asc(),
                    slack_demand_digest_table.c.demand_kind.asc(),
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---- end-to-end rebuild ---------------------------------------------------


def test_rebuild_materialises_demand_digest(migrated_engine: Engine) -> None:
    """Three crafted events produce the expected (channel, kind) rows."""
    store = SqlAlchemyEventStore(migrated_engine)

    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    events = [
        # public channel mention of self
        _slack_observed(
            channel_id="C100PUB",
            ts="1700000010.000010",
            body=f"<@{_SELF_USER_ID}> please review",
            title="bob in #general: please review",
            occurred_at=base,
        ),
        # DM (no mention literal needed; DM detection is independent)
        _slack_observed(
            channel_id="D200DMA",
            ts="1700000020.000020",
            body="quick question",
            title="alice in #alice: quick question",
            occurred_at=base + timedelta(minutes=1),
        ),
        # public channel mention of someone else — must not produce a row
        _slack_observed(
            channel_id="C100PUB",
            ts="1700000005.000005",
            body="<@U_OTHER> heads up",
            title="bob in #general: heads up",
            occurred_at=base - timedelta(minutes=5),
        ),
    ]
    for event in events:
        store.append(event)

    rebuild_all(migrated_engine, store, _build_projections(_SELF_USER_ID))

    snapshot = _read_digest_rows(migrated_engine)
    # Two rows expected: (C100PUB, mention) and (D200DMA, dm).
    assert [(row["channel_id"], row["demand_kind"]) for row in snapshot] == [
        ("C100PUB", "mention"),
        ("D200DMA", "dm"),
    ]
    # Mention row carries the newest (and only) qualifying ts for the
    # public channel — 1700000010, not the 1700000005 mention-of-other.
    assert snapshot[0]["last_demand_ts"] == pytest.approx(1700000010.000010)  # pyright: ignore[reportUnknownMemberType]
    assert snapshot[1]["last_demand_ts"] == pytest.approx(1700000020.000020)  # pyright: ignore[reportUnknownMemberType]
    # Channel type classification.
    assert snapshot[0]["channel_type"] == "public"
    assert snapshot[1]["channel_type"] == "im"


def test_rebuild_suppresses_self_authored_demand(migrated_engine: Engine) -> None:
    """Author-aware replay drops self-authored DMs / mentions (issue #534).

    Reproduces the #534 acceptance criterion through the full
    event-store → ``rebuild_all`` path: a peer's DM ping followed by the
    operator's own (strictly newer) reply in the same channel must leave
    the digest pinned to the peer's ping, never the self-reply.
    """
    store = SqlAlchemyEventStore(migrated_engine)
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    store.append(
        _slack_observed(
            channel_id="D900SELF",
            ts="1700000300.000300",
            body="can you take a look?",
            title="alice in #D900SELF: can you take a look?",
            occurred_at=base,
            author_id=_OTHER_USER_ID,
        )
    )
    store.append(
        _slack_observed(
            channel_id="D900SELF",
            ts="1700000301.000400",
            body="sure, on it",
            title=f"{_SELF_USER_ID} in #D900SELF: sure, on it",
            occurred_at=base + timedelta(minutes=1),
            author_id=_SELF_USER_ID,
        )
    )

    rebuild_all(migrated_engine, store, _build_projections(_SELF_USER_ID))

    snapshot = _read_digest_rows(migrated_engine)
    assert len(snapshot) == 1
    row = snapshot[0]
    assert row["channel_id"] == "D900SELF"
    assert row["demand_kind"] == "dm"
    # The peer's ping survives; the self-reply never advanced the row.
    assert row["last_demand_ts"] == pytest.approx(1700000300.000300)  # pyright: ignore[reportUnknownMemberType]
    assert row["last_demand_excerpt"] == "can you take a look?"
    assert row["last_demand_user_id"] == _OTHER_USER_ID
    # DM peer name resolved from the title prefix, not the opaque id.
    assert row["channel_name"] == "alice"


def test_rebuild_is_idempotent(migrated_engine: Engine) -> None:
    """Two consecutive ``rebuild_all`` runs produce identical digest snapshots."""
    store = SqlAlchemyEventStore(migrated_engine)

    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    store.append(
        _slack_observed(
            channel_id="C400REPLAY",
            ts="1700000100.000100",
            body=f"<@{_SELF_USER_ID}> first",
            occurred_at=base,
        )
    )
    store.append(
        _slack_observed(
            channel_id="C400REPLAY",
            ts="1700000200.000200",
            body=f"<@{_SELF_USER_ID}> second",
            occurred_at=base + timedelta(minutes=10),
        )
    )
    store.append(
        _slack_observed(
            channel_id="D500REPLAYDM",
            ts="1700000150.000150",
            body="hi",
            occurred_at=base + timedelta(minutes=5),
        )
    )

    rebuild_all(migrated_engine, store, _build_projections(_SELF_USER_ID))
    first = _read_digest_rows(migrated_engine)

    rebuild_all(migrated_engine, store, _build_projections(_SELF_USER_ID))
    second = _read_digest_rows(migrated_engine)

    assert second == first
    # Sanity: two rows (mention + dm), mention row reflects the latest
    # ts for ``C400REPLAY``.
    keyed = {(row["channel_id"], row["demand_kind"]): row for row in first}
    assert set(keyed) == {("C400REPLAY", "mention"), ("D500REPLAYDM", "dm")}
    assert keyed[("C400REPLAY", "mention")]["last_demand_ts"] == pytest.approx(1700000200.000200)  # pyright: ignore[reportUnknownMemberType]


def test_rebuild_with_no_slack_events_produces_no_digest_rows(
    migrated_engine: Engine,
) -> None:
    """An event store without Slack rows leaves the digest table empty."""
    store = SqlAlchemyEventStore(migrated_engine)
    # GitHub source — must be ignored by the digest projection.
    store.append(
        SourceObserved(
            aggregate_id=new_ulid(),
            occurred_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            recorded_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            actor="connector:github",
            connector_name="github",
            external_id="owner/repo#1",
            source_type="issue",
            title="non-slack",
            body=f"<@{_SELF_USER_ID}> mention but wrong connector",
        )
    )

    rebuild_all(migrated_engine, store, _build_projections(_SELF_USER_ID))
    assert _read_digest_rows(migrated_engine) == []


def test_rebuild_keys_rows_per_workspace(migrated_engine: Engine) -> None:
    """Same channel id in two workspaces → two rows; rebuild stays idempotent.

    Phase 24-D ([ADR-0041](../../docs/adr/0041-slack-multi-workspace.md)
    §(g), issue #556): the digest natural key is ``(team_id,
    channel_id, demand_kind)``, exercised here through the migrated
    schema (migration 0033) + the full event-store replay path. The
    older workspace-B demand must survive next to the newer
    workspace-A one (pre-re-key, the shared ``(channel_id, kind)``
    key's high-water guard would have swallowed it).
    """
    store = SqlAlchemyEventStore(migrated_engine)
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    store.append(
        _slack_observed(
            team_id="T-int",
            channel_id="D700COLLIDE",
            ts="1700000200.000200",
            body="ping in workspace A",
            occurred_at=base,
        )
    )
    store.append(
        _slack_observed(
            team_id="T-int2",
            channel_id="D700COLLIDE",
            ts="1700000100.000100",
            body="ping in workspace B",
            occurred_at=base + timedelta(minutes=1),
        )
    )

    projections: list[Projection] = []
    for projection in all_projections():
        if isinstance(projection, SlackDemandDigestProjection):
            projections.append(
                SlackDemandDigestProjection(
                    self_user_ids={"T-int": _SELF_USER_ID, "T-int2": "U_SELF_INT2"}
                )
            )
        else:
            projections.append(projection)

    rebuild_all(migrated_engine, store, projections)
    first = _read_digest_rows(migrated_engine)

    assert sorted((row["team_id"], row["channel_id"], row["demand_kind"]) for row in first) == [
        ("T-int", "D700COLLIDE", "dm"),
        ("T-int2", "D700COLLIDE", "dm"),
    ]
    keyed = {row["team_id"]: row["last_demand_ts"] for row in first}
    assert keyed["T-int"] == pytest.approx(1700000200.000200)  # pyright: ignore[reportUnknownMemberType]
    assert keyed["T-int2"] == pytest.approx(1700000100.000100)  # pyright: ignore[reportUnknownMemberType]

    # Replay equivalence: a second rebuild converges to the same rows.
    rebuild_all(migrated_engine, store, projections)
    assert _read_digest_rows(migrated_engine) == first
