"""Phase 6 Ollama-backed lifecycle integration test (optional).

Verifies that ``[llm] backend = "ollama"`` plus a fully mocked
``httpx`` transport runs the propose generate flow end-to-end without
ever touching a real Ollama daemon. The mocked transport answers the
two endpoints the client actually calls:

1. ``GET /api/tags`` — the construction-time daemon probe
   (ADR-0016 §決定 (h) fail-fast).
2. ``POST /v1/chat/completions`` — the OpenAI-compat structured-output
   endpoint that returns a ``tool_calls`` entry whose
   ``function.arguments`` JSON satisfies the
   :class:`ProposalCandidatesSchema`.

This pins the contract that all three LLM backends (Anthropic /
OpenAI / Ollama) produce equivalent propose flows. The shape mirrors
:mod:`tests.unit.llm.test_ollama_client` — ``httpx.Client`` is patched
so every constructed instance routes through the mock transport.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# Skip when sqlite-vec is not installed (matches the rest of the
# Phase 6 integration suite).
pytest.importorskip("sqlite_vec")
pytest.importorskip(
    "httpx",
    reason="Phase 6 Ollama lifecycle test requires the 'llm-ollama' extras",
)

import httpx
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import events_table
from opshub.projections.proposals import proposals_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Embedder stub (copied from the other Phase 6 integration modules so
# refactoring one cannot break another)
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase6-ollama-embedder"

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
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


# ---------------------------------------------------------------------------
# Ollama daemon mock
# ---------------------------------------------------------------------------


def _tags_response() -> httpx.Response:
    """Daemon probe response (``GET /api/tags``)."""
    return httpx.Response(200, json={"models": [{"name": "llama3.2:3b"}]})


def _structured_response(arguments: str, *, tool_name: str) -> httpx.Response:
    """OpenAI-shape response carrying a single ``tool_calls`` entry."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 40,
                "total_tokens": 70,
            },
        },
    )


def _install_mock_httpx(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    """Replace ``httpx.Client`` so every instance uses ``MockTransport``.

    Returns the recording list so the test can introspect requests.
    """
    real_client_cls = httpx.Client
    requests: list[httpx.Request] = []

    def _recorded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response: httpx.Response = handler(request)
        return response

    def _factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client_cls(  # pyright: ignore[reportUnknownVariableType]
            *args,
            transport=httpx.MockTransport(_recorded),
            **kwargs,
        )

    monkeypatch.setattr("httpx.Client", _factory)
    return requests


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# End-to-end happy path via the Ollama backend
# ---------------------------------------------------------------------------


def test_propose_generate_via_ollama_backend_through_cli(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``opshub propose generate`` works with ``backend = "ollama"``.

    The mock transport answers both the daemon probe and the structured
    completion call. The expected end-state mirrors
    :mod:`test_phase6_lifecycle` (one ``proposals`` row +
    ``ProposalRequested`` + ``ProposalGenerated``) but the ``model_id``
    reflects the Ollama default (``llama3.2:3b``).
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "ollama")
    _install_stub_embedder(monkeypatch)

    # The Pydantic schema serialisation is opaque from the test's
    # perspective; we construct the tool_calls.arguments JSON by hand
    # so the response satisfies :class:`ProposalCandidatesSchema`.
    arguments_json = (
        '{"candidates": ['
        '{"kind": "task", "schema_version": "v1", '
        '"title": "ollama-suggested follow up", '
        '"body": "tighten phase 6 docs"}'
        "]}"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/tags":
            return _tags_response()
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            # ``pydantic_to_tool_schema`` mangles the model name into a
            # tool name; the Ollama client passes ``tool_choice`` with
            # the same name, so we can simply read it back from the
            # request body to keep the mock-side parser independent
            # from the SSOT helper.
            import json as _json

            sent = _json.loads(request.content)
            tool_name = sent["tool_choice"]["function"]["name"]
            return _structured_response(arguments_json, tool_name=tool_name)
        raise AssertionError(f"unexpected request to mock Ollama: {request.method} {request.url}")

    requests = _install_mock_httpx(monkeypatch, _handler)

    # Seed one task so the recall path returns at least one hit (the
    # service still proceeds with an empty hit list, but a non-empty
    # seed exercises the prompt-build path).
    code, _, _ = _invoke(["task", "create", "ollama seed task"])
    assert code == 0
    code, _, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0

    code, generate_out, stderr = _invoke(["propose", "generate", "ollama-backed propose flow"])
    assert code == 0, stderr or generate_out
    assert "[0] task:" in generate_out, generate_out
    assert "ollama-suggested follow up" in generate_out, generate_out

    # The Ollama client makes at least 2 requests: one daemon probe at
    # construction time + one chat-completions call. (`_install_mock_httpx`
    # records every attempted request, so a regression that adds an
    # extra network call would surface here.)
    assert any(r.url.path == "/api/tags" for r in requests), requests
    assert any(r.url.path == "/v1/chat/completions" for r in requests), requests
    # The mock never sees a non-localhost URL: the base URL was the
    # Ollama default, and the mock transport rejects unexpected paths.
    for req in requests:
        assert req.url.host in {"localhost", "127.0.0.1"}, req.url

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(proposals_table)).all()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row.topic == "ollama-backed propose flow"
        # ``model_id`` reflects the Ollama config.
        assert row.model_id == "llama3.2:3b"
        assert list(row.candidate_states) == ["pending"]

        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.requested")
            ).all()
            generated = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.generated")
            ).all()
        assert len(requested) == 1
        assert len(generated) == 1
        assert requested[0].aggregate_id == generated[0].aggregate_id
    finally:
        engine.dispose()


# Re-export ``pytest`` so static analysers see this module is a pytest test.
_ = pytest
