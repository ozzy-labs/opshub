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
patterns covered here are the common bearer-token / API-key shapes:

- ``sk-...`` — OpenAI / Anthropic style secret keys
- ``ghp_...`` — GitHub personal access tokens (classic)
- ``github_pat_...`` — GitHub fine-grained PATs
- ``xoxp-`` / ``xoxb-`` / ``xoxa-`` / ``xoxr-`` / ``xoxs-`` — Slack
  user / bot / app / refresh / session tokens
- ``AKIA...`` — AWS access key id (16 char tail)
- ``AIza...`` — Google API key (35 char tail)
- ``eyJ...eyJ...`` — JWT (3-part base64url-encoded)
- ``Bearer ...`` — HTTP ``Authorization`` headers

Each match is rewritten to a fixed marker so the resulting message is
still self-describing in logs ("sk-***") rather than collapsed to a
single opaque placeholder.

``core/sanitise`` MUST NOT import from any other ``opshub`` submodule —
it lives at the foundation tier per ADR-0004.

The expanded shape set is also consumed by :mod:`opshub.mcp._redact`
(ADR-0022 §(b) Token Passthrough 禁止) so any token that slips into an
MCP tool's output or exception message is redacted before the agent
host's transcript records it.
"""

from __future__ import annotations

import re

__all__ = ["sanitise_error_message"]

# Token-shape regexes. Kept module-level so they compile once on first
# import and survive across calls (the embedding service used to hold
# these as module-level constants; they have moved here verbatim).
#
# Word-boundary anchors (``\b``) are used on the prefix-anchored token
# shapes (``AKIA`` / ``AIza`` / ``github_pat_`` / ``ghp_`` / ``sk-`` /
# ``xox*-``) so a longer surrounding identifier (URL path component,
# concatenated symbol, etc.) does not collapse into the marker. The
# trailing ``\b`` then pins the documented length without bleeding
# into adjacent alnum runs (Round 2 Cluster B M4 — Phase 10 audit
# follow-up). ``Bearer`` keeps the leading whitespace anchor because
# the ``Authorization: Bearer <token>`` shape already self-delimits.
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
_GHP_KEY_RE = re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")
# GitHub fine-grained PATs are documented as ``github_pat_<22>_<59>``
# but the separator and lengths drift across docs; match the prefix +
# 30+ chars of base62 + underscore for robustness.
_GITHUB_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")
# Slack tokens share the ``xox<letter>-`` prefix family (bot/user/app/
# refresh/session). The body uses digits, letters, and ``-``.
_SLACK_TOKEN_RE = re.compile(r"\bxox[pbars]-[A-Za-z0-9-]{10,}\b")
# AWS access key id: exactly ``AKIA`` + 16 uppercase alnum. Pin the
# bound so we do not over-match plain English ``AKIA`` runs, and
# anchor with ``\b`` so the prefix is not absorbed into a longer
# surrounding identifier.
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
# Google API key: ``AIza`` + 35 chars of base64url alphabet. ``\b``
# avoids matching ``...something/AIza...`` style URL-path false
# positives where the surrounding identifier would otherwise eat
# into the marker.
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")
# JWT: 3 base64url-encoded segments separated by ``.``. The first two
# segments always start with ``eyJ`` (the JSON ``{`` encoded). We
# anchor on that to avoid matching arbitrary dotted runs.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}=*")


def sanitise_error_message(message: str) -> str:
    """Strip API keys / bearer tokens from an error string.

    The function is intentionally permissive about input: callers can
    feed it a raw ``str(exception)`` payload and trust that obvious
    secret shapes are redacted before the message is persisted to the
    event log or surfaced in CLI output.

    Each token shape is rewritten to a fixed marker (``sk-***`` /
    ``ghp_***`` / ``github_pat_***`` / ``xox*-***`` / ``AKIA***`` /
    ``AIza***`` / ``[JWT REDACTED]`` / ``Bearer ***``) so the log line
    keeps its narrative even when the secret is stripped. The function
    does **not** truncate — callers that need a length cap (e.g. the
    Pydantic ``Field`` 2000-char ceiling on
    :class:`~opshub.domain.events.embedding.EmbeddingFailed.error_message`)
    must trim before calling.

    Ordering note: more specific shapes (``github_pat_``, JWT) are
    redacted before the more permissive ones (``Bearer``) so a token
    is not partially overwritten by a less informative marker.
    """
    # Specific prefixes first so the JWT / GitHub PAT markers win over
    # the catch-all ``Bearer`` pattern (a JWT can follow ``Bearer ``).
    message = _JWT_RE.sub("[JWT REDACTED]", message)
    message = _GITHUB_PAT_RE.sub("github_pat_***", message)
    message = _SLACK_TOKEN_RE.sub(lambda m: m.group(0)[:5] + "***", message)
    message = _SK_KEY_RE.sub("sk-***", message)
    message = _GHP_KEY_RE.sub("ghp_***", message)
    message = _AWS_ACCESS_KEY_RE.sub("AKIA***", message)
    message = _GOOGLE_API_KEY_RE.sub("AIza***", message)
    message = _BEARER_RE.sub("Bearer ***", message)
    return message
