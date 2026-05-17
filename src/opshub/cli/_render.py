"""Shared CLI list-renderers.

Phase 2 step 3 — multiple subcommands (``task list``, ``inbox list``,
upcoming ``decision list`` / ``workspace`` listings) all materialise a
small set of rows and render them in one of three formats:

* ``table`` — aligned fixed-width columns for terminal eyeballing.
* ``json`` — a JSON array of row dicts, suitable for piping into ``jq``.
* ``md`` — a GitHub-flavoured Markdown table for PR descriptions and
  agent transcripts.

Rather than duplicating that pattern across every subcommand (Phase 1's
``cli/_task_list.py`` was the first implementation), this module exposes
a tiny :class:`Column` descriptor + three pure rendering functions. A
subcommand declares its columns once and calls :func:`dispatch` with the
requested format and the rows; the rendering is uniform across the CLI.

The module is intentionally dependency-light: only stdlib + ``typing``.
Each subcommand still owns its own row-fetching code (SQLAlchemy queries
talk to the projection tables), but the rendering layer no longer
re-implements column alignment / JSON serialisation / Markdown escaping
per command.

ADR-0001 cold-start discipline:

The CLI subcommand modules (``cli/task.py``, ``cli/inbox.py`` etc.) keep
their module-level imports tiny so ``opshub --help`` stays under the
300ms budget. ``_render`` lives behind the ``_`` prefix and is only
loaded via the lazy-import inside a command callback — so the helper
modules it pulls in (``json``, ``datetime``) never enter the cold-start
path. The companion test in ``tests/integration/test_cli_imports.py``
only checks public CLI modules; private helpers like this one are free
to ``import json`` at the top.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opshub.services.briefings import Briefing
    from opshub.services.proposals import Proposal

__all__ = [
    "Column",
    "ProposalSummary",
    "dispatch",
    "format_date",
    "id_prefix",
    "render_briefing_json",
    "render_briefing_md",
    "render_json",
    "render_md",
    "render_proposal_json",
    "render_proposal_list_json",
    "render_proposal_list_md",
    "render_proposal_md",
    "render_table",
    "truncate",
]


_ALLOWED_FORMATS = ("table", "json", "md")
_DEFAULT_ID_PREFIX_LEN = 8
_ELLIPSIS = "..."


@dataclass(frozen=True)
class Column:
    """Describes one rendered column.

    Attributes
    ----------
    header:
        Display label. Used as the table header, the Markdown header, and
        — lower-cased and underscored — as the JSON key.
    accessor:
        Callable taking the row (a mapping or arbitrary object) and
        returning the raw value for the cell. Letting the caller supply
        the access function avoids a hardcoded ``row[key]`` shape and
        lets the renderer accept dicts, dataclasses, or SQLAlchemy
        ``RowMapping`` instances interchangeably.
    width:
        Fixed display width in the ``table`` format. ``None`` means
        "auto" — the renderer falls back to ``len(header)``.
    md_align:
        Markdown column alignment hint (``"left"`` / ``"right"`` /
        ``"center"``) — controls the separator row formatting.
    json_key:
        Optional override for the JSON key. Defaults to a derived form
        of ``header`` (lower-cased, spaces → underscores).
    """

    header: str
    accessor: Callable[[Any], Any]
    width: int | None = None
    md_align: str = "left"
    json_key: str | None = None

    @property
    def effective_json_key(self) -> str:
        """Return the JSON key the column will emit.

        Defaults to ``header.lower().replace(" ", "_")`` so a column
        header of ``"Updated At"`` becomes ``"updated_at"`` — matching
        the existing ``opshub task list --format json`` shape.
        """
        if self.json_key is not None:
            return self.json_key
        return self.header.lower().replace(" ", "_")

    @property
    def effective_width(self) -> int:
        """Return the column width to use in the ``table`` format.

        ``None`` widths fall back to the header length so the table
        always has *something* to align against. Callers can still
        explicitly set a width to truncate long content.
        """
        return self.width if self.width is not None else len(self.header)


def dispatch(fmt: str, columns: Sequence[Column], rows: Sequence[Any]) -> str:
    """Render ``rows`` in ``fmt`` using ``columns``.

    Raises
    ------
    ValueError
        If ``fmt`` is not one of ``"table"``, ``"json"``, ``"md"``.
        Callers (subcommand modules) catch this and re-raise as
        :class:`opshub.core.errors.ValidationError` so the CLI exit-code
        mapping in ``cli/app.main()`` takes effect.
    """
    if fmt not in _ALLOWED_FORMATS:
        raise ValueError(f"invalid format {fmt!r}; expected one of {', '.join(_ALLOWED_FORMATS)}")
    if fmt == "json":
        return render_json(rows, columns)
    if fmt == "md":
        return render_md(rows, columns)
    return render_table(rows, columns)


def render_table(rows: Sequence[Any], columns: Sequence[Column]) -> str:
    """Render rows as an aligned fixed-width plain-text table.

    The header is always rendered (even when ``rows`` is empty) so a
    user piping into ``head`` sees the column names; this matches the
    existing ``task list`` behaviour.
    """
    header_cells = [f"{col.header.upper():<{col.effective_width}}" for col in columns]
    header_line = "  ".join(header_cells)
    if not rows:
        return header_line

    body_lines: list[str] = []
    for row in rows:
        cells: list[str] = []
        for col in columns:
            raw = col.accessor(row)
            text = _stringify(raw)
            if col.width is not None:
                text = truncate(text, col.width)
            cells.append(f"{text:<{col.effective_width}}")
        body_lines.append("  ".join(cells))
    return "\n".join([header_line, *body_lines])


def render_json(rows: Sequence[Any], columns: Sequence[Column]) -> str:
    """Render rows as a JSON array of objects keyed by column JSON-key.

    Datetime / date values are serialised via :func:`datetime.isoformat`
    so the JSON survives a ``jq`` pipe without losing tzinfo (when
    present). Everything else is passed through to :func:`json.dumps`
    which raises for un-serialisable types — that is intentional;
    callers should pick accessors that return JSON-native values.
    """
    payload: list[dict[str, Any]] = []
    for row in rows:
        payload.append({col.effective_json_key: _jsonable(col.accessor(row)) for col in columns})
    return json.dumps(payload, ensure_ascii=False)


def render_md(rows: Sequence[Any], columns: Sequence[Column]) -> str:
    """Render rows as a GitHub-flavoured Markdown table.

    The separator row encodes ``md_align`` per column:
    ``:---`` (left), ``---:`` (right), ``:---:`` (center). The base
    ``---`` form (no colon) renders as left-aligned in GitHub, so
    callers that don't care about alignment can leave ``md_align`` at
    its default and get the same look as the existing ``task list``
    renderer.
    """
    header_line = "| " + " | ".join(col.header for col in columns) + " |"
    separator_line = "| " + " | ".join(_md_separator(col) for col in columns) + " |"
    if not rows:
        return "\n".join([header_line, separator_line])

    body_lines: list[str] = []
    for row in rows:
        cells = [_escape_md(_stringify(col.accessor(row))) for col in columns]
        body_lines.append("| " + " | ".join(cells) + " |")
    return "\n".join([header_line, separator_line, *body_lines])


# ---- public helpers -------------------------------------------------------


def id_prefix(value: str, length: int = _DEFAULT_ID_PREFIX_LEN) -> str:
    """Return the first ``length`` characters of a ULID-shaped string.

    Most CLI list views render an 8-character ULID prefix — long enough
    to disambiguate within a user's working set, short enough to scan
    quickly. Exported so callers can build :class:`Column` accessors
    without re-implementing the trim.
    """
    return value[:length]


def truncate(value: str, width: int) -> str:
    """Truncate ``value`` to ``width`` characters, suffixing ``...`` if cut.

    When ``width`` is smaller than the ellipsis itself, we fall back to
    a hard slice — ``...``-ing a 2-char column would look worse than
    just clipping.
    """
    if len(value) <= width:
        return value
    if width <= len(_ELLIPSIS):
        return value[:width]
    return value[: width - len(_ELLIPSIS)] + _ELLIPSIS


def format_date(value: Any) -> str:
    """Render a datetime / date / other value as ``YYYY-MM-DD``.

    SQLite's stdlib driver may surface ``DateTime(timezone=True)`` columns
    as naive datetimes whose components reflect UTC; both naive and
    aware datetimes are accepted. Anything else (string, ``None``, ...)
    falls through to ``str(value)`` so a single bad cell never crashes
    the render path.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# ---- private helpers ------------------------------------------------------


def _stringify(value: Any) -> str:
    """Coerce a cell value to a string for table / md rendering.

    Datetimes use the project-wide ``YYYY-MM-DD`` short form (full
    ISO strings are kept for the JSON path). ``None`` collapses to an
    empty string so an absent value doesn't bloat the column width.
    """
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return format_date(value)
    return str(value)


def _jsonable(value: Any) -> Any:
    """Return a JSON-serialisable representation of ``value``.

    Datetimes / dates are emitted as ISO strings (preserving precision
    and tzinfo when the source had it). Other types pass through —
    :func:`json.dumps` will raise on un-serialisable types, which is
    the right failure mode (the column accessor should be fixed, not
    the renderer).
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _md_separator(col: Column) -> str:
    """Build the Markdown separator cell honouring ``md_align``."""
    if col.md_align == "right":
        return "---:"
    if col.md_align == "center":
        return ":---:"
    return "---"


def _escape_md(value: str) -> str:
    """Escape pipe characters so cell content doesn't break the table.

    Pipes are the column delimiter in Markdown tables; replacing them
    with an escaped form (``\\|``) keeps arbitrary user input from
    corrupting the rendered output.
    """
    return value.replace("|", "\\|")


# ---- briefing renderers (Phase 5 step B4) ---------------------------------
#
# Briefings are single-record outputs (one :class:`Briefing` instance per
# ``opshub brief`` invocation), not tabular lists, so they don't fit the
# :class:`Column` / :func:`dispatch` shape. We expose two dedicated
# helpers — :func:`render_briefing_md` (raw markdown for the eyeball
# path) and :func:`render_briefing_json` (full record dump for
# pipe-into-tooling) — keeping the brief CLI body slim and the JSON
# schema centralised here for future ``opshub brief history``
# regression tests.


def render_briefing_md(briefing: Briefing) -> str:
    """Render a :class:`Briefing` for stdout output.

    The :class:`Briefing.markdown` field already carries the
    LLM-generated body (per ADR-0015 the model is instructed to emit
    Markdown). Returning the field unchanged keeps the CLI honest —
    operators can pipe ``opshub brief "topic" > out.md`` and get a
    clean Markdown file with no extra framing.
    """
    return briefing.markdown


def render_briefing_json(briefing: Briefing) -> str:
    """Render a :class:`Briefing` as a JSON document.

    Surfaces the full record (id, topic, scope, model identifiers,
    token usage, source refs, markdown body, generated timestamp) so
    callers piping into ``jq`` can introspect the cost trace or pluck
    individual source refs. The shape mirrors
    :class:`opshub.domain.events.briefing.BriefingGenerated` so a
    follow-up Phase 5.x ``opshub brief history --format json`` can
    emit the same keys.

    ``source_refs`` is emitted as a list of
    ``{"entity_type": ..., "entity_id": ...}`` objects rather than a
    flat tuple-of-tuples, which is friendlier to most JSON consumers
    (Python is the unusual one in allowing heterogeneous tuples in
    its serialisers).

    ``ensure_ascii=False`` so a topic that survives non-ASCII content
    (e.g. CJK characters in a quoted source body) stays human-readable
    in the output.
    """
    return json.dumps(
        {
            "briefing_id": briefing.briefing_id,
            "topic": briefing.topic,
            "scope": briefing.scope,
            "model_id": briefing.model_id,
            "model_version": briefing.model_version,
            "tokens_in": briefing.tokens_in,
            "tokens_out": briefing.tokens_out,
            "source_refs": [
                {"entity_type": entity_type, "entity_id": entity_id}
                for entity_type, entity_id in briefing.source_refs
            ],
            "markdown": briefing.markdown,
            "generated_at": briefing.generated_at.isoformat(),
        },
        indent=2,
        ensure_ascii=False,
    )


# ---- proposal renderers (Phase 6 step B4) --------------------------------
#
# Proposals come in two shapes here:
#
# * The freshly generated :class:`Proposal` returned by
#   :meth:`ProposalService.generate` — a single record with full
#   ``candidates`` payload + cost trace, rendered for the
#   ``opshub propose generate`` CLI body.
# * The ``proposals`` projection rows surfaced by
#   ``opshub propose list`` — a many-row summary view. The
#   :class:`ProposalSummary` dataclass below is the CLI-local row shape;
#   it lives here rather than in :mod:`opshub.cli.propose` so the
#   renderer + the row dataclass stay co-located (mirrors the
#   ``Column`` + ``dispatch`` pairing above).


# Length of the proposal-id short prefix shown in user-facing output.
# 6 hex/Base32 characters is enough to disambiguate within an
# operator's working window without overwhelming a one-row summary
# (mirrors the convention used by `git log --oneline`).
_PROPOSAL_ID_SHORT_LEN = 6
_PROPOSAL_TOPIC_TRUNCATE = 40
_CANDIDATE_BODY_TRUNCATE = 200


@dataclass(frozen=True)
class ProposalSummary:
    """One row of the ``opshub propose list`` view.

    The CLI builds these from a SELECT against the ``proposals``
    projection (``id`` / ``topic`` / ``candidate_states`` /
    ``generated_at``); the renderer below knows how to flatten the
    parallel ``candidate_states`` list into the ``Np/Mp/Kr`` summary
    cell. Kept here rather than on the projection module so the
    projection layer stays purely data-shape (no CLI concerns).
    """

    proposal_id: str
    topic: str
    candidate_states: list[str]
    generated_at: datetime | None


def render_proposal_md(proposal: Proposal) -> str:
    """Render a freshly generated :class:`Proposal` for stdout (markdown).

    The output is hand-formatted (not via :func:`dispatch`) because the
    candidates are heterogeneous: a task candidate has ``title`` /
    ``body``, a decision candidate has ``text`` / ``context``. The
    rendered shape is designed for an agent that needs to pick a next
    action without re-querying — the proposal id and per-candidate
    index appear verbatim so the operator can paste them straight into
    ``opshub propose apply <id> <index>``.

    The trailing usage hint mentions both ``apply`` and ``reject`` so
    a first-time user discovers both lifecycle verbs from the help
    output alone.
    """
    lines: list[str] = [
        f'# Proposals for "{proposal.topic}"',
        "",
        f"Proposal: {proposal.proposal_id}",
        (
            f"Model: {proposal.model_id} "
            f"(in: {proposal.tokens_in} tokens, out: {proposal.tokens_out} tokens)"
        ),
        f"Generated: {proposal.generated_at.isoformat()}",
    ]
    if proposal.briefing_id is not None:
        lines.append(f"Briefing: {proposal.briefing_id}")
    if proposal.scope and proposal.scope != "all":
        lines.append(f"Scope: {proposal.scope}")
    lines.append("")

    for index, candidate in enumerate(proposal.candidates):
        kind = getattr(candidate, "kind", "?")
        if kind == "task":
            title = getattr(candidate, "title", "")
            body = getattr(candidate, "body", None)
            lines.append(f"[{index}] task: {title}")
            if body is not None and str(body).strip():
                truncated = truncate(str(body), _CANDIDATE_BODY_TRUNCATE)
                lines.append(f"    body: {truncated}")
        elif kind == "decision":
            text = getattr(candidate, "text", "")
            context = getattr(candidate, "context", None)
            truncated_text = truncate(str(text), _CANDIDATE_BODY_TRUNCATE)
            lines.append(f"[{index}] decision: {truncated_text}")
            if context is not None and str(context).strip():
                truncated_context = truncate(str(context), _CANDIDATE_BODY_TRUNCATE)
                lines.append(f"    context: {truncated_context}")
        else:
            # Phase 6.x candidate kinds (e.g. inbox_item / source) land
            # here; the renderer falls back to a JSON dump of the
            # payload so an unexpected kind is still legible.
            lines.append(f"[{index}] {kind}: {candidate!r}")

    lines.extend(
        [
            "",
            f"To apply:  opshub propose apply {proposal.proposal_id} <index>",
            (f"To reject: opshub propose reject {proposal.proposal_id} <index> [--reason ...]"),
        ]
    )
    return "\n".join(lines)


def render_proposal_json(proposal: Proposal) -> str:
    """Render a freshly generated :class:`Proposal` as a JSON document.

    Surfaces the full record (id, topic, scope, optional briefing
    link, model identifiers, token usage, candidate list, generated
    timestamp) so callers piping into ``jq`` can introspect the cost
    trace or extract a specific candidate body. Each candidate is
    rendered as the dict produced by
    :meth:`pydantic.BaseModel.model_dump` so the ``kind`` /
    ``schema_version`` discriminators survive the JSON round-trip
    (ADR-0016 §決定 (f)).

    ``ensure_ascii=False`` so a CJK topic / candidate body stays
    human-readable in the output.
    """
    candidates_payload: list[Any] = []
    for candidate in proposal.candidates:
        # All current candidate types are Pydantic v2 models; fall back
        # to a best-effort string cast for any future non-Pydantic
        # candidate shape so the renderer never raises on the JSON
        # path.
        dump = getattr(candidate, "model_dump", None)
        if callable(dump):
            candidates_payload.append(dump(mode="json"))
        else:
            candidates_payload.append(str(candidate))
    return json.dumps(
        {
            "proposal_id": proposal.proposal_id,
            "topic": proposal.topic,
            "scope": proposal.scope,
            "briefing_id": proposal.briefing_id,
            "model_id": proposal.model_id,
            "model_version": proposal.model_version,
            "tokens_in": proposal.tokens_in,
            "tokens_out": proposal.tokens_out,
            "candidates": candidates_payload,
            "generated_at": proposal.generated_at.isoformat(),
        },
        indent=2,
        ensure_ascii=False,
    )


def render_proposal_list_md(rows: Sequence[ProposalSummary]) -> str:
    """Render a list of :class:`ProposalSummary` rows as a Markdown table.

    Each row is one proposal. The ``Candidates`` column collapses the
    parallel ``candidate_states`` list into a single ``Np/Ma/Kr`` cell
    (pending / applied / rejected counts) so an operator scanning the
    table can spot proposals that still have actionable candidates
    without expanding every row.

    The renderer uses :func:`dispatch` with ``fmt="md"`` so the
    output uses the GitHub-flavoured Markdown table shape that every
    other ``list`` view emits.
    """
    columns = _proposal_summary_columns()
    return dispatch("md", columns, list(rows))


def render_proposal_list_json(rows: Sequence[ProposalSummary]) -> str:
    """Render a list of :class:`ProposalSummary` rows as a JSON array.

    The shape matches the Markdown columns (id prefix, full topic,
    per-state counts, generated_at ISO string) plus the full
    ``candidate_states`` list so a ``jq`` consumer can re-derive the
    breakdown. The proposal id is emitted in full (not the short
    prefix) because the JSON path is the machine-readable surface.
    """
    payload: list[dict[str, Any]] = []
    for row in rows:
        counts = _candidate_state_counts(row.candidate_states)
        payload.append(
            {
                "proposal_id": row.proposal_id,
                "topic": row.topic,
                "candidate_count": len(row.candidate_states),
                "states": counts,
                "candidate_states": list(row.candidate_states),
                "generated_at": (
                    row.generated_at.isoformat() if row.generated_at is not None else None
                ),
            }
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _proposal_summary_columns() -> list[Column]:
    """Column descriptors for the ``opshub propose list`` Markdown view.

    Kept as a function (not a module-level constant) so the dataclass
    field accessors are constructed lazily — the dataclass forward
    ref :class:`ProposalSummary` is fully defined by the time the
    function is called.
    """
    return [
        Column(
            header="ID",
            accessor=lambda row: id_prefix(row.proposal_id, _PROPOSAL_ID_SHORT_LEN),
            width=_PROPOSAL_ID_SHORT_LEN,
            json_key="proposal_id",
        ),
        Column(
            header="Topic",
            accessor=lambda row: truncate(row.topic, _PROPOSAL_TOPIC_TRUNCATE),
            width=_PROPOSAL_TOPIC_TRUNCATE,
            json_key="topic",
        ),
        Column(
            header="Candidates",
            accessor=lambda row: _format_candidate_states(row.candidate_states),
            width=12,
            json_key="states",
        ),
        Column(
            header="Generated",
            accessor=lambda row: format_date(row.generated_at),
            width=10,
            json_key="generated_at",
        ),
    ]


def _format_candidate_states(states: list[str]) -> str:
    """Render the candidate-state breakdown as ``Np/Ma/Kr``.

    A proposal with 2 pending / 1 applied / 0 rejected candidates
    renders as ``2p/1a/0r`` — the trailing letter is the first
    character of each state name so the cell stays compact.
    """
    counts = _candidate_state_counts(states)
    return f"{counts['pending']}p/{counts['applied']}a/{counts['rejected']}r"


def _candidate_state_counts(states: list[str]) -> dict[str, int]:
    """Tally candidate states; unknown state labels collapse silently.

    Used by both the Markdown column accessor and the JSON renderer
    so the counts are computed from a single code path.
    """
    counts = {"pending": 0, "applied": 0, "rejected": 0}
    for state in states:
        if state in counts:
            counts[state] += 1
    return counts
