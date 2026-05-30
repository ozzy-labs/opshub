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
