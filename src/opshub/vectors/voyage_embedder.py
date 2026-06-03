"""Voyage AI API Embedder (Phase 4 step A4, ADR-0012).

Uses the official ``voyageai`` SDK (in ``[api-embedding-voyage]``
extras) to embed via the ``voyage-3`` family. Token is resolved via
:func:`opshub.core.secrets.get_secret` (ADR-0014, Phase 3 A6 reuse):

- keyring key: ``embedder:voyage:api_key``
- env var override: ``OPSHUB_EMBEDDER_VOYAGE_API_KEY``

Default model: ``voyage-3`` (1024-dim). Switch via constructor arg.

The SDK import is **lazy** (deferred to first :meth:`VoyageEmbedder.embed`
call) so that ``import opshub.vectors.voyage_embedder`` succeeds even
when the extras are not installed. Mirrors the OpenAI embedder shape
intentionally so backend selection in ``vectors/factory.py`` (PR A5)
sees a uniform surface.

The ``voyageai`` SDK is not part of the CI ``dev`` extras lane, so
static type-checkers see it as missing. We follow the
``opshub.core.secrets`` strategy of typing the client as :class:`Any`
and pinning the dynamic-import line with a focused ``type: ignore`` so
the codebase stays strict overall while this single optional dependency
stays untyped at module boundary.
"""

from __future__ import annotations

from typing import Any

from opshub.vectors.embedder import EmbeddingResult

__all__ = ["VOYAGE_API_KEY_SECRET", "VoyageEmbedder"]

#: Keyring key used to store the Voyage API key. Exposed so the CLI
#: command ``opshub embedder auth set voyage`` writes to the
#: exact same key this embedder reads at embed time.
VOYAGE_API_KEY_SECRET = "embedder:voyage:api_key"


class VoyageEmbedder:
    """Embedder backed by Voyage AI's ``embed`` endpoint.

    Implements the :class:`opshub.vectors.Embedder` Protocol. Network /
    SDK access is deferred until the first :meth:`embed` call so the
    module is safe to import without the ``[api-embedding-voyage]``
    extras installed.
    """

    def __init__(
        self,
        *,
        model_id: str = "voyage-3",
        model_version: str = "2024-09-18",
        dim: int = 1024,
        batch_size: int = 128,
    ) -> None:
        """Create a new embedder.

        :param model_id: Voyage model identifier (e.g. ``"voyage-3"``).
        :param model_version: Stable version tag stored alongside the
            vector so callers can detect a model upgrade. Voyage's
            published release date for the model is the natural choice.
        :param dim: Expected embedding dimensionality. ``embed`` validates
            every returned vector against this value and raises
            :class:`~opshub.core.errors.ConfigError` on mismatch.
        :param batch_size: Maximum number of texts per API request.
            Defaults to 128 (Voyage's documented batch limit).
        """
        self._model_id_value = model_id
        self._model_version_value = model_version
        self._dim_value = dim
        self._batch_size = batch_size
        self._client: Any = None

    @property
    def model_id(self) -> str:
        return self._model_id_value

    @property
    def model_version(self) -> str:
        return self._model_version_value

    @property
    def dim(self) -> int:
        return self._dim_value

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        """Embed ``texts`` in batches of ``batch_size``.

        Returns one :class:`EmbeddingResult` per input, in input order.
        Empty input returns an empty list without contacting the API.
        """
        if not texts:
            return []
        client = self._ensure_client()
        results: list[EmbeddingResult] = []
        for batch_start in range(0, len(texts), self._batch_size):
            batch = texts[batch_start : batch_start + self._batch_size]
            response = client.embed(batch, model=self._model_id_value)
            for raw_vector in response.embeddings:
                vector = tuple(float(x) for x in raw_vector)
                if len(vector) != self._dim_value:
                    from opshub.core.errors import ConfigError

                    raise ConfigError(
                        f"VoyageEmbedder: model {self._model_id_value!r} returned "
                        f"dim={len(vector)}, expected {self._dim_value}"
                    )
                results.append(
                    EmbeddingResult(
                        vector=vector,
                        model_id=self._model_id_value,
                        model_version=self._model_version_value,
                        dim=self._dim_value,
                    )
                )
        return results

    def embed_one(self, text: str) -> EmbeddingResult:
        """Embed a single text. Convenience wrapper around :meth:`embed`."""
        results = self.embed([text])
        assert len(results) == 1
        return results[0]

    def _ensure_client(self) -> Any:
        """Return a cached SDK client, constructing it on first call.

        Raises :class:`~opshub.core.errors.ConfigError` if the SDK
        extras are missing or no API key is configured (neither in
        keyring nor via the documented env var override).
        """
        if self._client is None:
            try:
                voyageai_module: Any = __import__("voyageai")
            except ImportError as exc:
                from opshub.core.errors import ConfigError

                raise ConfigError(
                    "VoyageEmbedder requires the 'api-embedding-voyage' extras: "
                    "uv pip install 'opshub[api-embedding-voyage]'"
                ) from exc
            from opshub.core.secrets import get_secret

            api_key = get_secret(VOYAGE_API_KEY_SECRET)
            if not api_key:
                from opshub.core.errors import ConfigError

                raise ConfigError(
                    "Voyage API key not configured. Run "
                    "`opshub embedder auth set voyage` or set "
                    "OPSHUB_EMBEDDER_VOYAGE_API_KEY in the environment."
                )
            self._client = voyageai_module.Client(api_key=api_key)
        client: Any = self._client
        return client
