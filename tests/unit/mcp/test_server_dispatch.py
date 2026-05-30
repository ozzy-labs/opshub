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
    """Handler exceptions are rewrapped as ``OpsHubError`` with the
    message scrubbed (ADR-0022 §(b) error-path redaction).
    """
    from opshub.mcp.server import dispatch_tool_call

    async def handler(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        raise ValueError("boom")

    spec = _spec("task.create", handler=handler, read=False)
    with pytest.raises(OpsHubError, match="boom") as excinfo:
        await dispatch_tool_call({spec.name: spec}, spec.name, {})
    # The original exception is preserved via ``__cause__`` so a
    # server-side log inspector can still see the type.
    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_dispatch_redacts_secrets_in_exception_message() -> None:
    """ADR-0022 §(b): tokens in raised exception messages must not leak.

    Regression: the MCP SDK serialises the raised exception's ``str()``
    into ``CallToolResult(isError=true)``. If a connector raises
    ``RuntimeError("upstream said Bearer xoxb-leak-…")`` the original
    code path forwarded the raw bearer token into the agent host's
    transcript. The dispatcher now scrubs it through ``redact_secrets``
    before re-raising.
    """
    from opshub.mcp.server import dispatch_tool_call

    leaking_messages = [
        "401 from slack: xoxb-1234567890-9876543210-abcdefghij",
        "401 from openai: sk-abcdefghijklmnopqrstuvwxyz123456",
        "401 from github: ghp_abcdef0123456789ABCDEF0123456789abcd",
        "401 from upstream: Authorization: Bearer abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890",
    ]
    for message in leaking_messages:

        async def handler(arguments: Mapping[str, Any], _msg: str = message) -> str:
            _ = arguments
            raise RuntimeError(_msg)

        spec = _spec("connector.sync", handler=handler, read=False)
        with pytest.raises(OpsHubError) as excinfo:
            await dispatch_tool_call({spec.name: spec}, spec.name, {})
        raised_text = str(excinfo.value)
        # The original token must not survive.
        for token_fragment in (
            "xoxb-1234567890",
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "ghp_abcdef0123456789ABCDEF0123456789abcd",
            "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890",
        ):
            assert token_fragment not in raised_text


@pytest.mark.asyncio
async def test_recall_search_schema_rejects_unknown_field() -> None:
    """ADR-0022 §(c) input schemas declare ``additionalProperties=false``.

    The MCP SDK validates ``call_tool`` arguments against ``inputSchema``
    before reaching :func:`dispatch_tool_call`. We cannot exercise the
    SDK validator from this layer (it lives one level up), but we can
    pin the registry schema rejects unknown fields. This is the ground-
    truth signal that the schema-validation contract is intact — a
    future PR that relaxes ``additionalProperties=false`` will fail
    this test before tokens can be smuggled through ``call_tool``.
    """
    import jsonschema

    from opshub.mcp._registry import build_tool_specs
    from opshub.mcp.server import dispatch_tool_call

    async def _stub(arguments: Mapping[str, Any]) -> str:
        _ = arguments
        return "ok"

    tool_names = (
        "recall.search",
        "task.list",
        "inbox.list",
        "decision.list",
        "task.create",
        "inbox.add",
        "connector.sync",
    )
    specs = build_tool_specs(handlers=dict.fromkeys(tool_names, _stub))
    recall = next(s for s in specs if s.name == "recall.search")

    bad_args = {"query": "x", "evil_field": "leak"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_args, schema=dict(recall.input_schema))

    # Sanity: the well-formed call validates and dispatches successfully.
    good_args = {"query": "x"}
    jsonschema.validate(instance=good_args, schema=dict(recall.input_schema))
    specs_by_name = {recall.name: recall}
    content = await dispatch_tool_call(specs_by_name, recall.name, good_args)
    assert content[0].text == "ok"


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
