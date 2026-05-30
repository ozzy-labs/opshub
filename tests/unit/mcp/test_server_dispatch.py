"""Dispatch behaviour for :func:`opshub.mcp.server._dispatch` (ADR-0022).

These tests stub the registry handlers so we exercise the wrapper
without spinning up the MCP SDK or a real SQLite engine. The wrapper
is where the cross-cutting invariants live:

* tool output flows through :func:`opshub.mcp._redact.redact_secrets`
  before reaching MCP ``TextContent`` (ADR-0022 §(b));
* OTel GenAI naming records fire on every call, even on failure
  (ADR-0022 §(e));
* unknown tool names raise :class:`OpsHubError` (so the SDK wraps
  the error correctly instead of crashing the server).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest

from opshub.core.errors import OpsHubError
from opshub.mcp._registry import (
    ReadCategory,
    ToolPolicy,
    ToolSpec,
    WriteCategory,
)


def _spec(
    name: str,
    *,
    handler: Callable[[Mapping[str, Any]], Awaitable[str]],
    read: bool,
) -> ToolSpec:
    policy = (
        ToolPolicy(read_only=True, destructive=False, idempotent=True, open_world=False)
        if read
        else ToolPolicy(read_only=False, destructive=True, idempotent=False, open_world=False)
    )
    category = ReadCategory.RECALL if read else WriteCategory.TASK_CREATE
    return ToolSpec(
        name=name,
        title=name,
        description=name,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        policy=policy,
        category=category,
        handler=handler,
    )


@pytest.mark.asyncio
async def test_dispatch_wraps_response_in_text_content() -> None:
    from opshub.mcp.server import dispatch_tool_call

    async def handler(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        return '{"ok":true}'

    spec = _spec("recall.search", handler=handler, read=True)
    content = await dispatch_tool_call({spec.name: spec}, spec.name, {})

    # One ``TextContent`` block containing the handler's output.
    assert len(content) == 1
    assert content[0].type == "text"
    assert content[0].text == '{"ok":true}'


@pytest.mark.asyncio
async def test_dispatch_redacts_secrets_in_response() -> None:
    from opshub.mcp.server import dispatch_tool_call

    async def handler(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        return (
            "result includes leaked sk-abcdefghijklmnopqrstuvwxyz123456 "
            "and ghp_abcdef0123456789ABCDEF0123456789abcd"
        )

    spec = _spec("recall.search", handler=handler, read=True)
    content = await dispatch_tool_call({spec.name: spec}, spec.name, {})
    text = content[0].text
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "ghp_abcdef0123456789ABCDEF0123456789abcd" not in text


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_raises_opshub_error() -> None:
    from opshub.mcp.server import dispatch_tool_call

    with pytest.raises(OpsHubError) as excinfo:
        await dispatch_tool_call({}, "no.such.tool", {})

    assert "no.such.tool" in str(excinfo.value)


@pytest.mark.asyncio
async def test_dispatch_propagates_handler_exception() -> None:
    """Handler exceptions are re-raised so the SDK can produce ``isError``."""
    from opshub.mcp.server import dispatch_tool_call

    async def handler(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        raise ValueError("boom")

    spec = _spec("task.create", handler=handler, read=False)
    with pytest.raises(ValueError, match="boom"):
        await dispatch_tool_call({spec.name: spec}, spec.name, {})


@pytest.mark.asyncio
async def test_dispatch_passes_arguments_through_to_handler() -> None:
    from opshub.mcp.server import dispatch_tool_call

    received: dict[str, Any] = {}

    async def handler(arguments: Mapping[str, Any]) -> str:
        received.update(arguments)
        return "ok"

    spec = _spec("recall.search", handler=handler, read=True)
    await dispatch_tool_call({spec.name: spec}, spec.name, {"query": "hello", "limit": 3})
    assert received == {"query": "hello", "limit": 3}


@pytest.mark.asyncio
async def test_dispatch_handles_none_arguments() -> None:
    """MCP can send ``None`` when a tool has no required arguments."""
    from opshub.mcp.server import dispatch_tool_call

    async def handler(arguments: Mapping[str, Any]) -> str:
        assert arguments == {}
        return "ok"

    spec = _spec("decision.list", handler=handler, read=True)
    content = await dispatch_tool_call({spec.name: spec}, spec.name, None)
    assert content[0].text == "ok"
