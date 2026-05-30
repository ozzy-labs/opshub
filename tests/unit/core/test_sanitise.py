"""Unit tests for :func:`opshub.core.sanitise.sanitise_error_message`.

These tests pin the regex behaviour of the shared sanitiser at the
foundation tier. The Phase 4 service-level test
(``tests/unit/services/test_embedding_service.py::test_sanitise_error_redacts_common_token_shapes``)
still asserts the redaction is wired into :class:`EmbeddingService`;
the cases here exercise the regex set directly so future event families
(Phase 5 :class:`BriefingFailed` etc.) inherit a single, well-tested
implementation.
"""

from __future__ import annotations

import pytest

from opshub.core.sanitise import sanitise_error_message
from tests._secrets import (
    FAKE_AWS_ACCESS_KEY,
    FAKE_GITHUB_PAT,
    FAKE_GOOGLE_API_KEY,
    FAKE_JWT,
    FAKE_SLACK_APP_TOKEN,
    FAKE_SLACK_BOT_TOKEN,
    FAKE_SLACK_LEGACY_TOKEN,
    FAKE_SLACK_REFRESH_TOKEN,
    FAKE_SLACK_USER_TOKEN,
    NON_BOUNDARY_AWS,
    NON_BOUNDARY_GOOGLE,
)


def test_redacts_sk_key_shape() -> None:
    """``sk-`` followed by 20+ alnum chars is the OpenAI / Anthropic shape."""
    message = "OpenAI returned 401 for key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345"
    out = sanitise_error_message(message)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345" not in out
    assert "sk-***" in out


def test_redacts_ghp_token_shape() -> None:
    """GitHub classic PATs start with ``ghp_`` + 30+ alnum chars."""
    message = "GitHub PAT ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 also failed."
    out = sanitise_error_message(message)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" not in out
    assert "ghp_***" in out


def test_redacts_bearer_authorization_header() -> None:
    """``Authorization: Bearer <token>`` is the HTTP header shape."""
    message = "Authorization: Bearer abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890"
    out = sanitise_error_message(message)
    assert "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890" not in out
    assert "Bearer ***" in out


def test_redacts_all_three_shapes_in_one_message() -> None:
    """A combined error mentioning all three shapes is fully scrubbed."""
    message = (
        "OpenAI returned 401 for key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345; "
        "GitHub PAT ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 also failed. "
        "Authorization: Bearer abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890"
    )
    out = sanitise_error_message(message)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345" not in out
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" not in out
    assert "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890" not in out
    assert "sk-***" in out
    assert "ghp_***" in out
    assert "Bearer ***" in out


def test_passthrough_when_no_token_shapes_present() -> None:
    """A benign message must be returned unchanged."""
    message = "upstream returned HTTP 500 after 3 retries"
    assert sanitise_error_message(message) == message


def test_empty_string_returns_empty_string() -> None:
    """Edge case: empty input is a no-op (no regex matches)."""
    assert sanitise_error_message("") == ""


def test_does_not_truncate_input() -> None:
    """The sanitiser MUST NOT length-cap output — callers do that.

    The Phase 4 :class:`EmbeddingService._sanitise_error` callsite
    truncates **before** invoking the helper so the Pydantic
    ``Field(max_length=2000)`` cap is respected; the helper itself
    only handles redaction.
    """
    payload = "x" * 5000
    assert sanitise_error_message(payload) == payload


@pytest.mark.parametrize(
    ("short_token", "expected_to_survive"),
    [
        # "sk-" needs 20+ alnum chars to trigger; "sk-ABC" stays as-is.
        ("sk-ABC", True),
        # "ghp_" needs 30+ alnum chars; a 10-char tail does not match.
        ("ghp_ABCDEFGHIJ", True),
    ],
)
def test_short_token_lookalikes_are_not_redacted(
    short_token: str,
    expected_to_survive: bool,
) -> None:
    """Lookalikes shorter than the documented token length stay intact.

    This is a deliberate trade-off: the regex set targets the wire-
    format shapes (long-enough to be real tokens), not every prefix
    that *could* be a leading substring. Service-layer callers that
    want a stricter scrub are free to apply their own pass on top.
    """
    message = f"saw {short_token} in the logs"
    out = sanitise_error_message(message)
    if expected_to_survive:
        assert short_token in out
    else:
        assert short_token not in out


# ---------------------------------------------------------------------- B
# Phase 10 audit follow-up (Cluster 2): broader token shape coverage so
# the MCP boundary redactor (:mod:`opshub.mcp._redact`) catches every
# common SaaS / cloud token in error messages too.


def test_redacts_github_fine_grained_pat() -> None:
    """``github_pat_<base62>_<base62>`` is the new fine-grained PAT shape.

    Previously only the classic ``ghp_`` prefix was matched, leaving
    fine-grained PATs (issued from 2022 onwards) to slip through.
    """
    message = f"GitHub returned 401 for {FAKE_GITHUB_PAT}"
    out = sanitise_error_message(message)
    assert FAKE_GITHUB_PAT not in out
    assert "github_pat_***" in out


@pytest.mark.parametrize(
    "token",
    [
        FAKE_SLACK_USER_TOKEN,
        FAKE_SLACK_BOT_TOKEN,
        FAKE_SLACK_APP_TOKEN,
        FAKE_SLACK_REFRESH_TOKEN,
        FAKE_SLACK_LEGACY_TOKEN,
    ],
)
def test_redacts_slack_token_family(token: str) -> None:
    """All five ``xox<letter>-`` prefixes (bot / user / app / refresh / session)."""
    message = f"slack 401 for {token}"
    out = sanitise_error_message(message)
    assert token not in out
    # Marker retains the prefix so logs stay diagnosable.
    assert token[:5] + "***" in out


def test_redacts_aws_access_key_id() -> None:
    """``AKIA`` + 16 uppercase alnum is the AWS access key id shape."""
    message = f"aws 403 for {FAKE_AWS_ACCESS_KEY}"
    out = sanitise_error_message(message)
    assert FAKE_AWS_ACCESS_KEY not in out
    assert "AKIA***" in out


def test_redacts_google_api_key() -> None:
    """``AIza`` + 35 chars of base64url alphabet is the Google API key."""
    message = f"google returned 403 for {FAKE_GOOGLE_API_KEY}"
    out = sanitise_error_message(message)
    assert FAKE_GOOGLE_API_KEY not in out
    assert "AIza***" in out


def test_redacts_jwt() -> None:
    """3-part ``eyJ...eyJ...`` JWTs (header.payload.signature)."""
    message = f"failed to validate {FAKE_JWT}"
    out = sanitise_error_message(message)
    assert FAKE_JWT not in out
    assert "[JWT REDACTED]" in out


def test_redacts_bearer_with_jwt_does_not_partially_overwrite() -> None:
    """A ``Bearer <jwt>`` payload must not survive as half-redacted JWT.

    Ordering regression: if the JWT regex runs after ``Bearer ...`` the
    JWT body collapses to ``Bearer ***`` first and the JWT marker is
    skipped. The implementation applies the JWT pass before the bearer
    pass so the JWT-specific marker wins.
    """
    message = f"Authorization: Bearer {FAKE_JWT}"
    out = sanitise_error_message(message)
    assert FAKE_JWT not in out


def test_redacts_all_expanded_shapes_in_one_message() -> None:
    """A combined error mentioning every expanded shape is fully scrubbed."""
    message = (
        f"boom: {FAKE_GITHUB_PAT}; "
        f"{FAKE_SLACK_BOT_TOKEN}; "
        f"{FAKE_AWS_ACCESS_KEY}; "
        f"{FAKE_GOOGLE_API_KEY}; "
        f"{FAKE_JWT}"
    )
    out = sanitise_error_message(message)
    forbidden_fragments = [
        FAKE_GITHUB_PAT,
        FAKE_SLACK_BOT_TOKEN,
        FAKE_AWS_ACCESS_KEY,
        FAKE_GOOGLE_API_KEY,
        FAKE_JWT,
    ]
    for fragment in forbidden_fragments:
        assert fragment not in out


# ---------------------------------------------------------------------- C
# Phase 10 audit Round 2 Cluster B (M4): word-boundary anchors on the
# prefix-anchored token shapes. The ``AKIA`` / ``AIza`` / ``ghp_`` /
# ``github_pat_`` / ``sk-`` / ``xox*-`` markers now require ``\b`` on
# both sides so a leading identifier prefix (URL path component,
# concatenated symbol) does not cause a false-positive match that
# eats unrelated text. The regexes pin the documented wire-format
# shapes — embedded substrings inside a longer alnum identifier
# remain untouched.


def test_aws_key_inside_longer_identifier_is_not_redacted() -> None:
    """An ``X``-prefixed AKIA-like identifier (no word boundary) must stay intact.

    Before Round 2 Cluster B M4 the regex matched the embedded
    AKIA-style slice even when the prefix sat in the middle of a
    longer alnum identifier. The ``\\b`` anchor now requires the
    AKIA prefix to start at a word boundary, eliminating that false
    positive class for arbitrary identifiers.
    """
    out = sanitise_error_message(f"saw {NON_BOUNDARY_AWS} in audit log")
    assert NON_BOUNDARY_AWS in out
    assert "AKIA***" not in out


def test_google_key_inside_url_path_is_not_redacted() -> None:
    """A URL path component starting with ``...XAIza...`` must survive.

    A real Google API key surfaces at a word boundary (URL query
    parameter, JSON value, log token). The ``\\b`` anchor avoids
    redacting an unrelated identifier that happens to contain
    ``AIza`` mid-string.
    """
    out = sanitise_error_message(f"https://example.com/path/{NON_BOUNDARY_GOOGLE}/more")
    assert NON_BOUNDARY_GOOGLE in out
    assert "AIza***" not in out


def test_google_key_at_word_boundary_is_still_redacted() -> None:
    """The boundary-preserving behaviour must NOT regress true positives.

    A real ``AIza`` key delimited by spaces / quotes / URL boundary
    still hits the marker — the anchor only suppresses false matches
    inside longer alnum runs.
    """
    out = sanitise_error_message(f"https://example.com/path?key={FAKE_GOOGLE_API_KEY}")
    assert FAKE_GOOGLE_API_KEY not in out
    assert "AIza***" in out


def test_aws_key_at_word_boundary_is_still_redacted() -> None:
    """A real AWS access key id at a boundary remains redacted."""
    out = sanitise_error_message(f"saw '{FAKE_AWS_ACCESS_KEY}' in audit log")
    assert FAKE_AWS_ACCESS_KEY not in out
    assert "AKIA***" in out
