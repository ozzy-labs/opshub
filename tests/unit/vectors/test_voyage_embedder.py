"""Tests for ``opshub.vectors.voyage_embedder`` (Phase 4 step A4).

The SDK is in the ``[api-embedding-voyage]`` extras; ``pytest.importorskip``
gates the entire file so CI without the extras skips cleanly.

No real HTTP request leaves the process — ``voyageai.Client`` is patched
on the source module so the lazy ``__import__("voyageai")`` inside the
embedder picks up the mock from ``sys.modules``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip(
    "voyageai",
    reason="opshub.vectors.voyage_embedder tests require the 'api-embedding-voyage' extras",
)

from opshub.core.errors import ConfigError
from opshub.vectors import EmbeddingResult
from opshub.vectors.voyage_embedder import VOYAGE_API_KEY_SECRET, VoyageEmbedder

if TYPE_CHECKING:
    from collections.abc import Iterable

# ---- helpers --------------------------------------------------------------


def _fake_response(vectors: Iterable[Iterable[float]]) -> MagicMock:
    """Build a MagicMock shaped like ``voyageai.api_resources.EmbeddingsObject``.

    The embedder reads ``response.embeddings`` (a ``list[list[float]]``)
    so we mirror exactly that.
    """
    response = MagicMock()
    response.embeddings = [list(v) for v in vectors]
    return response


def _patch_get_secret(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Force ``opshub.core.secrets.get_secret`` to return ``value``."""

    def _stub(_key: str) -> str | None:
        return value

    monkeypatch.setattr("opshub.core.secrets.get_secret", _stub)


# ---- constants ------------------------------------------------------------


def test_voyage_api_key_secret_constant() -> None:
    """Public contract: CLI writer and embedder reader must agree on this key."""
    assert VOYAGE_API_KEY_SECRET == "embedder:voyage:api_key"


# ---- properties -----------------------------------------------------------


def test_model_id_version_dim_properties() -> None:
    """Constructor args surface verbatim through the Embedder protocol properties."""
    embedder = VoyageEmbedder(model_id="voyage-3-large", model_version="v1", dim=2048)
    assert embedder.model_id == "voyage-3-large"
    assert embedder.model_version == "v1"
    assert embedder.dim == 2048


def test_defaults_match_documented_phase4_choice() -> None:
    """Defaults are pinned by phase-4-plan §1 確定事項 #4."""
    embedder = VoyageEmbedder()
    assert embedder.model_id == "voyage-3"
    assert embedder.dim == 1024


# ---- embed: empty input never touches the SDK -----------------------------


def test_embed_empty_input_returns_empty_without_calling_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty input must short-circuit before credential resolution so generic
    pipelines can pass ``[]`` without configuring the API key."""
    monkeypatch.delenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", raising=False)
    _patch_get_secret(monkeypatch, None)
    embedder = VoyageEmbedder()

    assert embedder.embed([]) == []


# ---- embed: batching ------------------------------------------------------


def test_embed_calls_api_with_correct_model_and_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """300 inputs at batch_size=128 → 3 API calls of 128/128/44."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", "vk-test")

    fake_client = MagicMock()
    fake_client.embed.side_effect = [
        _fake_response([[0.0] * 1024] * 128),
        _fake_response([[0.0] * 1024] * 128),
        _fake_response([[0.0] * 1024] * 44),
    ]
    with patch("voyageai.Client", return_value=fake_client) as voyage_cls:
        embedder = VoyageEmbedder()
        results = embedder.embed([f"text-{i}" for i in range(300)])

    assert len(results) == 300
    voyage_cls.assert_called_once_with(api_key="vk-test")
    assert fake_client.embed.call_count == 3
    call_batches = [call.args[0] for call in fake_client.embed.call_args_list]
    assert [len(batch) for batch in call_batches] == [128, 128, 44]
    for call in fake_client.embed.call_args_list:
        assert call.kwargs["model"] == "voyage-3"


# ---- embed: result shape --------------------------------------------------


def test_embed_returns_results_with_documented_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The returned EmbeddingResult carries the configured model id / dim."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", "vk-test")

    fake_client = MagicMock()
    fake_client.embed.return_value = _fake_response([[0.1] * 1024, [0.2] * 1024])
    with patch("voyageai.Client", return_value=fake_client):
        results = VoyageEmbedder().embed(["a", "b"])

    assert len(results) == 2
    for result in results:
        assert isinstance(result, EmbeddingResult)
        assert result.dim == 1024
        assert len(result.vector) == 1024
        assert result.model_id == "voyage-3"
        assert result.model_version == "2024-09-18"


def test_embed_one_wraps_embed_for_single_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", "vk-test")

    fake_client = MagicMock()
    fake_client.embed.return_value = _fake_response([[0.5] * 1024])
    with patch("voyageai.Client", return_value=fake_client):
        result = VoyageEmbedder().embed_one("hello")

    assert isinstance(result, EmbeddingResult)
    assert result.dim == 1024


# ---- embed: dim mismatch --------------------------------------------------


def test_dim_mismatch_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong-length vector must surface as ConfigError so we never store
    a corrupt row into ``embeddings_vec`` (which is dim-pinned)."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", "vk-test")

    fake_client = MagicMock()
    fake_client.embed.return_value = _fake_response([[0.0] * 768])
    with (
        patch("voyageai.Client", return_value=fake_client),
        pytest.raises(ConfigError) as excinfo,
    ):
        VoyageEmbedder().embed(["wrong-dim"])

    message = str(excinfo.value)
    assert "dim=768" in message
    assert "expected 1024" in message


# ---- credential resolution ------------------------------------------------


def test_missing_api_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty keyring + no env var → actionable ConfigError that mentions
    both the CLI command and the env var override."""
    monkeypatch.delenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", raising=False)
    _patch_get_secret(monkeypatch, None)

    with (
        patch("voyageai.Client") as voyage_cls,
        pytest.raises(ConfigError) as excinfo,
    ):
        VoyageEmbedder().embed(["x"])

    voyage_cls.assert_not_called()
    message = str(excinfo.value)
    assert "opshub connector auth set embedder:voyage" in message
    assert "OPSHUB_EMBEDDER_VOYAGE_API_KEY" in message


def test_env_var_api_key_used_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var override (ADR-0014) must reach the SDK constructor
    without keyring involvement."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", "vk-from-env")
    _patch_get_secret_to_observe_key(monkeypatch)

    fake_client = MagicMock()
    fake_client.embed.return_value = _fake_response([[0.0] * 1024])
    with patch("voyageai.Client", return_value=fake_client) as voyage_cls:
        VoyageEmbedder().embed(["x"])

    voyage_cls.assert_called_once_with(api_key="vk-from-env")


def _patch_get_secret_to_observe_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``get_secret`` with a stub that pins the documented key and
    delegates to the env var override, guaranteeing no keyring backend is
    touched in CI."""
    import os

    def _stub(key: str) -> str | None:
        assert key == VOYAGE_API_KEY_SECRET
        return os.environ.get("OPSHUB_EMBEDDER_VOYAGE_API_KEY")

    monkeypatch.setattr("opshub.core.secrets.get_secret", _stub)


# ---- client caching -------------------------------------------------------


def test_client_is_constructed_once_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK client construction is cached for the embedder's lifetime."""
    monkeypatch.setenv("OPSHUB_EMBEDDER_VOYAGE_API_KEY", "vk-test")

    fake_client = MagicMock()
    fake_client.embed.return_value = _fake_response([[0.0] * 1024])
    with patch("voyageai.Client", return_value=fake_client) as voyage_cls:
        embedder = VoyageEmbedder()
        embedder.embed(["a"])
        embedder.embed(["b"])
        embedder.embed(["c"])

    assert voyage_cls.call_count == 1
