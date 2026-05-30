"""Materialisation guard for :func:`opshub.mcp.server._to_mcp_tool`.

ADR-0022 §(c) puts the read / write policy on the wire via the
:class:`mcp.types.ToolAnnotations` block (``readOnlyHint`` /
``destructiveHint`` / ``idempotentHint`` / ``openWorldHint``). The
registry-level invariants are covered in
:mod:`tests.unit.mcp.test_registry_policy`, but those tests inspect the
:class:`opshub.mcp._registry.ToolPolicy` directly — they would miss a
mistranslation in the ``_to_mcp_tool`` conversion (e.g. swapping
``destructiveHint`` with ``idempotentHint``).

This module pins the materialisation on all four annotation axes so a
future refactor of the conversion has to update the test alongside.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import pytest

from opshub.mcp._registry import (
    ReadCategory,
    ToolPolicy,
    ToolSpec,
    WriteCategory,
)
from opshub.mcp.server import _to_mcp_tool  # pyright: ignore[reportPrivateUsage]


def _spec(
    name: str,
    *,
    policy: ToolPolicy,
    read: bool,
) -> ToolSpec:
    """Build a minimal ``ToolSpec`` for materialisation tests."""

    async def _stub(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        return "ok"

    handler: Callable[[Mapping[str, Any]], Awaitable[str]] = _stub
    category = ReadCategory.RECALL if read else WriteCategory.TASK_CREATE
    return ToolSpec(
        name=name,
        title=f"title-{name}",
        description=f"description-{name}",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        policy=policy,
        category=category,
        handler=handler,
    )


@pytest.mark.parametrize(
    "policy",
    [
        # Read-only / non-destructive / idempotent / closed-world (typical read tool)
        ToolPolicy(read_only=True, destructive=False, idempotent=True, open_world=False),
        # Write / destructive / non-idempotent / closed-world (task.create / inbox.add)
        ToolPolicy(read_only=False, destructive=True, idempotent=False, open_world=False),
        # Write / destructive / non-idempotent / open-world (connector.sync)
        ToolPolicy(read_only=False, destructive=True, idempotent=False, open_world=True),
        # Edge: idempotent write (e.g. a future ``link.upsert``) — pin the axis-by-axis
        # mapping so a code path that flattens to read/write loses no information.
        ToolPolicy(read_only=False, destructive=False, idempotent=True, open_world=False),
    ],
)
def test_to_mcp_tool_materialises_all_four_axes(policy: ToolPolicy) -> None:
    """Every ``ToolPolicy`` axis maps 1:1 onto the MCP annotation."""
    spec = _spec("probe.tool", policy=policy, read=policy.read_only)
    tool = _to_mcp_tool(spec)
    annotations = getattr(tool, "annotations", None)
    assert annotations is not None, "tool must carry a ToolAnnotations block"
    assert annotations.readOnlyHint is policy.read_only
    assert annotations.destructiveHint is policy.destructive
    assert annotations.idempotentHint is policy.idempotent
    assert annotations.openWorldHint is policy.open_world


def test_to_mcp_tool_preserves_metadata() -> None:
    """Name / title / description / inputSchema flow through unchanged."""
    policy = ToolPolicy(read_only=True, destructive=False, idempotent=True, open_world=False)
    spec = _spec("probe.tool", policy=policy, read=True)
    tool = _to_mcp_tool(spec)
    assert getattr(tool, "name", None) == "probe.tool"
    assert getattr(tool, "title", None) == "title-probe.tool"
    assert getattr(tool, "description", None) == "description-probe.tool"
    # ``additionalProperties: false`` is the second line of defence
    # against argument smuggling (ADR-0022 §(b) / §(c)). Pin the field
    # so a future refactor that copies the schema does not drop it.
    input_schema_any: Any = getattr(tool, "inputSchema", None)
    assert isinstance(input_schema_any, dict)
    input_schema = cast("dict[str, Any]", input_schema_any)
    assert input_schema.get("additionalProperties") is False


def test_to_mcp_tool_annotations_title_matches_spec() -> None:
    """``ToolAnnotations.title`` mirrors :attr:`ToolSpec.title`.

    The MCP spec lets servers carry a human-readable title separately
    from the tool name; opshub copies the spec title into both places
    so an agent UI can present the human label without the dotted
    namespace.
    """
    policy = ToolPolicy(read_only=True, destructive=False, idempotent=True, open_world=False)
    spec = _spec("probe.tool", policy=policy, read=True)
    tool = _to_mcp_tool(spec)
    annotations = getattr(tool, "annotations", None)
    assert annotations is not None
    assert annotations.title == "title-probe.tool"
