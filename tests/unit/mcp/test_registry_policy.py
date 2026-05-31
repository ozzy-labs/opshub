"""Static guards over the MCP tool registry (ADR-0022 §(c)).

The MCP tool surface is policy-as-data — the registry in
:mod:`opshub.mcp._registry` is the single source of truth for the
read / write split and the resulting ``ToolAnnotations``. These tests
keep the data shape honest:

* Every read-category tool is ``read_only=true`` and
  ``destructive=false``.
* Every write-category tool is ``read_only=false`` and
  ``destructive=true`` (ADR-0022 §(c) auto-approve 84% vs HITL <5%).
* Tool input schemas must not declare a ``token`` / ``access_token`` /
  ``api_key`` / ``Authorization`` field — secrets stay inside the
  ① core (ADR-0022 §(b) Token Passthrough 禁止).
* Every spec has a unique name (so dispatch is unambiguous) and an
  ``additionalProperties: false`` clause (so future agent argument
  smuggling does not slip past the input schema).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest

from opshub.mcp._registry import (
    ReadCategory,
    WriteCategory,
    build_tool_specs,
)


def _stub_handler() -> Callable[[Mapping[str, Any]], Awaitable[str]]:
    async def _h(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        return "ok"

    return _h


_TOOL_NAMES: tuple[str, ...] = (
    # Phase 10 C2 baseline.
    "recall.search",
    "task.list",
    "inbox.list",
    "decision.list",
    "task.create",
    "inbox.add",
    "connector.sync",
    # Step 1 widening (post Phase 10 / pre Phase 11): briefing, graph,
    # source, duplicates, HITL propose. See PR-#xxx ("MCP tool widening").
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
)

# Phase 12 H1: ``propose.apply`` is the only write-class tool with
# ``destructive=false`` + ``idempotent=true``. The handler layer
# normalises the idempotency case so a second call with the same
# ``(proposal_id, candidate_index)`` is observably a no-op (see
# ``opshub.mcp._writes.build_propose_apply_handler``). The
# ``destructive`` invariant test below carves this name out so the
# guard still catches the regression "new write tool forgot
# destructive=true" for every other write category.
_NON_DESTRUCTIVE_WRITES: frozenset[str] = frozenset({"propose.apply"})


@pytest.fixture
def specs() -> list[Any]:
    handlers = {name: _stub_handler() for name in _TOOL_NAMES}
    return build_tool_specs(handlers=handlers)


def test_read_tools_advertise_read_only_and_non_destructive(specs: list[Any]) -> None:
    for spec in specs:
        if isinstance(spec.category, ReadCategory):
            assert spec.policy.read_only is True, (
                f"read tool {spec.name!r} must declare read_only=true"
            )
            assert spec.policy.destructive is False, (
                f"read tool {spec.name!r} must declare destructive=false"
            )


def test_write_tools_advertise_destructive_and_non_read_only(specs: list[Any]) -> None:
    for spec in specs:
        if isinstance(spec.category, WriteCategory):
            assert spec.policy.read_only is False, (
                f"write tool {spec.name!r} must declare read_only=false"
            )
            # Phase 12 H1 (ADR-0022 改訂): ``propose.apply`` is the
            # documented carve-out — ``destructive=false`` because the
            # handler normalises the idempotency case (second call is
            # a no-op). Every other write category must still pin
            # ``destructive=true``.
            if spec.name in _NON_DESTRUCTIVE_WRITES:
                assert spec.policy.destructive is False, (
                    f"write tool {spec.name!r} is in the documented"
                    " _NON_DESTRUCTIVE_WRITES carve-out and must declare"
                    " destructive=false"
                )
            else:
                assert spec.policy.destructive is True, (
                    f"write tool {spec.name!r} must declare destructive=true"
                )


def test_no_input_schema_accepts_a_secret_field(specs: list[Any]) -> None:
    forbidden = {"token", "access_token", "api_key", "authorization", "secret"}
    for spec in specs:
        schema_any: Any = spec.input_schema or {}
        properties_any: Any = schema_any.get("properties", {})
        for prop_name in list(properties_any):
            name = str(prop_name)
            assert name.lower() not in forbidden, (
                f"tool {spec.name!r} input schema declares forbidden field {name!r} "
                "(ADR-0022 §(b) Token Passthrough 禁止)"
            )


def test_all_input_schemas_are_closed(specs: list[Any]) -> None:
    """Every input schema must set ``additionalProperties: false``.

    Without this clause an agent host could smuggle extra fields
    through ``call_tool``. The check defends the boundary even when
    new tools are added later.
    """
    for spec in specs:
        assert spec.input_schema.get("additionalProperties") is False, (
            f"tool {spec.name!r} input schema must set additionalProperties=false"
        )


def test_tool_names_are_unique(specs: list[Any]) -> None:
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"


def test_registry_covers_phase_10_c2_surface(specs: list[Any]) -> None:
    """Spec coverage matches ``docs/mcp-setup.md`` §3 table."""
    expected = set(_TOOL_NAMES)
    actual = {s.name for s in specs}
    assert actual == expected, (
        f"unexpected registry surface change: missing={expected - actual} extra={actual - expected}"
    )


def test_propose_generate_is_hitl_write(specs: list[Any]) -> None:
    """``propose.generate`` must surface as a destructive write tool.

    HITL boundary: ProposalGenerated lands on the durable event log
    (cost + audit) so hosts honouring ``destructiveHint=true`` will
    require operator confirmation. ``open_world=true`` reflects the LLM
    provider round-trip leaving the local box.
    """
    for spec in specs:
        if spec.name == "propose.generate":
            assert spec.policy.read_only is False
            assert spec.policy.destructive is True
            assert spec.policy.open_world is True
            return
    raise AssertionError("propose.generate spec missing from registry")


def test_brief_is_read_only_local(specs: list[Any]) -> None:
    """``brief`` is classified read despite calling the LLM provider.

    Mirrors the recall.search precedent: an LLM call is "observation
    only" from the agent host's perspective — no durable entity is
    created beyond the BriefingGenerated event, and the host already
    pays the cost. ``open_world`` stays false so the auto-approve
    surface is unchanged from a host policy standpoint.
    """
    for spec in specs:
        if spec.name == "brief":
            assert spec.policy.read_only is True
            assert spec.policy.destructive is False
            return
    raise AssertionError("brief spec missing from registry")


def test_read_tools_are_idempotent(specs: list[Any]) -> None:
    """Read tools are safe to retry; the hint helps host policies."""
    for spec in specs:
        if isinstance(spec.category, ReadCategory):
            assert spec.policy.idempotent is True, (
                f"read tool {spec.name!r} should advertise idempotent=true"
            )


def test_connector_sync_is_open_world(specs: list[Any]) -> None:
    """`connector.sync` interacts with an external SaaS, so the
    ``openWorldHint`` must be true.
    """
    for spec in specs:
        if spec.name == "connector.sync":
            assert spec.policy.open_world is True


def test_local_read_tools_are_closed_world(specs: list[Any]) -> None:
    """Read tools hit local SQLite only; ``openWorldHint`` is false."""
    for spec in specs:
        if isinstance(spec.category, ReadCategory):
            assert spec.policy.open_world is False, (
                f"read tool {spec.name!r} hits local SQLite only; open_world must be false"
            )


# ---------------------------------------------------------------------------
# Phase 12 H1 (ADR-0022 改訂) semantic pins.
# ---------------------------------------------------------------------------


def test_propose_apply_is_idempotent_non_destructive(specs: list[Any]) -> None:
    """``propose.apply`` advertises idempotent=true + destructive=false.

    The handler layer (``opshub.mcp._writes.build_propose_apply_handler``)
    catches ``OpsHubError("already applied/rejected")`` and returns
    a normalised ``{ok:true, already_applied:true, ...}`` envelope so
    the annotation contract is honest. Read-only stays false because
    the first call still emits durable events (TaskCreated /
    DecisionRecorded / ReplyDraftApplied + ProposalApplied).
    """
    for spec in specs:
        if spec.name == "propose.apply":
            assert spec.policy.read_only is False
            assert spec.policy.destructive is False
            assert spec.policy.idempotent is True
            assert spec.policy.open_world is False
            return
    raise AssertionError("propose.apply spec missing from registry")


def test_propose_apply_schema_takes_proposal_id_and_index(specs: list[Any]) -> None:
    """``propose.apply`` input schema must take ``proposal_id`` + ``candidate_index``."""
    for spec in specs:
        if spec.name == "propose.apply":
            schema_any: Any = spec.input_schema
            properties: Any = schema_any.get("properties", {})
            assert "proposal_id" in properties, (
                "propose.apply must accept ``proposal_id`` as the natural key"
            )
            assert "candidate_index" in properties, (
                "propose.apply must accept ``candidate_index`` for dispatch"
            )
            required: Any = schema_any.get("required", [])
            assert "proposal_id" in required
            assert "candidate_index" in required
            return
    raise AssertionError("propose.apply spec missing from registry")


def test_search_does_not_expose_raw_query(specs: list[Any]) -> None:
    """``search`` input schema must NOT expose the ``raw_query`` flag.

    ADR-0022 改訂 pins the contract: ``raw_query`` is CLI-only so the
    MCP boundary stays safe for host LLMs that pass free-form token
    streams (phrase quoting handles FTS5 syntax characters by default).
    A regressing PR that re-adds the flag here is exactly what this
    guard catches.
    """
    for spec in specs:
        if spec.name == "search":
            schema_any: Any = spec.input_schema
            properties: Any = schema_any.get("properties", {})
            assert "raw_query" not in properties, (
                "search MCP schema must NOT expose ``raw_query``"
                " (ADR-0022 改訂 §決定 — CLI-only flag)"
            )
            # And the schema must still take ``query`` as required input.
            assert "query" in properties
            assert "query" in schema_any.get("required", [])
            return
    raise AssertionError("search spec missing from registry")


def test_search_is_read_only_closed_world(specs: list[Any]) -> None:
    """``search`` advertises ``read_only=true`` + ``destructive=false`` + ``open_world=false``.

    Phase 12 audit Cluster B L8 pin: the existing read-only /
    closed-world invariants are checked through aggregate per-category
    loops (``test_read_tools_advertise_read_only_and_non_destructive``
    / ``test_local_read_tools_are_closed_world``) but the FTS5
    ``search`` surface deserves a dedicated symbolic pin so a refactor
    that drops it from :class:`ReadCategory` (e.g. by renaming the
    enum member) surfaces here with a clear message instead of as a
    silently-missing per-tool assertion. The four flags below are the
    canonical contract from ADR-0022 改訂 §決定 (f-1) — phrase-quoted
    FTS5 MATCH against local SQLite, no LLM round-trip, no external
    side effect.
    """
    for spec in specs:
        if spec.name == "search":
            assert spec.policy.read_only is True, (
                "search policy must have read_only=true (ADR-0022 §決定 (f-1))"
            )
            assert spec.policy.destructive is False, (
                "search policy must have destructive=false (read-only surface)"
            )
            assert spec.policy.open_world is False, (
                "search policy must have open_world=false — FTS5 MATCH"
                " hits local SQLite only, no LLM / external round-trip"
            )
            # Idempotent because the same query against the same DB
            # snapshot returns the same hits; pin so a future refactor
            # that flips the flag is caught.
            assert spec.policy.idempotent is True, (
                "search policy must have idempotent=true (read tools are safe to retry)"
            )
            return
    raise AssertionError("search spec missing from registry")


def test_list_tools_expose_physical_column_time_filters(specs: list[Any]) -> None:
    """Phase 12 H1 (ADR-0022 改訂): physical-column ``*_after/before`` filters.

    The plan §3 H1-b pins per-tool independent naming so the MCP
    argument names map 1:1 to the projection physical columns
    (``tasks.updated_at`` etc.). The mapping table below is the SSOT
    for this contract — any drift between the registry and the
    handler's where-clause is caught here.
    """
    expected: dict[str, tuple[str, str]] = {
        "task.list": ("updated_after", "updated_before"),
        "inbox.list": ("created_after", "created_before"),
        "decision.list": ("recorded_after", "recorded_before"),
        "source.list": ("observed_after", "observed_before"),
    }
    for spec in specs:
        pair = expected.get(spec.name)
        if pair is None:
            continue
        properties: Any = spec.input_schema.get("properties", {})
        for field_name in pair:
            assert field_name in properties, (
                f"{spec.name!r} input schema missing physical-column"
                f" time filter {field_name!r} (Phase 12 H1)"
            )
            field_schema: Any = properties[field_name]
            assert field_schema.get("type") == "string"
            assert field_schema.get("format") == "date-time"
