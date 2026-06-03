"""Regression tests for the Step 1 widening MCP tool handlers.

The widening adds 7 read tools (``brief`` / ``graph.related`` /
``graph.trace`` / ``graph.expand`` / ``source.list`` / ``source.get`` /
``embeddings.find_duplicates``) and 1 HITL write tool
(``propose.generate``). The pure-data handlers (``graph.*``,
``source.*``) are exercised here against an in-memory engine; the
LLM-backed handlers (``brief`` / ``propose.generate``) and the
backend-dependent ``embeddings.find_duplicates`` use stubbed services
because their real wiring resolves through
:func:`opshub.cli._wiring.build_*` which would require a configured
LLM / embedder backend.

Schema-level rejection of unknown / missing fields is covered for every
new tool via a single jsonschema parametrise so a future schema relax
trips the test before reaching the SDK.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
from sqlalchemy import insert
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.mcp._registry import build_tool_specs
from opshub.mcp._tools import (
    build_graph_expand_handler,
    build_graph_related_handler,
    build_graph_trace_handler,
    build_source_get_handler,
    build_source_list_handler,
)
from opshub.projections.links import links_table
from opshub.projections.sources import sources_table

_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB with the projection tables the new handlers touch."""
    db_path = tmp_path / "mcp_widening.sqlite"
    eng = create_engine_for_sqlite(db_path)
    try:
        sources_table.create(eng)
        links_table.create(eng)
        yield eng
    finally:
        eng.dispose()


def _seed_source(
    engine: Engine,
    *,
    source_id: str,
    connector_name: str = "github",
    source_type: str = "pull_request",
    title: str = "example title",
    summary: str | None = "example summary",
    url: str | None = "https://example.invalid/1",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name=connector_name,
                external_id=f"ext-{source_id}",
                source_type=source_type,
                title=title,
                url=url,
                summary=summary,
                observed_at=_NOW,
                updated_at=_NOW,
            )
        )


def _seed_link(
    engine: Engine,
    *,
    link_id: str,
    from_entity_type: str,
    from_entity_id: str,
    to_entity_type: str,
    to_entity_id: str,
    link_type: str = "references",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(links_table).values(
                id=link_id,
                from_entity_type=from_entity_type,
                from_entity_id=from_entity_id,
                to_entity_type=to_entity_type,
                to_entity_id=to_entity_id,
                link_type=link_type,
                created_at=_NOW,
                source_event_id=None,
                metadata=None,
            )
        )


def _parse(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


# --------------------------------------------------------------------- source.*


async def test_source_list_handler_filters_by_connector(engine: Engine) -> None:
    _seed_source(engine, source_id="01HSOURCEGITHUB00000000001", connector_name="github")
    _seed_source(engine, source_id="01HSOURCESLACK000000000002", connector_name="slack")
    handler = build_source_list_handler(engine)
    payload = _parse(await handler({"connector_name": "github", "limit": 10}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert len(items) == 1
    assert items[0]["connector_name"] == "github"


async def test_source_list_handler_filters_by_source_type(engine: Engine) -> None:
    _seed_source(
        engine,
        source_id="01HSOURCEPR0000000000000001",
        source_type="pull_request",
    )
    _seed_source(
        engine,
        source_id="01HSOURCEMSG000000000000002",
        source_type="slack_message",
    )
    handler = build_source_list_handler(engine)
    payload = _parse(await handler({"source_type": "pull_request"}))
    items = cast("list[dict[str, Any]]", payload["items"])
    assert len(items) == 1
    assert items[0]["source_type"] == "pull_request"


async def test_source_list_handler_truncates_long_title(engine: Engine) -> None:
    """Long title strings flow through ``_truncate``."""
    long_title = "x" * 500
    _seed_source(engine, source_id="01HSOURCELONG00000000000001", title=long_title)
    handler = build_source_list_handler(engine)
    payload = _parse(await handler({"limit": 5}))
    items = cast("list[dict[str, Any]]", payload["items"])
    # ``_truncate`` caps at 200 chars including the ``…`` sentinel.
    assert len(items[0]["title"]) <= 200


async def test_source_get_handler_returns_found_payload(engine: Engine) -> None:
    _seed_source(engine, source_id="01HSOURCEGET00000000000001", title="getme")
    handler = build_source_get_handler(engine)
    payload = _parse(await handler({"source_id": "01HSOURCEGET00000000000001"}))
    assert payload["found"] is True
    assert payload["id"] == "01HSOURCEGET00000000000001"
    assert payload["title"] == "getme"


async def test_source_get_handler_reports_missing(engine: Engine) -> None:
    handler = build_source_get_handler(engine)
    payload = _parse(await handler({"source_id": "01HSOURCEMISSING0000000000"}))
    assert payload["found"] is False
    assert payload["source_id"] == "01HSOURCEMISSING0000000000"


# ------------------------------------------------------------------ graph.*


async def test_graph_related_outbound_only(engine: Engine) -> None:
    _seed_link(
        engine,
        link_id="01HLNK0000000000000000A001",
        from_entity_type="task",
        from_entity_id="01HTASK000000000000000A001",
        to_entity_type="source",
        to_entity_id="01HSRC000000000000000A001",
    )
    _seed_link(
        engine,
        link_id="01HLNK0000000000000000A002",
        from_entity_type="decision",
        from_entity_id="01HDEC000000000000000A001",
        to_entity_type="task",
        to_entity_id="01HTASK000000000000000A001",
    )
    handler = build_graph_related_handler(engine)
    # outbound from the task → 1 link (task → source)
    payload = _parse(
        await handler(
            {
                "entity_type": "task",
                "entity_id": "01HTASK000000000000000A001",
                "direction": "outbound",
            }
        )
    )
    items = cast("list[dict[str, Any]]", payload["items"])
    assert len(items) == 1
    assert items[0]["to_entity_type"] == "source"


async def test_graph_related_both_directions(engine: Engine) -> None:
    _seed_link(
        engine,
        link_id="01HLNK0000000000000000B001",
        from_entity_type="task",
        from_entity_id="01HTASK000000000000000B001",
        to_entity_type="source",
        to_entity_id="01HSRC000000000000000B001",
    )
    _seed_link(
        engine,
        link_id="01HLNK0000000000000000B002",
        from_entity_type="decision",
        from_entity_id="01HDEC000000000000000B001",
        to_entity_type="task",
        to_entity_id="01HTASK000000000000000B001",
    )
    handler = build_graph_related_handler(engine)
    payload = _parse(
        await handler(
            {
                "entity_type": "task",
                "entity_id": "01HTASK000000000000000B001",
                "direction": "both",
            }
        )
    )
    items = cast("list[dict[str, Any]]", payload["items"])
    assert len(items) == 2


async def test_graph_trace_backward_chain(engine: Engine) -> None:
    # decision -> task -> source — trace from source upward 2 hops.
    _seed_link(
        engine,
        link_id="01HLNK0000000000000000T001",
        from_entity_type="task",
        from_entity_id="01HTASK000000000000000T001",
        to_entity_type="source",
        to_entity_id="01HSRC000000000000000T001",
    )
    _seed_link(
        engine,
        link_id="01HLNK0000000000000000T002",
        from_entity_type="decision",
        from_entity_id="01HDEC000000000000000T001",
        to_entity_type="task",
        to_entity_id="01HTASK000000000000000T001",
    )
    handler = build_graph_trace_handler(engine)
    payload = _parse(
        await handler(
            {
                "entity_type": "source",
                "entity_id": "01HSRC000000000000000T001",
                "depth": 2,
            }
        )
    )
    paths = cast("list[dict[str, Any]]", payload["paths"])
    # Backward chain reaches the decision through the task.
    assert any(p["depth"] == 2 for p in paths)


async def test_graph_expand_returns_root_and_neighbours(engine: Engine) -> None:
    _seed_link(
        engine,
        link_id="01HLNK0000000000000000E001",
        from_entity_type="task",
        from_entity_id="01HTASK000000000000000E001",
        to_entity_type="source",
        to_entity_id="01HSRC000000000000000E001",
    )
    handler = build_graph_expand_handler(engine)
    payload = _parse(
        await handler(
            {
                "entity_type": "task",
                "entity_id": "01HTASK000000000000000E001",
                "depth": 1,
            }
        )
    )
    nodes = cast("list[dict[str, str]]", payload["nodes"])
    # Root + 1 neighbour reached.
    assert payload["node_count"] == 2
    assert payload["edge_count"] == 1
    types = {node["entity_type"] for node in nodes}
    assert types == {"task", "source"}


# --------------------------------------------------- schema rejection guards


def _stub_handler() -> Any:
    async def _h(_arguments: dict[str, Any]) -> str:
        return "ok"

    return _h


_NEW_TOOL_NAMES = (
    "brief",
    "graph.related",
    "graph.trace",
    "graph.expand",
    "source.list",
    "source.get",
    "embeddings.find_duplicates",
    "propose.generate",
    # Phase 12 H1 (ADR-0022 改訂): FTS5 search + HITL propose.apply.
    "search",
    "propose.apply",
    # Phase 18-C (ADR-0033 §決定 (c)): Slack mention / DM demand digest.
    "slack.demand.list",
)


@pytest.fixture
def specs() -> list[Any]:
    tool_names = (
        "recall.search",
        "task.list",
        "inbox.list",
        "decision.list",
        "task.create",
        "inbox.add",
        "connector.sync",
        *_NEW_TOOL_NAMES,
    )
    handlers = {name: _stub_handler() for name in tool_names}
    return build_tool_specs(handlers=handlers)


@pytest.mark.parametrize("tool_name", _NEW_TOOL_NAMES)
def test_new_tool_input_schemas_are_closed(specs: list[Any], tool_name: str) -> None:
    """``additionalProperties: false`` is the cross-cutting safety rail."""
    spec = next(s for s in specs if s.name == tool_name)
    assert spec.input_schema.get("additionalProperties") is False


@pytest.mark.parametrize(
    ("tool_name", "bad_args"),
    [
        ("brief", {"topic": "x", "evil": "leak"}),
        ("graph.related", {"entity_id": "x", "entity_type": "task", "evil": "y"}),
        ("graph.trace", {"entity_id": "x", "entity_type": "task", "evil": "y"}),
        ("graph.expand", {"entity_id": "x", "entity_type": "task", "evil": "y"}),
        ("source.list", {"evil": "leak"}),
        ("source.get", {"source_id": "x", "evil": "leak"}),
        ("embeddings.find_duplicates", {"evil": "leak"}),
        ("propose.generate", {"topic": "x", "evil": "leak"}),
        ("search", {"query": "x", "evil": "leak"}),
        (
            "propose.apply",
            {"proposal_id": "x", "candidate_index": 0, "evil": "leak"},
        ),
        ("slack.demand.list", {"limit": 10, "evil": "leak"}),
    ],
)
def test_new_tool_input_schemas_reject_unknown_field(
    specs: list[Any], tool_name: str, bad_args: dict[str, Any]
) -> None:
    spec = next(s for s in specs if s.name == tool_name)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_args, schema=dict(spec.input_schema))


@pytest.mark.parametrize(
    ("tool_name", "missing_required_args"),
    [
        ("brief", {}),  # missing topic
        ("graph.related", {"entity_id": "x"}),  # missing entity_type
        ("graph.trace", {"entity_type": "task"}),  # missing entity_id
        ("graph.expand", {"entity_id": "x"}),  # missing entity_type
        ("source.get", {}),  # missing source_id
    ],
)
def test_new_tool_input_schemas_reject_missing_required(
    specs: list[Any], tool_name: str, missing_required_args: dict[str, Any]
) -> None:
    spec = next(s for s in specs if s.name == tool_name)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=missing_required_args, schema=dict(spec.input_schema))


# ----------------------------------------------- propose.generate schema accept


def test_propose_generate_accepts_topic_only(specs: list[Any]) -> None:
    """Topic-only call validates (``reply_to_source_id`` optional)."""
    spec = next(s for s in specs if s.name == "propose.generate")
    jsonschema.validate(instance={"topic": "next-action review"}, schema=dict(spec.input_schema))


def test_propose_generate_accepts_reply_mode(specs: list[Any]) -> None:
    """Reply-draft mode validates with only ``reply_to_source_id``."""
    spec = next(s for s in specs if s.name == "propose.generate")
    jsonschema.validate(
        instance={"reply_to_source_id": "01HSRC000000000000000R001"},
        schema=dict(spec.input_schema),
    )


def test_propose_generate_no_required_fields_validates_empty(specs: list[Any]) -> None:
    """Even ``{}`` validates schema-side (defaults fill in).

    The runtime handler raises ``OpsHubError`` when both topic and
    reply_to_source_id are absent; the schema does not enforce
    "exactly one of" because JSON Schema's ``oneOf`` does not compose
    cleanly with ``default`` fields. The unit boundary stays at the
    handler — exercised by the dispatch-level tests when wiring is
    available.
    """
    spec = next(s for s in specs if s.name == "propose.generate")
    jsonschema.validate(instance={}, schema=dict(spec.input_schema))


# ----------------------------------------------- registry policy guard


def test_propose_generate_is_destructive_open_world(specs: list[Any]) -> None:
    """HITL write boundary: destructive + open-world (LLM round trip)."""
    spec = next(s for s in specs if s.name == "propose.generate")
    assert spec.policy.read_only is False
    assert spec.policy.destructive is True
    assert spec.policy.open_world is True


def test_propose_generate_accepts_h4_mode(specs: list[Any]) -> None:
    """Phase 12 H4 (ADR-0016 §決定 (l)(b)): ``mode`` dispatch keys validate.

    Each of the three H4 modes must validate against the schema when
    paired with ``topic``. ``reply_to_source_id`` stays mutually
    exclusive (handler-level guard, not schema-level — same approach
    as ``test_propose_generate_no_required_fields_validates_empty``).
    """
    spec = next(s for s in specs if s.name == "propose.generate")
    for mode in ("inbox_triage", "source_extract", "meeting_followup"):
        jsonschema.validate(
            instance={"topic": "weekly inbox", "mode": mode},
            schema=dict(spec.input_schema),
        )


def test_propose_generate_rejects_unknown_mode(specs: list[Any]) -> None:
    """Unknown ``mode`` values must be rejected by the schema enum."""
    spec = next(s for s in specs if s.name == "propose.generate")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance={"topic": "x", "mode": "handoff_draft"},
            schema=dict(spec.input_schema),
        )


def test_propose_generate_rejects_text_only_modes_in_enum(specs: list[Any]) -> None:
    """ADR-0016 §決定 (l)(b) Negative arm: text-only modes are NOT in the enum.

    ``handoff_draft`` / ``announcement_draft`` skills return text only
    and never persist a proposal (§決定 (l)(a)). They must not appear
    in the ``mode`` enum so a misconfigured host LLM cannot accidentally
    route through ``propose.generate``.
    """
    spec = next(s for s in specs if s.name == "propose.generate")
    mode_schema = spec.input_schema["properties"]["mode"]
    enum_values = set(mode_schema.get("enum", ()))
    assert "handoff_draft" not in enum_values, (
        "handoff_draft must NOT be in ``mode`` enum (text-only, no persist path)"
    )
    assert "announcement_draft" not in enum_values, (
        "announcement_draft must NOT be in ``mode`` enum (text-only, no persist path)"
    )
    # Sanity check the positive arm — the enum must equal the H4 triple.
    assert enum_values == {"inbox_triage", "source_extract", "meeting_followup"}


def test_brief_is_read_only(specs: list[Any]) -> None:
    spec = next(s for s in specs if s.name == "brief")
    assert spec.policy.read_only is True
    assert spec.policy.destructive is False


def test_graph_tools_are_read_only_closed_world(specs: list[Any]) -> None:
    for tool_name in ("graph.related", "graph.trace", "graph.expand"):
        spec = next(s for s in specs if s.name == tool_name)
        assert spec.policy.read_only is True, tool_name
        assert spec.policy.open_world is False, tool_name


def test_source_tools_are_read_only(specs: list[Any]) -> None:
    for tool_name in ("source.list", "source.get"):
        spec = next(s for s in specs if s.name == tool_name)
        assert spec.policy.read_only is True, tool_name


def test_embeddings_find_duplicates_is_read_only(specs: list[Any]) -> None:
    spec = next(s for s in specs if s.name == "embeddings.find_duplicates")
    assert spec.policy.read_only is True
    assert spec.policy.destructive is False
