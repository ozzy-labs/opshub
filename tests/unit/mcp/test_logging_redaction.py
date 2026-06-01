"""Tests for MCP boundary logging redaction + env-var verbosity (T3 / #320).

Phase 14 T3 (parent epic #317) pins three contracts on the MCP server:

1. **OTel GenAI naming logs are redacted (R1 / R4)**: every record
   emitted by :mod:`opshub.mcp._logging` flows through the T1
   structlog redaction processor before reaching the renderer.
   Even if a future caller passes a token-shaped string into the
   ``gen_ai.tool.name`` / ``tool_name`` attribute by mistake, the
   stderr output must not surface the raw shape.
2. **``OPSHUB_LOG_LEVEL`` / ``OPSHUB_DEBUG`` drive verbosity**: the
   MCP server runs as a subprocess of the agent host, so the CLI
   flags that :mod:`opshub.cli.app` parses never reach
   :func:`opshub.mcp.server.serve_stdio`. The env vars (folded into
   :class:`opshub.core.logging.LogSettings`) are the only steering
   knob, and ``serve_stdio`` consumes them before any
   ``get_logger`` call in the dispatch path.
3. **ADR-0022 §(b) — no token passthrough on the MCP response**:
   even with ``OPSHUB_DEBUG=1`` (or any verbosity bump), the
   :func:`dispatch_tool_call` response must not carry raw tokens.
   The double-defence here is (a) handler output runs through
   :func:`opshub.mcp._redact.redact_secrets`, and (b) re-raised
   exception messages are scrubbed through the same path before the
   SDK serialises them into ``CallToolResult(isError=true)``.

The dispatch-side redaction unit tests live in
``tests/unit/mcp/test_server_dispatch.py``; this module focuses on
the *logging* / *configuration* side of the same surface so the
contracts stay pinned even if a refactor splits the dispatch path
again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest

from opshub.core.errors import OpsHubError
from opshub.mcp._logging import (
    log_tool_call_complete,
    log_tool_call_start,
    new_call_id,
)
from opshub.mcp._registry import (
    ReadCategory,
    ToolPolicy,
    ToolSpec,
    WriteCategory,
)
from tests._secrets import (
    FAKE_JWT,
    FAKE_SLACK_BOT_TOKEN,
)

# Token-shape literals built from concat (mirrors ``tests/unit/core/test_logging.py``).
FAKE_SK_KEY = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"
FAKE_GHP_KEY = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
FAKE_BEARER_TAIL = "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890"
FAKE_BEARER_HEADER = f"Bearer {FAKE_BEARER_TAIL}"


def _spec(
    name: str,
    *,
    handler: Callable[[Mapping[str, Any]], Awaitable[str]],
    read: bool,
) -> ToolSpec:
    """Build a synthetic :class:`ToolSpec` (mirrors test_server_dispatch)."""
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


# ============================================================================
# 1. OTel GenAI naming logs flow through the T1 redaction processor
# ============================================================================


class _CapturingLogger:
    """structlog-shaped logger that records every ``info`` call's kwargs.

    The MCP OTel helpers in :mod:`opshub.mcp._logging` accept the
    structlog ``FilteringBoundLogger`` Protocol — at the boundary the
    only operation they perform is ``logger.info(event, **kwargs)``.
    Using a capturing stand-in lets us assert what *would* be passed
    into the structlog pipeline (and therefore into the T1 redaction
    processor) without depending on process-scope ``configure_logging``
    ordering or pytest's stderr capture (structlog caches the stderr
    stream on first use, so ``capsys`` is unreliable here).

    The redaction-processor side of the same contract is pinned
    end-to-end in :mod:`tests.unit.core.test_logging`; this module
    only needs to confirm that **the OTel helpers themselves do not
    interpolate tokens into the payload** before handing it off.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


class TestOtelLogRedaction:
    """``log_tool_call_start`` / ``log_tool_call_complete`` events are scrubbed."""

    def test_tool_name_kwarg_value_is_routed_through_redaction(self) -> None:
        """The processor scrubs a token-shaped ``tool_name`` kwarg.

        This is a defensive pin: in practice the tool name is sourced
        from :class:`opshub.mcp._registry.ToolSpec.name` which is a
        registered constant. But the redaction processor catches the
        string regardless of provenance — if a future test fixture
        builds a tool with a token-shaped name we still don't leak it
        into the rendered output.

        We assert the contract in two steps so the test is fully
        deterministic: (1) the OTel helper sends the value into the
        structlog ``info`` call verbatim under ``gen_ai.tool.name``
        (no premature stringification or interpolation), and (2)
        running that event-dict through the T1 redaction processor
        rewrites the token to its marker form.
        """
        from opshub.core.logging import (
            _redaction_processor,  # pyright: ignore[reportPrivateUsage]
        )

        capturing = _CapturingLogger()
        log_tool_call_start(
            capturing,
            tool_name=f"connector.sync.{FAKE_SK_KEY}",
            call_id=new_call_id(),
        )

        assert len(capturing.events) == 1
        event, payload = capturing.events[0]
        # Step (1): the helper hands the value through cleanly.
        assert event == "mcp.execute_tool"
        raw_tool_name = payload["gen_ai.tool.name"]
        assert FAKE_SK_KEY in raw_tool_name

        # Step (2): the T1 redaction processor — the same one wired
        # into ``configure_logging`` — scrubs the value before any
        # renderer sees it.
        scrubbed = _redaction_processor(None, "info", dict(payload))
        assert FAKE_SK_KEY not in scrubbed["gen_ai.tool.name"]
        assert "sk-***" in scrubbed["gen_ai.tool.name"]

    def test_complete_payload_token_value_is_scrubbed_by_processor(self) -> None:
        """A bound bearer header on the completion payload is rewritten.

        ``log_tool_call_complete`` itself only adds fixed-shape
        attributes; but the structlog ``bind`` API lets callers attach
        arbitrary kwargs (e.g. ``logger.bind(header=auth_header)``)
        that propagate into every subsequent ``info`` call. The T1
        processor sits *after* ``format_exc_info`` and before any
        renderer, so the bound value is scrubbed regardless of the
        helper that emits the record.
        """
        from opshub.core.logging import (
            _redaction_processor,  # pyright: ignore[reportPrivateUsage]
        )

        # Simulate the dispatch path's ``logger.bind(component="mcp")``
        # chain by hand: the OTel helper produces the payload, and the
        # caller would merge bound kwargs (e.g. ``header``) into the
        # event-dict before the processor pipeline runs.
        capturing = _CapturingLogger()
        log_tool_call_complete(
            capturing,
            tool_name="recall.search",
            call_id="01HFAKEFAKEFAKEFAKEFAKEFAK",
            duration_ms=1.5,
            status="ok",
        )
        _event, payload = capturing.events[0]
        merged = {**payload, "header": FAKE_BEARER_HEADER}

        scrubbed = _redaction_processor(None, "info", merged)
        assert FAKE_BEARER_TAIL not in scrubbed["header"]
        assert "Bearer ***" in scrubbed["header"]

    def test_error_type_attribute_is_class_name_only(self) -> None:
        """``error.type`` carries the class name, not the message body.

        The OTel module documents this invariant in its docstring; the
        test pins it by capturing the kwargs that
        ``log_tool_call_complete`` hands to the structlog ``info``
        call. A future regression that quietly starts logging
        ``str(exc)`` (or any other free-form message) under that key
        will fail this assertion.
        """
        capturing = _CapturingLogger()
        log_tool_call_complete(
            capturing,
            tool_name="connector.sync",
            call_id="01HFAKEFAKEFAKEFAKEFAKEFAK",
            duration_ms=12.3,
            status="error",
            error_type="ConnectorSyncFailed",
        )

        assert len(capturing.events) == 1
        _event, payload = capturing.events[0]
        assert payload["error.type"] == "ConnectorSyncFailed"
        # And no token shapes anywhere in the payload values.
        for value in payload.values():
            if isinstance(value, str):
                for shape in (FAKE_SK_KEY, FAKE_BEARER_TAIL, FAKE_GHP_KEY, FAKE_JWT):
                    assert shape not in value


# ============================================================================
# 2. OPSHUB_LOG_LEVEL / OPSHUB_DEBUG steer MCP server verbosity
# ============================================================================


class TestServeStdioLogBootstrap:
    """``serve_stdio`` calls ``configure_logging`` with env-derived settings."""

    def test_resolve_log_settings_promotes_opshub_debug_to_debug_level(self) -> None:
        """T1 contract recap: ``OPSHUB_DEBUG=1`` → ``DEBUG`` level + ``debug=True``.

        ``serve_stdio`` reads this via ``resolve_log_settings()`` and
        passes the result to ``configure_logging``. The unit test in
        ``test_logging.py`` already covers the env-folding logic; here
        we pin that the *MCP* entry point reads the same env var, by
        re-checking the resolver and then asserting the bootstrap
        actually consumes the value.
        """
        from opshub.core.logging import resolve_log_settings

        settings = resolve_log_settings(env={"OPSHUB_DEBUG": "1"})
        assert settings.level == "DEBUG"
        assert settings.debug is True

    def test_resolve_log_settings_honours_opshub_log_level(self) -> None:
        from opshub.core.logging import resolve_log_settings

        settings = resolve_log_settings(env={"OPSHUB_LOG_LEVEL": "WARNING"})
        assert settings.level == "WARNING"

    def test_serve_stdio_configures_logging_via_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``serve_stdio`` invokes ``configure_logging`` with the env level.

        We don't actually run the stdio loop (it would block on
        stdin), but we can assert the bootstrap **call** by patching
        ``configure_logging`` and stopping execution right after.
        ``build_engine`` is also patched so the test does not need a
        live SQLite engine.
        """
        captured: dict[str, Any] = {}

        def _fake_configure(
            *,
            level: str = "INFO",
            json: bool | None = None,
            log_file: Any = None,
        ) -> None:
            captured["level"] = level
            captured["json"] = json
            captured["log_file"] = log_file
            # Short-circuit so the test does not need to fake the SDK.
            raise _BootstrapStopError

        class _BootstrapStopError(Exception):
            """Sentinel so the test exits after configure_logging."""

        # Patch into the module the function under test imports from.
        monkeypatch.setattr("opshub.core.logging.configure_logging", _fake_configure)
        monkeypatch.setenv("OPSHUB_LOG_LEVEL", "DEBUG")
        monkeypatch.delenv("OPSHUB_DEBUG", raising=False)
        monkeypatch.delenv("OPSHUB_LOG_FORMAT", raising=False)
        monkeypatch.delenv("OPSHUB_LOG_FILE", raising=False)

        import asyncio

        from opshub.mcp.server import serve_stdio

        with pytest.raises(_BootstrapStopError):
            asyncio.run(serve_stdio())

        assert captured["level"] == "DEBUG"

    def test_serve_stdio_configures_logging_via_opshub_debug(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``OPSHUB_DEBUG=1`` alone (no ``OPSHUB_LOG_LEVEL``) lifts level to DEBUG."""
        captured: dict[str, Any] = {}

        class _BootstrapStopError(Exception):
            pass

        def _fake_configure(
            *,
            level: str = "INFO",
            json: bool | None = None,
            log_file: Any = None,
        ) -> None:
            captured["level"] = level
            captured["json"] = json
            captured["log_file"] = log_file
            raise _BootstrapStopError

        monkeypatch.setattr("opshub.core.logging.configure_logging", _fake_configure)
        monkeypatch.setenv("OPSHUB_DEBUG", "1")
        monkeypatch.delenv("OPSHUB_LOG_LEVEL", raising=False)
        monkeypatch.delenv("OPSHUB_LOG_FORMAT", raising=False)
        monkeypatch.delenv("OPSHUB_LOG_FILE", raising=False)

        import asyncio

        from opshub.mcp.server import serve_stdio

        with pytest.raises(_BootstrapStopError):
            asyncio.run(serve_stdio())

        assert captured["level"] == "DEBUG"


# ============================================================================
# 3. ADR-0022 §(b) — no token passthrough even with verbose logging on
# ============================================================================


class TestNoTokenPassthroughUnderDebug:
    """``OPSHUB_DEBUG=1`` does not loosen the MCP response redactor."""

    @pytest.mark.asyncio
    async def test_dispatch_response_redacts_tokens_when_debug_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even when log verbosity is bumped, the MCP response body is scrubbed.

        Regression guard for the ADR-0022 §(b) invariant: log-level
        config and MCP response sanitisation are independent layers,
        and verbosity bumps must not bleed token content into the
        wire response. The dispatch path already pipes handler output
        through ``redact_secrets``; this test verifies that path
        survives ``OPSHUB_DEBUG=1`` (which would loosen the structlog
        renderer but must not touch the response redactor).
        """
        monkeypatch.setenv("OPSHUB_DEBUG", "1")

        from opshub.mcp.server import dispatch_tool_call

        async def handler(arguments: Mapping[str, Any]) -> str:
            _ = arguments
            return f"leaked {FAKE_GHP_KEY} and {FAKE_BEARER_HEADER}"

        spec = _spec("recall.search", handler=handler, read=True)
        content = await dispatch_tool_call({spec.name: spec}, spec.name, {})

        text = content[0].text
        assert FAKE_GHP_KEY not in text
        assert FAKE_BEARER_TAIL not in text
        assert "ghp_***" in text
        assert "Bearer ***" in text

    @pytest.mark.asyncio
    async def test_dispatch_exception_message_redacted_when_debug_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raised exceptions are still scrubbed under ``OPSHUB_DEBUG=1``.

        The SDK serialises the exception's ``str()`` into the
        ``CallToolResult(isError=true)`` payload. Even with debug
        logging on, the re-raised :class:`OpsHubError` must carry the
        redacted message body — otherwise the agent host's transcript
        records the raw token (violating ADR-0022 §(b)).
        """
        monkeypatch.setenv("OPSHUB_DEBUG", "1")

        from opshub.mcp.server import dispatch_tool_call

        async def handler(arguments: Mapping[str, Any]) -> str:
            _ = arguments
            raise RuntimeError(f"upstream 401 slack={FAKE_SLACK_BOT_TOKEN}")

        spec = _spec("connector.sync", handler=handler, read=False)
        with pytest.raises(OpsHubError) as excinfo:
            await dispatch_tool_call({spec.name: spec}, spec.name, {})

        raised = str(excinfo.value)
        assert FAKE_SLACK_BOT_TOKEN not in raised
        # The Slack marker keeps the prefix family hint (``xoxb-***``).
        assert "xoxb" in raised
        assert "***" in raised
