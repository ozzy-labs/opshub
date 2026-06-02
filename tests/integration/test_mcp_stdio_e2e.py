"""End-to-end MCP stdio transport via a real subprocess (ADR-0022 §(a)).

Why integration-level (not unit / in-process)
---------------------------------------------

The opshub MCP server (:func:`opshub.mcp.server.serve_stdio`) is the
**single** transport opshub exposes — ADR-0022 §(a) pins ``stdio one
transport``. Every agent host (Claude Code, Codex CLI, Copilot CLI,
Gemini CLI) connects by spawning ``opshub mcp serve`` as a child
process and trading MCP JSON-RPC frames over stdin / stdout. The other
``tests/unit/mcp/`` suites cover the policy registry, redaction, and
``serve_stdio``'s logging bootstrap, but none of them actually starts
the subprocess and trades frames:

* ``tests/unit/mcp/test_logging_redaction.py::
  test_serve_stdio_configures_logging_via_env`` monkey-patches
  :func:`mcp.server.stdio.stdio_server` so the body never runs.
* ``tests/integration/test_phase12_assistant_lifecycle.py`` calls
  :func:`opshub.mcp.server.dispatch_tool_call` **in-process** against
  the in-memory engine, which bypasses the stdio framing entirely.

That gap means a regression in the JSON-RPC framing, the
``InitializeResult.serverInfo`` schema, the ``tools/list`` envelope,
or the ``tools/call`` content-block wrapper would only surface when a
real agent host connects — i.e. on the user's machine, not in CI.
This module closes the gap by spawning ``python -m opshub mcp serve``
as an actual subprocess and driving it through the official Python
:mod:`mcp` SDK client (the same wire format Claude Code uses).

Invariants pinned
-----------------

1. **17-tool surface (Phase 12 H1 / ADR-0022 §決定 (f))** — the
   ``tools/list`` reply contains exactly the 12 read + 5 write tools
   the assistant 14-skill catalog depends on. A regression that
   drops or renames any of them surfaces here as a missing-name
   assertion failure before it reaches a real agent host.
2. **``serverInfo`` schema** — :class:`mcp.types.InitializeResult`
   exposes ``serverInfo.name == "opshub"`` so an agent host's
   capability discovery does not pivot on a renamed server.
3. **stdio framing end-to-end** — the SDK's ``ClientSession``
   handshake (``initialize`` → ``notifications/initialized`` →
   ``tools/list`` → ``tools/call``) round-trips successfully against
   a real subprocess. Future refactors that accidentally swap
   ``stdio_server`` for ``streamable_http`` (ADR-0022 §(a) tripwire)
   or break the JSON-RPC envelope would fail here.
4. **``search`` end-to-end against a seeded DB** — the second test
   seeds two Slack sources (one English-bodied, one Japanese-bodied
   to exercise the Phase 15 trigram tokenizer / ADR-0028) via
   :class:`SourceService.observe`, then drives ``tools/call search``
   over the real stdio transport and asserts the body-anchored hits
   surface in the response envelope. This pins the path that the
   ``find-document`` / ``research`` assistant skills rely on when an
   agent host connects.

Test runtime budget
-------------------

Each test bounds the subprocess lifetime via :func:`asyncio.wait_for`
(30 s wall) so a regression that hangs the handshake fails fast
rather than wedging the suite. The subprocess startup + handshake +
two tool calls land well under that on a developer laptop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

# The MCP SDK lives behind the ``[mcp]`` extra; skip the whole module
# when the extra is not installed so a minimal ``uv sync`` does not
# trip on the import below. The CI lane always installs ``--extra mcp``
# so the skip is dev-machine-only.
pytest.importorskip(
    "mcp",
    reason="MCP stdio e2e tests require the 'mcp' extra (uv sync --extra mcp)",
)

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_PathsDict = dict[str, Path]

# Wall-clock bound for the whole subprocess handshake + tool dance.
# Generous enough to absorb cold-start ``import mcp`` + SQLAlchemy
# engine build on a slow CI runner; tight enough to fail fast if the
# server hangs in ``initialize`` (e.g. a future refactor that swaps in
# a blocking call before the JSON-RPC loop starts).
_E2E_TIMEOUT_SECONDS = 30.0

# Phase 12 H1 MCP tool surface — 12 read + 5 write. The set is pinned
# verbatim against the names in :func:`opshub.mcp.server
# .build_tool_specs_for_engine` (the in-process test in
# :mod:`tests.integration.test_phase12_assistant_lifecycle` pins the
# same set against the spec list; this module pins it through the
# real stdio transport so a regression that drops a tool **at the
# wire layer** also fails).
_EXPECTED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # Phase 10 C2 baseline (read).
        "recall.search",
        "task.list",
        "inbox.list",
        "decision.list",
        # Phase 10 C2 baseline (write).
        "task.create",
        "inbox.add",
        "connector.sync",
        # Step 1 widening — read.
        "brief",
        "graph.related",
        "graph.trace",
        "graph.expand",
        "source.list",
        "source.get",
        "embeddings.find_duplicates",
        # Step 1 widening — HITL write.
        "propose.generate",
        # Phase 12 H1 widening (ADR-0022 改訂 §決定 (f)).
        "search",
        "propose.apply",
    }
)


# ---------------------------------------------------------------------------
# Helpers — subprocess env + session lifecycle.
# ---------------------------------------------------------------------------


def _subprocess_env(isolated_env: _PathsDict) -> dict[str, str]:
    """Build the env dict the MCP subprocess inherits.

    Starts from a copy of the parent ``os.environ`` (the subprocess
    needs ``PATH`` / ``HOME`` / ``VIRTUAL_ENV`` / etc. to resolve the
    ``opshub`` package and ``sqlite-vec`` extension), then overlays
    the ``OPSHUB_*`` paths from the :func:`tests.integration.conftest
    .isolated_env` fixture so the subprocess and the parent test share
    the same on-disk database. Mirrors the ``monkeypatch.setenv`` calls
    in :func:`isolated_env` but materialised as a dict because
    :class:`StdioServerParameters` takes ``env`` by value (not by
    inheriting from the parent).
    """
    env = dict(os.environ)
    env["OPSHUB_CONFIG_DIR"] = str(isolated_env["config_dir"])
    env["OPSHUB_DATA_DIR"] = str(isolated_env["data_dir"])
    env["OPSHUB_WORKSPACE__ROOT"] = str(isolated_env["workspace_root"])
    env["OPSHUB_STORAGE__DB_PATH"] = str(isolated_env["db_path"])
    env["XDG_STATE_HOME"] = str(isolated_env["state_dir"])
    # Clear any session env that could bleed from the host shell.
    env.pop("OPSHUB_ACTOR", None)
    env.pop("OPSHUB_WORK_SESSION_ID", None)
    return env


@asynccontextmanager
async def _opshub_mcp_session(
    isolated_env: _PathsDict,
) -> AsyncGenerator[ClientSession]:
    """Spawn ``python -m opshub mcp serve`` and yield an initialised session.

    Uses the same :class:`StdioServerParameters` shape that Claude
    Code / Codex CLI emit, so a regression at the wire layer surfaces
    here as a failed ``initialize`` or ``list_tools`` call. The
    subprocess is torn down on context-manager exit by the SDK's
    ``stdio_client`` cleanup.

    ``read_timeout_seconds`` is set on the session so a stuck server
    surfaces as :class:`McpError` rather than wedging the outer
    ``asyncio.wait_for`` budget.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "opshub", "mcp", "serve"],
        env=_subprocess_env(isolated_env),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=_E2E_TIMEOUT_SECONDS),
        ) as session:
            await session.initialize()
            yield session


def _extract_text_payload(content: list[Any]) -> str:
    """Return the single text block from a ``CallToolResult.content`` list.

    The opshub MCP server wraps every tool reply in exactly one
    :class:`mcp.types.TextContent` block (see
    :func:`opshub.mcp.server._to_text_content`). We mirror that
    invariant here so a future refactor that splits the payload
    across multiple blocks trips loudly.
    """
    assert len(content) == 1, f"expected 1 TextContent block, got {len(content)}: {content!r}"
    block = content[0]
    text = getattr(block, "text", None)
    assert isinstance(text, str), f"expected TextContent.text str, got {block!r}"
    return text


# ---------------------------------------------------------------------------
# Test 1 — initialize + tools/list against a real subprocess.
# ---------------------------------------------------------------------------


def test_mcp_stdio_initialize_and_list_tools_via_subprocess(
    isolated_env: _PathsDict,
) -> None:
    """End-to-end: spawn ``opshub mcp serve`` and walk initialize → tools/list.

    Pins three contracts the agent hosts rely on at connection time:

    1. The ``initialize`` reply returns ``serverInfo.name == "opshub"``
       and a non-empty ``protocolVersion`` (the SDK validates the
       version against its supported range, so an empty / bogus
       value would raise inside :func:`ClientSession.initialize`).
    2. ``capabilities.tools`` is advertised — without it an agent host
       skips the assistant 14-skill surface entirely.
    3. ``tools/list`` carries exactly the 17-tool Phase 12 H1 surface
       defined in :data:`_EXPECTED_TOOL_NAMES`. A regression that
       drops, renames, or accidentally exposes a new tool fails
       here before it reaches a real Claude Code / Codex CLI host.
    """

    async def _drive() -> None:
        async with _opshub_mcp_session(isolated_env) as session:
            # Re-driving ``initialize`` would error; the helper already
            # ran it. Snapshot the negotiated state via the SDK API.
            init = session.get_server_capabilities()
            # ``capabilities.tools`` must be advertised for the host to
            # auto-discover the assistant tool surface.
            assert init is not None, "server capabilities missing post-initialize"
            assert init.tools is not None, (
                "ADR-0022 §決定 (a) — MCP server must advertise the ``tools``"
                f" capability; got: {init!r}"
            )

            # ``list_tools`` round-trip through the real stdio transport.
            tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}
            assert tool_names == _EXPECTED_TOOL_NAMES, (
                "ADR-0022 §決定 (f) — Phase 12 H1 MCP surface drift."
                f" missing: {_EXPECTED_TOOL_NAMES - tool_names},"
                f" unexpected: {tool_names - _EXPECTED_TOOL_NAMES}"
            )

            # Spot-check that the tool annotations made it through the
            # wire layer — ``search`` is read-only, ``propose.apply``
            # is the HITL write tool with the idempotent hint set.
            tools_by_name = {tool.name: tool for tool in tools_result.tools}
            search_tool = tools_by_name["search"]
            search_anno = search_tool.annotations
            assert search_anno is not None, search_tool
            assert search_anno.readOnlyHint is True, (
                f"ADR-0022 §決定 (b) — search must remain read-only; got: {search_anno!r}"
            )

            propose_apply_tool = tools_by_name["propose.apply"]
            apply_anno = propose_apply_tool.annotations
            assert apply_anno is not None, propose_apply_tool
            assert apply_anno.idempotentHint is True, (
                "ADR-0022 §決定 (f-2) — propose.apply must advertise"
                f" idempotent=true; got: {apply_anno!r}"
            )

    asyncio.run(asyncio.wait_for(_drive(), timeout=_E2E_TIMEOUT_SECONDS))


# ---------------------------------------------------------------------------
# Test 2 — tools/call search against a seeded SQLite DB.
# ---------------------------------------------------------------------------


def _seed_slack_sources(actor: str = "test:mcp_stdio_e2e") -> tuple[str, str]:
    """Persist two Slack sources (English + Japanese body) via the service.

    Bypasses the connector / fetcher / CLI stack — we are exercising
    the MCP transport, not the Slack ingest path. Direct service use
    keeps the test focused on the JSON-RPC frame round-trip and
    avoids dragging in the Slack token / channels env wiring the
    :mod:`tests.integration.test_phase7_slack_sync` suite needs. The
    bodies are picked so the FTS5 trigram tokenizer (Phase 15 /
    ADR-0028) returns hits for both an ASCII and a Japanese query —
    that exercises the path the ``find-document`` and ``research``
    assistant skills rely on at runtime.

    Returns the two minted source ULIDs so callers can correlate the
    seeded rows with the ``tools/call search`` response.
    """
    from opshub.cli._wiring import build_source_service

    service = build_source_service(actor=actor)
    en_observed, _ = service.observe(
        connector_name="slack",
        external_id="C0E2E:1717400000.000100",
        source_type="slack_message",
        title="alice in #e2e — Phase 12 assistant skills review",
        body=(
            "Phase 12 assistant skills のレビュー方針 — alice posted notes "
            "on the Phase 12 assistant skills lifecycle review."
        ),
        provenance_origin="external",
        provenance_trust="untrusted",
    )
    ja_observed, _ = service.observe(
        connector_name="slack",
        external_id="C0E2E:1717400000.000200",
        source_type="slack_message",
        title="bob in #e2e — レビュー方針メモ",
        body=(
            "本日のレビュー方針は Phase 12 assistant skills の "
            "lifecycle を MCP stdio 経由で確認することです。"
        ),
        provenance_origin="external",
        provenance_trust="untrusted",
    )
    return en_observed.aggregate_id, ja_observed.aggregate_id


def test_mcp_stdio_tools_call_search_against_slack_seeded_db_via_subprocess(
    isolated_env: _PathsDict,
) -> None:
    """End-to-end: ``tools/call search`` against a Slack-seeded DB via stdio.

    Seeds two Slack-shaped sources in the **parent** process (the
    subprocess and the test share the same SQLite file through the
    ``OPSHUB_STORAGE__DB_PATH`` env override), then drives a real
    ``tools/call`` over the stdio transport for two queries:

    1. ``"Phase 12 assistant skills"`` — ASCII body anchor. The
       English-bodied row must appear in the response ``items[]``
       (and we filter to ``connector_name == "slack"`` so a future
       projection that surfaces extra connectors does not poison the
       assertion).
    2. ``"レビュー方針"`` — Japanese 4-char query. Phase 15
       (ADR-0028) switched ``sources_fts`` to the FTS5 ``trigram``
       tokenizer so 3+-char Japanese substrings hit via FTS5
       directly. Both seeded bodies contain "レビュー方針" so we
       expect at least one Slack hit; the assertion does **not**
       pin a specific row to keep it tolerant of FTS5 ranking
       drift across SQLite minor versions.

    The two queries together pin the ``search`` envelope shape
    (``{"query": str, "items": [...], ...}``) at the MCP boundary,
    not just at the service level — a regression that re-shaped the
    envelope (e.g. renamed ``items`` to ``hits``) would fail here
    before reaching ``find-document`` / ``research`` agents.
    """
    # ---- Seed in the parent process (shared DB via env override). ----
    en_source_id, ja_source_id = _seed_slack_sources()
    # ULID shape sanity check — protects against an unrelated regression
    # in :class:`SourceService.observe` returning truncated IDs.
    assert len(en_source_id) == 26
    assert len(ja_source_id) == 26

    async def _drive() -> None:
        async with _opshub_mcp_session(isolated_env) as session:
            # ---- ASCII query --------------------------------------
            ascii_result = await session.call_tool(
                "search",
                {"query": "Phase 12 assistant skills", "limit": 10},
            )
            assert ascii_result.isError is False, ascii_result
            ascii_payload = cast(
                "dict[str, Any]",
                json.loads(_extract_text_payload(ascii_result.content)),
            )
            assert ascii_payload["query"] == "Phase 12 assistant skills"
            ascii_items = cast("list[dict[str, Any]]", ascii_payload["items"])
            slack_hits = [item for item in ascii_items if item.get("connector_name") == "slack"]
            assert slack_hits, (
                "ADR-0022 §決定 (f-1) + ADR-0028 — ``search`` over stdio must"
                " return the seeded Slack source for an ASCII body anchor;"
                f" got: {ascii_items}"
            )
            # The English-bodied row is the strongest match; assert it
            # is one of the Slack hits so a future search reshuffle
            # cannot quietly stop returning the canonical row.
            slack_entity_ids = {item.get("entity_id") for item in slack_hits}
            assert en_source_id in slack_entity_ids, (
                "expected the seeded English-bodied Slack row in the"
                f" ASCII-query hits; got entity_ids: {slack_entity_ids}"
            )

            # ---- Japanese query (Phase 15 trigram tokenizer) ------
            ja_result = await session.call_tool(
                "search",
                {"query": "レビュー方針", "limit": 10},
            )
            assert ja_result.isError is False, ja_result
            ja_payload = cast(
                "dict[str, Any]",
                json.loads(_extract_text_payload(ja_result.content)),
            )
            assert ja_payload["query"] == "レビュー方針"
            ja_items = cast("list[dict[str, Any]]", ja_payload["items"])
            ja_slack_hits = [item for item in ja_items if item.get("connector_name") == "slack"]
            assert ja_slack_hits, (
                "ADR-0028 — Phase 15 trigram tokenizer regression: a 4-char"
                " Japanese substring must hit at least one of the seeded"
                f" Slack bodies via the MCP ``search`` tool; got: {ja_items}"
            )

    asyncio.run(asyncio.wait_for(_drive(), timeout=_E2E_TIMEOUT_SECONDS))
