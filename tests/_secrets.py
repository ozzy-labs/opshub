"""Test-only synthetic secret-shaped strings.

**NOT real secrets.** Each constant resolves at import time to a string
whose wire shape matches the production redactor regex
(``src/opshub/core/sanitise.py``) so the tests can prove the redactor
catches every shape end-to-end. The strings are built from concatenated
parts so this *source* file never contains a contiguous literal that
would trigger GitHub Secret Scanning / push-protection on the public
``opshub`` repository.

A previous version of these tests inlined the literal token shapes
(``"AIzaSyA-..."``, ``"xoxp-..."``, ``"github_pat_..."``, etc.) and
landed publicly via PR #229. GitHub flagged the Google API Key shape
as `secret_type=google_api_key` (alert #1, ``publicly_leaked=true``);
the secret itself was synthetic but the public alert noise was real.
This module is the structural fix: keep the wire shape, lose the
contiguous literal.

Anyone editing these constants must keep the **concatenation boundary**
(``"AI" + "za..."`` etc.) so the literal in source is shorter than the
detector regex requires.
"""

from __future__ import annotations

# Google API key wire shape: ``AIza`` + 35 chars [0-9A-Za-z_-].
FAKE_GOOGLE_API_KEY = "AI" + "zaSyA-1234567890abcdefghijklmnopqrstu"

# GitHub fine-grained PAT: ``github_pat_`` + 30+ chars [A-Za-z0-9_].
FAKE_GITHUB_PAT = "github_" + "pat_11ABCDEFG0_abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"

# AWS access key id: ``AKIA`` + 16 [A-Z0-9]. The body is the documented
# AWS placeholder ``IOSFODNN7EXAMPLE`` (AWS uses it in public docs).
FAKE_AWS_ACCESS_KEY = "AK" + "IAIOSFODNN7EXAMPLE"

# JWT three-part token (header.payload.signature).
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
)

# Slack token family. Each prefix is ``xox<letter>-`` followed by 10+
# allowed chars; split the ``xox`` to keep the source literal shorter
# than the redactor's minimum match length.
FAKE_SLACK_USER_TOKEN = "xo" + "xp-1234567890-1234567890-abcdefghij"
FAKE_SLACK_BOT_TOKEN = "xo" + "xb-1234567890-1234567890-abcdefghij"
FAKE_SLACK_BOT_TOKEN_ALT = "xo" + "xb-1234567890-9876543210-abcdefghij"
FAKE_SLACK_BOT_TOKEN_SHORT = "xo" + "xb-1234567890"
FAKE_SLACK_BOT_TOKEN_LETTERS = "xo" + "xb-1234567890-abcdefghij-klmnopqrstuvwxyz"
FAKE_SLACK_APP_TOKEN = "xo" + "xa-2-abcdefghijklmnopqrstuvwxyz"
FAKE_SLACK_REFRESH_TOKEN = "xo" + "xr-1234567890-1234567890-abcdefghij"
FAKE_SLACK_LEGACY_TOKEN = "xo" + "xs-1234567890-1234567890-abcdefghij"

# Slack tokens used as marker fixtures (storage / lookup paths). Short
# enough that some don't hit the redactor regex by themselves but are
# kept here so contributors discover the canonical home for any
# Slack-shaped test string.
FAKE_SLACK_USER_TOKEN_NAMESPACED = "xo" + "xp-namespaced"
FAKE_SLACK_USER_TOKEN_FROM_SECRET = "xo" + "xp-from-secret"
FAKE_SLACK_BOT_TOKEN_MARKER = "xo" + "xb-SECRET-TOKEN-DO-NOT-STORE"

# Non-matching control strings used by boundary tests
# (``\b``-anchor regression coverage in
# ``tests/unit/core/test_sanitise.py``). They start with a letter that
# prevents the redactor regex from matching but keep the shape so the
# test asserts they are *not* mangled.
NON_BOUNDARY_AWS = "X" + "AKIAIOSFODNN7EXAMPLE"
NON_BOUNDARY_GOOGLE = "X" + "AIzaSyA1234567890abcdefghijklmnopqrstuvw"
