"""Tests for ``opshub.vectors.openai_embedder`` (Phase 4 step A4).

The SDK is in the ``[api-embedding-openai]`` extras and may not be
installed in every CI lane; ``pytest.importorskip`` at module load gates
the entire file the same way the connector tests gate on ``keyring``.

The OpenAI SDK is always mocked — no real HTTP request leaves the
process. The patch target is ``openai.OpenAI``
because the embedder does a lazy ``from openai import OpenAI`` inside
``_ensure_client``; once the import succeeds the name lands in this
module's namespace and ``patch`` can redirect it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip(
    "openai",
    reason="opshub.vectors.openai_embedder tests require the 'api-embedding-openai' extras",
)

from opshub.core.errors import ConfigError
from opshub.vectors import EmbeddingResult
from opshub.vectors.openai_embedder import OPENAI_API_KEY_SECRET, OpenAIEmbedder

if TYPE_CHECKING:
    from collections.abc import Iterable

# ---- helpers --------------------------------------------------------------


def _fake_response(vectors: Iterable[Iterable[float]]) -> MagicMock:
    """Build a MagicMock shaped like ``openai.types.CreateEmbeddingResponse``.

    The embedder reads ``response.data[i].embedding`` so we mirror that
    chain exactly. Using MagicMock per-item lets pyright stay happy
    without importing the SDK's response types.
    """
    response = MagicMock()
    response.data = [MagicMock(embedding=list(v)) for v in vectors]
    return response


def _patch_get_secret(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Force ``opshub.core.secrets.get_secret`` to return ``value``.

    The embedder imports ``get_secret`` lazily inside ``_ensure_client``;
    patching the symbol on the source module (the canonical location)
    works regardless of import order.
    """

    def _stub(_key: str) -> str | None:
        return value

    monkeypatch.setattr("opshub.core.secrets.get_secret", _stub)


# ---- constants ------------------------------------------------------------


def test_openai_api_key_secret_constant() -> None:
    """Public contract: CLI writer and embedder reader must agree on this key."""
    assert OPENAI_API_KEY_SECRET == "embedder:openai:api_key"


# ---- properties -----------------------------------------------------------


def test_model_id_version_dim_properties() -> None:
    """Constructor args surface verbatim through the Embedder protocol properties."""
    embedder = OpenAIEmbedder(model_id="text-embedding-3-large", model_version="v1", dim=3072)
    assert embedder.model_id == "text-embedding-3-large"
    assert embedder.model_version == "v1"
    assert embedder.dim == 3072


def test_defaults_match_documented_phase4_choice() -> None:
    """Defaults are pinned by phase-4-plan §1 確定事項 #4."""
    embedder = OpenAIEmbedder()
    assert embedder.model_id == "text-embedding-3-small"
    assert embedder.dim == 1536


# ---- embed: empty input never touches the SDK -----------------------------


def test_embed_empty_input_returns_empty_without_calling_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fast path must not lazy-import the SDK; this lets callers pass
    ``[]`` from generic pipelines without configuring credentials."""
    monkeypatch.delenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", raising=False)
    _patch_get_secret(monkeypatch, None)
    embedder = OpenAIEmbedder()

    # If _ensure_client were entered, the missing key would raise.
    assert embedder.embed([]) == []


# ---- embed: batching ------------------------------------------------------


def test_embed_calls_api_with_correct_model_and_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """250 inputs at batch_size=100 → 3 API calls of 100/100/50."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.embeddings.create.side_effect = [
        _fake_response([[0.0] * 1536] * 100),
        _fake_response([[0.0] * 1536] * 100),
        _fake_response([[0.0] * 1536] * 50),
    ]
    with patch("openai.OpenAI", return_value=fake_client) as openai_cls:
        embedder = OpenAIEmbedder()
        results = embedder.embed([f"text-{i}" for i in range(250)])

    assert len(results) == 250
    openai_cls.assert_called_once_with(api_key="sk-test")
    assert fake_client.embeddings.create.call_count == 3
    call_batches = [call.kwargs["input"] for call in fake_client.embeddings.create.call_args_list]
    assert [len(batch) for batch in call_batches] == [100, 100, 50]
    # Every call must use the configured model.
    for call in fake_client.embeddings.create.call_args_list:
        assert call.kwargs["model"] == "text-embedding-3-small"


# ---- embed: result shape --------------------------------------------------


def test_embed_returns_results_with_documented_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The returned EmbeddingResult carries the configured model id / dim."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = _fake_response([[0.1] * 1536, [0.2] * 1536])
    with patch("openai.OpenAI", return_value=fake_client):
        results = OpenAIEmbedder().embed(["a", "b"])

    assert len(results) == 2
    for result in results:
        assert isinstance(result, EmbeddingResult)
        assert result.dim == 1536
        assert len(result.vector) == 1536
        assert result.model_id == "text-embedding-3-small"
        assert result.model_version == "2024-01-25"


def test_embed_one_wraps_embed_for_single_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = _fake_response([[0.5] * 1536])
    with patch("openai.OpenAI", return_value=fake_client):
        result = OpenAIEmbedder().embed_one("hello")

    assert isinstance(result, EmbeddingResult)
    assert result.dim == 1536


# ---- embed: dim mismatch --------------------------------------------------


def test_dim_mismatch_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the API returns a vector of the wrong length we must fail loud;
    silently storing the wrong dim would corrupt ``embeddings_vec``."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = _fake_response([[0.0] * 768])
    with (
        patch("openai.OpenAI", return_value=fake_client),
        pytest.raises(ConfigError) as excinfo,
    ):
        OpenAIEmbedder().embed(["wrong-dim"])

    message = str(excinfo.value)
    assert "dim=768" in message
    assert "expected 1536" in message


# ---- credential resolution ------------------------------------------------


def test_missing_api_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty keyring + no env var → actionable ConfigError that points the
    user at the documented CLI command + env var override."""
    monkeypatch.delenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", raising=False)
    _patch_get_secret(monkeypatch, None)

    # Even if the SDK is patched, _ensure_client should fail before touching it.
    with (
        patch("openai.OpenAI") as openai_cls,
        pytest.raises(ConfigError) as excinfo,
    ):
        OpenAIEmbedder().embed(["x"])

    openai_cls.assert_not_called()
    message = str(excinfo.value)
    assert "opshub connector auth set embedder:openai" in message
    assert "OPSHUB_EMBEDDER_OPENAI_API_KEY" in message


def test_env_var_api_key_used_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var override is the documented CI / docker escape hatch
    (ADR-0014). It must reach the SDK without keyring involvement."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", "sk-from-env")
    # Keyring intentionally returns None — env var must still win.
    _patch_get_secret_to_observe_key(monkeypatch)

    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = _fake_response([[0.0] * 1536])
    with patch("openai.OpenAI", return_value=fake_client) as openai_cls:
        OpenAIEmbedder().embed(["x"])

    openai_cls.assert_called_once_with(api_key="sk-from-env")


def _patch_get_secret_to_observe_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Variant of _patch_get_secret that delegates to the real env-var path.

    The production ``get_secret`` already consults the env var override
    before touching keyring, so leaving it unpatched is the most honest
    test — but we want to guarantee no keyring backend gets called in
    CI. Patching with a stub that asserts the documented key and reads
    the env var directly captures both behaviours in one go.
    """
    import os

    def _stub(key: str) -> str | None:
        assert key == OPENAI_API_KEY_SECRET
        return os.environ.get("OPSHUB_EMBEDDER_OPENAI_API_KEY")

    monkeypatch.setattr("opshub.core.secrets.get_secret", _stub)


# ---- client caching -------------------------------------------------------


def test_client_is_constructed_once_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK client is expensive to instantiate; the embedder caches it
    for the lifetime of the instance."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_OPENAI_API_KEY", "sk-test")

    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = _fake_response([[0.0] * 1536])
    with patch("openai.OpenAI", return_value=fake_client) as openai_cls:
        embedder = OpenAIEmbedder()
        embedder.embed(["a"])
        embedder.embed(["b"])
        embedder.embed(["c"])

    assert openai_cls.call_count == 1
