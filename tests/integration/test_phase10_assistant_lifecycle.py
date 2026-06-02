"""Phase 10 end-to-end assistant lifecycle (Sub-issue G3, closeout).

Pins the "人間 → アシスタント (MCP) → コマンド" flow on the platform side.
Because opshub follows Phase 10 形A (ADR-0004 改訂) and **hosts no LLM
runtime of its own**, the e2e test substitutes for the agent host by
replaying a deterministic script of MCP tool calls against the same
:class:`~opshub.mcp._registry.ToolSpec` surface a real Claude Code /
Codex CLI / Gemini CLI host would drive. The LLM client used by reply-
draft generation is replaced with a deterministic stub so the test
never touches the network.

What this pins
--------------

1. **取り込み** — multiple connector sources (Slack / GitHub / Box
   Drive) land in ``sources`` with full bodies + provenance tags
   (ADR-0020 §(e)). The FTS5 index (migration 0019) and embeddings
   path treat them uniformly.
2. **MCP read tools** — the personal-brief / next-actions surfaces
   (``task.list`` / ``inbox.list`` / ``decision.list`` / ``recall.search``)
   return data through the dispatch wrapper, with `redact_secrets`
   guard active.
3. **MCP cross-source search** — ``recall.search`` (the only read
   recall surface in Phase 10 C2) hits across connectors thanks to
   the body-based index — Slack / GitHub / Box Drive all surface for
   one query.
4. **Reply-draft via CLI through the same engine** — ``opshub propose
   generate --reply-to <source>`` mints a ``ReplyDraftCandidatePayload``
   from the deterministic LLM stub, and ``opshub propose apply`` saves
   it locally without invoking any SaaS write-back (ADR-0010 §禁止事項
   7 / ADR-0016 §決定 (i) `reply_draft` apply). The reply-draft skill
   uses CLI today (Phase 10 plan §3-D); the read MCP surface is the
   recall path.
5. **Write-back path absence** — connector packages expose no ``send``
   / ``post`` / ``write`` / ``comment_create`` callable. A future PR
   adding one without a separate ADR + opt-in is a security
   regression (mirrors the boundary guard in :mod:`tests.unit.skills`
   for the skill surface).

The MCP layer is exercised via the in-process
:func:`opshub.mcp.server.dispatch_tool_call` wrapper. That wrapper is
identical to what runs inside ``serve_stdio`` — we just skip the stdio
transport so the test stays hermetic. The same wrapper is what the
`tests/unit/mcp/test_server_dispatch.py` suite covers; this test
piggybacks on its proven dispatch path and adds the cross-engine
integration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

# Skip if sqlite-vec extension is unavailable; the embedder path is
# part of the lifecycle, and Phase 4+ tests in this folder use the
# same gate.
pytest.importorskip("sqlite_vec")

from pydantic import BaseModel
from typer.testing import CliRunner

from opshub.cli._wiring import build_engine, build_source_service
from opshub.cli.app import app
from opshub.domain.events.proposal import ReplyDraftCandidatePayload
from opshub.domain.events.source import ProvenanceOrigin, ProvenanceTrust
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs — mirror the Phase 8 lifecycle stubs but kept local to avoid
# cross-test coupling.
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub.

    Returns a 1024-dim vector keyed on input text so any "same text →
    same vector" semantic invariants stay observable.
    """

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase10-stub-embedder"

    @property
    def model_version(self) -> str:
        return "v1"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> EmbeddingResult:
        slots = [0.0] * self._dim
        for i, ch in enumerate(text):
            slots[i % self._dim] += (ord(ch) % 31 + 1) / 31.0
        norm = max(sum(x * x for x in slots) ** 0.5, 1e-9)
        return EmbeddingResult(
            vector=tuple(x / norm for x in slots),
            model_id=self.model_id,
            model_version=self.model_version,
            dim=self._dim,
        )


class _StubLLMClient:
    """Deterministic LLM client.

    Implements both ``complete`` (brief surface, unused in this test
    but kept for symmetry with the Phase 5/6/8 stubs) and
    ``complete_structured`` (reply-draft). The structured response
    returns a single :class:`ReplyDraftCandidatePayload` keyed on the
    requested source.
    """

    def __init__(
        self,
        *,
        reply_to_source_id: str = "",
        reply_to_source_type: str = "slack_message",
        reply_body: str = "Acknowledged — I'll take a look and circle back.",
        model_id: str = "phase10-stub-llm",
        model_version: str = "phase10-test",
        tokens_in: int = 100,
        tokens_out: int = 30,
    ) -> None:
        self._reply_to_source_id = reply_to_source_id
        self._reply_to_source_type = reply_to_source_type
        self._reply_body = reply_body
        self._model_id = model_id
        self._model_version = model_version
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.complete_calls: list[tuple[list[LLMMessage], int]] = []
        self.structured_calls: list[tuple[list[LLMMessage], type[BaseModel], int]] = []

    def set_reply_target(self, source_id: str, source_type: str) -> None:
        """Update which source the next ``complete_structured`` will target."""
        self._reply_to_source_id = source_id
        self._reply_to_source_type = source_type

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        del temperature, stop
        self.complete_calls.append((list(messages), max_tokens))
        return LLMResponse(
            text="# stub briefing\n\n- nothing to report",
            model_id=self._model_id,
            model_version=self._model_version,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        del temperature
        self.structured_calls.append((list(messages), schema, max_tokens))
        parsed = schema(
            candidates=[
                ReplyDraftCandidatePayload(
                    reply_to_source_id=self._reply_to_source_id,
                    reply_to_source_type=self._reply_to_source_type,
                    body=self._reply_body,
                )
            ]
        )
        return StructuredResponse(
            parsed=parsed,
            model_id=self._model_id,
            model_version=self._model_version,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the embedder factory so recall-side calls stay hermetic."""
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch, stub: _StubLLMClient) -> None:
    """Replace the LLM factory so reply-draft generation stays hermetic."""
    from opshub.core.config import OpsHubSettings
    from opshub.llm import factory as factory_module
    from opshub.llm.client import LLMClient

    def _builder(settings: OpsHubSettings) -> LLMClient:
        del settings
        return stub  # type: ignore[return-value,unused-ignore]

    monkeypatch.setattr(factory_module, "build_llm_client", _builder)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Invoke the CLI app and capture exit code + stdout + stderr."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# MCP call helpers — replay the agent host's tool invocations.
# ---------------------------------------------------------------------------


def _call_mcp_tool(
    specs_by_name: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any] | None = None,
) -> str:
    """Drive :func:`dispatch_tool_call` synchronously and unwrap text content.

    The Phase 10 C2 MCP surface always returns a single
    ``TextContent`` block (`opshub.mcp.server._to_text_content`); we
    unwrap and return the inner string so callers can `json.loads`.
    """
    import asyncio

    from opshub.mcp.server import dispatch_tool_call

    content = asyncio.run(dispatch_tool_call(specs_by_name, name, arguments or {}))
    assert len(content) == 1, f"expected 1 TextContent block, got {len(content)}"
    return str(content[0].text)


def _call_mcp_tool_json(
    specs_by_name: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Call an MCP tool and parse its JSON response into a typed dict.

    The Phase 10 C2 read tools always return a JSON object envelope
    (``{items: [...]}`` for list tools, ``{hits: [...]}`` for
    recall.search). We narrow to ``dict[str, Any]`` so pyright tracks
    the shape downstream.
    """
    raw = _call_mcp_tool(specs_by_name, name, arguments)
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# Source seeding — mimic what real connectors do via SourceService.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SourceFixture:
    """One connector source seed (Phase 10 plan §7.3 step 1).

    Frozen dataclass + Literal-typed provenance fields so pyright
    narrows the call into :meth:`SourceService.observe` without
    casts.
    """

    connector_name: str
    source_type: str
    external_id: str
    title: str
    body: str | None
    provenance_origin: ProvenanceOrigin | None


_SOURCE_FIXTURES: tuple[_SourceFixture, ...] = (
    _SourceFixture(
        connector_name="slack",
        source_type="slack_message",
        external_id="C0PHASE10:1717200000.000100",
        title="alice in #phase10 — can you ack?",
        body=(
            "Hey, can you take a look at the phase 10 ship plan? "
            "I want to confirm the assistant skill catalog before lunch."
        ),
        provenance_origin="external",
    ),
    _SourceFixture(
        connector_name="github",
        source_type="github_issue",
        external_id="ozzy-labs/opshub#203",
        title="epic: Phase 10 Assistant Agent Platform",
        body=(
            "Phase 10 brings full local body retention, encryption at rest, "
            "MCP server surface, and the assistant 5 skills (personal-brief, "
            "next-actions, reply-draft, pr-review, find-document)."
        ),
        provenance_origin="external",
    ),
    _SourceFixture(
        connector_name="box_drive",
        source_type="box_drive_file",
        external_id="Phase10/assistant-design.md",
        title="assistant-design.md",
        # box_drive is FS-scan only (ADR-0019); body=None and the
        # FTS5 row is empty. We still seed via SourceService so the
        # round-trip exercises the body=NULL branch end-to-end.
        body=None,
        # box_drive bodies are never tagged because there is no body.
        provenance_origin=None,
    ),
)


def _seed_sources(actor: str = "test:phase10_lifecycle") -> list[str]:
    """Seed three multi-connector source rows via :class:`SourceService`.

    Returns the source ULIDs in the same order as the fixture tuples so
    later assertions can locate them.
    """
    service = build_source_service(actor=actor)
    ids: list[str] = []
    for fixture in _SOURCE_FIXTURES:
        trust: ProvenanceTrust | None = None
        if fixture.provenance_origin == "external":
            trust = "untrusted"  # ADR-0020 §(e) — external bodies untrusted
        elif fixture.provenance_origin == "internal":
            trust = "trusted"
        observed, _ = service.observe(
            connector_name=fixture.connector_name,
            external_id=fixture.external_id,
            source_type=fixture.source_type,
            title=fixture.title,
            body=fixture.body,
            provenance_origin=fixture.provenance_origin,
            provenance_trust=trust,
        )
        ids.append(observed.aggregate_id)
    return ids


# ---------------------------------------------------------------------------
# Test body.
# ---------------------------------------------------------------------------


def test_phase10_assistant_lifecycle(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end "人間 → アシスタント (MCP) → コマンド" walk.

    Sequence (Phase 10 plan §7.3):

    1. Seed three connector sources (Slack / GitHub / Box Drive) via
       :class:`SourceService` so the body store has cross-connector
       material to brief / search / reply about.
    2. Drive ``opshub embeddings rebuild`` so the body-based vector
       store is populated (the recall MCP tool needs embeddings).
    3. Replay the agent host's personal-brief tool call sequence
       (``task.list`` / ``inbox.list`` / ``decision.list`` /
       ``recall.search``) and assert each returns a structured payload.
    4. Replay the agent host's find-document query
       (``recall.search`` with a body-derived term) and assert it
       crosses connectors (returns hits sourced from at least two
       different ``connector_name`` columns).
    5. Drive the reply-draft path through the CLI (the skill calls
       ``opshub propose generate --reply-to <id>`` then ``opshub
       propose apply <id> 0`` in Phase 10) and assert the draft lands
       in the proposals projection without invoking any external
       write-back.
    6. Assert the connector packages **do not** export any
       ``send`` / ``post`` / ``write`` / ``comment_create`` callable
       — proving the structural absence of a write-back path.
    """
    # ---- 0. monkeypatch the backends -----------------------------------
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    # ---- 1. seed sources ------------------------------------------------
    source_ids = _seed_sources()
    slack_source_id = source_ids[0]
    box_drive_source_id = source_ids[2]
    assert all(len(sid) == 26 for sid in source_ids)

    # ---- 2. embeddings rebuild (body-based, ADR-0012 改訂 §4) -----------
    code, rebuild_out, rebuild_err = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out + (rebuild_err or "")

    # ---- 3. wire the MCP surface against the same engine ---------------
    engine = build_engine()
    try:
        from opshub.mcp.server import build_tool_specs_for_engine

        specs = build_tool_specs_for_engine(engine)
        specs_by_name = {spec.name: spec for spec in specs}

        # Sanity: the Phase 10 C2 baseline + Step 1 widening + Phase 12
        # H1 widening advertises 17 tools (12 read, 5 write). A regression
        # that drops one would surface here. Phase 12 H1 (ADR-0022 改訂)
        # adds ``search`` (FTS5 read) and ``propose.apply`` (HITL write,
        # idempotent).
        assert set(specs_by_name) == {
            # Phase 10 C2 baseline.
            "recall.search",
            "task.list",
            "inbox.list",
            "decision.list",
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
            # Phase 12 H1 widening (ADR-0022 改訂).
            "search",
            "propose.apply",
        }

        # ---- 4. personal-brief script (read-only tool calls) --------------
        # These are the four read tools the assistant personal-brief and
        # next-actions skills auto-approve (ADR-0022 §(c)). Each call
        # must succeed and return the documented envelope ({items:
        # [...]} for list tools, {hits: [...]} for recall.search). We
        # assert on the envelope and the rough shape, not the specific
        # content, because the seed data may evolve.
        task_list_payload = _call_mcp_tool_json(specs_by_name, "task.list", {"limit": 50})
        task_items = cast("list[dict[str, Any]]", task_list_payload["items"])
        assert isinstance(task_items, list)

        inbox_list_payload = _call_mcp_tool_json(specs_by_name, "inbox.list", {"limit": 50})
        inbox_items_payload = cast("list[dict[str, Any]]", inbox_list_payload["items"])
        # SourceService.observe also enqueued one inbox row per source.
        assert len(inbox_items_payload) >= len(_SOURCE_FIXTURES)

        decision_list_payload = _call_mcp_tool_json(specs_by_name, "decision.list", {"limit": 50})
        decision_items = cast("list[dict[str, Any]]", decision_list_payload["items"])
        assert isinstance(decision_items, list)

        # A recall.search call seeded by a deterministic phrase from
        # one of the bodies. Phase 10 plan §7.3 step 2.
        recall_today_payload = _call_mcp_tool_json(
            specs_by_name,
            "recall.search",
            {"query": "phase 10 ship plan", "limit": 10},
        )
        recall_today_hits = cast("list[dict[str, Any]]", recall_today_payload["hits"])
        assert isinstance(recall_today_hits, list)
        assert recall_today_payload["truncated_snippets"] is True, (
            "ADR-0022 §(d) — recall.search must advertise truncated_snippets=True"
        )

        # ---- 5. find-document cross-connector search ---------------------
        # A query that should hit multiple connectors thanks to body
        # retention. The Slack body and the GitHub body both mention
        # "Phase 10" / "assistant skills"; we assert that recall.search
        # returns ≥1 source-class hit and that, joining back to the
        # ``sources`` projection, the hits span more than one connector.
        cross_payload = _call_mcp_tool_json(
            specs_by_name,
            "recall.search",
            {"query": "assistant skills phase 10", "limit": 10},
        )
        cross_hits = cast("list[dict[str, Any]]", cross_payload["hits"])
        source_hit_ids: list[str] = [
            cast("str", hit["entity_id"])
            for hit in cross_hits
            if hit.get("entity_type") == "source"
        ]
        assert source_hit_ids, (
            "recall.search for a body-anchored query must surface at least one"
            f" source-class hit (Phase 10 ADR-0020 body retention); got: {cross_payload}"
        )
        # Join back to sources.connector_name to assert cross-connector
        # coverage. The Phase 10 C2 recall.search tool does not echo
        # connector identity in its response (snippet-only, ADR-0022
        # §(d)); the join keeps the cross-connector assertion sharp
        # without leaking new fields into the agent context.
        from sqlalchemy import text

        with engine.connect() as conn:
            connector_names = {
                row.connector_name
                for row in conn.execute(
                    text(
                        "SELECT DISTINCT connector_name FROM sources"
                        " WHERE id IN ("
                        + ",".join(f":id{i}" for i in range(len(source_hit_ids)))
                        + ")"
                    ),
                    {f"id{i}": sid for i, sid in enumerate(source_hit_ids)},
                )
            }
        assert len(connector_names) >= 2, (
            "cross-connector recall.search must surface hits from ≥2 connectors"
            f" (Phase 10 plan §7.3 step 4); got: {connector_names}"
        )

        # ---- 6. reply-draft through the CLI ----------------------------
        # Phase 10 plan §3-D wires the reply-draft skill through
        # ``opshub propose generate --reply-to <id>`` + ``opshub
        # propose apply``. Drive that path so we observe the draft
        # land in the proposals projection (durable state change)
        # WITHOUT invoking any SaaS write-back.
        stub_llm.set_reply_target(slack_source_id, "slack_message")
        code, gen_out, gen_err = _invoke(
            [
                "propose",
                "generate",
                "ignored-topic-in-reply-draft-mode",
                "--reply-to",
                slack_source_id,
                "--format",
                "json",
            ]
        )
        assert code == 0, gen_out + (gen_err or "")
        gen_payload = cast("dict[str, Any]", json.loads(gen_out))
        proposal_id = cast("str", gen_payload["proposal_id"])
        assert len(proposal_id) == 26
        assert gen_payload["scope"] == f"reply_draft:{slack_source_id}"
        # The stub's reply body must surface verbatim — proves the
        # reply-draft body flows through the LLM client → projection
        # without being replaced by a sentinel.
        candidates_payload = cast("list[dict[str, Any]]", gen_payload.get("candidates", []))
        assert any(
            "Acknowledged" in (cast("str | None", c.get("body")) or "") for c in candidates_payload
        ), gen_payload

        # Apply the draft — saves locally, never sends.
        code, apply_out, apply_err = _invoke(["propose", "apply", proposal_id, "0"])
        assert code == 0, apply_out + (apply_err or "")
        # The CLI's reply-draft apply path prints "saved reply draft:"
        # (see ProposalService.apply for reply_draft candidates) —
        # assert on the verb rather than the exact wording to stay
        # robust against future stdout polish.
        assert "reply" in apply_out.lower() or "draft" in apply_out.lower(), apply_out

        # ---- 7. structural absence of a write-back path ---------------
        # Every connector package must NOT expose a callable named
        # send / post / write / comment_create. ADR-0010 §禁止事項 7
        # Phase 10 改訂 makes write-back out of scope; a future PR
        # that adds one without a dedicated ADR + opt-in is a
        # security regression. This guard is structural so the
        # regression is caught at test time, not runtime.
        forbidden_callables = {"send", "post", "write", "comment_create"}
        from opshub import connectors as connectors_pkg

        connector_modules = [
            f"opshub.connectors.{name}" for name in ("github", "slack", "ms365", "box", "box_drive")
        ]
        import importlib

        _ = connectors_pkg  # touched only for the package import
        for module_path in connector_modules:
            module = importlib.import_module(module_path)
            offenders = {
                name for name in forbidden_callables if callable(getattr(module, name, None))
            }
            assert not offenders, (
                f"connector {module_path} exposes write-back callable(s)"
                f" {offenders!r} — ADR-0010 §禁止事項 7 Phase 10 改訂"
                " forbids external write-back; remove the callable or"
                " surface a separate ADR + opt-in."
            )

        # ---- 8. ensure box_drive body=None still indexes (no leak) ----
        # box_drive's body is None by ADR-0019 §不変条件 (b). The FTS5
        # migration 0019 inserts an empty document so the rowid stays
        # 1:1 with sources. We confirm the body=NULL round-trip via the
        # already-imported ``text`` helper rather than rebuilding the
        # engine.
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, body FROM sources WHERE id = :id"),
                {"id": box_drive_source_id},
            ).first()
        assert row is not None
        assert row.body is None, "ADR-0019 §不変条件 (b) forbids FS-scan body retention"
    finally:
        engine.dispose()
