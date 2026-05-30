"""Tests for the MCP tool-output redactor (ADR-0022 §(b))."""

from __future__ import annotations

from opshub.mcp._redact import redact_secrets


def test_redact_sk_style_keys() -> None:
    text = "got sk-abcdefghijklmnopqrstuvwxyz123456 from upstream"
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in redact_secrets(text)


def test_redact_github_pat() -> None:
    text = "Authorization: token ghp_abcdef0123456789ABCDEF0123456789abcd"
    assert "ghp_abcdef0123456789ABCDEF0123456789abcd" not in redact_secrets(text)


def test_redact_bearer_token() -> None:
    text = "Authorization: Bearer abcdef0123456789ABCDEF0123456789abcd=="
    assert "abcdef0123456789ABCDEF0123456789abcd" not in redact_secrets(text)


def test_redact_empty_string_returns_empty() -> None:
    assert redact_secrets("") == ""


def test_redact_passthrough_for_safe_text() -> None:
    text = "task created: 01HABCDEFGHIJKLMNOPQRSTUVWX"
    assert redact_secrets(text) == text


def test_redact_handles_multiple_tokens_in_one_string() -> None:
    text = (
        "first sk-abcdefghijklmnopqrstuvwxyz123456 then "
        "ghp_abcdef0123456789ABCDEF0123456789abcd in same payload"
    )
    redacted = redact_secrets(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in redacted
    assert "ghp_abcdef0123456789ABCDEF0123456789abcd" not in redacted


# ---------------------------------------------------------------------- B
# Phase 10 audit follow-up (Cluster 2): the MCP boundary must redact
# the full set of token shapes that ``core/sanitise`` recognises so a
# stray token in any handler's output never reaches the agent host.


def test_redact_github_fine_grained_pat() -> None:
    text = (
        "GitHub returned 401 for github_pat_11ABCDEFG0_abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"
    )
    assert "github_pat_11ABCDEFG0_abcdefghijklmnopqrstuvwxyz1234567890ABCDEF" not in redact_secrets(
        text
    )


def test_redact_slack_xoxb_token() -> None:
    text = "Slack 401: xoxb-1234567890-1234567890-abcdefghij"
    assert "xoxb-1234567890-1234567890-abcdefghij" not in redact_secrets(text)


def test_redact_slack_xoxp_token() -> None:
    text = "Slack 401: xoxp-1234567890-1234567890-abcdefghij"
    assert "xoxp-1234567890-1234567890-abcdefghij" not in redact_secrets(text)


def test_redact_slack_xoxa_token() -> None:
    text = "Slack 401: xoxa-2-abcdefghijklmnopqrstuvwxyz"
    assert "xoxa-2-abcdefghijklmnopqrstuvwxyz" not in redact_secrets(text)


def test_redact_slack_xoxr_token() -> None:
    text = "Slack 401: xoxr-1234567890-1234567890-abcdefghij"
    assert "xoxr-1234567890-1234567890-abcdefghij" not in redact_secrets(text)


def test_redact_slack_xoxs_token() -> None:
    text = "Slack 401: xoxs-1234567890-1234567890-abcdefghij"
    assert "xoxs-1234567890-1234567890-abcdefghij" not in redact_secrets(text)


def test_redact_aws_access_key_id() -> None:
    text = "aws denied access for AKIAIOSFODNN7EXAMPLE"
    assert "AKIAIOSFODNN7EXAMPLE" not in redact_secrets(text)


def test_redact_google_api_key() -> None:
    text = "google returned 403 for AIzaSyA-1234567890abcdefghijklmnopqrstuvw"
    assert "AIzaSyA-1234567890abcdefghijklmnopqrstuvw" not in redact_secrets(text)


def test_redact_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    text = f"failed to validate {jwt}"
    assert jwt not in redact_secrets(text)
