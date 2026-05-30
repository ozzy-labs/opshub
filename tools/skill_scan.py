"""Security scanner for Anthropic Agent Skills (SKILL.md) — ADR-0004 §(c).

Phase 10 Sub-issue D adds secretary Agent Skills distributed via the
``ozzy-labs/skills`` preset. ADR-0004 §(c) commits opshub to keeping a
**skill security scan** that the ``ozzy-labs/skills`` CI lint and this
repo's own skill-spec tests both rely on. The Phase 10 plan §3-D pins
the four detection categories (carried over from the QwenPaw research
into adversarial skill content) plus the frontmatter checks:

1. **Prompt injection** — bodies that try to override the host agent's
   system prompt or instructions (``ignore previous instructions``,
   ``you are now``, ``forget your guidelines``, ``new system prompt``).
2. **Command injection** — bodies that embed shell metacharacters
   intended to chain unexpected commands when the host pastes the
   snippet into a terminal (`` `rm -rf /` `` inside a markdown fence,
   ``$(curl ... | sh)``, ``&& curl ... | bash`` patterns).
3. **Hard-coded secrets** — bodies that ship API keys, bearer tokens or
   private keys verbatim. Detects the well-known prefixes (``sk-``,
   ``ghp_``, ``xoxb-``, ``AKIA``, ``-----BEGIN PRIVATE KEY-----`` etc.)
   plus generic ``Authorization: Bearer`` headers with > 20 char tail.
4. **Data exfiltration** — bodies that wire the agent to ship the
   user's transcript / local files to an arbitrary remote URL
   (``curl ... -d @-``, ``http(s)://`` in a ``send``/``post`` context,
   webhook URLs disguised as documentation links).

Frontmatter is checked separately for two adversarial patterns that
are invisible in a casual code review but flip the meaning of the
skill once Claude / Codex / Cursor parses it:

* **Hidden Unicode** — bidi controls (``U+202A``..``U+202E``), zero
  width characters (``U+200B``, ``U+200C``, ``U+200D``, ``U+FEFF``)
  and tag characters (``U+E0000``..``U+E007F``) inside the
  ``name`` / ``description`` fields.
* **Hidden instruction overrides** — frontmatter ``description`` text
  that contains ``ignore previous instructions`` / ``forget all`` /
  ``new system prompt`` etc. (mirrors the body check above; we keep
  the rule explicit because frontmatter is what the host loader
  triggers on, so injection here is more dangerous).

The scanner returns a structured :class:`ScanResult` so both CI and
``pytest`` can render findings without re-parsing markdown.

Design notes:

* The regex set is small and tuned for **high precision** at the cost
  of recall. False positives in legitimate documentation (e.g. a
  retrospective discussing a *past* prompt-injection incident) are
  preferred to false negatives — operators can pin an allowlist by
  adding a literal ``# skill-scan: allow <category>`` comment line
  next to the offending snippet. The scanner respects per-finding
  allowlist comments to keep the noise floor sustainable.

* Detection is purely text-based; we never execute the skill body
  during scanning. That means dynamic ``$(...)`` shell substitutions
  flagged here are advisory — the upstream host decides whether to
  execute them.

* No third-party deps. Uses :mod:`re` and :mod:`unicodedata` only so
  the scanner can run inside the ``ozzy-labs/skills`` CI without
  pulling opshub's connector / LLM stack.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

__all__ = [
    "Finding",
    "FindingCategory",
    "ScanResult",
    "parse_frontmatter",
    "scan_skill_file",
    "scan_skill_text",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class FindingCategory(StrEnum):
    """Why a particular line tripped the scanner."""

    PROMPT_INJECTION = "prompt_injection"
    COMMAND_INJECTION = "command_injection"
    HARDCODED_SECRET = "hardcoded_secret"
    DATA_EXFILTRATION = "data_exfiltration"
    FRONTMATTER_HIDDEN_UNICODE = "frontmatter_hidden_unicode"
    FRONTMATTER_INSTRUCTION_OVERRIDE = "frontmatter_instruction_override"


@dataclass(frozen=True, slots=True)
class Finding:
    """One scanner hit."""

    category: FindingCategory
    line_number: int  # 1-based, matches editors and ``grep -n`` output
    snippet: str
    rule_id: str  # stable identifier for allowlisting (e.g. ``cmd-inject-curl-pipe``)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Aggregate scan outcome for one SKILL.md file."""

    path: Path | None
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Return ``True`` when no findings were recorded."""
        return not self.findings

    def by_category(self, category: FindingCategory) -> tuple[Finding, ...]:
        """Return all findings of a given category."""
        return tuple(f for f in self.findings if f.category == category)


# ---------------------------------------------------------------------------
# Allowlist comment parser
# ---------------------------------------------------------------------------

# Operators can suppress a particular rule on a particular line by adding
# ``# skill-scan: allow <category>`` (or ``allow <rule_id>``) anywhere on
# the same line. The format is intentionally narrow — a typo will not
# suppress the finding.
_ALLOWLIST_RE: Final = re.compile(
    r"#\s*skill-scan:\s*allow\s+(?P<token>[a-z][a-z0-9_-]*)",
    re.IGNORECASE,
)


def _is_allowlisted(line: str, category: FindingCategory, rule_id: str) -> bool:
    match = _ALLOWLIST_RE.search(line)
    if not match:
        return False
    token = match.group("token").lower()
    return token in {category.value, rule_id}


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE: Final = re.compile(
    r"\A---\s*\n(?P<body>.*?)\n---\s*\n",
    re.DOTALL,
)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into ``(frontmatter_dict, body_text)``.

    The function intentionally only supports the SKILL.md subset
    (string keys, single-line scalar values). A skill that uses
    multi-line values would fail the format test on its own merits
    and is out of scope for the scanner.

    Returns ``({}, text)`` when no frontmatter block is present so
    callers can still scan the body.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw = match.group("body")
    frontmatter: dict[str, str] = {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        frontmatter[key.strip()] = value.strip().strip("\"'")

    body = text[match.end() :]
    return frontmatter, body


# ---------------------------------------------------------------------------
# Body scanners
# ---------------------------------------------------------------------------


# We register one regex per rule_id so allowlisting is targeted. The
# tuples are ``(rule_id, compiled_regex)`` — the categories are
# attached by the dispatching function so a single regex can never
# accidentally claim two categories.
_PROMPT_INJECTION_PATTERNS: Final = (
    (
        "prompt-inject-ignore-prev",
        re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions?\b", re.IGNORECASE),
    ),
    (
        "prompt-inject-forget-all",
        re.compile(
            r"\bforget\s+(?:everything|all\s+(?:previous\s+)?(?:instructions?|guidelines?|rules?))\b",
            re.IGNORECASE,
        ),
    ),
    ("prompt-inject-new-system", re.compile(r"\bnew\s+system\s+prompt\b", re.IGNORECASE)),
    (
        "prompt-inject-you-are-now",
        re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\s+", re.IGNORECASE),
    ),
    (
        "prompt-inject-disregard",
        re.compile(
            r"\bdisregard\s+(?:all\s+)?(?:prior|previous|above)\s+(?:instructions?|directives?)\b",
            re.IGNORECASE,
        ),
    ),
)

# Command-injection patterns target the most common ``curl ... | sh`` /
# ``$(curl ... | bash)`` style snippets that adversarial skills use to
# get the host to paste a remote payload into a terminal. We require
# the pipe-to-shell pattern explicitly so a benign ``curl`` documented
# in the skill body does not trip the rule.
_COMMAND_INJECTION_PATTERNS: Final = (
    ("cmd-inject-curl-pipe-sh", re.compile(r"curl\s+[^\n|]*\|\s*(?:bash|sh|zsh)\b", re.IGNORECASE)),
    ("cmd-inject-wget-pipe-sh", re.compile(r"wget\s+[^\n|]*\|\s*(?:bash|sh|zsh)\b", re.IGNORECASE)),
    ("cmd-inject-curl-exec-subshell", re.compile(r"\$\(\s*curl\s+[^)]+\)", re.IGNORECASE)),
    ("cmd-inject-rm-rf-root", re.compile(r"\brm\s+-rf\s+/(?:\s|$|[^a-zA-Z0-9._-])")),
    (
        "cmd-inject-eval-base64",
        re.compile(
            r"\beval\s+[\"']?\$\(\s*(?:echo|printf)\s+[^|]+\|\s*base64\s+-?-d", re.IGNORECASE
        ),
    ),
)

# Secret patterns: provider prefixes that are stable enough to detect
# without huge false-positive rates. Anything matching here in a skill
# body is almost certainly a leaked credential because skills should
# never embed live secrets — they should reference ``opshub.core.secrets``.
_HARDCODED_SECRET_PATTERNS: Final = (
    ("secret-openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("secret-anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("secret-github-pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("secret-slack-token", re.compile(r"\bxox[abprso]-[0-9A-Za-z-]{10,}\b")),
    ("secret-aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret-google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "secret-private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    (
        "secret-bearer-header",
        re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
    ),
)

# Data exfiltration: skills that post the conversation to a remote
# URL. We require an HTTP verb word near the URL so a benign
# ``https://...`` link in documentation is not flagged.
_DATA_EXFIL_PATTERNS: Final = (
    (
        "exfil-curl-post-stdin",
        re.compile(
            r"curl\s+(?:-X\s+POST|--data(?:-binary)?|-d)\s+[^|\n]*@(?:-|/dev/stdin)", re.IGNORECASE
        ),
    ),
    ("exfil-curl-data-stdin", re.compile(r"curl\s+[^|\n]*-d\s+@(?:-|/dev/stdin)", re.IGNORECASE)),
    (
        "exfil-webhook-call",
        re.compile(
            r"(?:POST|GET|PUT|send|upload|exfiltrate)\s+(?:the\s+)?(?:transcript|conversation|context|history|messages?)\s+to\s+https?://",
            re.IGNORECASE,
        ),
    ),
    (
        "exfil-discord-webhook",
        re.compile(r"https://(?:discord(?:app)?\.com|hooks\.slack\.com|webhook\.site)/[^\s)]+"),
    ),
)


def _scan_body_lines(body: str, *, base_line_offset: int) -> list[Finding]:
    """Scan ``body`` text line-by-line for the four categories.

    ``base_line_offset`` lets the caller report 1-based line numbers
    relative to the original SKILL.md (the body starts after the
    frontmatter so we need to shift).
    """
    findings: list[Finding] = []

    rule_groups: tuple[tuple[FindingCategory, tuple[tuple[str, re.Pattern[str]], ...]], ...] = (
        (FindingCategory.PROMPT_INJECTION, _PROMPT_INJECTION_PATTERNS),
        (FindingCategory.COMMAND_INJECTION, _COMMAND_INJECTION_PATTERNS),
        (FindingCategory.HARDCODED_SECRET, _HARDCODED_SECRET_PATTERNS),
        (FindingCategory.DATA_EXFILTRATION, _DATA_EXFIL_PATTERNS),
    )

    for relative_idx, line in enumerate(body.splitlines(), start=1):
        line_number = base_line_offset + relative_idx
        for category, patterns in rule_groups:
            for rule_id, pattern in patterns:
                if not pattern.search(line):
                    continue
                if _is_allowlisted(line, category, rule_id):
                    continue
                findings.append(
                    Finding(
                        category=category,
                        line_number=line_number,
                        snippet=line.strip(),
                        rule_id=rule_id,
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Frontmatter scanners
# ---------------------------------------------------------------------------

# Unicode codepoints that have no business appearing in a skill name or
# description. Listing them explicitly is faster than walking the full
# ``unicodedata`` categories at scan time and keeps the policy auditable.
_HIDDEN_UNICODE_CODEPOINTS: Final = frozenset(
    {
        0x00AD,  # SOFT HYPHEN
        0x200B,  # ZERO WIDTH SPACE
        0x200C,  # ZERO WIDTH NON-JOINER
        0x200D,  # ZERO WIDTH JOINER
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
        0x202A,  # LEFT-TO-RIGHT EMBEDDING
        0x202B,  # RIGHT-TO-LEFT EMBEDDING
        0x202C,  # POP DIRECTIONAL FORMATTING
        0x202D,  # LEFT-TO-RIGHT OVERRIDE
        0x202E,  # RIGHT-TO-LEFT OVERRIDE
        0x2066,  # LEFT-TO-RIGHT ISOLATE
        0x2067,  # RIGHT-TO-LEFT ISOLATE
        0x2068,  # FIRST STRONG ISOLATE
        0x2069,  # POP DIRECTIONAL ISOLATE
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
    }
)


def _has_hidden_unicode(value: str) -> bool:
    """Return ``True`` when ``value`` contains a hidden / bidi codepoint.

    Also flags tag characters (``U+E0000``..``U+E007F``) — the Unicode
    Tag block has been used in published exploits to embed hidden
    instructions inside what looks like a plain ASCII description.
    """
    for ch in value:
        cp = ord(ch)
        if cp in _HIDDEN_UNICODE_CODEPOINTS:
            return True
        if 0xE0000 <= cp <= 0xE007F:
            return True
        # Generic catch-all: any character classified as a control
        # other than horizontal whitespace.
        category = unicodedata.category(ch)
        if category == "Cf" and cp not in {0x00AD}:  # 0x00AD already listed above
            return True
    return False


# Frontmatter description fields are short, so we re-use the
# prompt-injection patterns directly — every match is suspicious.
def _scan_frontmatter(
    frontmatter: dict[str, str],
    *,
    frontmatter_text_lines: list[str],
) -> list[Finding]:
    """Inspect ``name``/``description`` for hidden unicode + instruction overrides."""
    findings: list[Finding] = []

    # Build a name → first-line-index map so we can report accurate
    # 1-based line numbers (the leading ``---`` is line 1).
    field_to_line: dict[str, int] = {}
    for idx, raw_line in enumerate(frontmatter_text_lines, start=2):  # +1 for the opening ``---``
        stripped = raw_line.strip()
        if ":" not in stripped or stripped.startswith("#"):
            continue
        key = stripped.split(":", 1)[0].strip()
        field_to_line.setdefault(key, idx)

    for field_name in ("name", "description"):
        value = frontmatter.get(field_name)
        if value is None:
            continue
        line_no = field_to_line.get(field_name, 1)
        if _has_hidden_unicode(value):
            findings.append(
                Finding(
                    category=FindingCategory.FRONTMATTER_HIDDEN_UNICODE,
                    line_number=line_no,
                    snippet=f"{field_name}: <redacted-with-hidden-codepoint>",
                    rule_id=f"frontmatter-hidden-unicode-{field_name}",
                )
            )

        for rule_id, pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(value):
                findings.append(
                    Finding(
                        category=FindingCategory.FRONTMATTER_INSTRUCTION_OVERRIDE,
                        line_number=line_no,
                        snippet=f"{field_name}: {value}",
                        rule_id=f"frontmatter-{rule_id}",
                    )
                )
                break  # one finding per field is enough; over-counting noise

    return findings


# ---------------------------------------------------------------------------
# Public scan helpers
# ---------------------------------------------------------------------------


def scan_skill_text(text: str, *, path: Path | None = None) -> ScanResult:
    """Scan a SKILL.md text blob.

    The function does not require the file to actually exist; callers
    can pass synthesised content (e.g. from a unit test) by leaving
    ``path=None``.
    """
    frontmatter, body = parse_frontmatter(text)

    # Recover the frontmatter slice for accurate line reporting.
    frontmatter_text_lines: list[str] = []
    body_offset = 0
    if frontmatter:
        match = _FRONTMATTER_RE.match(text)
        if match is not None:
            frontmatter_text_lines = match.group("body").splitlines()
            # The body starts after the closing ``---`` and a newline.
            body_offset = text[: match.end()].count("\n")

    frontmatter_findings = _scan_frontmatter(
        frontmatter,
        frontmatter_text_lines=frontmatter_text_lines,
    )
    body_findings = _scan_body_lines(body, base_line_offset=body_offset)

    return ScanResult(
        path=path,
        findings=tuple(frontmatter_findings + body_findings),
    )


def scan_skill_file(path: Path) -> ScanResult:
    """Convenience wrapper that reads a file then dispatches to :func:`scan_skill_text`."""
    text = path.read_text(encoding="utf-8")
    return scan_skill_text(text, path=path)


# ---------------------------------------------------------------------------
# Helpers for callers that aggregate multiple files
# ---------------------------------------------------------------------------


def collate_findings(results: Iterable[ScanResult]) -> dict[FindingCategory, int]:
    """Return a per-category count across a sequence of results."""
    totals: dict[FindingCategory, int] = dict.fromkeys(FindingCategory, 0)
    for result in results:
        for finding in result.findings:
            totals[finding.category] += 1
    return totals
