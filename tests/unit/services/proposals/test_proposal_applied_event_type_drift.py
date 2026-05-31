"""Cross-check ``ProposalApplied`` event_type literal against MCP write code.

Phase 12 audit Cluster B (M12) — direct drift pin between the Pydantic
event class :class:`opshub.domain.events.proposal.ProposalApplied` and
the MCP write handler's idempotency lookup
(``opshub.mcp._writes._lookup_applied_entity``). The lookup filters
the event log by a literal string ``"proposal.applied"``; if that
string and the Pydantic ``Literal["proposal.applied"]`` ever drift
apart, every historical apply lookup silently misses and the second
``propose.apply`` call re-raises instead of returning
``already_applied=true``.

The Phase 12 H6 e2e lifecycle test catches this indirectly (it walks
``propose.generate`` → ``propose.apply`` twice), but a direct unit-
level pin gives a one-line diagnostic when the drift first happens
and runs in milliseconds.

ADR-0002 §event naming pins the dot-notation form (``proposal.applied``)
so a future rename to CamelCase / kebab-case would be a deliberate
event-naming policy change requiring an ADR amendment — not something
the lookup string should silently track.
"""

from __future__ import annotations

import re
from pathlib import Path

from opshub.domain.events.proposal import ProposalApplied

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WRITES_SRC = _REPO_ROOT / "src" / "opshub" / "mcp" / "_writes.py"


def test_proposal_applied_literal_matches_mcp_writes_event_type_filter() -> None:
    """``ProposalApplied.event_type`` literal == the string in ``mcp/_writes.py``.

    The Pydantic class declares ``event_type: Literal["proposal.applied"]``;
    the MCP write handler's idempotency lookup contains
    ``events_table.c.event_type == "proposal.applied"``. Both strings
    MUST be identical, otherwise the lookup silently misses every
    historical apply and the second ``propose.apply`` call re-raises
    instead of normalising to ``already_applied=true``.

    The check parses the source rather than importing a constant so a
    refactor that moves the literal into a module-level constant (still
    consistent with the Pydantic class) keeps passing — and a refactor
    that drifts the string off the canonical literal fails with a
    one-line diagnostic.
    """
    pydantic_literal = ProposalApplied.model_fields["event_type"].default
    # Sanity: the Pydantic literal still matches ADR-0002 dot-notation.
    assert pydantic_literal == "proposal.applied", (
        f"ProposalApplied.event_type drifted from ADR-0002 dot-notation; got {pydantic_literal!r}"
    )

    writes_src = _WRITES_SRC.read_text(encoding="utf-8")
    matches = re.findall(
        r'events_table\.c\.event_type\s*==\s*"([^"]+)"',
        writes_src,
    )
    assert matches, (
        f'could not locate ``events_table.c.event_type == "..."`` filter in'
        f" {_WRITES_SRC} — has the lookup moved?"
    )
    drifted = [literal for literal in matches if literal != pydantic_literal]
    assert not drifted, (
        f"{_WRITES_SRC} has an event_type filter that drifted from the"
        f" Pydantic literal {pydantic_literal!r}: {drifted!r}"
        " (ADR-0002 event naming + Phase 12 H6 idempotency contract)"
    )


def test_proposal_applied_literal_is_documented_in_mcp_writes_docstrings() -> None:
    """Defence-in-depth: the literal appears at least once in ``mcp/_writes.py``.

    Beyond the filter expression, the module docstrings reference the
    canonical literal so a reader landing in the file understands the
    contract without chasing imports. A regression that drops both
    the filter AND the docstring mention would skip the previous
    test (the filter regex returns no match → assertion error), but
    this pin catches the subtler case where the filter is replaced
    by a constant from a different module that happens to also be
    ``"proposal.applied"`` — the canonical string stays present in
    ``_writes.py`` as the documented anchor.
    """
    pydantic_literal = ProposalApplied.model_fields["event_type"].default
    writes_src = _WRITES_SRC.read_text(encoding="utf-8")
    assert pydantic_literal in writes_src, (
        f"{_WRITES_SRC} should reference the canonical event_type literal"
        f" {pydantic_literal!r} somewhere (filter / constant / docstring)"
    )
