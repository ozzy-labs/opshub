"""Tests for :mod:`opshub.vectors.factory` (Phase 4 step A5).

The factory module itself is lightweight (no SDK imports at module
level), so its tests can run in the base ``[dev,vector]`` CI lane.
Per-backend tests that materialise the concrete embedder gate on the
matching extras via :func:`pytest.importorskip`:

- ``test_build_embedder_local_*`` requires ``sentence_transformers``
  (``[local-embedding]``).
- ``test_build_embedder_openai_*`` requires ``openai``
  (``[api-embedding-openai]``).
- ``test_build_embedder_voyage_*`` requires ``voyageai``
  (``[api-embedding-voyage]``).
- ``test_build_vector_store_*`` requires ``sqlite_vec`` (``[vector]``,
  already in the ``just ci`` lane).

The disabled-path / unknown-backend / sentinel-properties tests have
no optional-extras gates: they exercise only the factory module and
stdlib code, so they run on every CI invocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from opshub.core.config import EmbeddingSettings, OpsHubSettings
from opshub.core.errors import ConfigError
from opshub.vectors import Embedder
from opshub.vectors.factory import NoOpEmbedder, build_embedder, build_vector_store

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def _make_settings(**embedding_kwargs: Any) -> OpsHubSettings:
    """Build an :class:`OpsHubSettings` with a specific embedding section.

    Keeps each test focused on the embedding fields under test without
    repeating the boilerplate of constructing the full settings tree.
    """
    return OpsHubSettings(embedding=EmbeddingSettings(**embedding_kwargs))


# ---- disabled backend / NoOpEmbedder -------------------------------------


def test_build_embedder_disabled_returns_noop() -> None:
    settings = _make_settings(backend="disabled")
    embedder = build_embedder(settings)
    assert isinstance(embedder, NoOpEmbedder)


def test_noop_embedder_properties_have_sentinels() -> None:
    """``NoOpEmbedder`` exposes sentinel identity so the CLI / status
    commands can introspect it like any other :class:`Embedder`."""
    embedder = NoOpEmbedder()
    assert embedder.model_id == "disabled"
    assert embedder.model_version == "v0"
    assert embedder.dim == 0


def test_noop_embedder_is_structural_embedder() -> None:
    """The Protocol is ``@runtime_checkable``; the sentinel must satisfy it."""
    assert isinstance(NoOpEmbedder(), Embedder)


def test_noop_embedder_embed_raises_config_error_with_rebuild_hint() -> None:
    embedder = NoOpEmbedder()
    with pytest.raises(ConfigError) as exc_info:
        embedder.embed(["any text"])
    message = str(exc_info.value)
    # The error must point operators at the config knob and the
    # rebuild command, otherwise "embedding is disabled" is dead-end.
    assert "backend" in message
    assert "opshub embeddings rebuild" in message


def test_noop_embedder_embed_one_raises_config_error() -> None:
    embedder = NoOpEmbedder()
    with pytest.raises(ConfigError):
        embedder.embed_one("any text")


# ---- unknown backend -----------------------------------------------------


def test_build_embedder_unknown_backend_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a future Literal addition is not wired into the factory,
    operators get a ``ConfigError`` listing every supported backend
    instead of silently falling off the ``if/elif`` chain."""
    settings = _make_settings(backend="disabled")
    # Bypass the Pydantic Literal guard by mutating the resolved
    # field. This simulates "Literal grew a new option in
    # core/config.py but vectors/factory.py was not updated to match"
    # — the exact regression we want the test to catch.
    monkeypatch.setattr(settings.embedding, "backend", "unknown", raising=False)
    with pytest.raises(ConfigError) as exc_info:
        build_embedder(settings)
    message = str(exc_info.value)
    assert "unknown" in message
    # Every known literal should appear in the error so the operator
    # sees the supported set without grepping the source.
    assert "local" in message
    assert "openai" in message
    assert "voyage" in message
    assert "disabled" in message


# ---- factory module is lazy ----------------------------------------------


def test_factory_module_does_not_import_heavy_deps() -> None:
    """Importing :mod:`opshub.vectors.factory` must NOT pull
    ``sentence_transformers`` / ``openai`` / ``voyageai`` /
    ``sqlite_vec``. Those imports live inside each backend branch so
    the cold-start path stays fast (ADR-0001 §3, M6 guard).

    Heavy deps may already be in ``sys.modules`` if some other test
    imported them earlier (e.g. the SqliteVecStore tests pull in
    ``sqlite_vec``). The guard we care about is the *factory module's
    own globals* — those must not reference the heavy modules even
    after import.
    """
    from opshub.vectors import factory as factory_module

    factory_globals = set(vars(factory_module).keys())
    for heavy_name in ("sentence_transformers", "openai", "voyageai", "sqlite_vec"):
        assert heavy_name not in factory_globals, (
            f"factory module exposes {heavy_name!r}; lazy import discipline broken"
        )


# ---- local backend (gated) -----------------------------------------------


def test_build_embedder_local_returns_local_class_with_defaults() -> None:
    pytest.importorskip("sentence_transformers")
    from opshub.vectors.local_embedder import LocalSentenceTransformerEmbedder

    settings = _make_settings(backend="local")
    embedder = build_embedder(settings)
    assert isinstance(embedder, LocalSentenceTransformerEmbedder)
    # Default per factory: BAAI/bge-m3 / 1024.
    assert embedder.model_id == "BAAI/bge-m3"
    assert embedder.dim == 1024


def test_build_embedder_local_respects_config_overrides() -> None:
    pytest.importorskip("sentence_transformers")
    from opshub.vectors.local_embedder import LocalSentenceTransformerEmbedder

    settings = _make_settings(
        backend="local",
        model_id="some-other-model",
        dimensions=768,
    )
    embedder = build_embedder(settings)
    assert isinstance(embedder, LocalSentenceTransformerEmbedder)
    assert embedder.model_id == "some-other-model"
    assert embedder.dim == 768


# ---- openai backend (gated) ----------------------------------------------


def test_build_embedder_openai_returns_openai_class_with_defaults() -> None:
    pytest.importorskip("openai")
    from opshub.vectors.openai_embedder import OpenAIEmbedder

    settings = _make_settings(backend="openai")
    embedder = build_embedder(settings)
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model_id == "text-embedding-3-small"
    assert embedder.dim == 1536


def test_build_embedder_openai_respects_config_overrides() -> None:
    pytest.importorskip("openai")
    from opshub.vectors.openai_embedder import OpenAIEmbedder

    settings = _make_settings(
        backend="openai",
        model_id="text-embedding-3-large",
        dimensions=3072,
    )
    embedder = build_embedder(settings)
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model_id == "text-embedding-3-large"
    assert embedder.dim == 3072


# ---- voyage backend (gated) ----------------------------------------------


def test_build_embedder_voyage_returns_voyage_class_with_defaults() -> None:
    pytest.importorskip("voyageai")
    from opshub.vectors.voyage_embedder import VoyageEmbedder

    settings = _make_settings(backend="voyage")
    embedder = build_embedder(settings)
    assert isinstance(embedder, VoyageEmbedder)
    assert embedder.model_id == "voyage-3"
    assert embedder.dim == 1024


def test_build_embedder_voyage_respects_config_overrides() -> None:
    pytest.importorskip("voyageai")
    from opshub.vectors.voyage_embedder import VoyageEmbedder

    settings = _make_settings(
        backend="voyage",
        model_id="voyage-large-2",
        dimensions=1536,
    )
    embedder = build_embedder(settings)
    assert isinstance(embedder, VoyageEmbedder)
    assert embedder.model_id == "voyage-large-2"
    assert embedder.dim == 1536


# ---- vector store --------------------------------------------------------


def test_build_vector_store_returns_sqlite_vec_store(tmp_path: Any) -> None:
    pytest.importorskip("sqlite_vec")
    from opshub.db.engine import create_engine_for_sqlite
    from opshub.vectors.sqlite_vec_store import SqliteVecStore

    db_path = tmp_path / "test.sqlite"
    engine: Engine = create_engine_for_sqlite(db_path)
    try:
        settings = _make_settings(backend="disabled")
        store = build_vector_store(settings, engine)
        assert isinstance(store, SqliteVecStore)
    finally:
        engine.dispose()
