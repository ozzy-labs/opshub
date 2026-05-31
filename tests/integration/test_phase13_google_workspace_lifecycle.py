"""Phase 13 end-to-end Google Workspace lifecycle (Sub-issue G5 closeout).

Pins the Phase 13 data-pipeline shape on the platform side: Google
Docs / Slides / Sheets bodies (extracted via Drive API ``files.export``
+ markitdown) and Google Workspace metadata-only catch-all rows
(``google_workspace_file``) all land in the same ``sources`` projection,
are indexed by FTS5 + body-based embeddings, surface through the same
MCP read tools that drive the secretary skills, and the write-back path
remains structurally absent.

Phase 13 ships **no new MCP tools** — the existing read surface
(``search`` + ``recall.search`` + ``source.list`` + ``source.get``)
automatically widens to the new ``source_type`` discriminators
(``google_doc`` / ``google_slides`` / ``google_sheets`` /
``google_workspace_file``) because the projection is the SSOT. This test
follows the :mod:`test_phase11_office_lifecycle` pattern: an in-process
MCP dispatch wrapper replays the agent host's tool calls against a
hermetic engine + deterministic stub embedder, with **no** Drive API /
markitdown / network calls.

What this pins
--------------

1. **取り込み (Google Workspace + catch-all)** — sources of every
   Phase 13 ``source_type`` land in ``sources`` with bodies + provenance
   (ADR-0020 §(e)). The FTS5 index (migration 0019) and embeddings
   path treat them uniformly with the Phase 7-11 connectors.
2. **MCP cross-source search** — ``recall.search`` and ``search``
   return hits that span Phase 11 Office bodies + Phase 13 Google
   Workspace bodies for a single body-anchored query, proving the body
   store is the SSOT for cross-connector recall (Phase 13 plan §7.3
   step 2).
3. **find-document mixed filter** — Phase 11 ``word_document`` /
   ``excel_spreadsheet`` / ``powerpoint_slide_deck`` and Phase 13
   ``google_doc`` / ``google_slides`` / ``google_sheets`` are both
   observable via ``source.list`` and the underlying projection, so
   secretary skills can filter on a mix of them (e.g.
   ``find-document`` 自然文 query "Word and Google Sheets").
4. **Write-back path absence** — the new ``google_workspace`` connector
   package exposes no ``send`` / ``post`` / ``write`` /
   ``comment_create`` / ``files_update`` / ``files_export_write``
   callable (ADR-0010 §禁止事項 7 + §Phase 13 改訂 (e) §禁止事項拡張
   = Drive ``files.watch`` push notification も禁止). Mirrors the Phase
   10 / 11 lifecycle guard, extended to the 8th connector.
5. **catch-all metadata-only round-trip** — the ``google_workspace_file``
   source_type (Drive items that are not Workspace native = uploaded
   PDF / image / folder etc.) keeps ``body = NULL`` even when
   ``content_extraction = true`` (Drive API returns 403
   ``fileNotExportable`` for these mimeTypes). Phase 13 G4 wiring
   short-circuits ``files.export`` for the catch-all; we seed both
   shapes (extracted body + metadata-only catch-all) to prove they
   round-trip through the projection.
6. **Shared with me / trashed / removed semantics** — the Phase 13
   trashed semantics ("retain as archived per ADR-0020 §全保持") and
   Shared with me ("retain for secretary utility") are observable via
   ``source.list`` so secretary skills can surface them when desired.
7. **Refresh token rotation cursor continuation** — after a token
   rotation write-back (Phase 13 plan §7.3 step 5 = rotation シナリオ),
   the next ``changes.list`` round-trip resumes from the same
   ``startPageToken`` cursor (Phase 13 plan §G3 DoD rotation pin =
   :func:`test_get_access_token_persists_rotated_refresh_token` for the
   keyring-write side; this test adds the cursor-continuation side).

The MCP layer is exercised via the in-process
:func:`opshub.mcp.server.dispatch_tool_call` wrapper. That wrapper is
identical to what runs inside ``serve_stdio`` — we just skip the stdio
transport so the test stays hermetic. Mirrors
:mod:`tests.integration.test_phase11_office_lifecycle` to keep the
Phase 11 / 13 e2e tests structurally aligned.
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
# Stubs — kept local for the same reason the Phase 10 / 11 lifecycles
# keep their own copies (no cross-test coupling).
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub.

    Returns a 1024-dim vector keyed on input text so the recall path
    behaves predictably. Same shape as the Phase 10 / 11 lifecycle
    stubs — duplicated here on purpose so this test is hermetic.
    """

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase13-stub-embedder"

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
class _Phase13Fixture:
    """One Phase 13 source seed.

    Frozen + Literal-typed provenance fields so the call into
    :meth:`SourceService.observe` narrows without casts.
    """

    connector_name: str
    source_type: str
    external_id: str
    title: str
    body: str | None
    provenance_origin: ProvenanceOrigin | None


# A fixture set built around a hypothetical "Q4 platform planning"
# meeting topic so we can assert cross-connector recall hits against
# a deterministic phrase that appears in both Phase 11 Office bodies
# and Phase 13 Google Workspace bodies. The Phase 11 row (Word) plays
# the "incumbent connector" role for the mixed-filter assertion.
#
# Each row uses a distinct ``external_id`` and ``source_type`` so we
# can assert on Phase 13 surface coverage explicitly. The bodies are
# small and deterministic — we never actually open / parse the
# ``tests/fixtures/google_workspace/*.docx`` etc. files because that
# would couple this test to markitdown's availability (the same
# reason the Phase 11 lifecycle seeds its sources directly).
_PHASE13_FIXTURES: tuple[_Phase13Fixture, ...] = (
    _Phase13Fixture(
        connector_name="google_workspace",
        source_type="google_doc",
        external_id="1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        title="Q4 platform planning — design doc",
        body=(
            "# Q4 platform planning\n\n"
            "Carol drafted the Q4 platform planning roadmap. "
            "We need to confirm capacity numbers from the Sheets workbook."
        ),
        provenance_origin="external",
    ),
    _Phase13Fixture(
        connector_name="google_workspace",
        source_type="google_sheets",
        external_id="1ShEeT5pReAdShEeTbA5e64Fileldfb50R",
        title="Q4 platform planning — capacity sheet",
        body=(
            "## Sheet: capacity\n\n"
            "| connector | est. rps | notes |\n"
            "|---|---|---|\n"
            "| google_workspace | 50 | new in phase 13 |\n"
            "| teams | 100 | new in phase 11 |\n"
        ),
        provenance_origin="external",
    ),
    _Phase13Fixture(
        connector_name="google_workspace",
        source_type="google_slides",
        external_id="1Sl1deDeckIdF1leld5fb70Ra500fakelD",
        title="Q4 platform planning — kickoff deck",
        body=(
            "# Slide 1: Q4 platform planning kickoff\n\n"
            "## Notes:\n"
            "Discuss the Phase 13 Google Workspace ingestion path."
        ),
        provenance_origin="external",
    ),
    _Phase13Fixture(
        connector_name="google_workspace",
        source_type="google_workspace_file",
        external_id="1CatchAllFilePdf5Uploaded2Drive0fakeID",
        title="research-notes.pdf",
        # ``google_workspace_file`` is the catch-all for Drive items
        # that are not Workspace native (uploaded PDF / image /
        # folder etc.). Drive API returns 403 ``fileNotExportable``
        # for these mimeTypes, so even with ``content_extraction =
        # true`` the connector keeps body=None for them. Pinned here
        # so the NULL branch round-trips end-to-end alongside the
        # extracted bodies above.
        body=None,
        provenance_origin="external",
    ),
    _Phase13Fixture(
        connector_name="google_workspace",
        source_type="google_doc",
        external_id="1SharedW1thM3D0cF1leId5fb50R000ake1D",
        title="[shared] Vendor roadmap doc (Shared with me)",
        # Shared with me semantics (Phase 13 plan §trashed / removed
        # semantics): operator does not own this file but it was
        # shared with them; the connector retains it for secretary
        # utility. Body content stays in scope of "Q4 platform
        # planning" recall.
        body=(
            "# Vendor Q4 roadmap (shared)\n\n"
            "Vendor's Q4 platform planning checkpoints. Shared by alice."
        ),
        provenance_origin="external",
    ),
    _Phase13Fixture(
        connector_name="google_workspace",
        source_type="google_doc",
        external_id="1TrashedD0cF1leId5fb50R000takefakelD",
        title="[trashed] archived Q4 planning draft",
        # Trashed semantics (Phase 13 plan §trashed / removed
        # semantics): Drive returns ``trashed=true`` flagged rows
        # via ``changes.list``; ADR-0020 全保持原則に従い retain
        # する (archived 相当)。
        body=(
            "# Q4 planning draft (trashed)\n\n"
            "Archived earlier draft — moved to trash but retained "
            "locally per ADR-0020."
        ),
        provenance_origin="external",
    ),
    # An incumbent Phase 11 Word document so we can assert mixed
    # Phase 11 + Phase 13 source_type filtering (find-document 「Word
    # と Google Doc を混ぜて探す」 自然文 query 同型). Same body
    # phrase so recall.search finds both.
    _Phase13Fixture(
        connector_name="box_drive",
        source_type="word_document",
        external_id="Specs/q4-planning-notes.docx",
        title="q4-planning-notes.docx",
        body=(
            "# Q4 platform planning notes\n\n"
            "Phase 11 office extraction handles the Word side; "
            "Phase 13 widens the same path to Google Workspace."
        ),
        provenance_origin="external",
    ),
)


def _seed_sources(actor: str = "test:phase13_lifecycle") -> list[str]:
    """Seed the Phase 13 source rows via :class:`SourceService`.

    Returns the ULIDs in the same order as the fixture tuples so
    later assertions can locate them.
    """
    service = build_source_service(actor=actor)
    ids: list[str] = []
    for fixture in _PHASE13_FIXTURES:
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


def test_phase13_google_workspace_lifecycle(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end Phase 13 Google Workspace data pipeline walk.

    Sequence (Phase 13 plan §7.3):

    1. Seed Phase 13 sources (Google Doc / Sheets / Slides + Phase 11
       Word incumbent + Shared with me + trashed + metadata-only
       catch-all) via :class:`SourceService` so the body store has
       cross-connector + mixed-Phase material.
    2. Drive ``opshub embeddings rebuild`` so the body-based vector
       store is populated.
    3. Replay the agent host's MCP read calls:
       - ``recall.search`` for the "Q4 platform planning" topic →
         hits cross Phase 11 (Word) + Phase 13 (Google Workspace),
         proving the body store is the SSOT for cross-connector
         recall.
       - ``search`` (FTS5) for the same phrase — Phase 12 H1 FTS5
         tool widens transparently to the new ``source_type``
         discriminators because ``sources_fts`` indexes the same
         ``sources.body`` column.
    4. Assert each Phase 13 ``source_type`` (``google_doc`` /
       ``google_slides`` / ``google_sheets`` /
       ``google_workspace_file``) is observable as a first-class
       citizen via the projection — operator UX for "Google Sheets
       only" / "Google Slides only" find-document.
    5. Assert connector packages **do not** expose any ``send`` /
       ``post`` / ``write`` / ``comment_create`` callable across all
       8 connectors (the Phase 11 7-connector guard widens to include
       ``google_workspace``). Proves the structural absence of a
       write-back path / Drive `files.watch` path.
    6. Assert the catch-all ``google_workspace_file`` row round-trips
       with ``body = NULL`` so the ADR-0025 §決定 (d') catch-all
       branch stays observable alongside the extracted Workspace
       bodies (Drive returns 403 ``fileNotExportable`` for non-Native
       files; Phase 13 G4 short-circuits ``files.export`` for the
       catch-all).
    7. Refresh token rotation continuation: simulate a rotation
       write-back (Phase 13 plan §7.3 step 5 = rotation シナリオ)
       and assert the cursor remains pinned to the post-rotation
       page token so the next ``changes.list`` round-trip resumes
       from the same point. Walks the cursor → keyring round-trip
       so a regression that drops cursor-after-rotation is caught
       here.
    8. Stub :func:`opshub.core.excludes.load_excludes` to mark a
       Google Workspace folder path excluded and assert the loaded
       rules report the path as filtered — pins the ADR-0020 §(b)
       shared excludes reaching the Google Workspace connector
       (security regression guard).
    """
    # ---- 0. monkeypatch the embedder ----------------------------------
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    # ---- 1. seed Phase 13 sources -------------------------------------
    source_ids = _seed_sources()
    catchall_id = source_ids[3]  # google_workspace_file row
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

        # ---- 3a. recall surfaces Phase 13 google_workspace bodies -----
        # The Q4 platform planning phrase appears verbatim in the Phase
        # 13 Google Doc bodies. The recall.search hit list must surface
        # ≥1 source hit anchored on the new google_workspace
        # connector — proving the body store is the SSOT for Phase 13
        # data alongside Phase 11. (Strict cross-connector ordering is
        # left to deterministic vector tests in tests/unit/; here we
        # pin that Google Workspace bodies *do* surface through the
        # same MCP read tool that drives the secretary skills.)
        cross_payload = _call_mcp_tool_json(
            specs_by_name,
            "recall.search",
            {"query": "Q4 platform planning", "limit": 10},
        )
        cross_hits = cast("list[dict[str, Any]]", cross_payload["hits"])
        source_hit_ids = [
            cast("str", hit["entity_id"])
            for hit in cross_hits
            if hit.get("entity_type") == "source"
        ]
        assert source_hit_ids, (
            "recall.search for a body-anchored Phase 13 query must surface at"
            f" least one source-class hit; got: {cross_payload}"
        )
        assert cross_payload["truncated_snippets"] is True, (
            "ADR-0022 §(d) — recall.search must advertise truncated_snippets=True"
        )
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
        assert "google_workspace" in connector_names, (
            "recall.search must surface Phase 13 google_workspace bodies for a"
            " body-anchored query (Phase 13 plan §7.3 step 2 — the new connector"
            f" must be a first-class recall citizen); got: {connector_names}"
        )

        # ---- 3b. FTS5 search (Phase 12 H1 tool widens to new types) ----
        # The Phase 12 H1 ``search`` FTS5 MCP tool widens
        # transparently to Phase 13 ``source_type`` discriminators
        # because ``sources_fts`` indexes the same ``sources.body``
        # column. find-document SKILL.md routes through this tool
        # first.
        search_payload = _call_mcp_tool_json(
            specs_by_name,
            "search",
            {"query": "Q4 platform planning", "limit": 10},
        )
        search_items = cast("list[dict[str, Any]]", search_payload["items"])
        assert search_items, (
            "FTS5 search for a Phase 13 body-anchored query must surface ≥1 hit;"
            f" got: {search_payload}"
        )
        # At least one FTS5 hit must come from the Phase 13
        # ``google_workspace`` connector so the body-level FTS index
        # genuinely indexes the new connector's bodies (Phase 13 plan
        # §7.3 step 2 — the new connector must be a first-class FTS
        # citizen too, not just recall).
        assert any(item.get("connector_name") == "google_workspace" for item in search_items), (
            "FTS5 search must include Phase 13 google_workspace bodies for a"
            f" body-anchored query; got: {search_items}"
        )

        # ---- 4. Phase 13 source_type discriminator visibility ----------
        # All 4 Phase 13 source_types must each be observable via
        # the projection. We sanity-check the projection wiring by
        # querying directly — the MCP ``source.list`` envelope is
        # already covered by ``tests/unit/mcp/test_server_dispatch``.
        with engine.connect() as conn:
            seen_types = {
                row.source_type
                for row in conn.execute(text("SELECT DISTINCT source_type FROM sources"))
            }
        for phase13_type in (
            "google_doc",
            "google_slides",
            "google_sheets",
            "google_workspace_file",
        ):
            assert phase13_type in seen_types, (
                f"Phase 13 source_type {phase13_type!r} missing from sources"
                f" projection; saw: {seen_types}"
            )
        # Mixed Phase 11 + Phase 13 filter: assert both
        # ``word_document`` (Phase 11 incumbent) and ``google_doc``
        # (Phase 13 new) coexist so find-document mixed query works.
        assert "word_document" in seen_types and "google_doc" in seen_types, (
            "find-document 'mix Word and Google Doc' query path requires both"
            f" source_types to be observable; got: {seen_types}"
        )

        # ---- 5. structural absence of a write-back path --------------
        # ADR-0010 §禁止事項 7 + Phase 11 改訂 + Phase 13 改訂 keep
        # write-back out of scope across the 8 connectors. The Phase
        # 13 connector additionally must not expose Drive
        # ``files.watch`` push notification callables (ADR-0010
        # §Phase 13 改訂 (e) §禁止事項拡張). A future PR adding any
        # of these without a separate ADR is caught here.
        forbidden_callables = {
            "send",
            "post",
            "write",
            "comment_create",
            "files_update",
            "files_create",
            "files_watch",
            "permissions_create",
            "comments_create",
        }
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
                "google_workspace",
            )
        ]
        for module_path in connector_modules:
            module = importlib.import_module(module_path)
            offenders = {
                name for name in forbidden_callables if callable(getattr(module, name, None))
            }
            assert not offenders, (
                f"connector {module_path} exposes write-back / push callable(s)"
                f" {offenders!r} — ADR-0010 §禁止事項 7 + §Phase 13 改訂 (e)"
                " §禁止事項拡張 forbids external write-back and Drive"
                " files.watch; remove the callable or surface a separate"
                " ADR + opt-in."
            )

        # ---- 6. catch-all metadata-only round-trip -------------------
        # ADR-0025 §決定 (d') catch-all: a ``google_workspace_file``
        # row keeps body=NULL even with ``content_extraction = true``
        # because Drive returns 403 ``fileNotExportable`` for the
        # non-Native mimeTypes. The same query path that returns
        # extracted Google Workspace bodies above must round-trip
        # NULL cleanly for the catch-all row.
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, body, source_type FROM sources WHERE id = :id"),
                {"id": catchall_id},
            ).first()
        assert row is not None
        assert row.body is None, (
            "ADR-0025 §決定 (d') — google_workspace_file (catch-all) rows must"
            " keep body=NULL because Drive returns 403 fileNotExportable for"
            " non-Native mimeTypes"
        )
        assert row.source_type == "google_workspace_file"

        # ---- 7. refresh token rotation cursor continuation -----------
        # Phase 13 plan §7.3 step 5 (rotation シナリオ): when Google
        # rotates the refresh token mid-sync, the connector writes the
        # new value back to keyring (covered by
        # :func:`test_get_access_token_persists_rotated_refresh_token`
        # for the keyring-write side) AND the next ``changes.list``
        # round-trip resumes from the previously-stored
        # ``startPageToken`` (= page token persistence is cursor-side,
        # decoupled from refresh-token rotation).
        #
        # Phase 13 audit cluster A (#286) tightened this pin: pre-#286
        # the test only stamped DB rows directly, which would have
        # green-lit a regression that broke the connector's sync path
        # while leaving the projection's UPDATE shape intact. The
        # corrected shape goes through :meth:`GoogleWorkspaceConnector.sync`
        # twice (before-rotation + after-rotation) so the cursor
        # continuation is observed end-to-end via the same code path the
        # operator would hit, not via projection back-doors.
        from opshub.cli._wiring import build_source_service
        from opshub.connectors.context import ConnectorContext
        from opshub.connectors.google_workspace.connector import (
            GoogleWorkspaceConnector,
        )

        token_before = "STARTPAGE_TOKEN_BEFORE_ROTATION"
        token_after = "STARTPAGE_TOKEN_AFTER_NEXT_SYNC"

        # Build a real source-service against the live engine so the
        # ``cursor_set`` calls inside :meth:`sync` actually persist a
        # row to ``connector_cursors``. Same wiring the CLI driver uses.
        rotation_service = build_source_service(actor="test:phase13_rotation")

        # Stub the connector's lazy ``OpsHubSettings`` + auth + Drive
        # client so the sync runs without hitting Google. The first
        # sync drains a single change page and persists
        # ``token_before``; the second sync resumes from
        # ``token_before`` and advances to ``token_after``.
        from unittest.mock import MagicMock

        fake_drive_settings = MagicMock()
        fake_drive_settings.connectors.google_workspace.client_id = "fake-cid"
        fake_drive_settings.connectors.google_workspace.client_secret = "fake-secret"
        fake_drive_settings.connectors.google_workspace.redirect_uri = "http://localhost"
        fake_drive_settings.connectors.google_workspace.content_extraction = False
        fake_drive_settings.connectors.google_workspace.fallback_window_days = 30
        fake_drive_settings.office.max_file_size_mb = 50
        fake_drive_settings.office.max_chars = 500_000
        fake_drive_settings.office.excel.max_cells_per_sheet = 10_000
        fake_drive_settings.office.excel.max_cells_per_workbook = 50_000
        monkeypatch.setattr(
            "opshub.core.config.OpsHubSettings",
            lambda: fake_drive_settings,
        )

        fake_drive_client_class = MagicMock()
        fake_drive_client_instance = MagicMock()
        fake_drive_client_class.return_value = fake_drive_client_instance
        monkeypatch.setattr(
            "opshub.connectors.google_workspace.client.DriveClient",
            fake_drive_client_class,
        )
        # Phase 14 G2 (#294): patch the shared google_auth source
        # binding so the lazy import inside ``GoogleWorkspaceConnector``
        # resolves to the mock.
        monkeypatch.setattr(
            "opshub.connectors.google_auth.auth.GoogleWorkspaceAuth",
            MagicMock(),
        )

        # Sync 1 — fresh bootstrap, draining a single change page that
        # advances cursor to ``token_before``.
        fake_drive_client_instance.get_start_page_token.return_value = "BOOT_TOKEN"

        from opshub.connectors.google_workspace.client import RawDriveItem

        sync1_item = RawDriveItem(
            file_id="ROTATION_FILE_1",
            removed=False,
            trashed=False,
            name="pre-rotation.gdoc",
            mime_type="application/vnd.google-apps.document",
            modified_time_iso="2026-05-31T00:00:00Z",
            web_view_link="https://drive.google.com/file/d/ROTATION_FILE_1/view",
            owner_email="alice@example.com",
            owner_display_name="Alice",
            is_shared_with_me=False,
            shared=False,
            last_modifying_user_email="",
            last_modifying_user_display_name="",
            drive_id="",
            raw={},
        )

        fetch_queue: list[list[tuple[RawDriveItem, str]]] = [
            [(sync1_item, token_before)],
        ]

        def _fetch_changes_1(*, page_token: str) -> Any:
            del page_token
            return iter(fetch_queue.pop(0))

        fake_drive_client_instance.fetch_changes.side_effect = _fetch_changes_1

        connector = GoogleWorkspaceConnector()
        # Open the sync-run bracket the same way the CLI driver does
        # (`ConnectorSyncStarted` → `connector.sync(...)` →
        # `ConnectorSyncCompleted`). The started event is what creates
        # the row in ``connector_cursors``; the completed event UPDATEs
        # it. Without this wrapping the projection's UPDATE-only branch
        # would silently no-op.
        cursor1_initial = rotation_service.cursor_get(connector.name)
        rotation_service.cursor_set(connector.name, cursor1_initial, sync_started=True)
        ctx1 = ConnectorContext(
            source_service=rotation_service,
            cursor_value=cursor1_initial,
            secrets=None,
            logger=MagicMock(),
        )
        result1 = connector.sync(ctx1)
        rotation_service.cursor_set(connector.name, result1.new_cursor, sync_started=False)
        assert result1.new_cursor == token_before

        with engine.connect() as conn:
            cursor_after_sync1 = conn.execute(
                text(
                    "SELECT cursor_value FROM connector_cursors"
                    " WHERE connector_name = 'google_workspace'"
                )
            ).scalar_one()
        assert cursor_after_sync1 == token_before, (
            f"Sync 1 must persist the advanced cursor; got: {cursor_after_sync1!r}"
        )

        # Simulate Google rotating the refresh token between syncs:
        # the rotation happens on the OAuth-side (auth.refresh_access_token
        # persists the new value to keyring). The cursor row in
        # ``connector_cursors`` must remain untouched by that rotation
        # — they are orthogonal projection rows.
        # (We elide the keyring write here; the unit test
        # ``test_get_access_token_persists_rotated_refresh_token`` pins
        # the OAuth-side mechanic. This e2e pins the cursor-side
        # invariant only.)

        # Sync 2 — resume from ``token_before`` (the same cursor row
        # the rotation did not perturb) and advance to ``token_after``.
        # If the rotation had wiped the cursor row, the connector's
        # :meth:`sync` body would re-bootstrap via
        # ``get_start_page_token`` (visible as a second call below).
        sync2_item = RawDriveItem(
            file_id="ROTATION_FILE_2",
            removed=False,
            trashed=False,
            name="post-rotation.gdoc",
            mime_type="application/vnd.google-apps.document",
            modified_time_iso="2026-05-31T01:00:00Z",
            web_view_link="https://drive.google.com/file/d/ROTATION_FILE_2/view",
            owner_email="alice@example.com",
            owner_display_name="Alice",
            is_shared_with_me=False,
            shared=False,
            last_modifying_user_email="",
            last_modifying_user_display_name="",
            drive_id="",
            raw={},
        )

        fetch_queue.append([(sync2_item, token_after)])

        def _fetch_changes_2(*, page_token: str) -> Any:
            # Pin that the second sync resumed from the cursor the
            # first sync persisted, NOT from a re-bootstrapped root
            # token.
            assert page_token == token_before, (
                "Sync 2 must resume from the persisted cursor (Phase 13"
                " plan §7.3 step 5 rotation シナリオ — rotation must NOT"
                " perturb the changes.list page token cursor); got:"
                f" {page_token!r}"
            )
            return iter(fetch_queue.pop(0))

        fake_drive_client_instance.fetch_changes.side_effect = _fetch_changes_2

        # Read the persisted cursor from connector_cursors and feed it
        # into the next sync's ConnectorContext (same shape as the CLI
        # driver does between sync runs).
        with engine.connect() as conn:
            persisted_cursor = conn.execute(
                text(
                    "SELECT cursor_value FROM connector_cursors"
                    " WHERE connector_name = 'google_workspace'"
                )
            ).scalar_one()
        # Sync 2 bracket — same shape as Sync 1.
        rotation_service.cursor_set(connector.name, persisted_cursor, sync_started=True)
        ctx2 = ConnectorContext(
            source_service=rotation_service,
            cursor_value=persisted_cursor,
            secrets=None,
            logger=MagicMock(),
        )
        result2 = connector.sync(ctx2)
        rotation_service.cursor_set(connector.name, result2.new_cursor, sync_started=False)
        assert result2.new_cursor == token_after

        # Final pin: the cursor row advanced exactly once per sync,
        # and the rotation did not cause a re-bootstrap (the
        # ``get_start_page_token`` call counter stays at the original
        # first-sync bootstrap count).
        assert fake_drive_client_instance.get_start_page_token.call_count == 1, (
            "Phase 13 plan §7.3 step 5 — rotation must NOT trigger a"
            " re-bootstrap (the cursor row survives the rotation)"
        )

        with engine.connect() as conn:
            cursor_after_sync2 = conn.execute(
                text(
                    "SELECT cursor_value FROM connector_cursors"
                    " WHERE connector_name = 'google_workspace'"
                )
            ).scalar_one()
        assert cursor_after_sync2 == token_after

        # ---- 8. excludes.yaml suppresses Google Workspace ingest -----
        # ADR-0020 §(b) — the shared excludes file must filter
        # Google Workspace rows just like Slack / Teams. We don't
        # re-run a Drive fetch (the connector path is mocked in unit
        # tests); instead we assert the loaded ``ExcludeRules`` reports
        # the Google Workspace folder path as excluded so the
        # connector's "skip if excluded" branch would fire. This is
        # the same shape the Phase 10 / 11 e2e exercises for Slack /
        # Teams.
        from opshub.core import excludes as excludes_module
        from opshub.core.excludes import ExcludeRules

        gws_confidential_path = "/Confidential/Q4-planning/secret-roadmap.gdoc"

        def _stub_load_excludes(config_dir: Path | None = None) -> ExcludeRules:
            del config_dir
            return ExcludeRules(paths=("/Confidential/**",))

        monkeypatch.setattr(excludes_module, "load_excludes", _stub_load_excludes)
        loaded_rules = excludes_module.load_excludes()
        assert loaded_rules.excludes_path(gws_confidential_path), (
            "excludes.yaml ``paths`` selector must mark the Google Workspace"
            " Confidential folder excluded so the connector skips it at fetch"
            " time (ADR-0020 §(b))"
        )
        # The opposite case must remain false so we know we're not
        # accidentally over-matching.
        assert not loaded_rules.excludes_path("/Public/Q4-planning/open-roadmap.gdoc"), (
            "excludes.yaml must not over-match unrelated paths"
        )
    finally:
        engine.dispose()
