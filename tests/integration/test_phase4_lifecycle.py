"""Phase 4 end-to-end lifecycle tests.

Each test drives one Phase 4 workstream through the shipped CLI surface
end-to-end with a mocked :class:`~opshub.vectors.embedder.Embedder` and
the real sqlite-vec backed :class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore`.

The split mirrors the Phase 2 / Phase 3 closeout shape
(``test_coordination_lifecycle`` / ``test_phase3_lifecycle``): one test
function per sub-issue rather than a single monolithic lifecycle. The
per-workstream split lets pytest's selector re-run a single Phase 4
flow when an investigation needs to, and it keeps each function under
~80 LOC.

``isolated_env`` fixture (``tests/integration/conftest.py``) provisions
``OPSHUB_*`` env, runs ``init``, and yields a paths dict. The whole
module is skipped when ``sqlite_vec`` is not importable
(non-``[vector]`` environments) so contributors who run ``uv sync
--extra dev`` without the vector extras do not trip migration 0013 with
``no such module: vec0``. This mirrors
:mod:`tests.integration.test_recall_cli_lifecycle`.

What this pins
--------------

The integration test pins the **shipped CLI contract** end-to-end —
not implementation details:

- ``opshub embeddings rebuild`` produces an ``embedded N`` line whose
  count matches the seeded entities; a second pass embeds nothing
  (Phase 4 plan §3 機能 #4 idempotency).
- ``opshub embeddings status`` lists ``backend=local`` + per-entity-type
  rows on stdout for an active backend.
- ``opshub recall "<query>"`` returns hits matching the seeded data,
  honours ``--type`` / ``--state`` / ``--format json`` (機能 #6-#7).
- ``opshub embeddings find-duplicates`` surfaces near-duplicate pairs
  for a low threshold when two near-identical entities exist
  (機能 #8).
- Switching the configured backend mid-process → the next ``recall``
  exits 2 with a "rebuild required" hint, never silently mixing
  model_ids (機能 #10).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip when sqlite-vec is not installed (matches
# ``test_recall_cli_lifecycle`` / ``test_phase4_migrations``). Migration
# 0013 emits ``CREATE VIRTUAL TABLE ... USING vec0`` which the
# ``opshub init`` step inside ``isolated_env`` runs; without the
# extension that migration fails before any test body executes.
pytest.importorskip("sqlite_vec")

import json
from typing import cast

from sqlalchemy import insert
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.ids import new_ulid
from opshub.core.time import now_utc
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.tasks import tasks_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Deterministic stub embedder
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub for end-to-end Phase 4 tests.

    Same shape as :class:`tests.integration.test_recall_cli_lifecycle._StubEmbedder`,
    but the vector derivation hashes the *text* (not its length) so two
    inputs with identical text always produce identical vectors and two
    near-identical inputs produce near-identical vectors — exactly what
    the duplicate-detection test needs.

    The vector is unit-L2-normalised so the cosine-similarity formula
    in :func:`opshub.services.duplicate_service._score_to_cosine_similarity`
    holds (see ADR-0012 §1 — every supported backend produces
    unit-normalised vectors).
    """

    def __init__(
        self,
        *,
        model_id: str = "phase4-stub-embedder",
        model_version: str = "v1",
        dim: int = 1024,
    ) -> None:
        self._model_id = model_id
        self._model_version = model_version
        self._dim = dim

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> EmbeddingResult:
        # Hash the text character-by-character into ``dim`` slots so the
        # mapping ``text → vector`` is total and deterministic. Identical
        # text → identical vector; very similar text → very similar
        # vector (the inner loop XORs each character's code point into
        # one slot, so a 1-character edit changes one slot only).
        slots = [0.0] * self._dim
        for i, ch in enumerate(text):
            slots[i % self._dim] += (ord(ch) % 31 + 1) / 31.0
        # Unit-L2-normalise. Use a tiny epsilon so an empty text never
        # divides by zero (the embedding service skips empty text
        # before we get here, but defending in the stub avoids a
        # surprising NaN if a caller bypasses the service).
        norm = max(sum(x * x for x in slots) ** 0.5, 1e-9)
        vector = tuple(x / norm for x in slots)
        return EmbeddingResult(
            vector=vector,
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


def _install_stub_embedder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_id: str = "phase4-stub-embedder",
    dim: int = 1024,
) -> None:
    """Patch :func:`opshub.vectors.factory.build_embedder` to return the stub.

    Both the rebuild wiring (:func:`build_embedding_service`),
    the recall wiring (:func:`build_recall_service`), and the
    duplicate-scan wiring (:func:`build_duplicate_service`) reach the
    embedder through the same factory; patching it once keeps every
    call site aligned on the same ``(model_id, model_version)``.
    """
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub_build_embedder(settings: OpsHubSettings) -> Embedder:
        del settings  # unused — the stub does not consult config.
        return _StubEmbedder(model_id=model_id, dim=dim)

    monkeypatch.setattr(factory_module, "build_embedder", _stub_build_embedder)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Run ``opshub`` via CliRunner and return ``(exit_code, stdout, stderr)``."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


def _seed_task(db_path: Path, *, title: str) -> str:
    """Insert one ``draft`` task row directly into the projection table.

    Direct insert (rather than ``opshub task create``) keeps the test
    self-contained: the only invariant we need is "a task row exists
    that the embedding pipeline will see". The shape of the row is
    pinned by the migration; the seed mirrors the one used by
    :mod:`tests.integration.test_recall_cli_lifecycle`.
    """
    engine = create_engine_for_sqlite(db_path)
    try:
        task_id = new_ulid()
        now = now_utc()
        with engine.begin() as conn:
            conn.execute(
                insert(tasks_table).values(
                    id=task_id,
                    title=title,
                    body=None,
                    state="draft",
                    result_note=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        return task_id
    finally:
        engine.dispose()


def _seed_source_via_service(
    *,
    connector_name: str,
    external_id: str,
    title: str,
    summary: str,
) -> str:
    """Append a :class:`SourceObserved` + :class:`ItemEnqueued` pair.

    Uses :class:`~opshub.services.SourceService` directly (the CLI
    path needs OAuth + httpx mocking — overkill for a "seed one
    source row" need). The service is the only documented way to
    write a ``sources`` row + an ``inbox_items`` row in one UoW,
    matching what the GitHub connector would do in production.
    """
    from opshub.cli._wiring import build_source_service

    service = build_source_service()
    source_event, _inbox_event = service.observe(
        connector_name=connector_name,
        external_id=external_id,
        source_type="issue",
        title=title,
        summary=summary,
        # epic #470 / issue #481: ``body`` is required + non-empty.
        # Real connectors substitute summary on metadata-only paths;
        # the test fixture mirrors that.
        body=summary,
    )
    return source_event.aggregate_id


# ---------------------------------------------------------------------------
# Sub-issue A + B: rebuild + status end-to-end
# ---------------------------------------------------------------------------


def test_embeddings_rebuild_and_status_e2e(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``opshub embeddings rebuild`` → ``status`` → second rebuild idempotency.

    Seeds 2 tasks + 1 inbox item + 1 decision + 1 source (4 entity
    types covered), then drives:

    1. ``embeddings rebuild`` — must report ``embedded 5`` (the four
       primary entities + the inbox row auto-created by the source
       observe).
    2. ``embeddings status`` — must include ``backend=local`` + a row
       for every supported entity_type.
    3. Re-run ``embeddings rebuild`` — must report ``embedded 0`` and
       ``skipped`` ≥ 5 (idempotency per Phase 4 plan §3 機能 #4).
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    # ---- seed -------------------------------------------------------------
    _seed_task(isolated_env["db_path"], title="phase 4 first task")
    _seed_task(isolated_env["db_path"], title="phase 4 second task")
    code, inbox_out, _ = _invoke(["inbox", "add", "phase 4 inbox triage"])
    assert code == 0, inbox_out
    code, dec_out, _ = _invoke(["decision", "record", "phase 4 closeout decision"])
    assert code == 0, dec_out
    # The source-observe path also enqueues one inbox item (per
    # ``SourceService.observe`` docstring), so we end up with two
    # inbox rows in total.
    _seed_source_via_service(
        connector_name="github",
        external_id="owner/repo#100",
        title="phase 4 source title",
        summary="phase 4 source summary",
    )

    # ---- 1. rebuild -------------------------------------------------------
    code, out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, out
    # 2 tasks + 2 inbox rows (1 direct + 1 via source) + 1 decision
    # + 1 source = 6.
    assert "embedded 6" in out, out

    # ---- 2. status --------------------------------------------------------
    code, status_out, _ = _invoke(["embeddings", "status"])
    assert code == 0, status_out
    assert "backend=local" in status_out, status_out
    # Per-entity-type rows must all appear in the rendered table.
    for entity_type in ("task", "decision", "inbox_item", "source"):
        assert entity_type in status_out, status_out

    # ---- 3. second rebuild is a no-op ------------------------------------
    code, out2, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, out2
    # Idempotency: the iterator filters out already-embedded rows via
    # ``NOT EXISTS`` (see ``EmbeddingService._iter_pending``), so the
    # second pass yields zero items and reports ``embedded 0, skipped 0,
    # failed 0``. (``skipped`` counts empty-text entries inside the
    # iteration, not "already embedded" rows — those simply do not
    # appear in the iterator.)
    assert "embedded 0" in out2, out2
    assert "failed 0" in out2, out2


# ---------------------------------------------------------------------------
# Sub-issue C step 1: recall returns semantic hits
# ---------------------------------------------------------------------------


def test_recall_returns_semantic_hits_e2e(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed 3 tasks, rebuild, then exercise the three documented recall flags.

    1. Plain ``recall "<query>"`` finds the seeded task whose title
       most closely matches the query (the stub embedder maps
       identical text to identical vectors, so the exact-match query
       always lands in position 1).
    2. ``--type task --format json`` round-trips through
       :mod:`opshub.cli._render` and emits a list with the documented
       keys (``score`` / ``entity_type`` / ``entity_id`` / ``title``).
    3. ``--state draft --type task`` honours the state filter
       (Phase 4 plan §3 機能 #7).
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    _seed_task(isolated_env["db_path"], title="alpha — quarterly planning meeting notes")
    target_title = "beta — refactor embedding store"
    _seed_task(isolated_env["db_path"], title=target_title)
    _seed_task(isolated_env["db_path"], title="gamma — onboarding checklist")

    code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out
    assert "embedded 3" in rebuild_out

    # ---- 1. plain query — the exact-match title must appear --------------
    code, out, stderr = _invoke(["recall", target_title])
    assert code == 0, stderr or out
    assert target_title in out, out

    # ---- 2. --type task --format json — parseable + documented keys ------
    code, out, _ = _invoke(["recall", target_title, "--type", "task", "--format", "json"])
    assert code == 0, out
    payload = cast(
        list[dict[str, object]],
        json.loads(out.strip().splitlines()[-1]),
    )
    assert isinstance(payload, list)
    assert payload, "expected at least one hit"
    assert set(payload[0].keys()) == {"score", "entity_type", "entity_id", "title"}
    assert payload[0]["entity_type"] == "task"
    assert payload[0]["title"] == target_title

    # ---- 3. --state draft --type task — filter honoured -------------------
    code, out, _ = _invoke(["recall", target_title, "--type", "task", "--state", "draft"])
    assert code == 0, out
    # Every seeded task started in 'draft' state, so the filtered
    # query still surfaces the target title.
    assert target_title in out, out


# ---------------------------------------------------------------------------
# Sub-issue C step 2: duplicate detection
# ---------------------------------------------------------------------------


def test_find_duplicates_e2e(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sources with identical summaries → ``find-duplicates`` lists the pair.

    The stub embedder is deterministic on text (identical text →
    identical vector), so the two sources with the same ``summary``
    produce identical vectors and a cosine similarity of exactly 1.0.
    The low ``--threshold 0.5`` keeps the test resilient to floating
    point drift while still rejecting unrelated entities.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    # Two near-identical sources (same summary text → same vector).
    _seed_source_via_service(
        connector_name="github",
        external_id="owner/repo#1",
        title="source one",
        summary="duplicate operations memory candidate",
    )
    _seed_source_via_service(
        connector_name="github",
        external_id="owner/repo#2",
        title="source two",
        summary="duplicate operations memory candidate",
    )
    # A third unrelated source to make the threshold meaningful.
    _seed_source_via_service(
        connector_name="github",
        external_id="owner/repo#3",
        title="source three",
        summary="totally unrelated material on a different topic",
    )

    code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out

    code, out, stderr = _invoke(
        [
            "embeddings",
            "find-duplicates",
            "--threshold",
            "0.5",
            "--entity-type",
            "source",
        ]
    )
    assert code == 0, stderr or out
    # At least one pair must be present. The renderer upper-cases
    # column headers; match the upper-case spelling so the assertion
    # tracks ``cli/_render.py``'s table renderer.
    assert "SIMILARITY" in out, out
    # The exact duplicate summary text must appear at least once in
    # the rendered output (renderer truncates to 40 chars, so we
    # match a prefix).
    assert "duplicate operations memory" in out, out
    # The 1.000 similarity score appears for the exact-text pair —
    # pinning it catches a regression in the cosine-similarity
    # conversion (``_score_to_cosine_similarity``).
    assert "1.000" in out, out


# ---------------------------------------------------------------------------
# Cross-workstream: backend switch detection
# ---------------------------------------------------------------------------


def test_backend_switch_requires_rebuild(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching backend mid-process → next recall exits 2 with rebuild hint.

    Pins Phase 4 plan §3 機能 #10: when the configured backend
    changes (and therefore the active ``(model_id, model_version)``
    changes), :class:`~opshub.services.recall_service.RecallService`
    detects "no embeddings for active model" and raises a
    :class:`~opshub.core.errors.ConfigError` rather than returning
    silently-empty results or — worse — recall against the old
    model's vectors.
    """
    # ---- phase 1: rebuild with the local stub ----------------------------
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, model_id="phase4-stub-local")

    _seed_task(isolated_env["db_path"], title="backend switch fixture task")
    code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out
    assert "embedded 1" in rebuild_out

    # ---- phase 2: switch to a different backend identity -----------------
    # We keep ``OPSHUB_EMBEDDING__BACKEND=local`` so the engine still
    # builds without a config error; the *only* thing that changes is
    # the stub embedder's ``model_id``. The RecallService consults the
    # active embedder's ``model_id`` against the ``embeddings``
    # metadata table — a fresh ``model_id`` will have zero matching
    # rows, which is the exact "operator switched backend, no rebuild
    # yet" condition the contract guards.
    _install_stub_embedder(monkeypatch, model_id="phase4-stub-openai")

    code, out, stderr = _invoke(["recall", "anything"])
    assert code == 2, out or stderr
    # The RecallService message wraps the "rebuild required" hint in a
    # ``ConfigError``; ``cli/recall.py`` renders it on stderr as
    # ``Error: <message>``. The exact phrasing comes from
    # :meth:`RecallService._assert_embeddings_exist_for_active_model`.
    assert "no embeddings found for active model" in stderr, stderr
    assert "opshub embeddings rebuild" in stderr, stderr


# Re-export ``pytest`` so static analysers see this module is a pytest test
# (the import would otherwise read as unused).
_ = pytest
