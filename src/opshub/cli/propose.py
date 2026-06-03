"""``opshub propose ...`` subcommands — Phase 6 step B4 (ADR-0016).

The proposal CLI surfaces the four operator-facing verbs of the Phase 6
Action loop on top of the :class:`ProposalService` wired in step B3:

* ``opshub propose generate "<topic>"`` — calls
  :meth:`ProposalService.generate` with the supplied topic + optional
  briefing seed / candidate / token caps, renders the resulting
  :class:`Proposal` either as Markdown (default) or JSON.
* ``opshub propose list`` — queries the ``proposals`` projection
  read-model and prints a one-line-per-proposal table (id prefix,
  topic, candidate-state breakdown, ``generated_at``).
* ``opshub propose apply <proposal-id> <candidate-index>`` — calls
  :meth:`ProposalService.apply` and prints the new task / decision id
  on success.
* ``opshub propose reject <proposal-id> <candidate-index>`` — calls
  :meth:`ProposalService.reject` (optional ``--reason``) and confirms
  on stdout.

Shape rationale
---------------

Unlike :mod:`opshub.cli.brief`, ``propose`` has 4 sub-verbs so it is
registered as a nested :class:`typer.Typer` sub-app — the conventional
shape used by ``opshub task`` / ``opshub inbox`` / ``opshub decision``.

Module-level imports stay within the M6 cold-start whitelist
(``__future__`` / ``typer`` / ``typing`` / ``pathlib`` — the last two
are *declared* via the static import audit even though the body uses
them through ``from typing import Annotated``). Every ``opshub``
import lives inside the command function body so the cold-start
budget is paid only when the operator actually invokes a propose
subcommand (ADR-0001).

Exit-code contract
------------------

The four subcommands share a tight contract that matches
:mod:`opshub.cli.brief`:

* ``0`` — success.
* ``1`` — :class:`~opshub.core.errors.OpsHubError` raised by the
  service (e.g. LLM provider failed, candidate already applied /
  rejected, proposal not found, candidate index out of range).
* ``2`` — :class:`~opshub.core.errors.ConfigError`: most commonly the
  ``[llm] backend = "disabled"`` short-circuit on ``generate``; also
  surfaces when a forced env-var override lands a
  :class:`NoOpLLMClient` inside the service.

``generate`` short-circuits *before* :func:`build_proposal_service`
when ``[llm] backend = "disabled"`` so the operator running ``opshub
propose`` straight after install gets a clean stderr hint rather than
a stack trace from the wiring trying to resolve the LLM client.
"""

from __future__ import annotations

from typing import Annotated

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

propose_app = typer.Typer(
    name="propose",
    help="LLM-backed next-action proposals (generate / list / apply / reject).",
    no_args_is_help=True,
)


@propose_app.command("generate")
def propose_generate(
    topic: Annotated[
        str,
        typer.Argument(
            help=(
                "The topic to propose next-actions for (free-form text). "
                "Ignored when --reply-to is supplied (Phase 10 reply-draft mode)."
            ),
        ),
    ],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Scope of the proposal (Phase 6 MVP only supports 'all').",
        ),
    ] = "all",
    from_briefing: Annotated[
        str | None,
        typer.Option(
            "--from-briefing",
            help="ULID of a briefing whose markdown seeds the LLM prompt as extra context.",
        ),
    ] = None,
    reply_to: Annotated[
        str | None,
        typer.Option(
            "--reply-to",
            help=(
                "ULID of a source to draft a reply for (Phase 10 Sub-issue E, "
                "ADR-0016 §決定 (i)). Switches to reply-draft mode: the LLM "
                "produces ReplyDraftCandidatePayload candidates grounded in the "
                "named source. Apply records the draft locally; no external "
                "send is performed (ADR-0010 §禁止事項 7 Phase 10 改訂)."
            ),
        ),
    ] = None,
    max_candidates: Annotated[
        int,
        typer.Option(
            "--max-candidates",
            help="Cap on the number of candidates the LLM may return (default 5).",
        ),
    ] = 5,
    max_tokens: Annotated[
        int,
        typer.Option(
            "--max-tokens",
            help="LLM response token cap (caller pays the cost — see ADR-0015 §決定 (h)).",
        ),
    ] = 2000,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: md | json. Defaults to md.",
        ),
    ] = "md",
    expand_graph: Annotated[
        bool,
        typer.Option(
            "--expand-graph",
            help=(
                "Expand context via the knowledge graph: each recall hit's "
                "1-hop neighbours (referenced_in_briefing / references / "
                "applied_to links) are appended as additional sources "
                "(Phase 8, ADR-0017). In reply-draft mode the 1-hop walk "
                "starts from --reply-to and emits referenced_in_reply_draft "
                "links (Phase 10 step E2)."
            ),
        ),
    ] = False,
) -> None:
    """Generate a proposal for ``topic`` and render it to stdout.

    See the module docstring for the full exit-code contract. On
    success, the rendered candidate list is the only thing on stdout
    so a follow-up ``opshub propose apply <id> <index>`` can find the
    proposal id by piping into ``grep`` / ``jq``.
    """
    # Lazy imports keep CLI cold start fast (ADR-0001) and satisfy the
    # ``test_cli_imports`` module-level whitelist.
    from opshub.cli._render import render_proposal_json, render_proposal_md
    from opshub.cli._wiring import build_proposal_service
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError, OpsHubError

    if fmt not in {"md", "json"}:
        # Mirror the brief CLI's format guard — propose has no
        # ``table`` format because a single Proposal is one record,
        # not a list.
        typer.echo(
            f"Error: invalid --format {fmt!r}; expected one of 'md', 'json'",
            err=True,
        )
        raise typer.Exit(code=2)

    settings = OpsHubSettings()
    if settings.llm.backend == "disabled":
        # Short-circuit BEFORE :func:`build_proposal_service` so we
        # never open the SQLite engine / resolve embedder + LLM client
        # when the operator has no LLM backend configured. Hint is
        # symmetric to :mod:`opshub.cli.brief`.
        typer.echo(
            "[llm] backend is disabled; configure 'anthropic' or 'openai' "
            "in opshub.toml (or set OPSHUB_LLM_BACKEND env var) and run "
            "`opshub llm auth set <backend>` to store the API key.",
            err=True,
        )
        raise typer.Exit(code=2)

    service = build_proposal_service(actor="cli:proposals_generate")
    try:
        if reply_to is not None:
            # Phase 10 step E2: reply-draft mode. The `topic` argument
            # is ignored — the service derives the recall query from
            # the source row's title / body. ``from_briefing`` is
            # also ignored (reply-draft has its own context loading
            # via --expand-graph + style-example recall).
            proposal = service.generate_reply_draft(
                reply_to,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                expand_graph=expand_graph,
            )
        else:
            proposal = service.generate(
                topic,
                scope=scope,
                from_briefing_id=from_briefing,
                max_candidates=max_candidates,
                max_tokens=max_tokens,
                expand_graph=expand_graph,
            )
    except ConfigError as exc:
        # Defensive: env-var override bypassed the pre-check above.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        # LLM provider failure / schema validation / empty-candidates
        # — surfaced by ProposalService after recording ProposalFailed
        # so the audit row is durable before the re-raise.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if fmt == "json":
        typer.echo(render_proposal_json(proposal))
    else:
        typer.echo(render_proposal_md(proposal))


@propose_app.command("list")
def propose_list(
    state: Annotated[
        str | None,
        typer.Option(
            "--state",
            help="Filter to proposals with at least one candidate in this state.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Maximum number of proposals to display (default 20).",
        ),
    ] = 20,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: md | json. Defaults to md.",
        ),
    ] = "md",
) -> None:
    """List recent proposals from the ``proposals`` projection.

    The query is CLI-local — there is no dedicated query method on
    :class:`ProposalService` because the list view does not require
    service-layer validation (read-only projection scan).

    Rows are sorted by ``generated_at DESC`` so the most recent
    proposals appear at the top. ``--state`` filters by membership:
    a proposal is included iff any of its candidate states equals the
    filter (so an apply on one candidate of a 5-candidate proposal
    still surfaces under ``--state pending`` because the other 4
    remain).
    """
    # Lazy imports keep CLI cold start fast (ADR-0001).
    from opshub.cli._render import (
        ProposalSummary,
        render_proposal_list_json,
        render_proposal_list_md,
    )
    from opshub.cli._wiring import build_engine

    if fmt not in {"md", "json"}:
        typer.echo(
            f"Error: invalid --format {fmt!r}; expected one of 'md', 'json'",
            err=True,
        )
        raise typer.Exit(code=2)

    allowed_states = {"pending", "applied", "rejected"}
    if state is not None and state not in allowed_states:
        typer.echo(
            f"Error: invalid --state {state!r}; expected one of "
            f"{', '.join(sorted(allowed_states))}",
            err=True,
        )
        raise typer.Exit(code=2)

    # Lazy SQLAlchemy import lives inside the function so the
    # cold-start budget is not paid by ``opshub --help``.
    from sqlalchemy import select

    from opshub.projections.proposals import proposals_table

    engine = build_engine()
    statement = (
        select(
            proposals_table.c.id,
            proposals_table.c.topic,
            proposals_table.c.candidate_states,
            proposals_table.c.generated_at,
        )
        .order_by(proposals_table.c.generated_at.desc())
        .limit(limit)
    )
    with engine.connect() as conn:
        raw_rows = conn.execute(statement).all()

    summaries: list[ProposalSummary] = []
    for raw in raw_rows:
        proposal_id, topic, states, generated_at = raw
        state_list = list(states)
        # ``--state pending`` filters by membership rather than
        # equality: an apply on one candidate of a 5-candidate
        # proposal still leaves the other 4 pending, so the operator
        # expects to see it in the pending bucket.
        if state is not None and state not in state_list:
            continue
        summaries.append(
            ProposalSummary(
                proposal_id=str(proposal_id),
                topic=str(topic),
                candidate_states=state_list,
                generated_at=generated_at,
            )
        )

    if fmt == "json":
        typer.echo(render_proposal_list_json(summaries))
    else:
        typer.echo(render_proposal_list_md(summaries))


@propose_app.command("apply")
def propose_apply(
    proposal_id: Annotated[
        str,
        typer.Argument(help="ULID of the proposal."),
    ],
    candidate_index: Annotated[
        int,
        typer.Argument(help="Zero-based index of the candidate inside the proposal."),
    ],
) -> None:
    """Apply candidate ``candidate_index`` of ``proposal_id``.

    On success, prints the new entity kind (``task`` / ``decision``)
    and its ULID on stdout. The ULID is the only useful pipe target so
    we keep the success message terse (one line + indented detail).
    """
    # Lazy imports keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_proposal_service
    from opshub.core.errors import ConfigError, OpsHubError

    service = build_proposal_service(actor="cli:proposals_apply")
    try:
        applied_entity_type, applied_entity_id = service.apply(proposal_id, candidate_index)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    short_id = proposal_id[-6:] if len(proposal_id) >= 6 else proposal_id
    typer.echo(f"Applied candidate [{candidate_index}] from proposal {short_id}:")
    typer.echo(f"  created {applied_entity_type}: {applied_entity_id}")


@propose_app.command("reject")
def propose_reject(
    proposal_id: Annotated[
        str,
        typer.Argument(help="ULID of the proposal."),
    ],
    candidate_index: Annotated[
        int,
        typer.Argument(help="Zero-based index of the candidate inside the proposal."),
    ],
    reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            help="Optional free-form note for the audit log (<=1000 chars).",
        ),
    ] = None,
) -> None:
    """Reject candidate ``candidate_index`` of ``proposal_id``."""
    # Lazy imports keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_proposal_service
    from opshub.core.errors import ConfigError, OpsHubError

    service = build_proposal_service(actor="cli:proposals_reject")
    try:
        service.reject(proposal_id, candidate_index, reason=reason)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    short_id = proposal_id[-6:] if len(proposal_id) >= 6 else proposal_id
    typer.echo(f"Rejected candidate [{candidate_index}] from proposal {short_id}")
