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
    message = (
        "GitHub returned 401 for github_pat_11ABCDEFG0_abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"
    )
    out = sanitise_error_message(message)
    assert "github_pat_11ABCDEFG0_abcdefghijklmnopqrstuvwxyz1234567890ABCDEF" not in out
    assert "github_pat_***" in out


@pytest.mark.parametrize(
    "token",
    [
        "xoxp-1234567890-1234567890-abcdefghij",
        "xoxb-1234567890-1234567890-abcdefghij",
        "xoxa-2-abcdefghijklmnopqrstuvwxyz",
        "xoxr-1234567890-1234567890-abcdefghij",
        "xoxs-1234567890-1234567890-abcdefghij",
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
    message = "aws 403 for AKIAIOSFODNN7EXAMPLE"
    out = sanitise_error_message(message)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "AKIA***" in out


def test_redacts_google_api_key() -> None:
    """``AIza`` + 35 chars of base64url alphabet is the Google API key."""
    message = "google returned 403 for AIzaSyA-1234567890abcdefghijklmnopqrstuvw"
    out = sanitise_error_message(message)
    assert "AIzaSyA-1234567890abcdefghijklmnopqrstuvw" not in out
    assert "AIza***" in out


def test_redacts_jwt() -> None:
    """3-part ``eyJ...eyJ...`` JWTs (header.payload.signature)."""
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    message = f"failed to validate {jwt}"
    out = sanitise_error_message(message)
    assert jwt not in out
    assert "[JWT REDACTED]" in out


def test_redacts_bearer_with_jwt_does_not_partially_overwrite() -> None:
    """A ``Bearer <jwt>`` payload must not survive as half-redacted JWT.

    Ordering regression: if the JWT regex runs after ``Bearer ...`` the
    JWT body collapses to ``Bearer ***`` first and the JWT marker is
    skipped. The implementation applies the JWT pass before the bearer
    pass so the JWT-specific marker wins.
    """
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    message = f"Authorization: Bearer {jwt}"
    out = sanitise_error_message(message)
    assert jwt not in out


def test_redacts_all_expanded_shapes_in_one_message() -> None:
    """A combined error mentioning every expanded shape is fully scrubbed."""
    message = (
        "boom: github_pat_11ABCDEFG0_abcdefghijklmnopqrstuvwxyz1234567890ABCDEF; "
        "xoxb-1234567890-1234567890-abcdefghij; "
        "AKIAIOSFODNN7EXAMPLE; "
        "AIzaSyA-1234567890abcdefghijklmnopqrstuvw; "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
    )
    out = sanitise_error_message(message)
    forbidden_fragments = [
        "github_pat_11ABCDEFG0_abcdefghijklmnopqrstuvwxyz1234567890ABCDEF",
        "xoxb-1234567890-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA-1234567890abcdefghijklmnopqrstuvw",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in out
