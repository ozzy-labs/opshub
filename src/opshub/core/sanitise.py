"""Shared error-message sanitiser (Phase 5 step B1).

Centralises the "redact obvious API key shapes from an error string"
helper so multiple services can reuse it. Phase 4 introduced the logic
inline on :class:`opshub.services.embedding_service.EmbeddingService`;
Phase 5 step B1 extracts it here so :class:`BriefingService` (step B3)
can apply the same scrub before stamping
:class:`~opshub.domain.events.briefing.BriefingFailed.error_message`.

Scope (intentionally narrow)
----------------------------

This is a **defensive net**, not a full PII scrubber. The concrete
embedder / LLM client implementations are still responsible for not
raising exceptions that include the API key in the first place. The
patterns covered here are the common bearer-token shapes:

- ``sk-...`` — OpenAI / Anthropic style secret keys
- ``ghp_...`` — GitHub personal access tokens (legacy fine-grained
  tokens share the prefix family ``github_pat_`` which is not
  matched here; PR-feedback can widen if needed)
- ``Bearer ...`` — HTTP ``Authorization`` headers

Each match is rewritten to a fixed marker so the resulting message is
still self-describing in logs ("sk-***") rather than collapsed to a
single opaque placeholder.

``core/sanitise`` MUST NOT import from any other ``opshub`` submodule —
it lives at the foundation tier per ADR-0004.
"""

from __future__ import annotations

import re

__all__ = ["sanitise_error_message"]

# Token-shape regexes. Kept module-level so they compile once on first
# import and survive across calls (the embedding service used to hold
# these as module-level constants; they have moved here verbatim).
_SK_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")
_GHP_KEY_RE = re.compile(r"ghp_[A-Za-z0-9]{30,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}=*")


def sanitise_error_message(message: str) -> str:
    """Strip API keys / bearer tokens from an error string.

    The function is intentionally permissive about input: callers can
    feed it a raw ``str(exception)`` payload and trust that obvious
    secret shapes are redacted before the message is persisted to the
    event log or surfaced in CLI output.

    Each token shape is rewritten to a fixed marker (``sk-***`` /
    ``ghp_***`` / ``Bearer ***``) so the log line keeps its narrative
    even when the secret is stripped. The function does **not**
    truncate — callers that need a length cap (e.g. the Pydantic
    ``Field`` 2000-char ceiling on
    :class:`~opshub.domain.events.embedding.EmbeddingFailed.error_message`)
    must trim before calling.
    """
    message = _SK_KEY_RE.sub("sk-***", message)
    message = _GHP_KEY_RE.sub("ghp_***", message)
    message = _BEARER_RE.sub("Bearer ***", message)
    return message
