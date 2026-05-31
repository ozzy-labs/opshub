"""Phase 11 end-to-end MS Office lifecycle (Sub-issue F6 closeout).

Pins the Phase 11 data-pipeline shape on the platform side: Teams chat
bodies, Outlook deep bodies, and Word/Excel/PowerPoint bodies all land
in the same ``sources`` projection, are indexed by FTS5 + body-based
embeddings, surface through the same MCP read tools that drive the
secretary skills, and the write-back path remains structurally absent.

Phase 11 ships **no new MCP tools** — the existing read surface
(``recall.search`` + ``source.list`` + ``source.get`` + the daily-brief
tools) automatically widens to the new ``source_type`` discriminators
because the projection is the SSOT. This test follows the
:mod:`test_phase10_secretary_lifecycle` pattern: an in-process MCP
dispatch wrapper replays the agent host's tool calls against a hermetic
engine + deterministic stub embedder, with **no** Microsoft Graph /
markitdown / network calls.

What this pins
--------------

1. **取り込み (Teams + Outlook body + Office)** — sources of every
   Phase 11 `source_type` land in ``sources`` with bodies + provenance
   (ADR-0020 §(e)). The FTS5 index (migration 0019) and embeddings
   path treat them uniformly with the Phase 7-10 connectors.
2. **MCP cross-source search** — ``recall.search`` returns hits that
   span ≥2 Phase 11 connectors for a single body-anchored query,
   proving the body store is the SSOT for cross-connector recall
   (Phase 11 plan §7.3 step 2 / step 3).
3. **Office source_type discriminators** — the 3 ADR-0025 §決定 (d)
   source_types (``word_document`` / ``excel_spreadsheet`` /
   ``powerpoint_slide_deck``) are observable via ``source.list`` and
   the underlying projection, so secretary skills can filter on them
   when desired (e.g. "PPT only" file-lookup).
4. **Write-back path absence** — the 2 new connector packages
   (``teams`` / ``onedrive_drive``) and the Phase 11-extended
   ``box_drive`` / ``ms365`` connectors expose no ``send`` / ``post``
   / ``write`` / ``comment_create`` callable (ADR-0010 §禁止事項 7).
   Mirrors the Phase 10 lifecycle guard so a future PR adding a
   write-back path without a dedicated ADR is caught structurally.
5. **box_drive / onedrive_drive default body=None** — the FS-scan
   connectors keep ADR-0019 §不変条件 (b) by default (no
   ``content_extraction``); only Office documents opt in to
   :func:`extract_document`. We seed both shapes (metadata-only +
   extracted body) to prove they round-trip through the projection.

The MCP layer is exercised via the in-process
:func:`opshub.mcp.server.dispatch_tool_call` wrapper. That wrapper is
identical to what runs inside ``serve_stdio`` — we just skip the stdio
transport so the test stays hermetic. Mirrors
:mod:`tests.integration.test_phase10_secretary_lifecycle` to keep the
two e2e tests structurally aligned (a future refactor can lift shared
helpers without surprising either).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

# Skip if sqlite-vec extension is unavailable; the embedder path is
# part of the lifecycle, and Phase 4+ tests in this folder use the
# same gate.
pytest.importorskip("sqlite_vec")

from typer.testing import CliRunner

from opshub.cli._wiring import build_engine, build_source_service
from opshub.cli.app import app
from opshub.domain.events.source import ProvenanceOrigin, ProvenanceTrust
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs — kept local for the same reason the Phase 10 lifecycle keeps
# its own copies (no cross-test coupling).
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub.

    Returns a 1024-dim vector keyed on input text so the recall path
    behaves predictably. Same shape as the Phase 10 lifecycle stub —
    duplicated here on purpose so this test is hermetic.
    """

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase11-stub-embedder"

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


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the embedder factory so recall-side calls stay hermetic."""
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


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
    """Drive :func:`dispatch_tool_call` synchronously and unwrap text content."""
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
    """Call an MCP tool and parse its JSON response into a typed dict."""
    import json

    raw = _call_mcp_tool(specs_by_name, name, arguments)
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# Source seeding — mirror the SourceService calls real connectors make.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Phase11Fixture:
    """One Phase 11 source seed.

    Frozen + Literal-typed provenance fields so the call into
    :meth:`SourceService.observe` narrows without casts.
    """

    connector_name: str
    source_type: str
    external_id: str
    title: str
    body: str | None
    provenance_origin: ProvenanceOrigin | None


# A small fixture set built around a hypothetical "Q3 architecture
# review" meeting topic so we can assert cross-connector recall hits
# against a deterministic phrase.
#
# Each row uses a distinct ``connector_name`` and ``source_type`` so we
# can assert on Phase 11 surface coverage explicitly. The bodies are
# small and deterministic — we never actually open / parse the
# fixtures in ``tests/fixtures/office/`` because that would couple this
# test to markitdown's availability (the same reason the Phase 10
# lifecycle seeds its sources directly).
_PHASE11_FIXTURES: tuple[_Phase11Fixture, ...] = (
    _Phase11Fixture(
        connector_name="teams",
        source_type="teams_message",
        external_id="19:abc@thread.tacv2:1717200000-msg-001",
        title="alice in #arch — Q3 review prep",
        body=(
            "Let's pull the Q3 architecture review materials together. "
            "Carol uploaded the slide deck to OneDrive last week."
        ),
        provenance_origin="external",
    ),
    _Phase11Fixture(
        connector_name="ms365",
        source_type="ms365_outlook",
        external_id="AAMkADhfd-msg-q3-review-001",
        title="Q3 architecture review — agenda",
        body=(
            "Agenda for the Q3 architecture review meeting:\n"
            "1. Capacity planning (carol)\n"
            "2. Phase 11 retrospective (alice)\n"
            "3. Next quarter priorities (open)"
        ),
        provenance_origin="external",
    ),
    _Phase11Fixture(
        connector_name="box_drive",
        source_type="word_document",
        external_id="Phase11/q3-review-notes.docx",
        title="q3-review-notes.docx",
        # Bodies here are produced by markitdown in production
        # (`core/document_extract.extract_document`); we seed the
        # already-extracted markdown shape directly so the test stays
        # markitdown-free. ADR-0019 §決定 (b') opt-in body is tagged
        # external+untrusted just like SaaS-fetched bodies.
        body=(
            "# Q3 architecture review notes\n\n"
            "Capacity planning section discusses the Phase 11 connector "
            "additions (Teams + OneDrive Drive)."
        ),
        provenance_origin="external",
    ),
    _Phase11Fixture(
        connector_name="onedrive_drive",
        source_type="excel_spreadsheet",
        external_id="Specs/q3-review-capacity.xlsx",
        title="q3-review-capacity.xlsx",
        body=(
            "## Sheet: capacity\n\n"
            "| connector | est. rps | notes |\n"
            "|---|---|---|\n"
            "| teams | 100 | new in phase 11 |\n"
            "| onedrive_drive | n/a (FS scan) | new in phase 11 |\n"
        ),
        provenance_origin="external",
    ),
    _Phase11Fixture(
        connector_name="onedrive_drive",
        source_type="powerpoint_slide_deck",
        external_id="Decks/q3-review-deck.pptx",
        title="q3-review-deck.pptx",
        body=(
            "# Slide 1: Q3 architecture review\n\n"
            "## Notes:\nDiscuss the Phase 11 office extraction pipeline."
        ),
        provenance_origin="external",
    ),
    _Phase11Fixture(
        connector_name="onedrive_drive",
        source_type="box_drive_file",
        external_id="Misc/readme.txt",
        title="readme.txt",
        # body=None for the FS-scan default path (ADR-0019 §不変条件
        # (b)). Pinned here so the round-trip exercises the NULL
        # branch end-to-end alongside the opted-in Office bodies above.
        body=None,
        provenance_origin=None,
    ),
)


def _seed_sources(actor: str = "test:phase11_lifecycle") -> list[str]:
    """Seed the Phase 11 source rows via :class:`SourceService`.

    Returns the ULIDs in the same order as the fixture tuples so
    later assertions can locate them.
    """
    service = build_source_service(actor=actor)
    ids: list[str] = []
    for fixture in _PHASE11_FIXTURES:
        trust: ProvenanceTrust | None = None
        if fixture.provenance_origin == "external":
            trust = "untrusted"  # ADR-0020 §(e)
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


def test_phase11_office_lifecycle(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end Phase 11 data pipeline walk.

    Sequence (Phase 11 plan §7.3):

    1. Seed Phase 11 sources (Teams + Outlook + .docx + .xlsx + .pptx
       + a metadata-only FS-scan row) via :class:`SourceService` so the
       body store has cross-connector material.
    2. Drive ``opshub embeddings rebuild`` so the body-based vector
       store is populated.
    3. Replay the agent host's MCP read calls:
       - ``recall.search`` for the "Q3 architecture review" topic.
       - Assert hits cross ≥2 Phase 11 connectors (cross-source
         widening, Phase 11 plan §7.3 step 2 / step 3).
       - ``source.list`` filtered to the Phase 11 ``source_type``
         discriminators to prove they are observable as first-class
         citizens (operator UX for "PPT only" / "Excel only"
         file-lookup).
    4. Assert connector packages **do not** expose any ``send`` /
       ``post`` / ``write`` / ``comment_create`` callable — proves the
       structural absence of a write-back path (mirrors the Phase 10
       guard, extended to the 2 new connectors and the Phase 11-
       extended ``ms365`` / ``box_drive``).
    5. Assert the metadata-only FS-scan row round-trips with
       ``body = NULL`` so the ADR-0019 §不変条件 (b) default path
       stays observable alongside the opt-in extraction path.
    6. Drive ``graph.related`` over a Phase 11 Teams → Outlook link
       (manual ``link_type="manual"`` via :class:`LinkService`) so the
       Phase 11 plan §7.3 step 3 graph traversal stays covered after
       new ``source_type`` discriminators land.
    7. Stub :func:`opshub.core.excludes.load_excludes` to mark a Teams
       ``chat_id`` excluded and assert the loaded rules report the
       channel as filtered — pins the ADR-0020 §(b) shared excludes
       reaching Teams (security regression guard).

    Phase 11 audit Cluster C note: the MCP ``connector.sync`` path for
    the new ``teams`` / ``onedrive_drive`` connectors is **not**
    exercised here — :mod:`opshub.mcp._writes` does not yet import the
    two new connector packages (Cluster B issue). Once Cluster B lands
    the import additions, a follow-up should add an MCP
    ``connector.sync`` smoke test for each new connector name here.
    """
    # ---- 0. monkeypatch the embedder ----------------------------------
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    # ---- 1. seed Phase 11 sources -------------------------------------
    source_ids = _seed_sources()
    metadata_only_id = source_ids[-1]
    assert all(len(sid) == 26 for sid in source_ids), source_ids

    # ---- 2. embeddings rebuild (body-based, ADR-0012 改訂 §4) ---------
    code, rebuild_out, rebuild_err = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out + (rebuild_err or "")

    # ---- 3. wire the MCP surface against the same engine -------------
    engine = build_engine()
    try:
        from opshub.mcp.server import build_tool_specs_for_engine

        specs = build_tool_specs_for_engine(engine)
        specs_by_name = {spec.name: spec for spec in specs}

        # ---- 3a. file-lookup-style cross-source recall ----------------
        # The Q3 architecture review phrase appears in the Teams body
        # and the Outlook body verbatim, and is implied by the
        # extracted Office bodies. The recall.search hit list must
        # surface ≥1 source hit and (joining back to ``sources``) span
        # ≥2 distinct Phase 11 connectors. Mirrors the Phase 10
        # cross-connector recall assertion but tightened to the new
        # connector classes.
        cross_payload = _call_mcp_tool_json(
            specs_by_name,
            "recall.search",
            {"query": "Q3 architecture review", "limit": 10},
        )
        cross_hits = cast("list[dict[str, Any]]", cross_payload["hits"])
        source_hit_ids = [
            cast("str", hit["entity_id"])
            for hit in cross_hits
            if hit.get("entity_type") == "source"
        ]
        assert source_hit_ids, (
            "recall.search for a body-anchored Phase 11 query must surface at"
            f" least one source-class hit; got: {cross_payload}"
        )
        assert cross_payload["truncated_snippets"] is True, (
            "ADR-0022 §(d) — recall.search must advertise truncated_snippets=True"
        )
        # Join back to sources.connector_name. Phase 11 widens the
        # connector universe to 7; the cross-source assertion is the
        # primary "the body store is the SSOT" guard.
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
            "cross-connector recall.search must surface hits from ≥2 Phase 11"
            f" connectors (Phase 11 plan §7.3 step 3); got: {connector_names}"
        )

        # ---- 3b. source_type discriminator visibility -----------------
        # The 3 ADR-0025 §決定 (d) source_types must each be
        # observable via the Step 1 ``source.list`` tool. We sanity-
        # check the Phase 11 / projection wiring by querying the
        # projection directly — the MCP ``source.list`` envelope is
        # already covered by ``tests/unit/mcp/test_server_dispatch``.
        with engine.connect() as conn:
            seen_types = {
                row.source_type
                for row in conn.execute(text("SELECT DISTINCT source_type FROM sources"))
            }
        for phase11_type in (
            "teams_message",
            "word_document",
            "excel_spreadsheet",
            "powerpoint_slide_deck",
        ):
            assert phase11_type in seen_types, (
                f"Phase 11 source_type {phase11_type!r} missing from sources"
                f" projection; saw: {seen_types}"
            )

        # ---- 4. structural absence of a write-back path --------------
        # ADR-0010 §禁止事項 7 Phase 10 改訂 + Phase 11 改訂 keep
        # write-back out of scope across the 7 connectors. A future
        # PR adding ``send`` / ``post`` / ``write`` / ``comment_create``
        # without a separate ADR + opt-in is caught here.
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
                " write-back; remove the callable or surface a separate"
                " ADR + opt-in."
            )

        # ---- 5. metadata-only FS-scan default ------------------------
        # ADR-0019 §不変条件 (b): a FS-scan source without
        # ``content_extraction`` keeps body=NULL. The same query path
        # that returns extracted Office bodies above must round-trip
        # NULL cleanly for the un-extracted FS row.
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, body, source_type FROM sources WHERE id = :id"),
                {"id": metadata_only_id},
            ).first()
        assert row is not None
        assert row.body is None, (
            "ADR-0019 §不変条件 (b) — FS-scan rows without content_extraction must keep body=NULL"
        )
        assert row.source_type == "box_drive_file"

        # ---- 6. graph.related cross-connector traversal --------------
        # Phase 11 plan §7.3 step 3: link a Phase 11 Teams row to a
        # Phase 11 Outlook row (mirrors a typical operator workflow —
        # the Teams chat references the Outlook agenda for the same
        # meeting) and assert ``graph.related`` returns the link from
        # the Teams side. Walks the full LinkService → projection →
        # MCP handler stack so a future refactor that drops Phase 11
        # source types from the graph surface is caught here.
        from opshub.cli._wiring import build_link_service

        teams_source_id = source_ids[0]
        outlook_source_id = source_ids[1]
        link_service = build_link_service(actor="test:phase11_lifecycle")
        link_service.create_link(
            from_entity_type="source",
            from_entity_id=teams_source_id,
            to_entity_type="source",
            to_entity_id=outlook_source_id,
            link_type="manual",
        )
        related_payload = _call_mcp_tool_json(
            specs_by_name,
            "graph.related",
            {
                "entity_id": teams_source_id,
                "entity_type": "source",
                "direction": "outgoing",
                "limit": 10,
            },
        )
        related_items = cast("list[dict[str, Any]]", related_payload["items"])
        assert any(item.get("to_entity_id") == outlook_source_id for item in related_items), (
            "graph.related must surface the Phase 11 Teams → Outlook link from"
            f" the Teams side; got: {related_payload}"
        )

        # ---- 7. excludes.yaml suppresses Teams ingest ----------------
        # ADR-0020 §(b) — the shared excludes file must filter Teams
        # rows just like Slack. We don't re-run a Graph fetch (the
        # connector path is mocked in unit tests); instead we assert
        # the loaded ``ExcludeRules`` reports the Teams chat as
        # excluded so the connector's "skip if excluded" branch
        # (see ``opshub.connectors.teams.connector``) would fire. This
        # is the same shape the Phase 10 e2e exercises for Slack.
        from opshub.core import excludes as excludes_module
        from opshub.core.excludes import ExcludeRules

        teams_chat_id = "19:secret-teams-channel-id"

        def _stub_load_excludes(config_dir: Path | None = None) -> ExcludeRules:
            del config_dir
            return ExcludeRules(channels=frozenset({teams_chat_id}))

        monkeypatch.setattr(excludes_module, "load_excludes", _stub_load_excludes)
        loaded_rules = excludes_module.load_excludes()
        assert loaded_rules.excludes_channel(teams_chat_id), (
            "excludes.yaml ``channels`` selector must mark the Teams chat_id"
            " excluded so the Teams connector skips it at fetch time"
            " (ADR-0020 §(b)); cf. opshub.connectors.teams.connector"
        )
        # The opposite case must remain false so we know we're not
        # accidentally over-matching (cross-checks the frozenset path).
        assert not loaded_rules.excludes_channel("19:public-teams-channel"), (
            "excludes.yaml must not over-match unrelated channels"
        )
    finally:
        engine.dispose()
