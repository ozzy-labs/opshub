"""Tests for the MCP tool-output redactor (ADR-0022 §(b))."""

from __future__ import annotations

from opshub.mcp._redact import redact_secrets
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
)


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
    text = f"GitHub returned 401 for {FAKE_GITHUB_PAT}"
    assert FAKE_GITHUB_PAT not in redact_secrets(text)


def test_redact_slack_xoxb_token() -> None:
    text = f"Slack 401: {FAKE_SLACK_BOT_TOKEN}"
    assert FAKE_SLACK_BOT_TOKEN not in redact_secrets(text)


def test_redact_slack_xoxp_token() -> None:
    text = f"Slack 401: {FAKE_SLACK_USER_TOKEN}"
    assert FAKE_SLACK_USER_TOKEN not in redact_secrets(text)


def test_redact_slack_xoxa_token() -> None:
    text = f"Slack 401: {FAKE_SLACK_APP_TOKEN}"
    assert FAKE_SLACK_APP_TOKEN not in redact_secrets(text)


def test_redact_slack_xoxr_token() -> None:
    text = f"Slack 401: {FAKE_SLACK_REFRESH_TOKEN}"
    assert FAKE_SLACK_REFRESH_TOKEN not in redact_secrets(text)


def test_redact_slack_xoxs_token() -> None:
    text = f"Slack 401: {FAKE_SLACK_LEGACY_TOKEN}"
    assert FAKE_SLACK_LEGACY_TOKEN not in redact_secrets(text)


def test_redact_aws_access_key_id() -> None:
    text = f"aws denied access for {FAKE_AWS_ACCESS_KEY}"
    assert FAKE_AWS_ACCESS_KEY not in redact_secrets(text)


def test_redact_google_api_key() -> None:
    # Google API keys are exactly ``AIza`` + 35 chars; the fake fixture
    # honours that wire-format length so the ``\b``-anchored regex
    # (Round 2 Cluster B M4) matches without overrun. Built from parts
    # in ``tests/_secrets.py`` so the literal never lives in source.
    text = f"google returned 403 for {FAKE_GOOGLE_API_KEY}"
    assert FAKE_GOOGLE_API_KEY not in redact_secrets(text)


def test_redact_jwt() -> None:
    text = f"failed to validate {FAKE_JWT}"
    assert FAKE_JWT not in redact_secrets(text)
