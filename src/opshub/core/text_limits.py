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

ADR context
-----------

The Phase 3-9 era ADR-0005 (External Content Minimization) pinned a
"summary + metadata only, never the body" posture; Phase 10 ADR-0020
(Full Local Content Retention) superseded ADR-0005 and flipped the
body-side rule to retain-everything. The summary-side caps and
optional-text normalisation defined here remain in force under
ADR-0020 — body and summary serve different surfaces (forensic /
search SSOT vs. preview), so the supersession only inverted the
body-side rule, not the summary-side discipline this module owns.
"""

from __future__ import annotations

__all__ = ["clip_author_field", "normalise_optional_text", "truncate_with_marker"]


def clip_author_field(value: str | None, *, max_chars: int) -> str | None:
    """Normalise + bound a cross-connector author field (Phase 25-A).

    The SSOT for the ``author_handle`` / ``author_display`` side of the
    Phase 25-A author normalisation: empty / whitespace-only input
    collapses to ``None`` (same rule as :func:`normalise_optional_text`),
    and an over-long value is truncated to ``max_chars`` so a
    pathological sender (a 500-char ``From:`` header, a degenerate Drive
    owner display name) never raises a mid-sync ``ValidationError``
    against :class:`opshub.domain.events.source.SourceObserved`'s
    ``max_length`` bound. Unlike the summary path no ``"…"`` marker is
    appended — an author handle is a join key, not a preview, so a
    silent clip keeps the stored value usable as a prefix match.

    Parameters
    ----------
    value:
        The candidate author handle / display string, or ``None``.
    max_chars:
        The schema cap for the target field
        (:data:`opshub.domain.events.source.AUTHOR_HANDLE_MAX_CHARS` /
        :data:`opshub.domain.events.source.AUTHOR_DISPLAY_MAX_CHARS`).

    Returns
    -------
    str | None
        ``None`` for missing / empty / whitespace-only input; otherwise
        the stripped value clipped to ``max_chars``.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_chars]


def normalise_optional_text(text: str | None) -> str | None:
    """Return ``None`` for ``None``, empty, or whitespace-only ``text``;
    else return the stripped value.

    Issue #343 promoted this helper out of the per-connector mappers
    after a cross-cutting audit of #332 / #337 found five SSOT-violating
    sites (``ms365`` / ``google_workspace`` / ``google_mail`` /
    ``google_calendar`` mappers + ``github`` notification path) that
    each open-coded "treat missing / empty summary as ``None``" with
    subtly different semantics:

    * The four helper-based mappers used
      ``summary if summary else None`` — empty string normalised to
      ``None``, but **whitespace-only** strings leaked through into the
      ``sources.summary`` column as visually-empty previews.
    * The GitHub notification path called
      ``_truncate_optional(raw_summary, SUMMARY_MAX_CHARS)`` which
      preserved both empty and whitespace-only strings.
    * PR #340 / #342 had already extended Slack / Teams mappers + the
      ``SourceService.observe`` inbox-side fallback to also catch
      whitespace-only input, but the four helper-based mappers and
      GitHub's notification call site were never updated, so the
      "summary is missing" semantics diverged across connectors.

    Funnelling every connector's optional summary through this single
    helper makes the rule SSOT-uniform: a summary is considered
    "missing" iff the input is ``None``, empty, or contains only
    whitespace (Unicode ``str.strip()`` semantics). The helper returns
    the **stripped** value rather than the verbatim input — every
    existing call site that previously open-coded the check fed the
    output straight into ``SourceObserved.summary`` for preview /
    briefing surfaces where leading / trailing whitespace adds no
    recognition value, and PRs #340 / #342 had already adopted
    ``_truncate(raw.text.strip(), ...)`` so returning the stripped
    value keeps the cross-connector semantics aligned. The full body
    retention path (ADR-0020) intentionally preserves whitespace and
    is **not** routed through this helper.

    The rule lives here precisely because ADR-0005's
    "minimise everything" stance was superseded by ADR-0020's
    "retain everything in the body, normalise the preview" split:
    the summary cap + normalisation discipline survived the
    supersession unchanged, and this helper is the SSOT for the
    summary side of that split (body retention is the symmetric
    counterpart owned by the connector mappers themselves).

    Parameters
    ----------
    text:
        The candidate summary string, or ``None``.

    Returns
    -------
    str | None
        ``None`` when ``text`` is ``None``, empty, or whitespace-only;
        otherwise the leading/trailing-whitespace-stripped value.
    """
    if text is None:
        return None
    stripped = text.strip()
    return stripped if stripped else None


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
