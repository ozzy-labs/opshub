"""Phase 12 end-to-end assistant lifecycle (Sub-issue H6, closeout).

Pins the Phase 12 14-skill assistant surface end-to-end. opshub follows
Phase 10 形A (ADR-0004 改訂) so it **hosts no LLM runtime of its own** —
the e2e test substitutes for the agent host by replaying a deterministic
script of MCP tool calls against the same :class:`ToolSpec` surface a
real Claude Code / Codex CLI / Gemini CLI host would drive. The LLM
client used by reply-draft / proposal generation is replaced with a
deterministic stub so the test never touches the network.

This test is the Phase 12 H6 successor to
:mod:`tests.integration.test_phase10_assistant_lifecycle` and
:mod:`tests.integration.test_phase11_office_lifecycle`. It widens the
e2e surface to all 14 assistant skills and the 4 new MCP tools added in
Phase 12 H1 (ADR-0022 改訂 §決定 (f)):

1. **`search` (FTS5, Phase 12 H1)** — body-level full-text search,
   ``raw_query`` flag intentionally absent from the MCP schema.
2. **`propose.apply` (HITL idempotent, Phase 12 H1)** — second call for
   the same ``(proposal_id, candidate_index)`` returns
   ``{ok: true, already_applied: true}`` instead of raising.
3. **Physical-column time filters (Phase 12 H1)** on the 4 list tools
   (``task.list.updated_after/before`` / ``inbox.list.created_after/before``
   / ``decision.list.recorded_after/before`` /
   ``source.list.observed_after/before``).
4. **`propose.generate` ``mode`` argument (Phase 12 H4)** — accepts
   ``inbox_triage`` / ``source_extract`` / ``meeting_followup``
   (ADR-0016 §決定 (l)(b)).

What this pins
--------------

The 14 assistant skills route into the MCP surface as documented in
``docs/assistant-agent.md`` §6 (MCP tool 依存マップ). We don't run the
SKILL.md files themselves — that is the agent host's responsibility.
Instead we replay the **tool call patterns** that each skill's
``呼び出し順`` section prescribes so a regression that drops or
re-shapes one of the underlying MCP tools fails here.

Sequence (Phase 12 plan §7.3):

1. Seed Phase 11-shape multi-connector sources (Slack / GitHub / Box
   Drive / Teams / Outlook / Word / Excel / PowerPoint) so the body
   store has cross-connector + Office material the 14 skills can hit.
2. Drive ``opshub embeddings rebuild`` so the body-based vector store
   is populated.
3. Replay the 4 new MCP tools (``search`` / ``propose.apply`` /
   time-filtered ``task.list`` / ``decision.list``) end-to-end.
4. Replay representative tool calls for each of the 14 skills (the
   read tools the read-tier skills auto-approve, the
   ``propose.generate`` + ``propose.apply`` round-trip the HITL-write
   skills require). 14 skill assertions, one per skill.
5. Assert the HITL boundary: ``propose.apply`` annotation =
   ``read_only=false, destructive=false, idempotent=true``; other write
   tools remain ``destructive=true``.
6. Assert structural absence of write-back path + handoff/announcement
   persist path + ``ProposalGenerated`` handoff/announcement scope path.

The MCP layer is exercised via the in-process
:func:`opshub.mcp.server.dispatch_tool_call` wrapper — same as
:mod:`tests.integration.test_phase10_assistant_lifecycle` and
:mod:`tests.integration.test_phase11_office_lifecycle`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from opshub.domain.events.proposal import (
    DecisionCandidatePayload,
    ReplyDraftCandidatePayload,
    TaskCandidatePayload,
)
from opshub.domain.events.source import ProvenanceOrigin, ProvenanceTrust
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs — local copies for the same reason the Phase 10 / 11 lifecycles
# keep their own (no cross-test coupling).
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub keyed on input text."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase12-stub-embedder"

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

    Implements ``complete`` (brief) and ``complete_structured``
    (proposal). The structured response branches on the requested
    Pydantic schema so reply-draft and topic-mode proposals both
    work:

    * If the schema's candidate union allows ``ReplyDraftCandidatePayload``
      and a reply target was set, return one reply-draft candidate.
    * Otherwise return one TaskCandidatePayload + one
      DecisionCandidatePayload so the HITL-write skills'
      ``propose.generate`` round trip carries a multi-kind candidate
      list (mirrors what an LLM would do for ``mode=inbox_triage``
      etc.).
    """

    def __init__(
        self,
        *,
        reply_to_source_id: str = "",
        reply_to_source_type: str = "slack_message",
        reply_body: str = "Acknowledged — I'll take a look and circle back.",
        task_title: str = "Phase 12 H6 stub task",
        decision_text: str = "Phase 12 H6 stub decision",
        model_id: str = "phase12-stub-llm",
        model_version: str = "phase12-test",
        tokens_in: int = 100,
        tokens_out: int = 30,
    ) -> None:
        self._reply_to_source_id = reply_to_source_id
        self._reply_to_source_type = reply_to_source_type
        self._reply_body = reply_body
        self._task_title = task_title
        self._decision_text = decision_text
        self._model_id = model_id
        self._model_version = model_version
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.complete_calls: list[tuple[list[LLMMessage], int]] = []
        self.structured_calls: list[tuple[list[LLMMessage], type[BaseModel], int]] = []

    def set_reply_target(self, source_id: str, source_type: str) -> None:
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

        # Build payload list — the schema is always
        # ``ProposalCandidatesSchema`` (one ``candidates`` field whose
        # union is :data:`Candidate`); the stub picks per-kind payloads
        # that satisfy the union and let the call site distinguish
        # between reply-draft mode and topic / mode dispatch.
        if self._reply_to_source_id:
            parsed = schema(
                candidates=[
                    ReplyDraftCandidatePayload(
                        reply_to_source_id=self._reply_to_source_id,
                        reply_to_source_type=self._reply_to_source_type,
                        body=self._reply_body,
                    )
                ]
            )
        else:
            parsed = schema(
                candidates=[
                    TaskCandidatePayload(title=self._task_title),
                    DecisionCandidatePayload(text=self._decision_text),
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
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch, stub: _StubLLMClient) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.llm import factory as factory_module
    from opshub.llm.client import LLMClient

    def _builder(settings: OpsHubSettings) -> LLMClient:
        del settings
        return stub  # type: ignore[return-value,unused-ignore]

    monkeypatch.setattr(factory_module, "build_llm_client", _builder)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# MCP call helpers.
# ---------------------------------------------------------------------------


def _call_mcp_tool(
    specs_by_name: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any] | None = None,
) -> str:
    from opshub.mcp.server import dispatch_tool_call

    content = asyncio.run(dispatch_tool_call(specs_by_name, name, arguments or {}))
    assert len(content) == 1, f"expected 1 TextContent block, got {len(content)}"
    return str(content[0].text)


def _call_mcp_tool_json(
    specs_by_name: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw = _call_mcp_tool(specs_by_name, name, arguments)
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# Source seeding — combine Phase 10 and Phase 11 shapes so all 14 skills
# have body-level material to recall, FTS5-search, graph-walk against.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Phase12Fixture:
    connector_name: str
    source_type: str
    external_id: str
    title: str
    # epic #470 / issue #481: ``body`` is required + non-empty.
    body: str
    provenance_origin: ProvenanceOrigin | None


# Built around two topical anchors so the 14 skills' recall / search /
# brief paths hit deterministically:
# - "Q3 architecture review" — meeting-prep / meeting-followup /
#   research / personal-brief / external-brief / find-document.
# - "phase 12 assistant skills" — pr-review / decision-rationale /
#   handoff-draft / announcement-draft.
_PHASE12_FIXTURES: tuple[_Phase12Fixture, ...] = (
    _Phase12Fixture(
        connector_name="slack",
        source_type="slack_message",
        external_id="C0PHASE12:1717200000.000200",
        title="bob in #phase12 — can you ack the H6 closeout?",
        body=(
            "Hey, the Phase 12 assistant skills lifecycle (14 skills) "
            "is almost ready. Can you confirm the H6 closeout DoD before EOD?"
        ),
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="github",
        source_type="github_issue",
        external_id="ozzy-labs/opshub#253",
        title="epic: Phase 12 Assistant Skills 拡張",
        body=(
            "Phase 12 widens the assistant skill catalog from 5 to 14, "
            "exposes search (FTS5) and propose.apply over MCP, and adds "
            "physical-column time filters to the existing 4 list tools."
        ),
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="box_drive",
        source_type="box_drive_file",
        external_id="Phase12/skill-catalog.md",
        title="skill-catalog.md",
        # epic #470 / issue #481 (ADR-0010 §不変条件 metadata-only
        # rule): FS-scan default emits body=summary so the projection
        # satisfies the body NOT NULL invariant without violating
        # ADR-0019 §不変条件 (b).
        body="path: Phase12/skill-catalog.md",
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="teams",
        source_type="teams_message",
        external_id="19:phase12@thread.tacv2:1717300000-msg-001",
        title="carol in #arch — Q3 architecture review prep",
        body=(
            "Let's pull together the Q3 architecture review materials. "
            "Alice wrote up the agenda in Outlook last week."
        ),
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="ms365",
        source_type="ms365_outlook",
        external_id="AAMkADhfd-msg-q3-review-002",
        title="Q3 architecture review — agenda",
        body=(
            "Agenda for the Q3 architecture review meeting:\n"
            "1. Phase 12 assistant skills retrospective (alice)\n"
            "2. Phase 13 outlook (carol)\n"
            "3. Open issues (open)"
        ),
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="ms365",
        source_type="ms365_calendar",
        external_id="AAMkADhfd-cal-q3-review-001",
        title="Q3 architecture review",
        body=(
            "Calendar event: Q3 architecture review.\n"
            "Attendees: alice, bob, carol.\n"
            "Linked agenda: Q3 architecture review — agenda."
        ),
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="box_drive",
        source_type="word_document",
        external_id="Phase12/q3-review-notes.docx",
        title="q3-review-notes.docx",
        body=(
            "# Q3 architecture review notes\n\n"
            "Phase 12 assistant skills section: meeting-prep + "
            "meeting-followup pair, research + decision-rationale standalone."
        ),
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="onedrive_drive",
        source_type="excel_spreadsheet",
        external_id="Specs/q3-review-capacity.xlsx",
        title="q3-review-capacity.xlsx",
        body=(
            "## Sheet: capacity\n\n"
            "| component | est. items | notes |\n"
            "|---|---|---|\n"
            "| assistant skills | 14 | Phase 12 (was 5) |\n"
            "| MCP tools | 17 | read 12 + write 5 |\n"
        ),
        provenance_origin="external",
    ),
    _Phase12Fixture(
        connector_name="onedrive_drive",
        source_type="powerpoint_slide_deck",
        external_id="Decks/q3-review-deck.pptx",
        title="q3-review-deck.pptx",
        body=(
            "# Slide 1: Q3 architecture review\n\n"
            "## Notes:\nPhase 12 assistant skills are now MCP-direct, "
            "with search (FTS5) and propose.apply (HITL idempotent)."
        ),
        provenance_origin="external",
    ),
)


def _seed_sources(actor: str = "test:phase12_lifecycle") -> list[str]:
    service = build_source_service(actor=actor)
    ids: list[str] = []
    for fixture in _PHASE12_FIXTURES:
        trust: ProvenanceTrust | None = None
        if fixture.provenance_origin == "external":
            trust = "untrusted"
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


def test_phase12_assistant_lifecycle(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 12 end-to-end 14-skill lifecycle.

    Walks the platform side of the assistant surface:

    1. Seed 9 sources spanning all 7 connectors and the Phase 11
       source_types so the 14 skills have material to recall, search,
       graph-walk, and brief over.
    2. Drive ``opshub embeddings rebuild`` for the recall path.
    3. Assert the Phase 12 H1 MCP surface (17 tools).
    4. Walk the 4 new Phase 12 H1 MCP tools end-to-end:
       - ``search`` (FTS5) hits cross-source on a body-anchored query
         and rejects the ``raw_query`` flag at schema level.
       - ``propose.apply`` is idempotent (second call returns
         ``already_applied=true`` instead of raising).
       - Physical-column time filters (`updated_after` on ``task.list``,
         ``recorded_after`` on ``decision.list``) honour the half-open
         interval semantics.
    5. Replay representative tool calls for each of the 14 skills.
    6. Assert HITL boundary (``propose.apply`` policy =
       ``destructive=false, idempotent=true``; other writes remain
       ``destructive=true``).
    7. Assert structural absence: no write-back path on any connector;
       no persistence path for handoff/announcement-draft (no
       ``ProposalGenerated`` with ``scope`` matching those names).
    """
    # ---- 0. monkeypatch backends -------------------------------------
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    # ---- 1. seed sources --------------------------------------------
    source_ids = _seed_sources()
    slack_source_id = source_ids[0]
    box_drive_source_id = source_ids[2]
    calendar_source_id = source_ids[5]
    word_source_id = source_ids[6]
    assert all(len(sid) == 26 for sid in source_ids)

    # ---- 2. embeddings rebuild --------------------------------------
    code, rebuild_out, rebuild_err = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out + (rebuild_err or "")

    # ---- 3. MCP surface --------------------------------------------
    engine = build_engine()
    try:
        from opshub.mcp.server import build_tool_specs_for_engine

        specs = build_tool_specs_for_engine(engine)
        specs_by_name = {spec.name: spec for spec in specs}

        # Phase 12 H1 + Phase 18-C surface: 18 tools (13 read + 5 write).
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
            # Phase 12 H1 widening (ADR-0022 改訂 §決定 (f)).
            "search",
            "propose.apply",
            # Phase 18-C widening (ADR-0033 §決定 (c)).
            "slack.demand.list",
        }

        # ---- 4. Phase 12 H1 widening end-to-end -----------------------

        # 4a. ``search`` (FTS5) — phrase-quoted by default; ``raw_query``
        #     is intentionally absent from the MCP schema.
        search_spec = specs_by_name["search"]
        search_input_schema = search_spec.input_schema
        # The input schema is a JSON-schema-compatible dict; the FTS5
        # raw query flag must NOT appear there. ADR-0022 §決定 (f-1).
        assert "raw_query" not in search_input_schema.get("properties", {}), (
            "ADR-0022 §決定 (f-1) — ``search`` MCP tool input schema must"
            " not expose ``raw_query``; that flag is CLI-only."
        )

        search_payload = _call_mcp_tool_json(
            specs_by_name,
            "search",
            {"query": "Phase 12 assistant skills", "limit": 10},
        )
        # FTS5 returns hits across multiple connectors thanks to body
        # retention (ADR-0020 + ADR-0012 改訂 §4). The MCP envelope
        # uses ``items`` (consistent with the other list tools); see
        # :func:`opshub.mcp._tools.build_search_handler`.
        search_hits = cast("list[dict[str, Any]]", search_payload["items"])
        assert isinstance(search_hits, list), search_payload
        # The Slack message body and the GitHub issue body both mention
        # "Phase 12 assistant skills"; we should hit at least one of them.
        assert len(search_hits) >= 1, search_payload

        # 4b. ``propose.apply`` idempotency — generate then apply twice
        #     and assert the second call returns ``already_applied=true``
        #     instead of raising. Uses topic mode (multi-kind candidates)
        #     so the apply target is a TaskCandidatePayload.
        gen_payload = _call_mcp_tool_json(
            specs_by_name,
            "propose.generate",
            {"topic": "phase 12 closeout review", "max_candidates": 2},
        )
        proposal_id = cast("str", gen_payload["proposal_id"])
        candidates = cast("list[dict[str, Any]]", gen_payload["candidates"])
        # Pick the task candidate (kind="task") so apply creates a Task.
        task_index = next(i for i, c in enumerate(candidates) if c.get("kind") == "task")

        first_apply = _call_mcp_tool_json(
            specs_by_name,
            "propose.apply",
            {"proposal_id": proposal_id, "candidate_index": task_index},
        )
        assert first_apply["ok"] is True
        assert first_apply["already_applied"] is False
        assert first_apply["applied_entity_type"] == "task"
        applied_task_id = cast("str", first_apply["applied_entity_id"])
        assert len(applied_task_id) == 26

        second_apply = _call_mcp_tool_json(
            specs_by_name,
            "propose.apply",
            {"proposal_id": proposal_id, "candidate_index": task_index},
        )
        assert second_apply["ok"] is True, second_apply
        assert second_apply["already_applied"] is True, (
            "ADR-0022 §決定 (f-2) — ``propose.apply`` second call must"
            f" return already_applied=true; got: {second_apply}"
        )
        assert second_apply["applied_entity_type"] == "task"
        assert second_apply["applied_entity_id"] == applied_task_id, (
            "second-call ``applied_entity_id`` must match first call"
            " (recovered via _lookup_applied_entity)"
        )

        # 4c. Physical-column time filters (Phase 12 H1, ADR-0022 §決定
        #     (f-3)). Boundary check: half-open ``>= after`` /
        #     ``< before``.
        now = datetime.now(UTC)
        future = (now + timedelta(hours=1)).isoformat()
        past = (now - timedelta(hours=1)).isoformat()

        # task.list — ``updated_after = future`` returns empty.
        task_future_payload = _call_mcp_tool_json(
            specs_by_name,
            "task.list",
            {"limit": 50, "updated_after": future},
        )
        task_future_items = cast("list[dict[str, Any]]", task_future_payload["items"])
        assert task_future_items == [], (
            "task.list ``updated_after`` half-open semantics regression:"
            f" rows from before {future} must not surface; got: {task_future_items}"
        )

        # task.list — ``updated_after = past`` includes the task just
        # created via propose.apply.
        task_past_payload = _call_mcp_tool_json(
            specs_by_name,
            "task.list",
            {"limit": 50, "updated_after": past},
        )
        task_past_items = cast("list[dict[str, Any]]", task_past_payload["items"])
        assert any(item.get("id") == applied_task_id for item in task_past_items), (
            "task.list ``updated_after`` must include the just-applied"
            f" task; got: {task_past_items}"
        )

        # decision.list — ``recorded_after = future`` returns empty.
        decision_future_payload = _call_mcp_tool_json(
            specs_by_name,
            "decision.list",
            {"limit": 50, "recorded_after": future},
        )
        assert decision_future_payload["items"] == []

        # source.list — ``observed_after = future`` returns empty.
        source_future_payload = _call_mcp_tool_json(
            specs_by_name,
            "source.list",
            {"limit": 50, "observed_after": future},
        )
        assert source_future_payload["items"] == []

        # inbox.list — ``created_after = future`` returns empty.
        inbox_future_payload = _call_mcp_tool_json(
            specs_by_name,
            "inbox.list",
            {"limit": 50, "created_after": future},
        )
        assert inbox_future_payload["items"] == []

        # ---- 5. 14-skill replay ---------------------------------------
        # Each skill is exercised through one representative tool call
        # from its SKILL.md "呼び出し順" section. A regression that
        # drops one of these tools (or re-shapes its return envelope)
        # fails here.

        # personal-brief (read 自律 OK) — combines task.list / inbox.list
        # / decision.list / recall.search. We sanity-check task.list
        # since it carries the new Phase 12 H1 time-filter args.
        pb_payload = _call_mcp_tool_json(specs_by_name, "task.list", {"limit": 50})
        assert isinstance(pb_payload["items"], list)

        # next-actions (read 自律 / write 人確認) — task.list +
        # recall.search.
        na_recall = _call_mcp_tool_json(
            specs_by_name,
            "recall.search",
            {"query": "phase 12 closeout", "limit": 5},
        )
        assert "hits" in na_recall

        # reply-draft (HITL write) — propose.generate (reply_to_source_id)
        # round-trip. The stub returns one ReplyDraftCandidatePayload.
        stub_llm.set_reply_target(slack_source_id, "slack_message")
        rd_gen = _call_mcp_tool_json(
            specs_by_name,
            "propose.generate",
            {"reply_to_source_id": slack_source_id, "max_candidates": 1},
        )
        rd_candidates = cast("list[dict[str, Any]]", rd_gen["candidates"])
        assert rd_candidates and rd_candidates[0].get("kind") == "reply_draft"
        rd_apply = _call_mcp_tool_json(
            specs_by_name,
            "propose.apply",
            {"proposal_id": rd_gen["proposal_id"], "candidate_index": 0},
        )
        assert rd_apply["ok"] is True

        # Reset the stub so subsequent propose.generate calls without
        # ``reply_to_source_id`` go through the topic-mode branch.
        stub_llm.set_reply_target("", "slack_message")

        # pr-review (read 自律 OK) — recall.search + decision.list +
        # graph.related / graph.trace.
        pr_recall = _call_mcp_tool_json(
            specs_by_name,
            "recall.search",
            {"query": "ozzy-labs/opshub#253", "limit": 5},
        )
        assert "hits" in pr_recall

        # find-document (read 自律 OK) — search (FTS5) primary + source.get
        # / source.list secondary.
        fd_payload = _call_mcp_tool_json(
            specs_by_name,
            "search",
            {"query": "Q3 architecture review", "limit": 10},
        )
        assert "items" in fd_payload
        fd_get = _call_mcp_tool_json(
            specs_by_name,
            "source.get",
            {"source_id": word_source_id},
        )
        assert fd_get.get("found") is True
        assert fd_get.get("source_type") == "word_document"

        # meeting-prep (read 自律 OK) — source.list (source_type=
        # ms365_calendar + observed_after/before) + recall.search +
        # graph.related.
        mp_payload = _call_mcp_tool_json(
            specs_by_name,
            "source.list",
            {"source_type": "ms365_calendar", "limit": 5},
        )
        mp_items = cast("list[dict[str, Any]]", mp_payload["items"])
        assert any(item.get("id") == calendar_source_id for item in mp_items), (
            f"meeting-prep source.list (ms365_calendar) regression: {mp_items}"
        )

        # research (read 自律 OK) — recall.search + search + graph.expand
        # + brief.
        rs_search = _call_mcp_tool_json(
            specs_by_name,
            "search",
            {"query": "Q3 architecture review", "limit": 10},
        )
        assert "items" in rs_search
        rs_recall = _call_mcp_tool_json(
            specs_by_name,
            "recall.search",
            {"query": "Q3 architecture review", "limit": 10},
        )
        assert "hits" in rs_recall

        # external-brief (read 自律 OK) — task.list (state=completed +
        # updated_after) + decision.list (recorded_after) + brief.
        eb_payload = _call_mcp_tool_json(
            specs_by_name,
            "task.list",
            {"state": "completed", "limit": 50},
        )
        assert "items" in eb_payload
        eb_dec = _call_mcp_tool_json(
            specs_by_name,
            "decision.list",
            {"limit": 50},
        )
        assert "items" in eb_dec

        # decision-rationale (read 自律 OK) — decision.list + graph.trace
        # + recall.search. graph.trace needs an entity that has incoming
        # links; the just-applied task is the safest target because
        # ProposalApplied creates an automatic ``applied_to`` link from
        # the proposal.
        dr_trace = _call_mcp_tool_json(
            specs_by_name,
            "graph.trace",
            {"entity_type": "task", "entity_id": applied_task_id, "depth": 3},
        )
        assert "paths" in dr_trace

        # handoff-draft (read 自律 OK, text-only) — task.list (state=
        # in_progress) + decision.list + recall.search + graph.related.
        hd_payload = _call_mcp_tool_json(
            specs_by_name,
            "task.list",
            {"state": "in_progress", "limit": 50},
        )
        assert "items" in hd_payload

        # announcement-draft (read 自律 OK, text-only) — recall.search +
        # decision.list (recorded_after=last_release) + brief.
        ad_dec = _call_mcp_tool_json(
            specs_by_name,
            "decision.list",
            {"limit": 50, "recorded_after": past},
        )
        assert "items" in ad_dec

        # inbox-triage (HITL write) — inbox.list (state=open) +
        # propose.generate (mode=inbox_triage) + propose.apply.
        it_inbox = _call_mcp_tool_json(
            specs_by_name,
            "inbox.list",
            {"state": "open", "limit": 20},
        )
        assert "items" in it_inbox
        it_gen = _call_mcp_tool_json(
            specs_by_name,
            "propose.generate",
            {
                "topic": "triage open inbox items",
                "mode": "inbox_triage",
                "max_candidates": 2,
            },
        )
        assert it_gen["scope"] == "inbox_triage", (
            f"propose.generate mode=inbox_triage must stamp scope; got: {it_gen}"
        )
        it_candidates = cast("list[dict[str, Any]]", it_gen["candidates"])
        # Apply the task candidate so the HITL path is exercised
        # end-to-end (idempotent contract already covered above).
        it_task_idx = next(i for i, c in enumerate(it_candidates) if c.get("kind") == "task")
        it_apply = _call_mcp_tool_json(
            specs_by_name,
            "propose.apply",
            {"proposal_id": it_gen["proposal_id"], "candidate_index": it_task_idx},
        )
        assert it_apply["ok"] is True

        # source-extract (HITL write) — source.get + propose.generate
        # (mode=source_extract) + propose.apply.
        se_get = _call_mcp_tool_json(
            specs_by_name,
            "source.get",
            {"source_id": word_source_id},
        )
        assert se_get.get("found") is True
        se_gen = _call_mcp_tool_json(
            specs_by_name,
            "propose.generate",
            {
                "topic": "extract from Q3 review notes",
                "mode": "source_extract",
                "max_candidates": 2,
            },
        )
        assert se_gen["scope"] == "source_extract"

        # meeting-followup (HITL write) — source.list (source_type=
        # calendar_event + observed_after/before) + source.get +
        # recall.search + propose.generate (mode=meeting_followup) +
        # propose.apply.
        mf_list = _call_mcp_tool_json(
            specs_by_name,
            "source.list",
            {"source_type": "ms365_calendar", "limit": 5},
        )
        assert "items" in mf_list
        mf_gen = _call_mcp_tool_json(
            specs_by_name,
            "propose.generate",
            {
                "topic": "Q3 architecture review follow-up",
                "mode": "meeting_followup",
                "max_candidates": 2,
            },
        )
        assert mf_gen["scope"] == "meeting_followup"

        # ---- 6. HITL boundary policy assertions ----------------------
        # propose.apply is the only write tool with ``destructive=false``
        # (ADR-0022 改訂 §決定 (f-2)).
        apply_policy = specs_by_name["propose.apply"].policy
        assert apply_policy.read_only is False
        assert apply_policy.destructive is False, (
            "ADR-0022 §決定 (f-2) — propose.apply must be destructive=false"
            " (idempotent HITL write); other write tools remain destructive=true."
        )
        assert apply_policy.idempotent is True, (
            "ADR-0022 §決定 (f-2) — propose.apply must be idempotent=true."
        )

        # The other 4 write tools keep destructive=true.
        for write_name in ("task.create", "inbox.add", "connector.sync", "propose.generate"):
            wp = specs_by_name[write_name].policy
            assert wp.read_only is False
            assert wp.destructive is True, (
                f"{write_name} must keep destructive=true — only propose.apply"
                " is carved out (ADR-0022 改訂 §決定 (f-2))."
            )

        # search is a read tool with the standard read policy.
        search_policy = specs_by_name["search"].policy
        assert search_policy.read_only is True
        assert search_policy.destructive is False

        # ---- 7. structural absence guards ----------------------------
        # 7a. Write-back path on every connector (mirrors Phase 10 / 11
        #     lifecycle guards; extended for completeness in Phase 12).
        forbidden_callables = {"send", "post", "write", "comment_create"}
        from opshub import connectors as connectors_pkg

        _ = connectors_pkg  # touched only for the package import
        connector_modules = [
            f"opshub.connectors.{name}"
            for name in (
                "github",
                "slack",
                "ms365",
                "box",
                "box_drive",
                "teams",
                "onedrive_drive",
            )
        ]
        for module_path in connector_modules:
            module = importlib.import_module(module_path)
            offenders = {
                name for name in forbidden_callables if callable(getattr(module, name, None))
            }
            assert not offenders, (
                f"connector {module_path} exposes write-back callable(s)"
                f" {offenders!r} — ADR-0010 §禁止事項 7 forbids external"
                " write-back."
            )

        # 7b. handoff-draft / announcement-draft must NOT have persisted
        #     proposals (text-only, ADR-0016 §決定 (l)(a)). We never
        #     called propose.generate with mode=handoff_draft /
        #     announcement_draft above (and the ``mode`` enum at the
        #     handler whitelist makes that path structurally
        #     unreachable). Verify by walking the proposals projection
        #     and asserting no row has scope=handoff_draft /
        #     announcement_draft.
        from sqlalchemy import text

        with engine.connect() as conn:
            scopes_seen = {
                row.scope for row in conn.execute(text("SELECT DISTINCT scope FROM proposals"))
            }
        for forbidden_scope in ("handoff_draft", "announcement_draft"):
            assert forbidden_scope not in scopes_seen, (
                f"ADR-0016 §決定 (l)(a) — proposals.scope={forbidden_scope!r} is"
                " forbidden (text-only skills must not persist); saw"
                f" scopes: {scopes_seen}"
            )

        # 7c. Verify the handler whitelist (``_PROPOSE_GENERATE_MODES``)
        #     does NOT include handoff_draft / announcement_draft. We
        #     import via ``getattr`` on the module to avoid pyright's
        #     ``reportPrivateUsage`` while still pinning the contract.
        from opshub.mcp import _writes as _mcp_writes

        propose_generate_modes = cast(
            "frozenset[str]",
            getattr(_mcp_writes, "_PROPOSE_GENERATE_MODES"),  # noqa: B009
        )
        assert "handoff_draft" not in propose_generate_modes
        assert "announcement_draft" not in propose_generate_modes
        assert propose_generate_modes == frozenset(
            {"inbox_triage", "source_extract", "meeting_followup"}
        ), (
            "ADR-0016 §決定 (l)(b) — propose.generate ``mode`` whitelist must"
            " be the 3 persist-bearing dispatch keys exactly; got:"
            f" {propose_generate_modes}"
        )

        # 7d. box_drive default body=summary round-trip (epic #470 /
        #     issue #481 replaced the Phase 10/11 ``body=NULL`` shim).
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT body FROM sources WHERE id = :id"),
                {"id": box_drive_source_id},
            ).first()
        assert row is not None
        assert row.body == "path: Phase12/skill-catalog.md", (
            "epic #470 / #481 — FS-scan body=summary (ADR-0010 §不変条件)"
        )
    finally:
        engine.dispose()
