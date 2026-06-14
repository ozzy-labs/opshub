"""Source aggregate events (Phase 3, ADR-0002, ADR-0010).

A "source" is an external item observed by a connector (a GitHub Issue, a
Slack message, a workspace markdown file, ...). Two events bound a
source's lifecycle in OpsHub:

- :class:`SourceObserved` — a connector saw (or re-saw) an external item.
  The projector upserts on the natural key
  ``(connector_name, external_id)`` so re-observations of the same item
  collapse into a single row.
- :class:`SourceReferenced` — a task / decision / inbox_item now points
  at this source. Recorded as a separate event so the graph of links
  remains queryable without joining through entity payloads.

``aggregate_id`` is the source's ULID for both events; the first
:class:`SourceObserved` mints it, subsequent observations of the same
``(connector_name, external_id)`` reuse it (the projector enforces this
via the unique index, ADR-0010).

External payloads are kept deliberately small (title + optional summary)
per the **External Content Minimization** principle (ADR-0005): OpsHub
stores enough to *recognise* the item, never the full body.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent

ProvenanceOrigin = Literal["external", "internal"]
"""Where an observed item's body came from (ADR-0020 §(e)).

``"external"`` — fetched from a third-party SaaS / local FS scan by a
connector. The body is **not** under operator control and may contain
adversarial content (content poisoning / indirect prompt injection).

``"internal"`` — produced inside opshub (operator-authored workspace
ingest, opshub-generated text). Trusted by default.
"""

#: Schema cap on :attr:`SourceObserved.author_handle` (Phase 25-A). 320
#: chars accommodates the longest valid email address (RFC 5321: 64-char
#: local-part + ``@`` + 255-char domain) — the widest ``author_handle``
#: any connector ships — without letting a pathological payload bloat the
#: event log. Mappers truncate to this cap before construction so an
#: over-long sender never raises a mid-sync ``ValidationError``.
AUTHOR_HANDLE_MAX_CHARS = 320

#: Schema cap on :attr:`SourceObserved.author_display` (Phase 25-A). A
#: display name is a recognition cue, never a join key, so 200 chars
#: (the same budget as ``summary``) is generous. Mappers truncate to
#: this cap before construction.
AUTHOR_DISPLAY_MAX_CHARS = 200


ProvenanceTrust = Literal["trusted", "untrusted"]
"""Trust level applied to an observed item's body (ADR-0020 §(e)).

``"untrusted"`` content MUST be surfaced to an agent / LLM context as
reference material, never as instructions (ADR-0015 §決定 (f)
do-not-follow preamble). ``"trusted"`` content is operator-authored or
opshub-generated.
"""


class SourceObserved(DomainEvent):
    """A connector observed an external item.

    ``connector_name`` is the connector's stable identifier (e.g.
    ``"github"``). ``external_id`` is the connector's native ID for the
    item (e.g. ``"owner/repo#42"`` for a GitHub Issue). Together they
    form the natural key the projector upserts on.

    ``source_type`` is a free-form connector-defined tag (``"issue"``,
    ``"pull_request"``, ``"notification"``, ...) — kept as ``str`` rather
    than ``Literal`` so each connector can extend the vocabulary without
    a schema bump.

    Known discriminators as of Phase 13:

    * Phase 3 GitHub: ``"issue"`` / ``"pull_request"`` / ``"notification"``
    * Phase 7 Slack: ``"slack_message"``
    * Phase 7 MS365: ``"ms365_calendar"`` / ``"ms365_onedrive"`` /
      ``"ms365_outlook"``
    * Phase 7 Box: ``"box_event"``
    * Phase 9 box_drive: ``"box_drive_file"``
    * Phase 11 Teams (F5, ADR-0010 §改訂 (a)): ``"teams_message"``
    * Phase 11 Office (F2, ADR-0025 §決定 (d)):
      ``"word_document"`` / ``"excel_spreadsheet"`` /
      ``"powerpoint_slide_deck"`` — produced by F4 mappers when they
      route through :mod:`opshub.core.document_extract`. The
      authoritative extension → discriminator table lives at
      :data:`opshub.core.document_extract.SOURCE_TYPE_BY_EXTENSION`
      so mappers MUST import it rather than re-deriving the string.
    * Phase 13 Google Workspace (G2 #276, ADR-0025 §決定 (d')):
      ``"google_doc"`` / ``"google_slides"`` / ``"google_sheets"`` —
      produced by the Phase 13 G4 (#278) Drive API mapper when it
      routes Workspace native Docs / Slides / Sheets through
      :func:`opshub.core.document_extract.extract_workspace_export`.
      The three literals are published as
      :data:`opshub.core.document_extract.GOOGLE_DOC_SOURCE_TYPE` /
      :data:`opshub.core.document_extract.GOOGLE_SLIDES_SOURCE_TYPE` /
      :data:`opshub.core.document_extract.GOOGLE_SHEETS_SOURCE_TYPE`
      (``Final[Literal[...]]``) with the runtime tuple
      :data:`opshub.core.document_extract.GOOGLE_WORKSPACE_SOURCE_TYPES`,
      and the authoritative ``mimeType`` → ``source_type`` lookup table
      lives at
      :data:`opshub.core.document_extract.GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE`
      so the Phase 13 G3 (#277) Drive API mapper MUST import them
      rather than re-deriving the strings (same pattern as
      :data:`opshub.core.document_extract.SOURCE_TYPE_BY_EXTENSION` for
      the Phase 11 Office path).
    * Phase 13 Google Workspace catch-all (G4 #278, ADR-0025 §決定 (d')):
      ``"google_workspace_file"`` — stamped by the Drive API mapper on
      any non-native Drive item (uploaded PDFs, binary blobs, shortcuts,
      ...) where :func:`files.export` would return 403
      ``fileNotExportable``. The catch-all is published as
      :data:`opshub.connectors.google_workspace.mapper.GENERIC_FILE_SOURCE_TYPE`
      and lives **outside** the three-element
      :data:`opshub.core.document_extract.GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE`
      lookup table on purpose: the connector short-circuits these rows
      to metadata-only (``body = summary``, the operator-facing path
      label) without spending an export round-trip
      (`connector.py::_maybe_extract_body`).
    * Phase 14 Google Calendar (G4 #296, ADR-0010 §Phase 14 改訂 (l)):
      ``"google_calendar"`` — stamped by the Calendar API v3 mapper on
      every event (master + override alike; instance expansion of
      RRULE-driven occurrences is deferred to a Phase 15+ projection
      layer per Phase 14 plan §Alternatives 4). The literal is
      published as
      :data:`opshub.connectors.google_calendar.mapper.GOOGLE_CALENDAR_SOURCE_TYPE`
      and is the Google-side symmetric counterpart to ``ms365_calendar``
      (mapper symmetry pinned by
      ``tests/unit/connectors/test_mapper_symmetry.py`` so the host
      LLM / skill side treats both calendars uniformly).
    * Phase 14 Gmail (G3 #295, ADR-0010 §Phase 14 改訂 (k) + plan §1 OQ8):
      ``"gmail_message"`` — vendor-brand discriminator parallel to
      ``"ms365_outlook"``. Published as
      :data:`opshub.connectors.google_mail.mapper.GMAIL_SOURCE_TYPE`
      (``Final[Literal["gmail_message"]]``). The connector emits one
      :class:`SourceObserved` per Gmail message; thread aggregation is
      a Phase 15+ projection responsibility (Phase 14 plan §X.2 +
      §Phase 15+ outlook). Body extraction follows the Outlook recipe
      (text/plain preferred → text/html fallback, kept verbatim, no
      markitdown, no attachment retention; Phase 14 plan §1 OQ4 +
      ADR-0010 §Phase 14 改訂 (k)) so assistant skills treat Gmail
      and Outlook messages symmetrically.

    ``title`` is the human-readable label. ``url`` and ``summary`` are
    optional, both bounded; ``summary`` is intentionally short — full
    bodies belong outside OpsHub (ADR-0005).

    ``summary`` carries a schema-level ``max_length=200`` cap that
    matches the per-connector operational cap (Phase 7 mappers ship
    ``SUMMARY_MAX_CHARS = 200`` and the Phase 3 GitHub mapper enforces
    the same — see :mod:`opshub.connectors.slack.mapper`,
    :mod:`opshub.connectors.ms365.mapper`,
    :mod:`opshub.connectors.box.mapper`, and
    :mod:`opshub.connectors.github.api`). Pinning the cap on the event
    schema makes ADR-0005 a hard contract: any connector that ever
    forgets to truncate fails fast with a Pydantic ``ValidationError``
    rather than silently inflating the event log. ADR-0010 Phase 7
    Validation §3 calls this rule out explicitly ("全 connector で
    summary ≤ 200 chars enforce") and this constraint is the
    schema-layer half of that contract.

    ``fingerprint`` (Phase 9 step A2, ADR-0019 §決定 (d))
    -----------------------------------------------------
    Added by the Phase 9 ``box_drive`` connector to suppress
    :class:`SourceObserved` event noise across the 100k+ files that
    typically sit under a Box Drive mount. The connector computes
    ``f"{size}:{mtime_ns}"`` from :func:`os.stat` (no file body read —
    ADR-0019 §不変条件 (b)) and the scanner compares the live
    fingerprint against the value persisted in the ``sources``
    projection (migration ``0017_add_fingerprint_to_sources``) to
    skip files whose ``size`` and ``mtime_ns`` are both unchanged.

    Every other connector (``github`` / ``slack`` / ``ms365`` /
    ``box``) leaves the field at its default ``None`` — they observe
    SaaS resources whose diff detection is driven by API-side sync
    cursors, not local stat() metadata. ``None`` is written as
    ``NULL`` by :class:`~opshub.projections.sources.SourcesProjection`
    so the four pre-existing connectors are byte-identical after the
    Phase 9 schema bump.

    Adding ``fingerprint`` is a backward-compatible field addition
    (ADR-0002 §4 "new optional fields may be added without bumping
    schema_version"), so ``schema_version`` stays at ``1``. Historic
    events deserialised by :class:`pydantic.TypeAdapter` against the
    Phase 3 / Phase 7 stream pick up the default ``None`` and a
    ``projections rebuild`` reproduces the ``NULL`` write through the
    projector — no data migration is required.

    ``body`` / ``provenance_origin`` / ``provenance_trust``
    -------------------------------------------------------
    (Phase 10 step A2, ADR-0020; epic #470 follow-up)

    ADR-0020 (Full Local Content Retention) supersedes ADR-0005
    (External Content Minimization): connectors retain the full body of
    an observed item rather than only a ≤200-char summary. ``body``
    carries that full text and is **required** (``min_length=1``) for
    every observation — epic #470 / issue #481 lifted the Phase 10 Optional
    shim and pinned the "every source has a body" invariant on the event
    schema.

    metadata-only / stat-only connectors (the ``box_drive`` FS scan with
    ``content_extraction=False``, the Google Workspace
    ``google_workspace_file`` catch-all, the GitHub notification path,
    Box-events, MS365 OneDrive metadata, ...) MUST satisfy the invariant
    by emitting ``body = summary``. ADR-0010 §不変条件 records the rule;
    ADR-0020 §(d) defers the historical ``body = NULL`` shim to the
    pre-userbase cleanup that landed alongside this revision.

    ``provenance_origin`` (``"external"`` / ``"internal"``) and
    ``provenance_trust`` (``"trusted"`` / ``"untrusted"``) tag where the
    body came from and how far it can be trusted (ADR-0020 §(e)). They
    are the core mitigation for the content-poisoning / indirect
    prompt-injection attack surface that retaining external bodies
    introduces: an agent / LLM consuming an ``external`` + ``untrusted``
    body must treat it as reference material, never instructions
    (combined with ADR-0015 §決定 (f) do-not-follow preamble).

    ``provenance_*`` remain optional (``None`` permitted) because the
    operator-authored workspace ingest path leaves both unset; the
    SaaS connector family stamps ``external`` / ``untrusted`` uniformly.

    ``author_id`` (Phase 23-D, ADR-0033 §改訂)
    -----------------------------------------
    The connector-native id of the human / bot that **authored** the
    observed item (Slack ``U...`` / bot id, GitHub login, ...). It is a
    generic, optional field — most connectors leave it ``None`` — added
    so a downstream projection can tell self-authored content apart from
    content that demands the operator's attention without re-deriving the
    author from the free-text ``title``.

    The Phase 23-D ``slack_demand_digest`` projection is the first
    consumer: it compares ``author_id`` against the operator's own Slack
    id to drop DMs / mentions the operator themselves last spoke in (a
    DM the operator already replied to must not surface as "next to
    read"). Threading the author id onto the event — rather than parsing
    it back out of the title — revises ADR-0033 §決定 (a)'s "connector /
    mapper / event schema には触れない" premise: the self-authored
    false-positive cannot be suppressed without the author identity, and
    the title only carries the resolved *display name* (ambiguous across
    users that share a name).

    Adding ``author_id`` is a backward-compatible optional-field addition
    (ADR-0002 §4) exactly like ``fingerprint`` (Phase 9): ``schema_version``
    stays at ``1``, historic events deserialise with the default ``None``,
    and a ``projections rebuild`` reproduces a ``NULL`` write for every
    pre-Phase-23 Slack event (those events predate author threading, so a
    full re-sync — not just a rebuild — is required to back-fill the
    column; see ``docs/upgrading.md`` §Phase 23-D).

    ``author_handle`` / ``author_display`` (Phase 25-A, ADR-0010 §改訂)
    ------------------------------------------------------------------
    Phase 25 (秘書化 v1, epic #566) needs "who wrote this" on **every**
    connector — the person-axis entity resolution (25-B) names people
    by their connector-native ids and the commitment ledger (25-C)
    decides ``i_owe`` vs ``owed_to_me`` from whether the operator
    authored the source. Phase 23-D's ``author_id`` only covered Slack;
    Phase 25-A generalises the author identity into two normalised,
    connector-agnostic fields the ``sources`` projection persists for
    downstream services:

    * ``author_handle`` — the connector-native identifier of the author
      (Slack ``U...`` / bot id, an email address for Gmail / Outlook /
      Calendar organiser, a GitHub login, the Teams ``from.user.id``,
      the Box actor id, the Google Drive ``lastModifyingUser`` email).
      It is the stable join key the person-axis resolver (25-B) groups
      identities on, so it carries the raw native id rather than a
      display label.
    * ``author_display`` — the human-readable display name when the
      connector exposes one (``displayName`` on Teams / Drive, the
      parsed display part of a Gmail ``From:`` header, the Box actor
      name, the Slack ``user_display_name``). Recognition cue only —
      never a join key (display names collide across people).

    ``author_id`` is retained for backward compatibility with the
    Phase 23-D ``slack_demand_digest`` projection (the Slack mapper
    keeps populating it alongside ``author_handle``); new consumers
    read ``author_handle`` / ``author_display``. Local-filesystem
    connectors (``box_drive`` / ``onedrive_drive``) and the operator-
    listed ``web`` connector surface no SaaS author identity, so they
    leave all three fields ``None``.

    All three are backward-compatible optional-field additions
    (ADR-0002 §4) so ``schema_version`` stays at ``1``; historic events
    deserialise with the default ``None`` and a ``projections rebuild``
    reproduces a ``NULL`` write. Back-filling the columns for already-
    observed items requires a full re-sync, not just a rebuild (the
    older events predate author normalisation; see
    ``docs/upgrading.md`` §Phase 24 / §Phase 25-A).
    """

    event_type: Literal["source.observed"] = "source.observed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    connector_name: str = Field(min_length=1, max_length=50)
    external_id: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=500)
    url: str | None = None
    summary: str | None = Field(default=None, max_length=200)
    fingerprint: str | None = None
    body: str = Field(min_length=1)
    provenance_origin: ProvenanceOrigin | None = None
    provenance_trust: ProvenanceTrust | None = None
    author_id: str | None = Field(default=None, max_length=200)
    author_handle: str | None = Field(default=None, max_length=AUTHOR_HANDLE_MAX_CHARS)
    author_display: str | None = Field(default=None, max_length=AUTHOR_DISPLAY_MAX_CHARS)


class SourceReferenced(DomainEvent):
    """A task / decision / inbox_item now references this source.

    ``entity_type`` selects the referencing aggregate; ``entity_id`` is
    its ULID. ``aggregate_id`` is the source's ULID so the projector
    groups all references under the source row.

    Phase 8 first-class promotion (ADR-0017 §決定 (c))
    -------------------------------------------------
    This event was introduced in Phase 3 as a *placeholder*: no
    projector consumed it for the entire Phase 3-7 window. Phase 8
    (Knowledge graph) closes that gap — the ``LinksProjector``
    (``opshub.projections.links``) consumes :class:`SourceReferenced`
    via the ``LinksExtractor`` derived-state path (ADR-0017 §決定 (c))
    to materialise ``source → entity`` rows with
    ``link_type="references"``. The event itself is semantically a
    Phase 3 source-family fact; only its consumer side is new.

    Connector-side automatic emission — where a connector parses the
    body of an observed item (e.g. ``#task-id`` references in a GitHub
    Issue, a permalink in a Slack message) and emits
    :class:`SourceReferenced` automatically — is **deferred to Phase
    8.x** per ADR-0017 §決定 (g). Phase 8 MVP only populates this
    event via the manual ``opshub link add ... --type references``
    path (the CLI service translates the manual link into the
    corresponding :class:`SourceReferenced` semantics where
    appropriate) and via any pre-existing events written before the
    Phase 8 work.
    """

    event_type: Literal["source.referenced"] = "source.referenced"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    entity_type: Literal["task", "decision", "inbox_item"]
    entity_id: str
