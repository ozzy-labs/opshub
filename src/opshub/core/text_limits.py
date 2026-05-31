"""Shared text-truncation primitives (Phase 11 audit Cluster B INFO E2).

Phase 11 introduced two independent text-length defences with the same
intent (head-truncate + append a deterministic marker so downstream
consumers see the truncation cue without sidecar fields):

* :func:`opshub.core.document_extract.extract_document` head-truncates
  extracted Office markdown at ``max_chars`` (ADR-0025 §決定 (b-2))
  and appends ``"\\n\\n[truncated: original=<N> chars, limit=<M>]"``.
* :func:`opshub.connectors.ms365.mapper._truncate_outlook_body`
  head-truncates Outlook HTML bodies at ``MAX_OUTLOOK_BODY_CHARS``
  (Phase 11 OQ2) and appends
  ``"\\n\\n[outlook body truncated: <kept> / <original> chars]"``.

The Phase 11 plan §3 F3 anticipated a shared
``core/text_limits`` module precisely so the two paths converge on one
SSOT. The cross-cutting audit (Cluster B INFO E2) recorded that the
shared module never landed; this module is that landing. Both callers
now compose :func:`truncate_with_marker` to compute the truncated body
+ a ``truncated`` flag, while still controlling their own marker shape
so existing operator / regex parsers (notably
``tests/integration/test_phase11_office_lifecycle.py``) stay green.

Design intent
-------------

* **Single function, two markers.** The function accepts a
  ``marker_template`` so each caller keeps its own audit-trail shape.
  Forcing a single marker would touch every consumer that greps
  ``sources.body``; the variability sits inside the template, not
  inside the truncation logic.
* **Stable return contract.** The function returns
  ``tuple[str, bool]`` — the (possibly truncated) text and a
  ``truncated`` flag the caller can surface separately (e.g. a chip
  in ``opshub source show``). The boolean removes a second
  ``len() > limit`` branch in caller code.
* **No structlog warning here.** Both call sites have their own
  structured-log shape (``mapper.outlook.body_truncated`` /
  ``document_extract.truncated``); duplicating the warning shape
  inside the helper would either force a single namespace on both
  callers or accept three near-identical log emissions. Keeping the
  warning at the call site keeps the audit trail intact and the
  helper context-free (cheaper to test, easier to reuse).

This module is core-tier (ADR-0004): no connector / projection /
service imports. It stays import-safe on the M6 cold-start path
(stdlib only).
"""

from __future__ import annotations

__all__ = ["truncate_with_marker"]


def truncate_with_marker(
    text: str,
    *,
    max_chars: int,
    marker_template: str,
) -> tuple[str, bool]:
    """Head-truncate ``text`` to ``max_chars`` chars + append a marker.

    Parameters
    ----------
    text:
        Source string. ``None`` is intentionally NOT accepted — every
        caller already gates against the ``None`` body / message case
        before invoking truncation, so accepting it here would just
        push the branch one level down without simplifying the call
        sites. Passing the wrong type fails loudly at runtime via the
        ``len()`` call rather than silently returning ``None``.
    max_chars:
        Character ceiling. When ``max_chars <= 0`` the cap is treated
        as disabled (mirrors :func:`extract_document` and the
        Outlook mapper's ``"0 = unlimited"`` convention from ADR-0025
        §決定 (b)); the function returns ``(text, False)`` unchanged.
    marker_template:
        Format string with two well-known placeholders:

        * ``{kept}`` — the number of chars retained (== ``max_chars``).
        * ``{original}`` — the original ``len(text)`` before truncation.

        Use exactly the marker shape your downstream consumers expect.
        The two Phase 11 templates are::

            # document_extract (ADR-0025 §決定 (b-2)):
            "\\n\\n[truncated: original={original} chars, limit={kept}]"

            # ms365 outlook mapper (Phase 11 OQ2):
            "\\n\\n[outlook body truncated: {kept} / {original} chars]"

        Any other placeholder is ignored by :meth:`str.format` — but
        :class:`KeyError` propagates verbatim for unknown placeholders
        so a typo (``{limit}`` vs ``{kept}``) fails loudly at runtime
        rather than silently dropping the marker.

    Returns
    -------
    tuple[str, bool]
        ``(body, truncated)`` where:

        * ``body`` is the original string when ``len(text) <= max_chars``
          (or ``max_chars <= 0``); otherwise the head + formatted marker.
        * ``truncated`` reflects whether truncation actually happened
          — callers surface this as a structured signal (chip in
          recall / inbox, structured-log warning, etc.).

    Notes
    -----
    The function uses **head**-truncation (keep prefix, drop suffix)
    because the leading prose / table headers carry the most retrieval
    value for both Office documents (ADR-0012 §4 head-truncation
    strategy) and Outlook bodies (subject + opening lines drive
    recognition). Tail-truncation would lose the recognition signal
    and require either a sentinel prefix or a sidecar flag to
    surface — neither composes cleanly with the existing call
    sites.
    """
    if max_chars <= 0:
        return (text, False)
    original_length = len(text)
    if original_length <= max_chars:
        return (text, False)
    head = text[:max_chars]
    marker = marker_template.format(kept=max_chars, original=original_length)
    return (head + marker, True)
