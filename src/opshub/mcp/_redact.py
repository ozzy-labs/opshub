"""Secret redaction for MCP tool outputs (ADR-0022 §(b)).

ADR-0022 forbids SaaS tokens from crossing the MCP boundary. The first
line of defence is the tool input schemas — none of them accept a
token field. The second line is this redactor, which scrubs any token
that slipped into a tool's output (e.g. an exception message captured
by the wrapping handler in :mod:`opshub.mcp.server`).

The module deliberately wraps :func:`opshub.core.sanitise.sanitise_error_message`
rather than re-implementing the regex set. ``core/sanitise`` already
covers the bearer-token shapes (``sk-…``, ``ghp_…``, ``Bearer …``);
extending it benefits every existing caller too (embedding /
briefing / connectors) so divergence is harder to introduce later.

Scope notes:

* This is a defensive net, not a full PII scrubber (the same caveat
  applies to ``core/sanitise``). Tool implementations must still
  refuse to read tokens from the arguments and must not echo body
  content that contains them.
* The function is pure / side-effect-free and safe to call from
  ``asyncio`` handlers; it does not log.
"""

from __future__ import annotations

from opshub.core.sanitise import sanitise_error_message

__all__ = ["redact_secrets"]


def redact_secrets(text: str) -> str:
    """Strip known SaaS / API token shapes from ``text``.

    Returns the input unchanged if it contains no token-shaped runs.
    Always returns a string so handlers can pipe the result directly
    into MCP ``TextContent``.
    """
    if not text:
        return text
    return sanitise_error_message(text)
