"""Backend resolution for Embedder + VectorStore (Phase 4 step A5, ADR-0012 §3).

Two factory functions take :class:`~opshub.core.config.OpsHubSettings`
and return the configured concrete instance:

- :func:`build_embedder` returns the :class:`~opshub.vectors.Embedder`
  implementation selected by ``settings.embedding.backend`` (one of
  ``"local"`` / ``"openai"`` / ``"voyage"`` / ``"disabled"``).
- :func:`build_vector_store` returns the
  :class:`~opshub.vectors.VectorStore` implementation. Phase 4 MVP
  always returns :class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore`
  regardless of the embedding backend — vec0 table routing happens
  inside the store by dimension (see ADR-0012 §1 #5). Phase 4.x may
  add alternative stores; the factory shape is in place for that.

The ``"disabled"`` backend returns a :class:`NoOpEmbedder` whose
:meth:`~NoOpEmbedder.embed` raises :class:`~opshub.core.errors.ConfigError`
so the caller surfaces a clear "embedding is off, run rebuild" message
rather than silently producing no vectors.

Lazy-import discipline
----------------------

The module-level imports are intentionally lightweight. Each concrete
embedder / store is imported **inside** the branch that selects it, so
``import opshub.vectors.factory`` does not pull in
``sentence_transformers`` / ``openai`` / ``voyageai`` / ``sqlite_vec``.
This preserves the M6 cold-start budget (ADR-0001 §3) — only the
backend actually requested by the user pays the import cost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opshub.core.errors import ConfigError
from opshub.vectors.embedder import EmbeddingResult

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opshub.core.config import OpsHubSettings
    from opshub.vectors.embedder import Embedder
    from opshub.vectors.store import VectorStore

__all__ = ["NoOpEmbedder", "build_embedder", "build_vector_store"]


# Per-backend defaults applied when ``settings.embedding`` leaves the
# fields unset. Kept here (not in :class:`EmbeddingSettings`) so the
# config layer stays backend-agnostic — the choice of "what does
# backend=local mean" lives next to the factory branch that resolves it.
_LOCAL_DEFAULT_MODEL_ID = "BAAI/bge-m3"
_LOCAL_DEFAULT_DIM = 1024
_OPENAI_DEFAULT_MODEL_ID = "text-embedding-3-small"
_OPENAI_DEFAULT_DIM = 1536
_VOYAGE_DEFAULT_MODEL_ID = "voyage-3"
_VOYAGE_DEFAULT_DIM = 1024


class NoOpEmbedder:
    """Embedder returned when ``settings.embedding.backend == "disabled"``.

    The identity properties return sentinel values so the rest of the
    system can introspect a well-formed :class:`~opshub.vectors.Embedder`
    object (e.g. ``opshub embeddings status`` listing the configured
    backend) without special-casing the disabled state.

    :meth:`embed` raises :class:`~opshub.core.errors.ConfigError`
    instead of returning an empty list so callers cannot accidentally
    persist zero-vector rows. The error message documents how to flip
    the backend on, mirroring ADR-0012 §3's "fail loud" guidance for
    config drift.
    """

    @property
    def model_id(self) -> str:
        return "disabled"

    @property
    def model_version(self) -> str:
        return "v0"

    @property
    def dim(self) -> int:
        return 0

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        del texts  # unused; the ConfigError is the only effect.
        raise ConfigError(
            "Embedding is disabled. Set [embedding] backend to "
            "'local' / 'openai' / 'voyage' in ~/.config/opshub/config.toml "
            "(or OPSHUB_EMBEDDING__BACKEND env var) and run "
            "`opshub embeddings rebuild`."
        )

    def embed_one(self, text: str) -> EmbeddingResult:
        # ``embed`` always raises, so this delegation is unreachable in
        # practice. Kept for Protocol parity so ``NoOpEmbedder`` is a
        # structural :class:`~opshub.vectors.Embedder`.
        return self.embed([text])[0]


def build_embedder(settings: OpsHubSettings) -> Embedder:
    """Resolve the configured :class:`~opshub.vectors.Embedder`.

    Reads from ``settings.embedding`` (the section introduced in
    Phase 1 step 12 / PR #8 and refined here in step A5 to expose
    ``dimensions``). Each branch lazily imports the concrete embedder
    module so the heavy SDK / model dependency is loaded only for the
    backend the user selected.

    :param settings: Resolved root settings (typically obtained via
        :func:`opshub.core.config.get_settings`).
    :returns: A concrete embedder satisfying the
        :class:`~opshub.vectors.Embedder` Protocol.
    :raises ConfigError: When ``settings.embedding.backend`` is not one
        of the documented literals. Mirrors the typed
        :data:`~opshub.core.config.EmbeddingBackend` literal so any
        future addition without a matching factory branch fails loud.
    """
    backend = settings.embedding.backend
    if backend == "disabled":
        return NoOpEmbedder()
    if backend == "local":
        from opshub.vectors.local_embedder import LocalSentenceTransformerEmbedder

        return LocalSentenceTransformerEmbedder(
            model_id=settings.embedding.model_id or _LOCAL_DEFAULT_MODEL_ID,
            dim=settings.embedding.dimensions or _LOCAL_DEFAULT_DIM,
        )
    if backend == "openai":
        from opshub.vectors.openai_embedder import OpenAIEmbedder

        return OpenAIEmbedder(
            model_id=settings.embedding.model_id or _OPENAI_DEFAULT_MODEL_ID,
            dim=settings.embedding.dimensions or _OPENAI_DEFAULT_DIM,
        )
    if backend == "voyage":
        from opshub.vectors.voyage_embedder import VoyageEmbedder

        return VoyageEmbedder(
            model_id=settings.embedding.model_id or _VOYAGE_DEFAULT_MODEL_ID,
            dim=settings.embedding.dimensions or _VOYAGE_DEFAULT_DIM,
        )
    raise ConfigError(
        f"unknown embedding backend {backend!r}; expected one of "
        f"'local', 'openai', 'voyage', 'disabled'"
    )


def build_vector_store(settings: OpsHubSettings, engine: Engine) -> VectorStore:
    """Resolve the configured :class:`~opshub.vectors.VectorStore`.

    Phase 4 MVP always returns
    :class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore` — the only
    implementation. The store routes upserts / queries to the
    backend-specific vec0 table by inspecting embedding dimension
    (ADR-0012 §1 #5 backend-active semantics), so a single store
    serves multiple embedder backends in series.

    :param settings: Resolved root settings. Currently unused; the
        parameter is kept so Phase 4.x can dispatch on
        ``settings.storage`` / a future ``settings.vector_store``
        section without changing the call site.
    :param engine: SQLAlchemy engine for the opshub SQLite database
        (must have the sqlite-vec connect listener installed, as
        :func:`opshub.db.engine.create_engine_for_sqlite` does).
    :returns: A concrete :class:`~opshub.vectors.VectorStore`.
    """
    del settings  # unused today; reserved for Phase 4.x alt stores.
    from opshub.vectors.sqlite_vec_store import SqliteVecStore

    return SqliteVecStore(engine)
