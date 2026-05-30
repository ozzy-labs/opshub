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
    "recall.search",
    "task.list",
    "inbox.list",
    "decision.list",
    "task.create",
    "inbox.add",
    "connector.sync",
)


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
