"""Unit tests for :mod:`opshub.core.text_limits`.

Covers:

* :func:`normalise_optional_text` — issue #343 SSOT helper for the
  "summary is missing iff ``None`` / empty / whitespace-only" rule used
  across every connector mapper. The four whitespace variants
  (space / tab / newline / mixed) are pinned individually so a future
  regression that special-cases one whitespace class fails loudly
  rather than slipping through behind ``str.strip()``'s catch-all.
* :func:`truncate_with_marker` — pre-existing Phase 11 helper. The
  smoke cases here guard the new module API (``__all__``) and the
  default behaviour at the three boundary lengths
  (``< max_chars`` / ``== max_chars`` / ``> max_chars``).
"""

from __future__ import annotations

from opshub.core.text_limits import normalise_optional_text, truncate_with_marker


class TestNormaliseOptionalText:
    """Issue #343: SSOT whitespace-only summary normalisation.

    The helper is exercised independently of any connector mapper so
    the contract is pinned without having to assemble a full
    ``SourceObserved`` event in every test. Each mapper carries its
    own integration-style regression test that re-asserts the wiring
    end-to-end.
    """

    def test_returns_none_for_none(self) -> None:
        assert normalise_optional_text(None) is None

    def test_returns_none_for_empty(self) -> None:
        assert normalise_optional_text("") is None

    def test_returns_none_for_whitespace_only_space(self) -> None:
        assert normalise_optional_text("   ") is None

    def test_returns_none_for_whitespace_only_tab(self) -> None:
        assert normalise_optional_text("\t\t") is None

    def test_returns_none_for_whitespace_only_newline(self) -> None:
        assert normalise_optional_text("\n\n") is None

    def test_returns_none_for_whitespace_only_mixed(self) -> None:
        # Mirrors the Teams HTML-strip residue case ("<p> </p>") that
        # PR #342 caught for the Teams mapper — leading newline +
        # internal tab + trailing space.
        assert normalise_optional_text("\n \t ") is None

    def test_returns_verbatim_for_regular_text(self) -> None:
        assert normalise_optional_text("hello") == "hello"

    def test_returns_stripped_for_text_with_surrounding_whitespace(self) -> None:
        """Surrounding whitespace is stripped (NOT returned verbatim).

        Issue #343 explicitly adopted the stripped value as the SSOT
        contract because every existing call site already fed the
        output straight into ``SourceObserved.summary`` (a preview /
        briefing surface where leading / trailing whitespace adds no
        recognition value), and PRs #340 / #342 had already adopted
        ``_truncate(raw.text.strip(), ...)`` for the Slack / Teams
        mappers — returning the stripped value keeps the cross-connector
        semantics aligned. The body retention path (ADR-0020) is
        intentionally **not** routed through this helper so whitespace
        in full bodies remains intact.
        """
        assert normalise_optional_text("  hello  ") == "hello"

    def test_preserves_internal_whitespace(self) -> None:
        """Only leading / trailing whitespace is stripped.

        Internal whitespace (the boundary between "hello" and "world"
        below) is recognition signal — both mapper summary composers
        ("from: <addr>, subject: <subj>", "<start> - <end>") emit
        single-space separators that must survive normalisation.
        """
        assert normalise_optional_text("  hello world  ") == "hello world"


class TestTruncateWithMarkerSmoke:
    """Smoke coverage so the new ``__all__`` entry does not regress the
    pre-existing :func:`truncate_with_marker` contract."""

    def test_below_limit_returns_verbatim(self) -> None:
        body, truncated = truncate_with_marker(
            "short", max_chars=10, marker_template="\n\n[cut: {kept}/{original}]"
        )
        assert body == "short"
        assert truncated is False

    def test_at_limit_returns_verbatim(self) -> None:
        body, truncated = truncate_with_marker(
            "0123456789", max_chars=10, marker_template="\n\n[cut: {kept}/{original}]"
        )
        assert body == "0123456789"
        assert truncated is False

    def test_above_limit_truncates_and_appends_marker(self) -> None:
        body, truncated = truncate_with_marker(
            "0123456789ABCDE",
            max_chars=10,
            marker_template="\n\n[cut: {kept}/{original}]",
        )
        assert body == "0123456789\n\n[cut: 10/15]"
        assert truncated is True

    def test_zero_max_chars_disables_truncation(self) -> None:
        body, truncated = truncate_with_marker(
            "long text", max_chars=0, marker_template="\n\n[cut: {kept}/{original}]"
        )
        assert body == "long text"
        assert truncated is False
