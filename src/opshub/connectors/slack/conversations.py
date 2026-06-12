"""Slack conversation discovery helper (#366; replaces ``channels`` from #341).

Iterates Slack's ``users.conversations`` API (default) — or
``conversations.list`` when the caller asks for the workspace-wide view via
``all=True`` — and yields :class:`SlackConversation` rows so the CLI surface
can render a human-friendly table / TOML snippet / JSON payload.

Sort axis (Phase 19-D, ADR-0035; supersedes ADR-0034 §(b) §(g) §(h) §(i) CLI surface)
-------------------------------------------------------------------------------------

The caller selects a sort key via ``sort``; the activity-probe axis is
derived from the sort key + ``since`` combination:

* ``sort="last_self_post"`` (engagement axis, ADR-0034 §(a)). One
  ``search.messages`` call with ``query=f"from:<@{self_user_id}>"`` +
  ``oldest=since`` builds a per-channel index of the operator's own
  most-recent post ts. Listing rows are kept only when the channel
  appears in the index; the retained ts lands on
  :attr:`SlackConversation.last_self_post_ts`. Bot Tokens cannot hold
  ``search:read`` so the helper raises :class:`ConfigError` and
  recommends ``--sort=last_activity``.
* ``sort="last_activity"`` (any-author axis, #374 pre-19-B). One
  ``conversations.history?limit=1&oldest=<since>`` per row,
  ``last_activity_ts`` populated, broadcast / announcement-only channels
  included. Requires ``*:history`` for the requested ``types``.
* ``sort="name"`` (default) — no probe, ever (with or without
  ``since``). Both axis fields remain ``None`` and the JSON renderer
  drops them. Phase 23-G (#537) removed the former ``sort="name"`` +
  ``since`` implicit engagement default (ADR-0035 §(d)): the activity
  axis is now selected *only* by an explicit ``--sort``, so ``--since``
  never covertly switches API / token / scope. ``--sort=name`` combined
  with ``--since`` is rejected at the CLI boundary (no activity ts to
  filter by).

When ``sort in ("last_self_post", "last_activity")`` is paired with
``since=None``, the helper applies an implicit ``since = now_utc() -
timedelta(days=90)`` cutoff to cap probe cost (chatty workspaces /
``search.messages`` paging) and emits the ADR-0035 §(e) notice once
to stderr.

The two fields stay disjoint per row: engagement-axis paths write only
``last_self_post_ts`` (the any-axis field stays ``None``), any-axis
paths write only ``last_activity_ts``. The JSON renderer drops whichever
field is ``None`` so consumers see exactly one axis per row.

Motivation (over the original ``list_channels`` helper)
-------------------------------------------------------

The original ``channels`` command (#341) called ``conversations.list``,
which returns *every* public channel the token's principal can see — not
just the ones the operator has joined. In practice operators want the
"my Slack" view (channels + DMs + group DMs they actually participate
in). ``users.conversations`` returns exactly that set and is the
recommended Slack API for "what does this token see today".

Scope (compared to the legacy helper)
-------------------------------------

* Four conversation types are first-class: ``public_channel``,
  ``private_channel``, ``im`` (1:1 DM), ``mpim`` (multi-party DM).
* Default ``types`` set: all four. The CLI exposes ``--types`` so the
  operator can scope down to e.g. ``public,private`` for a discovery
  paste into ``opshub.toml``.
* ``all=True`` flips the API call from ``users.conversations``
  (joined-only) to ``conversations.list`` (workspace-wide). The
  workspace-wide path is intentionally opt-in — it requires the broader
  scope set (``channels:read`` + ``groups:read``) and surprises
  operators who expect "what I see in Slack".
* Archived conversations are filtered client-side on the per-row
  ``is_archived`` flag (DM/MPIM never archive, so the gate is a no-op
  for them). Mirrors the legacy helper's single-flag-everywhere posture.
* Activity filter (``since``) is opt-in. When set, the helper makes
  one extra ``conversations.history?limit=1&oldest=<since>`` call per
  row that survives the type / archived / filter gates; rows whose
  latest message predates ``since`` are dropped. ``last_activity_ts``
  on the yielded :class:`SlackConversation` carries the actual ts so
  the CLI can sort / display by activity. Two operator-visible error
  buckets are skip-able rather than abort-the-call:

  - ``missing_scope`` (type-scoped): disables the affected conversation
    type (one warning per type, not per row).
  - ``channel_not_found`` / ``not_in_channel`` (row-scoped): drops just
    the offending row and accumulates a single aggregate warning at
    call end. Surfaces for Slack Connect / external shared channels,
    DMs with deactivated peers, archive / leave races between the
    listing and the probe, and Enterprise Grid DLP restrictions. The
    sync hot path (:class:`opshub.connectors.slack.fetcher.SlackFetcher`)
    keeps the *fail-fast* posture for these codes — sync targets
    explicit channel ids from ``opshub.toml`` where the same code is
    a config drift to surface, while discovery enumerates dynamically
    where the row-skip is operationally what the operator wants. The
    asymmetry is intentional; do not unify the two.

  Both buckets surface via the ``warnings`` parameter (see
  :func:`list_conversations`).
* DM (``im``) and MPIM (``mpim``) rows have no ``name`` — Slack does
  not assign one. The helper resolves a human-readable label via
  ``users.info`` (im) and ``conversations.members`` + ``users.info``
  (mpim) and stores it in :attr:`SlackConversation.display_name`. The
  ``users.info`` cache is per-call so a 50-channel listing does not
  hammer the API for repeat user ids.

Pagination
----------

Both ``users.conversations`` and ``conversations.list`` use the same
cursor token (``response_metadata.next_cursor``). We pass ``limit=200``
per page (the SDK default and Slack's recommended sweet spot — higher
values trigger gateway timeouts on workspaces with thousands of
channels). The optional ``limit`` parameter on :func:`list_conversations`
caps the **total yielded count** post-filter, not the per-page size.

Progress reporting
------------------

An optional ``reporter`` (any :class:`opshub.cli._progress.ProgressReporter`)
is advanced by the raw page size before client-side filtering. This
matches ``connector sync``'s "items observed at the API" semantics —
the operator sees the spinner tick for every Slack-returned row, not
only the post-filter survivors. ``reporter=None`` keeps the helper
caller-agnostic for non-CLI use.

Cold-start guard
----------------

``slack_sdk`` is imported lazily inside :func:`list_conversations` so a
cold ``import opshub.connectors.slack.conversations`` never pulls the
SDK onto the cold-start path. The Slack subpackage's ``__init__`` does
not re-export this helper (the CLI handler imports it directly at call
time per ADR-0001).

Token safety
------------

The resolved Slack OAuth token is never echoed in raised exceptions —
we surface the Slack API ``error`` code (a documented short string
such as ``invalid_auth`` / ``missing_scope``) and, for
``missing_scope``, the ``needed`` scope name. This mirrors the
contract pinned by :class:`opshub.connectors.slack.fetcher.SlackFetcher`
so the redaction stance (ADR-0027) is uniform across the connector.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from opshub.connectors.slack._retry import retry_on_rate_limit
from opshub.connectors.slack.scopes import history_scope_for_type
from opshub.core.errors import ConfigError, ConnectorFailedError
from opshub.core.time import now_utc

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from opshub.cli._progress import ProgressReporter
    from opshub.connectors.slack.auth import SlackAuth


__all__ = [
    "CONVERSATION_TYPES",
    "ConversationType",
    "SlackConversation",
    "SortKey",
    "list_conversations",
]


#: Per-page size for both ``users.conversations`` and ``conversations.list``.
#: 200 is the SDK default and Slack's recommended ceiling — higher values
#: trigger occasional gateway timeouts on large workspaces. Exposed as a
#: module constant so tests can pin the value without re-reading the magic
#: number out of :func:`list_conversations`.
_PAGE_SIZE = 200

#: The four Slack conversation types this helper understands. The
#: ``ConversationType`` :class:`typing.Literal` is the dataclass-field
#: type so callers see the exact accept-list at the type level.
ConversationType = Literal["public", "private", "im", "mpim"]

#: Tuple ordering mirrors Slack's own enumeration (channels first, DMs
#: next). Tests assert against this tuple to pin the default ``--types``
#: set without re-typing the literals.
CONVERSATION_TYPES: tuple[ConversationType, ...] = ("public", "private", "im", "mpim")


#: Map our short type names to the Slack API's ``types`` parameter
#: tokens. Kept here (rather than at the call site) so a future Slack
#: rename is a one-line patch.
_API_TYPE_TOKEN: dict[ConversationType, str] = {
    "public": "public_channel",
    "private": "private_channel",
    "im": "im",
    "mpim": "mpim",
}

#: Sort-key literal (Phase 19-D, ADR-0035). ``"name"`` is the default
#: (display_name within type bucket, ADR-0035 §(b)); ``"last_self_post"``
#: triggers the engagement axis (search.messages-backed, ADR-0034 §(a));
#: ``"last_activity"`` triggers the any-author axis (one
#: ``conversations.history`` call per row, #374 pre-19-B). The key name
#: maps 1:1 to the populated dataclass field
#: (``last_self_post_ts`` / ``last_activity_ts``) so CLI / JSON / DB
#: schema share one vocabulary (ADR-0035 §(c) §不採用案 2/3).
SortKey = Literal["name", "last_self_post", "last_activity"]

#: Default implicit cutoff applied when ``sort in ("last_self_post",
#: "last_activity")`` is paired with ``since=None`` (ADR-0035 §(e)).
#: 90 days balances "recent enough to be discoverable" against the
#: ``search.messages`` page budget on chatty workspaces. ``--since``
#: overrides explicitly.
_IMPLICIT_CUTOFF_DAYS = 90

#: Per-page size for ``search.messages`` engagement-index calls. Slack
#: caps page-based pagination at ``count*page <= 10000``; ``count=100``
#: is the documented sweet spot for the engagement use case (the
#: cursor walk terminates as soon as the operator has seen all of
#: their recent posts, so a tighter page also limits the worst-case
#: API budget on chatty workspaces).
_SEARCH_MESSAGES_PAGE_SIZE = 100

#: ``search.messages`` indexing lag advisory. Slack's full-text index
#: lags message ingestion by minutes — the engagement axis surfaces a
#: stale snapshot relative to live ``conversations.history``. We emit
#: this once per ``list_conversations`` call (before the index fetch)
#: so an operator who sees a freshly-posted channel missing from the
#: output has a documented cause to map to (ADR-0034 §(i)).
_INDEXING_LAG_NOTICE = (
    "notice: search.messages may lag by minutes; use --sort=last_activity for live activity."
)

#: ``conversations.history`` scope required to fetch the last-message ts
#: for a given conversation type. Used to build the per-type warning
#: surfaced when ``--since`` is set but the token lacks the relevant
#: ``*:history`` scope (ADR-0018 §Decision (7)).
#:
#: Phase 23-I (#539, ADR-0040): derived from the feature→scope SSOT in
#: :mod:`opshub.connectors.slack.scopes` instead of an independent literal, so
#: the type → history-scope mapping has exactly one home. Behaviour is
#: unchanged (``public`` → ``channels:history`` etc.); the existing per-type
#: ``test_conversations.py`` warnings pin it indirectly.
_HISTORY_SCOPE_FOR_TYPE: dict[ConversationType, str] = {
    conversation_type: history_scope_for_type(conversation_type)
    for conversation_type in CONVERSATION_TYPES
}

#: Reverse map for classifying rows returned by the API. ``is_im`` /
#: ``is_mpim`` / ``is_private`` / ``is_channel`` flags on the row tell
#: us which bucket each conversation belongs to.
_TYPE_FROM_ROW_ORDER: tuple[tuple[str, ConversationType], ...] = (
    # Order matters: ``im`` and ``mpim`` rows also carry ``is_private=True``
    # in some workspaces' shapes, so we check the DM flags first.
    ("is_im", "im"),
    ("is_mpim", "mpim"),
    ("is_private", "private"),
    ("is_channel", "public"),
)


@dataclass(frozen=True, slots=True)
class SlackConversation:
    """One Slack conversation surfaced for the discovery CLI.

    Attributes
    ----------
    id:
        The Slack conversation id (``"C..."`` for public, ``"G..."`` for
        legacy private / mpim, ``"D..."`` for im). Pasted verbatim into
        ``opshub.toml``'s ``[connectors.slack.workspaces.<alias>]``
        ``channels = [...]`` list.
    type:
        One of :data:`CONVERSATION_TYPES`. Lets the CLI / TOML / JSON
        formatters branch on conversation kind without re-inspecting
        Slack's raw boolean flags.
    name:
        The conversation name without the ``#`` prefix (Slack stores it
        this way). ``None`` for ``im`` / ``mpim`` rows — those have no
        Slack-assigned name; consult :attr:`display_name` instead.
    display_name:
        Human-readable label. For ``public_channel`` / ``private_channel``
        this is just ``name``. For ``im`` it is the other participant's
        ``display_name`` (or ``real_name`` as fallback). For ``mpim`` it
        is a comma-joined list of participant names (capped via the
        CLI formatter; this field carries the full list).
    participants:
        For ``mpim`` rows, the resolved participant display names in the
        order Slack returned them. Empty tuple for non-mpim types so
        the field type stays simple. The CLI formatter caps the visible
        count (``+N`` suffix) without losing the full list here.
    is_private:
        ``True`` for private channels and for all DM/MPIM rows (Slack
        treats DMs as private by definition). Kept as a flat boolean
        for the TOML/JSON formatters that mirror the legacy
        ``is_private`` field shape.
    is_archived:
        ``True`` for archived channels. DMs and MPIMs never archive so
        this is always ``False`` for those types.
    purpose:
        Free-form purpose text (``conversation.purpose.value``). Empty
        string when the conversation has no purpose set (DMs always).
    last_activity_ts:
        Unix epoch seconds of the most recent message in the
        conversation **regardless of author**, populated only on the
        ``sort="last_activity"`` path (one extra ``conversations.history``
        call per row). ``None`` on the engagement-axis path
        (``sort="last_self_post"``) and when no probe runs (``sort="name"``)
        — the JSON renderer drops the key in that
        case so the discovery payload stays free of meaningless nulls
        when the operator did not opt into the any-axis probe.
    last_self_post_ts:
        Unix epoch seconds of the operator's own most-recent post in
        the conversation, populated on the engagement-axis path
        (``sort="last_self_post"``, ADR-0035 §(c); Phase 23-G #537 made it
        the only engagement trigger). ``None`` on the ``sort="last_activity"``
        / ``sort="name"`` paths or when no probe runs. The two axis fields are disjoint by
        design — see ADR-0034 §(g) ("axes are orthogonal; one row
        carries one axis ts") so a JSON consumer can branch on field
        presence without re-reading the invocation flags.
    """

    id: str
    type: ConversationType
    name: str | None
    display_name: str
    is_private: bool
    is_archived: bool
    purpose: str
    participants: tuple[str, ...] = field(default_factory=tuple)
    last_activity_ts: float | None = None
    last_self_post_ts: float | None = None


def list_conversations(
    auth: SlackAuth,
    *,
    types: tuple[ConversationType, ...] = CONVERSATION_TYPES,
    include_archived: bool = False,
    filter_substring: str | None = None,
    limit: int | None = None,
    all: bool = False,
    since: datetime | None = None,
    sort: SortKey = "name",
    warnings: list[str] | None = None,
    resolved_cutoff: list[datetime] | None = None,
    reporter: ProgressReporter | None = None,
) -> Iterator[SlackConversation]:
    """Yield conversations the configured token can see, one row at a time.

    Parameters
    ----------
    auth:
        Resolved :class:`SlackAuth`. The token is read at call time
        (no SDK instantiation happens before the function body so the
        cold-start guard holds even for the auth resolution path).
    types:
        Tuple of conversation types to request. Defaults to all four
        (:data:`CONVERSATION_TYPES`). The CLI parses ``--types
        public,private`` and passes the parsed tuple through.
    include_archived:
        When ``True``, archived channels are yielded alongside live
        ones. DM/MPIM rows never set ``is_archived`` so the gate is a
        no-op for them.
    filter_substring:
        Case-insensitive substring match against ``name`` /
        ``display_name`` (so the operator can filter DMs by participant
        name as well as channels by ``#name``). ``None`` (or empty
        string) disables filtering.
    limit:
        Maximum number of conversations to yield post-filter. ``None``
        means "no cap".
    all:
        When ``True``, the helper calls ``conversations.list``
        (workspace-wide) instead of ``users.conversations``
        (joined-only). ``conversations.list`` does not return DM/MPIM
        even when those are requested in ``types`` — Slack reserves
        DM/MPIM listing for ``users.conversations``. The helper passes
        whatever Slack returns; the operator decides whether the
        joined-only or workspace-wide perspective is useful for their
        paste-into-opshub.toml workflow.
    since:
        When non-``None``, filter rows by recent activity. The actual
        probe strategy depends on ``sort`` (see below). ``since``
        must be a tz-aware :class:`datetime.datetime`; the helper does
        not normalise — callers should pre-attach UTC. When
        ``sort in ("last_self_post", "last_activity")`` is paired with
        ``since=None``, the helper applies an implicit
        ``now_utc() - timedelta(days=90)`` cutoff and emits an ADR-0035
        §(e) notice once to stderr to cap probe cost.
    sort:
        Selects the sort key + activity-probe axis (ADR-0035):

        * ``"name"`` (default) — display_name within type bucket. **No
          probe runs**, with or without ``since`` (Phase 23-G #537): both
          axis fields stay ``None``. The CLI rejects ``--sort=name`` +
          ``--since`` (no activity ts to filter by); a bare
          ``list_conversations(sort="name", since=...)`` call simply
          ignores ``since`` (no axis to apply it to). The former
          ADR-0035 §(d) implicit engagement default was removed so
          ``--since`` never covertly requires ``search:read``.
        * ``"last_self_post"`` (engagement axis, ADR-0034 §(a)): one
          ``search.messages`` paginated call before the listing loop
          (``query="from:<@<self>>"`` + ``oldest=since`` +
          ``sort=timestamp,sort_dir=desc``) builds a per-channel index
          of the operator's own most-recent post ts. Rows whose
          ``channel_id`` is absent from the index are dropped. The
          retained ts lands on
          :attr:`SlackConversation.last_self_post_ts`; the any-axis
          ``last_activity_ts`` stays ``None``. Bot Tokens cannot hold
          ``search:read`` — the helper raises
          :class:`~opshub.core.errors.ConfigError` on principal
          mismatch and recommends ``--sort=last_activity``.
        * ``"last_activity"`` (any-author axis, pre-19-B): one
          ``conversations.history?limit=1&oldest=<since>`` per surviving
          row; ``last_activity_ts`` populated, ``last_self_post_ts``
          stays ``None``. Required scope set is
          ``*:history`` (per type). Broadcast / announcement-only
          channels survive even when the operator never wrote in them.
    warnings:
        Optional list the helper appends operator-facing warnings to.
        Used when ``since`` is set and the ``conversations.history``
        call fails. Two warning shapes land here:

        * ``missing_scope`` (type-scoped): one
          ``warning: skipping <type> conversations: missing_scope ...``
          per affected type (so a 50-row MPIM workspace yields one
          ``mpim:history`` warning, not 50). The type's rows are
          dropped from the yielded stream.
        * ``channel_not_found`` / ``not_in_channel`` (row-scoped): one
          ``warning: skipped N inaccessible channels
          (channel_not_found=X, not_in_channel=Y). ...`` appended at
          call end (after pagination exhaustion or ``--limit`` early
          return). Counts are grouped by error code so an operator
          with many Slack Connect channels sees one stderr line, not
          one per row.

        ``None`` discards the warnings, which is fine for non-CLI
        callers that only want the row stream.
    resolved_cutoff:
        Optional single-element sink (Phase 23-G #537): when an explicit
        ts-axis sort runs without ``--since`` and the implicit ``90d``
        cutoff is applied, the resolved tz-aware cutoff datetime is
        appended here so the CLI can stamp it into the listing output
        (making the default observable in-band, not just via the stderr
        notice). ``None`` discards it.
    reporter:
        Optional :class:`opshub.cli._progress.ProgressReporter`. The
        helper advances it by the raw page size (pre-filter) so the
        operator sees the spinner tick for every Slack-returned row.
        ``None`` makes the helper caller-agnostic.

    Yields
    ------
    SlackConversation
        One row per conversation that survives the archived / filter
        gates, in the order Slack returns them. Order is not contract
        — the CLI formatter sorts deterministically before rendering.

    Raises
    ------
    ConnectorFailedError
        On any Slack API error that is not a recoverable 429 (e.g.
        ``invalid_auth``, ``missing_scope``), or when the 429 retry
        budget is exhausted. The error message includes the Slack
        ``error`` code; for ``missing_scope`` it additionally surfaces
        the ``needed`` scope name and a link to ADR-0018 +
        ``https://api.slack.com/scopes``. The Slack OAuth token never
        appears in raised messages.
    """
    # Lazy-imported inside the function so importing this module never
    # pulls slack_sdk onto the cold-start path. Mirrors the fetcher's
    # import strategy.
    from slack_sdk import WebClient

    client = WebClient(token=auth.token)

    # Build Slack's ``types`` parameter (comma-separated API tokens) from
    # the caller's short-name tuple. Preserve the tuple order so tests
    # can assert against ``call_args.kwargs["types"]``.
    api_types = ",".join(_API_TYPE_TOKEN[t] for t in types)
    requested_types = frozenset(types)

    # Normalise the filter once so the hot loop only does the case-
    # insensitive containment check. ``None`` and the empty string both
    # disable filtering.
    needle = (filter_substring or "").lower() or None

    # Per-call user-info cache. DM/MPIM name resolution looks up the
    # same user ids repeatedly across rows — caching ensures one
    # ``users.info`` call per user_id even for a large MPIM-heavy
    # workspace.
    user_name_cache: dict[str, str] = {}

    # Resolve the activity-probe axis from the sort key + since
    # combination. Phase 23-G (#537): the axis is selected *only* by an
    # explicit ``--sort`` — ``engagement_axis`` is the ``sort="last_self_post"``
    # path (§(c)). ``sort="name"`` never probes (with or without ``--since``);
    # ``sort="last_activity"`` falls through to the per-row
    # ``conversations.history`` (any-author) branch below by leaving
    # ``self_post_index`` ``None``. The former ``sort="name"`` + ``--since``
    # implicit engagement default (ADR-0035 §(d)) was removed so ``--since``
    # never covertly switches API / token / scope.
    engagement_axis = sort == "last_self_post"

    # Apply the implicit cutoff (ADR-0035 §(e)) when an explicit ts-sort
    # was requested without a ``--since`` value. ``sort="name"`` keeps both
    # axis fields ``None`` (no probe at all); this branch only triggers for
    # the two explicit ts-axis modes. The resolved cutoff is surfaced via the
    # ``resolved_cutoff`` out-param (Phase 23-G, #537) so the CLI can stamp it
    # into the listing output, making the 90d default observable in-band.
    if since is None and sort in ("last_self_post", "last_activity"):
        since = now_utc() - timedelta(days=_IMPLICIT_CUTOFF_DAYS)
        if resolved_cutoff is not None:
            resolved_cutoff.append(since)
        _emit_implicit_cutoff_notice(sort=sort)

    # Pre-compute the ``oldest`` token Slack expects on
    # ``conversations.history`` calls — a stringified unix epoch with
    # optional fractional part. ``None`` short-circuits the per-row
    # history fetch entirely so the no-``since`` path costs zero extra
    # API calls. Phase 23-G (#537): ``sort="name"`` never filters by
    # activity (it has no axis ts), so ``since`` is ignored for name sort
    # even if a caller passes it — the CLI rejects ``--sort=name`` +
    # ``--since`` upstream; a direct call degrades to the unfiltered name
    # listing rather than covertly running the any-author probe.
    since_ts = since.timestamp() if (since is not None and sort != "name") else None

    # Track which conversation types have already produced a
    # ``missing_scope`` failure so subsequent rows of the same type
    # skip the history call (and the warning emits once per type, not
    # once per row).
    disabled_history_types: set[ConversationType] = set()

    # Per-error-code counter for row-scoped ``conversations.history``
    # misses (``channel_not_found`` / ``not_in_channel``). The listing
    # drops each offending row inline and emits **one** aggregate
    # warning at the end of the call so an operator with 50 Slack
    # Connect channels does not see 50 lines of stderr noise.
    inaccessible_history_counts: dict[str, int] = {}

    # Engagement-axis (``sort="last_self_post"`` or implicit-default
    # ``sort="name"`` + ``since``, ADR-0035 §(c) §(d)) state. Built lazily before
    # the first row that needs it so workspaces with no since-filter
    # never pay for the principal check or the ``search.messages``
    # round-trip. ``self_post_index`` is set to ``None`` until the
    # engagement path has built it (or determined that no engagement
    # path applies); subsequent code reads ``self_post_index is not
    # None`` to discriminate between "engagement applies, lookup
    # below" and "any-axis path".
    self_post_index: dict[str, float] | None = None
    referenced_channel_ids: set[str] | None = None
    if since_ts is not None and engagement_axis:
        # Reject Bot Tokens early: ``search:read`` is a User Token-only
        # scope, so a Bot Token can never satisfy the engagement axis.
        # The check runs *before* the index fetch so the operator sees
        # a documented ``ConfigError`` instead of a Slack
        # ``missing_scope`` error code that does not name the token
        # principal.
        principal_info = auth.test_token()
        if principal_info.get("principal") == "bot":
            raise ConfigError(
                "Slack Bot Token cannot satisfy `search:read` "
                "(engagement axis); use a User Token (`xoxp-`) "
                "or rerun with --sort=last_activity."
            )
        self_user_id = principal_info.get("user_id") or ""
        # Indexing-lag advisory: emit before the fetch so the operator
        # has the cue queued up before the spinner ticks over to the
        # listing pages. Single-shot per call: list_conversations is
        # already a per-invocation entry point, so the dedupe is
        # implicit in the call boundary.
        _emit_indexing_lag_notice()
        self_post_index = _fetch_self_post_index(
            client=client,
            self_user_id=self_user_id,
            since_ts=since_ts,
        )
        referenced_channel_ids = set()

    yielded = 0
    page_cursor: str | None = None
    while True:
        response = _call_list(
            client=client,
            api_types=api_types,
            cursor=page_cursor,
            all=all,
        )

        rows_obj = response.get("channels")
        rows: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], rows_obj) if isinstance(rows_obj, list) else []
        )

        # Advance the progress reporter by the raw page size — matches
        # ``connector sync``'s "items observed at the API" semantics.
        if reporter is not None and rows:
            reporter.advance(len(rows))

        for raw in rows:
            conversation = _row_from_dict(
                raw,
                client=client,
                user_name_cache=user_name_cache,
            )
            if conversation is None:
                # Malformed row (no id, or unknown type): skip rather
                # than crash so a single bad payload does not poison
                # the discovery output.
                continue
            # Slack honours ``types=`` on the API call, but some
            # workspaces' shapes leak adjacent types (e.g. a private
            # channel arriving on a ``public_channel``-only request
            # because the row carries both flags). Re-gate client-side
            # so the operator-visible accept-list and the API request
            # cannot drift.
            if conversation.type not in requested_types:
                continue
            if conversation.is_archived and not include_archived:
                continue
            if needle is not None and not _matches_filter(conversation, needle):
                continue
            if since_ts is not None:
                if self_post_index is not None:
                    # Engagement axis (``sort="last_self_post"`` or
                    # ``sort="name"`` + ``since``, ADR-0035 §(c) §(d)):
                    # the pre-built index is the source of truth. Rows
                    # absent from the index are dropped (the operator
                    # has not written there since ``since``); the
                    # retained ts lands on ``last_self_post_ts`` and
                    # the any-axis ``last_activity_ts`` stays ``None``
                    # so the two axes never bleed into the same row
                    # (ADR-0034 §(g)).
                    assert referenced_channel_ids is not None
                    referenced_channel_ids.add(conversation.id)
                    self_post_ts = self_post_index.get(conversation.id)
                    if self_post_ts is None or self_post_ts < since_ts:
                        continue
                    conversation = replace(conversation, last_self_post_ts=self_post_ts)
                else:
                    if conversation.type in disabled_history_types:
                        # Earlier row of this type tripped
                        # ``missing_scope``; the warning was recorded
                        # once and we now drop the entire type from
                        # the activity-filtered output.
                        continue
                    try:
                        activity_ts = _fetch_last_activity_ts(
                            client=client,
                            channel_id=conversation.id,
                            since_ts=since_ts,
                        )
                    except _MissingHistoryScopeError as missing:
                        disabled_history_types.add(conversation.type)
                        if warnings is not None:
                            warnings.append(
                                _format_missing_scope_warning(
                                    conversation_type=conversation.type,
                                    needed=missing.needed,
                                )
                            )
                        continue
                    except _InaccessibleHistoryError as inaccessible:
                        # Row-scoped skip: Slack Connect external channels,
                        # DMs with deactivated peers, and rows archived /
                        # left between the listing and the per-row probe
                        # all surface here. Bump the counter and drop just
                        # this row — the other rows of the same type are
                        # not affected. Per-row debug log carries the
                        # channel id so an operator running with
                        # ``--debug`` can map back to which rows were
                        # dropped (the aggregate warning at call end only
                        # reports counts, by design — ADR-0027 redaction
                        # processor strips any token on the off-chance the
                        # event dict picks one up).
                        from opshub.core.logging import get_logger

                        get_logger(__name__).debug(
                            "slack.conversations.history.row_skipped",
                            channel_id=conversation.id,
                            error_code=inaccessible.error_code,
                            conversation_type=conversation.type,
                        )
                        inaccessible_history_counts[inaccessible.error_code] = (
                            inaccessible_history_counts.get(inaccessible.error_code, 0) + 1
                        )
                        continue
                    if activity_ts is None:
                        # No message newer than ``since`` — drop.
                        continue
                    conversation = replace(conversation, last_activity_ts=activity_ts)
            yield conversation
            yielded += 1
            if limit is not None and yielded >= limit:
                _flush_inaccessible_history_warning(
                    counts=inaccessible_history_counts,
                    warnings=warnings,
                )
                _emit_orphan_index_debug(
                    self_post_index=self_post_index,
                    referenced_channel_ids=referenced_channel_ids,
                )
                return

        # Walk to the next page only if Slack signals more data.
        response_metadata_obj = response.get("response_metadata")
        response_metadata: dict[str, Any] = (
            cast(dict[str, Any], response_metadata_obj)
            if isinstance(response_metadata_obj, dict)
            else {}
        )
        next_cursor = response_metadata.get("next_cursor")
        if not next_cursor:
            _flush_inaccessible_history_warning(
                counts=inaccessible_history_counts,
                warnings=warnings,
            )
            _emit_orphan_index_debug(
                self_post_index=self_post_index,
                referenced_channel_ids=referenced_channel_ids,
            )
            return
        page_cursor = str(next_cursor)


# ---------------------------------------------------------------------- helpers


def _row_from_dict(
    raw: dict[str, Any],
    *,
    client: Any,
    user_name_cache: dict[str, str],
) -> SlackConversation | None:
    """Build a :class:`SlackConversation` from a ``conversations.list`` row.

    Returns ``None`` when the row is malformed (missing ``id``, or
    unknown type — no ``is_im`` / ``is_mpim`` / ``is_private`` /
    ``is_channel`` flag set). DM/MPIM rows trigger an extra
    ``users.info`` lookup (cached) to resolve a human-readable label.
    """
    conversation_id = str(raw.get("id") or "")
    if not conversation_id:
        return None

    conversation_type = _classify_type(raw)
    if conversation_type is None:
        return None

    # ``is_private`` / ``is_archived`` documented as booleans; the
    # ``bool(...)`` cast normalises thin-proxy quirks (``"true"`` strings)
    # while keeping the happy path identity-equal.
    is_private = bool(raw.get("is_private"))
    is_archived = bool(raw.get("is_archived"))

    # ``purpose`` is a sub-object ``{"value": "...", "creator": "...",
    # "last_set": <ts>}`` — we only render ``value``. Missing or
    # non-dict ``purpose`` falls through to an empty string so the
    # dataclass field type stays ``str``.
    purpose_obj = raw.get("purpose")
    if isinstance(purpose_obj, dict):
        purpose_dict = cast(dict[str, Any], purpose_obj)
        purpose = str(purpose_dict.get("value") or "")
    else:
        purpose = ""

    name: str | None
    raw_name = raw.get("name")
    name = str(raw_name) if isinstance(raw_name, str) and raw_name else None

    display_name: str
    participants: tuple[str, ...] = ()
    if conversation_type == "im":
        # ``user`` is the other participant's id on a 1:1 DM. Slack
        # returns it on every ``im`` row; absent / empty falls back to
        # the channel id so the operator still sees something useful.
        peer_id = str(raw.get("user") or "")
        display_name = _resolve_user_name(
            peer_id,
            client=client,
            cache=user_name_cache,
            fallback=peer_id or conversation_id,
        )
    elif conversation_type == "mpim":
        members = _fetch_mpim_members(conversation_id, client=client)
        participants = tuple(
            _resolve_user_name(
                user_id,
                client=client,
                cache=user_name_cache,
                fallback=user_id,
            )
            for user_id in members
        )
        display_name = ", ".join(participants) if participants else conversation_id
    else:
        # Public / private channel: ``name`` is always present per
        # Slack's contract. Defensive fallback for malformed proxies.
        display_name = name or conversation_id

    return SlackConversation(
        id=conversation_id,
        type=conversation_type,
        name=name,
        display_name=display_name,
        is_private=is_private,
        is_archived=is_archived,
        purpose=purpose,
        participants=participants,
    )


def _classify_type(raw: dict[str, Any]) -> ConversationType | None:
    """Return the :class:`ConversationType` for a raw Slack row, or ``None``.

    Checks the documented flags in the order
    :data:`_TYPE_FROM_ROW_ORDER` lists them (DM flags first so an
    ``im`` row carrying ``is_private=True`` is not misclassified as a
    private channel).
    """
    for flag, conversation_type in _TYPE_FROM_ROW_ORDER:
        if bool(raw.get(flag)):
            return conversation_type
    return None


def _matches_filter(conversation: SlackConversation, needle: str) -> bool:
    """Return True if ``needle`` is a substring of ``name`` or ``display_name``.

    DM/MPIM rows have no ``name`` (only a ``display_name`` built from
    participant ids) so checking both fields lets the operator filter
    by either ``--filter eng-backend`` (channel name) or
    ``--filter alice`` (DM participant) without remembering which
    column the value lives in.
    """
    if conversation.name and needle in conversation.name.lower():
        return True
    return needle in conversation.display_name.lower()


def _resolve_user_name(
    user_id: str,
    *,
    client: Any,
    cache: dict[str, str],
    fallback: str,
) -> str:
    """Resolve a Slack user id to a human-readable label, with per-call cache.

    Prefers ``profile.display_name`` (the user-configured handle), then
    ``profile.real_name``, then the raw user id. Any API error is
    swallowed and the fallback returned — DM/MPIM listing must not
    fail because one of many users is no longer resolvable.
    """
    if not user_id:
        return fallback
    if user_id in cache:
        return cache[user_id]

    name = _call_user_info(user_id, client=client) or fallback
    cache[user_id] = name
    return name


def _call_user_info(user_id: str, *, client: Any) -> str | None:
    """Call ``users.info`` for ``user_id`` and return the best-name string.

    Returns ``None`` on any error so the caller can apply the fallback
    (raw user id). The token never appears in any logged / returned
    string; we discard the exception payload entirely.
    """
    from slack_sdk.errors import SlackApiError

    try:
        response: Any = client.users_info(user=user_id)
    except SlackApiError:
        return None
    data = _as_response_dict(response)
    user_obj = data.get("user")
    if not isinstance(user_obj, dict):
        return None
    user = cast(dict[str, Any], user_obj)
    profile_obj = user.get("profile")
    if isinstance(profile_obj, dict):
        profile = cast(dict[str, Any], profile_obj)
        display_name = str(profile.get("display_name") or "")
        if display_name:
            return display_name
        real_name = str(profile.get("real_name") or "")
        if real_name:
            return real_name
    real_name = str(user.get("real_name") or "")
    if real_name:
        return real_name
    name = str(user.get("name") or "")
    return name or None


def _fetch_mpim_members(conversation_id: str, *, client: Any) -> list[str]:
    """Return user ids participating in an MPIM, walking pagination if needed.

    Slack caps members per page at 100; MPIMs are at most 9 participants
    in practice (Slack's own limit) so the loop almost always runs
    once. Any API error → empty list (the caller falls back to the
    conversation id for the display name).
    """
    from slack_sdk.errors import SlackApiError

    members: list[str] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"channel": conversation_id, "limit": 100}
        if cursor is not None:
            kwargs["cursor"] = cursor
        try:
            response: Any = client.conversations_members(**kwargs)
        except SlackApiError:
            return members
        data = _as_response_dict(response)
        page_obj = data.get("members")
        if isinstance(page_obj, list):
            members.extend(str(uid) for uid in cast(list[str], page_obj) if uid)
        response_metadata_obj = data.get("response_metadata")
        if not isinstance(response_metadata_obj, dict):
            return members
        next_cursor = cast(dict[str, Any], response_metadata_obj).get("next_cursor")
        if not next_cursor:
            return members
        cursor = str(next_cursor)


def _call_list(
    *,
    client: Any,
    api_types: str,
    cursor: str | None,
    all: bool,
) -> dict[str, Any]:
    """Call ``users.conversations`` (default) or ``conversations.list`` (all=True).

    Both endpoints share request shape and pagination semantics; the
    only difference is which SDK method we dispatch on. Retry policy
    (3 attempts, ``Retry-After`` honoured, 1s / 2s / 4s exponential
    fallback) lives in
    :func:`opshub.connectors.slack._retry.retry_on_rate_limit` so the
    listing path now shares the same source of truth as the
    per-conversation history calls (#379). Non-429
    :class:`SlackApiError` (and the final 429 after budget exhaustion)
    re-raise to the local error mapper
    :func:`_to_connector_failed` which surfaces an endpoint-qualified
    :class:`ConnectorFailedError` (``users.conversations`` vs
    ``conversations.list``) — preserves the operator-visible error
    vocabulary established in #366.
    """
    from slack_sdk.errors import SlackApiError

    kwargs: dict[str, Any] = {
        "limit": _PAGE_SIZE,
        "types": api_types,
    }
    if cursor is not None:
        kwargs["cursor"] = cursor

    def _call() -> Any:
        if all:
            return client.conversations_list(**kwargs)
        return client.users_conversations(**kwargs)

    try:
        response = retry_on_rate_limit(_call)
    except SlackApiError as exc:
        raise _to_connector_failed(exc, all=all) from exc
    return _as_response_dict(response)


class _MissingHistoryScopeError(Exception):
    """Sentinel raised by :func:`_fetch_last_activity_ts` on ``missing_scope``.

    The listing loop catches this to disable history probing for the
    affected conversation type without aborting the whole call: an
    operator who granted ``channels:history`` but not ``mpim:history``
    still wants to see public-channel activity, just with an MPIM-skip
    warning.
    """

    def __init__(self, needed: str) -> None:
        super().__init__(needed)
        self.needed = needed


#: Slack API error codes that mean "history is unreadable for *this* row,
#: but the rest of the listing is fine". ``channel_not_found`` is the
#: documented surface for Slack Connect / external shared channels that
#: list via ``users.conversations`` but block the history API, for DMs
#: with deactivated peers, and for race conditions where a channel was
#: archived / left between the listing and the per-row probe.
#: ``not_in_channel`` covers the User Token / Bot Token cases where the
#: principal is no longer a member of a private channel at probe time.
_INACCESSIBLE_HISTORY_ERRORS: frozenset[str] = frozenset({"channel_not_found", "not_in_channel"})


class _InaccessibleHistoryError(Exception):
    """Sentinel raised by :func:`_fetch_last_activity_ts` on a per-row history miss.

    Distinct from :class:`_MissingHistoryScopeError` because the skip is
    **row-scoped**, not type-scoped: a single ``channel_not_found`` on
    one external-shared channel must not disable history probing for
    the rest of the workspace's public channels. The listing loop
    catches this, drops just the offending row, and bumps a counter so
    the operator-visible summary warning at the end of the call names
    the aggregate (``skipped 3 inaccessible channels``) rather than
    emitting one warning per row.
    """

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _flush_inaccessible_history_warning(
    *,
    counts: dict[str, int],
    warnings: list[str] | None,
) -> str | None:
    """Append the aggregate row-scoped history-skip warning, if any.

    Called from each terminating ``return`` in :func:`list_conversations`
    so the operator sees one stderr line summarising every row that the
    activity probe could not read — even if the loop exited early via
    the ``--limit`` cap. The warning names the per-error-code counts
    (``channel_not_found=3`` / ``not_in_channel=1``) so the operator
    can map back to documented causes (Slack Connect external channels
    vs. principal-not-member) without re-running with ``--debug``.

    Returns the formatted warning string for tests that prefer to
    assert against the message directly; the production code path only
    consumes the side-effect on ``warnings``.
    """
    if not counts:
        return None
    parts = [f"{code}={counts[code]}" for code in sorted(counts)]
    total = sum(counts.values())
    plural = "channel" if total == 1 else "channels"
    message = (
        f"warning: skipped {total} inaccessible {plural} "
        f"({', '.join(parts)}). See "
        f"https://api.slack.com/methods/conversations.history#errors "
        f"for the error catalogue."
    )
    if warnings is not None:
        warnings.append(message)
    return message


def _format_missing_scope_warning(
    *,
    conversation_type: ConversationType,
    needed: str,
) -> str:
    """Build the operator-facing warning for a per-type history scope miss.

    The wording matches ADR-0027's "name the failure, point at the
    scope catalogue, never leak the token" stance: we surface the
    conversation type the operator can recognise (``public`` / ``im``)
    and the documented Slack scope short string (``channels:history``).
    """
    return (
        f"warning: skipping {conversation_type} conversations: "
        f"missing_scope (needed: {needed!r}). See ADR-0018 §Decision (7) or "
        f"https://api.slack.com/scopes for the scope catalogue."
    )


def _fetch_last_activity_ts(
    *,
    client: Any,
    channel_id: str,
    since_ts: float,
) -> float | None:
    """Return the latest ``conversations.history`` message ts > ``since_ts``.

    ``None`` means "no activity newer than ``since_ts``" (the
    conversation is either empty or its last message predates the
    cutoff). The caller drops the row in that case.

    Raises :class:`_MissingHistoryScopeError` on ``missing_scope`` so the
    listing loop can disable the affected type and surface a single
    operator-facing warning. Other Slack API errors bubble up via the
    same :class:`ConnectorFailedError` channel as the listing call,
    keeping the discovery command's error vocabulary uniform.
    """
    response = _call_history_oldest(
        client=client,
        channel_id=channel_id,
        oldest=since_ts,
    )
    messages_obj = response.get("messages")
    if not isinstance(messages_obj, list) or not messages_obj:
        return None
    messages = cast(list[dict[str, Any]], messages_obj)
    head = messages[0]
    ts_raw = head.get("ts")
    if ts_raw is None:
        return None
    try:
        return float(ts_raw)
    except (TypeError, ValueError):
        # A defensive arm for thin proxies that return non-numeric
        # ts values. The listing loop drops the row, which is the
        # right behaviour for "we can't trust this row's activity".
        return None


def _call_history_oldest(
    *,
    client: Any,
    channel_id: str,
    oldest: float,
) -> dict[str, Any]:
    """Call ``conversations.history?limit=1&oldest=<ts>`` with 429 retry.

    The 429 + ``Retry-After`` policy lives in
    :func:`opshub.connectors.slack._retry.retry_on_rate_limit` so the
    discovery activity probe and the sync fetcher's hot-path history
    call share one source of truth (#377). This wrapper only spells
    out the Slack SDK method to dispatch and the call-site-specific
    non-429 error translation:

    * ``missing_scope`` → :class:`_MissingHistoryScopeError` so the
      listing loop can disable the affected conversation type.
    * ``channel_not_found`` / ``not_in_channel`` →
      :class:`_InaccessibleHistoryError` so the listing loop can drop
      just the offending row (Slack Connect external channels, DMs with
      deactivated peers, archived-between-list-and-probe races) without
      aborting the rest of the listing.
    * Any other non-429 error → :class:`ConnectorFailedError` with an
      endpoint-qualified message that names ``conversations.history``
      (matches the listing-call error vocabulary so operators see one
      consistent shape across both Slack endpoints).
    """
    from slack_sdk.errors import SlackApiError

    kwargs: dict[str, Any] = {
        "channel": channel_id,
        "limit": 1,
        # ``oldest=ts`` filters at the API: only messages strictly
        # newer than ``ts`` are returned (with ``inclusive=False`` we
        # exclude the boundary message itself so an exact-match
        # ``since`` does not double-count). Slack accepts the unix
        # epoch as a stringified float — ``repr`` preserves the full
        # IEEE-754 precision that ``now_utc().timestamp()`` carries,
        # so a ``--since 7d`` cutoff close to a message ts is not
        # silently rounded into either side of the cutoff (review
        # finding: 6-digit ``f"{oldest:.6f}"`` was banker-rounded and
        # could nudge the cutoff a fraction of a microsecond in
        # either direction).
        "oldest": repr(float(oldest)),
        "inclusive": False,
    }

    def _call() -> Any:
        return client.conversations_history(**kwargs)

    try:
        response = retry_on_rate_limit(_call)
    except SlackApiError as exc:
        response_any = cast(Any, exc.response)
        if response_any is not None:
            error_code = response_any.get("error") or ""
            if error_code == "missing_scope":
                needed = response_any.get("needed") or ""
                raise _MissingHistoryScopeError(needed) from exc
            if error_code in _INACCESSIBLE_HISTORY_ERRORS:
                raise _InaccessibleHistoryError(error_code) from exc
        raise _to_connector_failed_history(exc) from exc
    return _as_response_dict(response)


def _to_connector_failed_history(exc: Any) -> ConnectorFailedError:
    """Map a ``conversations.history`` :class:`SlackApiError` → :class:`ConnectorFailedError`.

    Kept distinct from :func:`_to_connector_failed` so the error
    message names the endpoint the operator was waiting on: a 429 at
    history time is operationally different from a 429 on the listing
    call (the former indicates per-channel hot-path; the latter
    indicates listing pagination).
    """
    response_any = cast(Any, getattr(exc, "response", None))
    error_code = "unknown"
    if response_any is not None:
        error_code = response_any.get("error") or type(exc).__name__
    return ConnectorFailedError(f"Slack conversations.history failed: {error_code}")


def _to_connector_failed(exc: Any, *, all: bool) -> ConnectorFailedError:
    """Map a :class:`SlackApiError` to :class:`ConnectorFailedError`.

    The error message names the Slack endpoint we called
    (``users.conversations`` vs ``conversations.list``) so the operator
    sees which scope to extend without re-reading the docs.
    """
    response_any = cast(Any, getattr(exc, "response", None))
    error_code = "unknown"
    needed = ""
    if response_any is not None:
        error_code = response_any.get("error") or type(exc).__name__
        if error_code == "missing_scope":
            needed = response_any.get("needed") or ""

    endpoint = "conversations.list" if all else "users.conversations"
    if error_code == "missing_scope":
        return ConnectorFailedError(
            f"Slack {endpoint} failed: missing_scope "
            f"(needed: {needed!r}). See ADR-0018 §Decision (7) or "
            f"https://api.slack.com/scopes for the scope catalogue."
        )
    return ConnectorFailedError(f"Slack {endpoint} failed: {error_code}")


def _emit_indexing_lag_notice() -> None:
    """Emit the engagement-axis indexing-lag advisory to stderr (once per call).

    Slack's ``search.messages`` full-text index lags message ingestion
    by minutes — the engagement axis surfaces a stale snapshot
    relative to live ``conversations.history``. ADR-0034 §(i)
    documents the cue so the operator who notices a freshly-posted
    channel missing from the output has a documented cause to map to
    without re-reading the SDK error model.

    Goes through ``sys.stderr`` (not the structured logger) because
    the listing CLI surface mixes a spinner + warnings on stderr — a
    one-shot advisory is the same stream the operator is already
    watching for the post-call warnings (``warning: skipping ...``).
    The structured logger path stays free for ``--debug`` traces.
    """
    import sys

    print(_INDEXING_LAG_NOTICE, file=sys.stderr)


def _emit_implicit_cutoff_notice(*, sort: SortKey) -> None:
    """Emit the ADR-0035 §(e) implicit-cutoff notice to stderr.

    Triggered once per ``list_conversations`` call when the operator
    asked for a ts-axis sort (``--sort=last_self_post`` or
    ``--sort=last_activity``) without specifying ``--since``. The
    helper applies a ``90d`` cutoff to cap probe cost (``search.messages``
    page consumption / per-row ``conversations.history`` call budget)
    and surfaces the notice so the choice is observable rather than
    silent magic. Explicit ``--since`` overrides the implicit cap.

    Goes through ``sys.stderr`` (matches :func:`_emit_indexing_lag_notice`'s
    one-shot advisory pattern). ADR-0035 §(e) pins the wording —
    keep the format string in lock-step with the test fixture in
    ``tests/unit/connectors/slack/test_conversations.py``.
    """
    import sys

    print(
        f"notice: --sort={sort} defaulted to --since 90d to cap probe "
        "cost; pass --since explicitly to override.",
        file=sys.stderr,
    )


def _fetch_self_post_index(
    *,
    client: Any,
    self_user_id: str,
    since_ts: float,
) -> dict[str, float]:
    """Walk ``search.messages`` and return ``{channel_id: max_ts}`` for self.

    Parameters
    ----------
    client:
        The :mod:`slack_sdk` ``WebClient`` (or a mock-shaped stand-in).
    self_user_id:
        Slack user id resolved via :meth:`SlackAuth.test_token`. The
        documented ``from:`` operator format wraps the id in angle
        brackets + ``@`` (``"from:<@U012345>"``) so the search engine
        interprets it as a user-mention filter rather than a string
        literal.
    since_ts:
        Unix epoch seconds lower bound. Forwarded to Slack as
        ``oldest=<since_ts>``; the search response only contains
        messages strictly newer than this cutoff.

    Returns
    -------
    dict[str, float]
        ``{channel_id: max_message_ts}`` aggregated across every page
        Slack returned. Channels appear in the dict only when the
        operator wrote at least one message in them within the
        ``since_ts`` window. The listing loop joins this dict against
        the discovered channel rows; unmatched rows are dropped.

    Pagination
    ----------

    Cursor-style pagination (``cursor=*`` initial → ``next_cursor`` in
    ``response_metadata``) is the first choice; page-style pagination
    (``paging.page`` / ``paging.pages``) is the documented fallback
    Slack still emits on the ``search.messages`` response shape. The
    helper consumes whichever shape the response advertises so an
    SDK-shaped proxy that omits ``next_cursor`` does not silently
    short-circuit after one page.

    Error mapping
    -------------

    Non-429 :class:`SlackApiError` (the budget-exhausted 429 included)
    is translated by :func:`_to_connector_failed_search` into an
    endpoint-qualified :class:`ConnectorFailedError`. The 429 retry /
    backoff loop is shared with the rest of the connector via
    :func:`retry_on_rate_limit`.
    """
    from slack_sdk.errors import SlackApiError

    query = f"from:<@{self_user_id}>"
    index: dict[str, float] = {}

    # Slack returns either cursor or page-based pagination on
    # ``search.messages`` — defaults below let the response shape pick
    # the next-page mechanism.
    next_cursor: str | None = "*"
    page_number = 1

    while True:
        kwargs: dict[str, Any] = {
            "query": query,
            "count": _SEARCH_MESSAGES_PAGE_SIZE,
            "sort": "timestamp",
            "sort_dir": "desc",
            "oldest": repr(float(since_ts)),
        }
        if next_cursor is not None:
            kwargs["cursor"] = next_cursor
        else:
            kwargs["page"] = page_number

        def _call(kwargs: dict[str, Any] = kwargs) -> Any:
            return client.search_messages(**kwargs)

        try:
            response = retry_on_rate_limit(_call)
        except SlackApiError as exc:
            raise _to_connector_failed_search(exc) from exc

        data = _as_response_dict(response)
        messages_obj = data.get("messages")
        if not isinstance(messages_obj, dict):
            # Defensive: a malformed proxy that lacks ``messages``
            # short-circuits cleanly. The caller will see an empty
            # index and drop every row — matches the "no recent
            # self posts" semantics.
            return index
        messages = cast(dict[str, Any], messages_obj)
        matches_obj = messages.get("matches")
        if isinstance(matches_obj, list):
            for match in cast(list[dict[str, Any]], matches_obj):
                channel_id, ts = _extract_search_match(match)
                if channel_id and ts is not None:
                    previous = index.get(channel_id)
                    if previous is None or ts > previous:
                        index[channel_id] = ts

        # Cursor pagination wins when ``next_cursor`` is present.
        response_metadata_obj = data.get("response_metadata")
        new_cursor: str | None = None
        if isinstance(response_metadata_obj, dict):
            cursor_raw = cast(dict[str, Any], response_metadata_obj).get("next_cursor")
            if cursor_raw:
                new_cursor = str(cursor_raw)
        if new_cursor:
            next_cursor = new_cursor
            continue

        # Fallback: walk page-based pagination via ``paging.page`` /
        # ``paging.pages``. The Slack docs response example carries
        # both shapes — page-based is the legacy default on
        # ``search.messages`` so the fallback is mandatory, not
        # paranoid.
        paging_obj = messages.get("paging")
        if isinstance(paging_obj, dict):
            paging = cast(dict[str, Any], paging_obj)
            current_page = _safe_int(paging.get("page"))
            total_pages = _safe_int(paging.get("pages"))
            if current_page is not None and total_pages is not None and current_page < total_pages:
                page_number = current_page + 1
                next_cursor = None
                continue

        return index


def _extract_search_match(match: dict[str, Any]) -> tuple[str, float | None]:
    """Return ``(channel_id, ts)`` from one ``search.messages`` match.

    The match shape is documented as ``{channel: {id, name, ...},
    ts: "1234567890.000100", ...}``. Defensive against thin proxies
    that flatten ``channel`` to a string id or drop the ``ts`` field;
    the caller treats missing values as "skip this match" so a
    malformed page never poisons the aggregate index.
    """
    channel_obj = match.get("channel")
    if isinstance(channel_obj, dict):
        channel_id = str(cast(dict[str, Any], channel_obj).get("id") or "")
    elif isinstance(channel_obj, str):
        channel_id = channel_obj
    else:
        channel_id = ""
    ts_raw = match.get("ts")
    if ts_raw is None:
        return channel_id, None
    try:
        return channel_id, float(ts_raw)
    except (TypeError, ValueError):
        return channel_id, None


def _safe_int(value: Any) -> int | None:
    """Best-effort ``int`` coerce; returns ``None`` for unparseable input."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_connector_failed_search(exc: Any) -> ConnectorFailedError:
    """Map a ``search.messages`` :class:`SlackApiError` → :class:`ConnectorFailedError`.

    Kept distinct from :func:`_to_connector_failed` and
    :func:`_to_connector_failed_history` so the error message names the
    endpoint the operator was waiting on (the discovery engagement axis
    uses one ``search.messages`` call per invocation rather than per
    row, so a 429 / scope failure here is operationally distinct from
    the listing / history paths).
    """
    response_any = cast(Any, getattr(exc, "response", None))
    error_code = "unknown"
    needed = ""
    if response_any is not None:
        error_code = response_any.get("error") or type(exc).__name__
        if error_code == "missing_scope":
            needed = response_any.get("needed") or ""
    if error_code == "missing_scope":
        return ConnectorFailedError(
            f"Slack search.messages failed: missing_scope "
            f"(needed: {needed!r}). User Token must hold "
            f"'search:read' for --sort=last_self_post (the engagement "
            f"axis). Rerun with "
            f"--sort=last_activity if you cannot grant the scope. "
            f"See ADR-0018 §Decision (7) or "
            f"https://api.slack.com/scopes for the scope catalogue."
        )
    return ConnectorFailedError(f"Slack search.messages failed: {error_code}")


def _emit_orphan_index_debug(
    *,
    self_post_index: dict[str, float] | None,
    referenced_channel_ids: set[str] | None,
) -> None:
    """Emit a debug counter for index channels not seen in the listing.

    On the engagement axis a channel can appear in the
    ``search.messages`` index but not in ``users.conversations``
    output — e.g. Slack Connect external channels, archived rows that
    listing excludes by default, or type-filtered listings
    (``--types public`` while the operator posted in a DM). The count
    is a *debug* signal only (``opshub --debug`` surfaces it); we do
    not warn the operator because the asymmetry is structural, not a
    misconfiguration.

    A ``None`` index (any-axis path) short-circuits cleanly.
    """
    if self_post_index is None or referenced_channel_ids is None:
        return
    orphan_count = sum(1 for cid in self_post_index if cid not in referenced_channel_ids)
    if orphan_count <= 0:
        return
    from opshub.core.logging import get_logger

    get_logger(__name__).debug(
        "slack.conversations.engagement_index_orphan",
        engagement_index_orphan=orphan_count,
        index_size=len(self_post_index),
        listing_size=len(referenced_channel_ids),
    )


def _as_response_dict(response: Any) -> dict[str, Any]:
    """Normalise a ``slack_sdk`` ``SlackResponse`` (or dict) into a dict.

    Mirrors :func:`opshub.connectors.slack.fetcher._as_response_dict` —
    kept as a private duplicate (rather than imported) so this module
    stays self-contained and the fetcher's private helper does not
    become an implicit public surface.
    """
    if isinstance(response, dict):
        return cast(dict[str, Any], response)
    data_obj = getattr(response, "data", None)
    if isinstance(data_obj, dict):
        return cast(dict[str, Any], data_obj)
    return cast(dict[str, Any], dict(response))
