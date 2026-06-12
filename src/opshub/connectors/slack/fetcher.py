"""Slack message fetcher (Phase 7 step A2).

Calls Slack's ``conversations.history`` API for the configured channels
with cursor-based pagination, yielding raw message dicts that the
Phase 7 step A3 mapper will translate to :class:`SourceObserved`
events. This module is intentionally scoped to fetching: no domain
events are emitted here, no UoW is opened — the caller (the future
A3 ``services/connector_sync_service.py`` glue) owns the transaction
boundary so a crash mid-loop is idempotent on retry (cursor advances
in the same UoW as the event commit).

Sync semantics
--------------

The "cursor" for Slack is the most recent message ``ts`` (timestamp)
we have observed in each channel. On resume we pass it as the
``oldest`` parameter to ``conversations.history`` with
``inclusive=False`` so we never re-fetch a message we've already
mapped (Slack's ``ts`` is monotonically increasing within a channel
and unique — it doubles as the message primary key in their data
model).

Pagination has two axes inside one fetch call:

* ``oldest`` (our cursor) — resumes from the last seen ``ts``.
* ``cursor`` (Slack's pagination cursor) — pages through the
  response chunk when ``has_more=True``.

The yield tuple ``(channel_id, message, new_cursor)`` lets the caller
persist a fresh cursor *per message* so an interrupted sync only
re-fetches messages after the last successfully-committed event.
This matches the Phase 3 :class:`GitHubConnector` precedent
("cursor advances in lock-step with the source observation") even
though the GitHub fetcher streams items rather than yielding the
cursor inline.

Rate-limit handling
-------------------

Slack returns HTTP 429 with a ``Retry-After`` header on tier-limit
overruns. ``slack_sdk`` surfaces this as a :class:`SlackApiError`
whose ``response.status_code`` is 429 and whose ``response.headers``
carry the ``Retry-After`` value. We honour ``Retry-After`` if
present and otherwise back off exponentially (1s / 2s / 4s) for up
to three attempts per phase-7-plan §1 #8. Exhausting the budget
escalates to :class:`ConnectorFailedError` so the caller's UoW can
record a ``ConnectorSyncFailed`` event.

Non-rate-limit ``SlackApiError`` (``invalid_auth`` /
``channel_not_found`` / ``missing_scope`` / ``not_in_channel`` /
...) is mapped directly to :class:`ConnectorFailedError` without
retry — re-running the fetch with the same token will fail the
same way (phase-3-plan §4 Q3 fail-fast posture).
``missing_scope`` typically indicates the configured channel
requires a scope the current token does not have (e.g. a private
channel without ``groups:history``, a DM without ``im:history``);
the operator should extend the token's scope set per ADR-0018.
``not_in_channel`` indicates the configured channel is not
accessible to the current token's principal — for a User Token
the operator should join the channel via Slack UI; for a Bot
Token they should ``/invite`` the bot to the channel.

Cold-start guard
----------------

``slack_sdk`` is imported lazily inside :meth:`SlackFetcher.fetch_messages`
so a cold ``import opshub.connectors.slack.fetcher`` (e.g. via
``opshub --help``-driven registry discovery in step A3) never pulls
the SDK onto the cold-start path. The cold-start integration test
(``tests/integration/test_cli_imports.py``) and the in-package
static guard for the A1 auth module enforce this rule for the
Slack subpackage as a whole.

Token safety
------------

The resolved Slack OAuth token never appears in raised exceptions:
we surface the Slack API ``error`` code (which is a documented
string like ``invalid_auth``) or, for transport errors, the
exception's type name only. The token never leaks even if an
operator pastes the error into a bug report. This invariant holds
regardless of principal (User Token / Bot Token) per ADR-0018.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.connectors.slack._retry import retry_on_rate_limit
from opshub.core.errors import ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.slack.auth import SlackAuth
    from opshub.core.excludes import ExcludeRules


__all__ = ["RawSlackMessage", "SlackFetcher"]


#: Final-fallback display name used only when *every* author resolution
#: path failed (no ``user`` id, no ``bot_id``, no ``bot_profile.name``).
#: The A3 mapper renders this as ``"unknown in #channel"`` — kept as a
#: last-resort safety net so the projection never lands an empty string
#: as the author. Issue #367 narrowed the surface: previously this was
#: returned for every bot / system message, masking the real author /
#: bot identity even when Slack populated ``bot_id`` or ``bot_profile``.
_UNKNOWN_USER_DISPLAY = "unknown"


@dataclass(frozen=True, slots=True)
class RawSlackMessage:
    """One Slack message normalised for the Phase 7 A3 mapper.

    Attributes
    ----------
    team_id:
        The Slack workspace ``team_id`` (``"T..."``) the message
        belongs to (Phase 24-B, [ADR-0041](
        docs/adr/0041-slack-multi-workspace.md) §(i)). Leads the
        mapper's ``external_id``
        (``f"{team_id}:{channel_id}:{ts}"``) so channel ids that
        collide across workspaces never collide in the source
        namespace. The connector resolves it once per sync via the
        bind guard (``auth.test``) and threads it through the
        fetcher constructor — no additional API call is paid per
        message.
    channel_id:
        The Slack channel id (``"C..."``). Pairs with ``team_id`` and
        ``ts`` to form the mapper's ``external_id``
        (``f"{team_id}:{channel_id}:{ts}"``).
    channel_name:
        The human-readable channel name (without the ``#`` prefix),
        resolved once per channel via ``conversations.info``.
    ts:
        The message timestamp (Slack's primary key for messages —
        ``"1700000000.123456"`` format). The mapper converts this to
        ``observed_at`` (UTC datetime).
    text:
        The raw message text. The mapper truncates this to ~200 chars
        for the ``sources.summary`` column per ADR-0005 (External
        Content Min) — we keep the full string here for the mapper
        to slice rather than truncating early and losing fidelity if
        the cap is revisited.
    user_id:
        The Slack user id (``"U..."``) that authored the message, or
        the empty string for bot / system messages (where ``user`` is
        absent in the API payload).
    user_display_name:
        The author's display name resolved via ``users.info`` for real
        users, ``bot_profile.name`` (falling back to ``"bot:{bot_id}"``)
        for bot messages, and only as a final safety net the literal
        ``"unknown"`` constant — see :data:`_UNKNOWN_USER_DISPLAY`. Cached
        per fetcher lifetime. The mapper composes this into the
        :attr:`SourceObserved.title` so any regression that re-routes a
        bot / system message back through the ``"unknown"`` arm
        immediately surfaces in the projection.
    subtype:
        The Slack ``subtype`` field (``"bot_message"`` / ``"channel_join"``
        / ``"me_message"`` / ...) or ``None`` for ordinary user
        messages. Carried as a first-class field rather than read from
        ``raw["subtype"]`` so a typo in the mapper surfaces at type-check
        time instead of silently routing every payload through the
        default arm (issue #367).
    permalink:
        Stable URL (``chat.getPermalink`` result) the mapper persists
        as the source's ``url`` column.
    raw:
        The complete ``conversations.history`` message dict, kept
        verbatim for forensic debugging (mapper test fixtures, future
        backfill of new fields without re-syncing). The mapper must
        not persist this — only its derived fields land in projections.
    thread_ts:
        The Slack ``thread_ts`` field — the parent message's ``ts``
        for a thread reply, or the parent's own ``ts`` for a thread
        root that already has replies. ``None`` for ordinary top-level
        messages that are not part of any thread (ADR-0030 §(c)).

        The field carries the same semantics as Gmail's ``threadId``
        (ADR-0010 §Phase 14 改訂 (k) 不変条件 3): thread membership is a
        per-message field, not a separate source_type. Downstream
        consumers (``reply-draft``, ``recall.search``) read it off
        ``SourceObserved.raw["thread_ts"]`` to assemble sibling-reply
        context without a projection schema change.
    """

    team_id: str
    channel_id: str
    channel_name: str
    ts: str
    text: str
    user_id: str
    user_display_name: str
    permalink: str
    raw: dict[str, Any]
    subtype: str | None = None
    thread_ts: str | None = None


class SlackFetcher:
    """Paginate ``conversations.history`` for one or more channels.

    The fetcher holds no SDK state at construction time — it only
    records the auth resolver + channel list — so it is cheap to
    construct in the CLI cold-start path. The ``slack_sdk.WebClient``
    is instantiated only when :meth:`fetch_messages` is actually
    called, keeping the cold-start budget (ADR-0001) intact.

    Per-instance caches (``_user_name_cache`` / ``_channel_name_cache``)
    avoid the per-message ``users.info`` / ``conversations.info``
    round-trip that would otherwise dominate sync latency on a busy
    channel. The caches are scoped to a single fetcher lifetime —
    long enough to amortise the lookups across one sync run, short
    enough that operator-side renames are visible on the next run.
    """

    def __init__(self, auth: SlackAuth, *, channels: list[str], team_id: str) -> None:
        if not channels:
            # Empty channel list almost certainly means an operator typo
            # in the config (``[connectors.slack.workspaces.<alias>] channels = []``). Failing
            # at construction time gives an actionable error instead of a
            # silently-no-op sync that misleads the operator into thinking
            # Slack is configured.
            raise ValueError("SlackFetcher requires at least one channel id")
        if not team_id:
            # Phase 24-B (ADR-0041 §(i)): ``team_id`` is a constituent of
            # the mapper's ``external_id`` (``f"{team_id}:{channel_id}:{ts}"``).
            # An empty value would silently mint malformed natural keys, so
            # fail loud at construction time — the connector's bind guard
            # resolves a non-empty value (or raises ConfigError) before
            # building the fetcher, making this branch a programming-error
            # tripwire rather than an operator-facing path.
            raise ValueError("SlackFetcher requires a non-empty team_id")
        self._auth = auth
        self._team_id = team_id
        # Defensive copy: the caller might mutate the supplied list
        # between construction and the actual sync call (e.g. config
        # reload). Pinning a snapshot keeps the fetcher behaviour
        # deterministic regardless.
        self._channels = list(channels)
        # Per-fetcher caches for the user / channel id → display-name
        # resolutions. See class docstring for the lifetime rationale.
        self._user_name_cache: dict[str, str] = {}
        self._channel_name_cache: dict[str, str] = {}

    def fetch_messages(
        self,
        *,
        cursor_per_channel: dict[str, str | None],
        latest_per_channel: dict[str, str | None] | None = None,
        max_per_channel: int = 100,
        excludes: ExcludeRules | None = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        """Yield ``(channel_id, message, new_cursor)`` for every new message.

        Parameters
        ----------
        cursor_per_channel:
            Maps channel id to the last-observed ``ts`` for that
            channel (the ``oldest`` parameter on the next
            ``conversations.history`` call). Pass ``None`` (or omit
            the channel id) to fetch the most recent
            ``max_per_channel`` messages on first sync.
        latest_per_channel:
            Optional per-channel **upper** ts bound (Phase 22-C,
            [ADR-0038](docs/adr/0038-slack-sync-gap-backfill.md) §(c)).
            When set for a channel, ``conversations.history`` is called
            with ``latest=<bound>`` so only messages at-or-below that ts
            are fetched. This drives the gap-backfill pass: the connector
            (22-D) runs a second ``fetch_messages`` over the channels
            whose floor was lowered, with ``cursor_per_channel={ch:
            floor_new}`` and ``latest_per_channel={ch: low_water}``, to
            fetch the newly-uncovered window ``(floor_new, low_water]``
            without re-touching the forward set ``(low_water, now]``.
            ``None`` (the default) preserves the forward-only Phase 7/20
            behaviour (no upper bound). See :meth:`_call_history` for the
            ``inclusive`` semantics on the bounded call.
        max_per_channel:
            Page size passed to the Slack API. Tier-2 Slack methods
            (``conversations.history``) allow up to ~1000 per page,
            but 100 is the SDK default and a comfortable per-call
            budget that avoids the rare large-response timeout.
        excludes:
            Optional :class:`~opshub.core.excludes.Excludes` filter
            (ADR-0020 §(b) shared ingest excludes). When a parent
            message would be excluded by ``channels`` or ``senders``,
            the fetcher skips the follow-up
            ``conversations.replies`` call as well — the reply
            messages would also be filtered out at the connector
            boundary, and skipping the call saves API budget on
            workspaces where excluded channels are noisy enough to
            dominate the Tier-3 ``conversations.replies`` budget
            (ADR-0030 §(b)). Pass ``None`` (the default) to fetch
            every thread reply unconditionally — the connector still
            applies its own per-yield excludes filter on the
            returned messages.

        Yields
        ------
        tuple[str, RawSlackMessage, str | None]
            The triple ``(channel_id, message, new_cursor)``. The
            third element is the cursor to persist *after* committing
            the SourceObserved event for ``message`` — i.e. the
            parent message's own ``ts`` for both parent and reply
            yields (ADR-0030 §(d): thread reply ts is **not** allowed
            to advance the per-channel resume cursor because the
            cursor must remain anchored to the channel's parent
            timeline so a partial-progress resume re-fetches the
            parent's replies after the cursor write but never skips a
            parent that lies between two reply timestamps). The
            caller writes this back into ``connector_cursors`` inside
            the same UoW that commits the event, so a crash here
            means at-most-once-or-no-loss delivery (the next sync
            resumes from the last committed cursor and re-fetches
            anything not yet committed).

        Raises
        ------
        ConnectorFailedError
            On any Slack API error that is not a recoverable 429
            (e.g. ``invalid_auth``, ``channel_not_found``), or when
            the 429 retry budget is exhausted. The error message
            includes the Slack ``error`` code and the offending
            channel id; it never includes the bot token. For
            ``missing_scope`` the message additionally surfaces the
            ``needed`` scope (when Slack populates it) and a link to
            ADR-0018 + ``https://api.slack.com/scopes`` so the
            operator can remediate without round-tripping the docs.
            For ``conversations.replies`` failures the message also
            includes the offending ``thread_ts`` so an operator can
            map the failure back to a specific thread (ADR-0030 §(e)
            error surface).
        """
        # Lazy-imported inside the method so importing this module
        # never pulls slack_sdk onto the cold-start path. The static
        # cold-start guard in this module's test suite asserts the
        # absence of top-level ``slack_sdk`` imports.
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        client = WebClient(token=self._auth.token)

        for channel_id in self._channels:
            oldest = cursor_per_channel.get(channel_id)
            latest = latest_per_channel.get(channel_id) if latest_per_channel is not None else None
            try:
                channel_name = self._resolve_channel_name(client, channel_id)
                yield from self._iter_channel(
                    client=client,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    oldest=oldest,
                    latest=latest,
                    limit=max_per_channel,
                    excludes=excludes,
                )
            except SlackApiError as exc:
                # ``invalid_auth`` / ``channel_not_found`` /
                # ``missing_scope`` / ``not_in_channel`` /
                # exhausted-retries-on-429 all land here. We surface
                # the API ``error`` code (a documented short string,
                # never the token) and the channel id so the operator
                # can map the failure back to a config change without
                # exposing secrets. ``missing_scope`` / ``not_in_channel``
                # are principal-sensitive — see the module docstring
                # for the User Token / Bot Token resolution paths.
                # ``exc.response`` is typed as ``SlackResponse`` whose
                # ``.get`` is partially-unknown to pyright; cast to
                # ``Any`` so the documented dict access type-checks.
                response_any = cast(Any, exc.response)
                error_code = response_any.get("error") or type(exc).__name__
                if error_code == "missing_scope":
                    # Slack populates ``needed`` on missing_scope responses
                    # with the scope name(s) the token lacks. The field
                    # contains only documented scope identifiers (e.g.
                    # ``channels:history``) — never the token — so it is
                    # safe to echo. User Token vs. Bot Token scope tabs
                    # diverge in the Slack admin UI, so we point at
                    # ADR-0018 (which documents the principal split) and
                    # the canonical scope catalogue rather than re-listing
                    # scopes inline.
                    needed = response_any.get("needed") or ""
                    raise ConnectorFailedError(
                        f"Slack fetch failed for channel {channel_id}: "
                        f"missing_scope (needed: {needed!r}). See "
                        f"ADR-0018 §Decision (7) or "
                        f"https://api.slack.com/scopes for the scope "
                        f"catalogue."
                    ) from exc
                raise ConnectorFailedError(
                    f"Slack fetch failed for channel {channel_id}: {error_code}"
                ) from exc

    # ------------------------------------------------------------------ helpers

    def _iter_channel(
        self,
        *,
        client: Any,
        channel_id: str,
        channel_name: str,
        oldest: str | None,
        latest: str | None = None,
        limit: int,
        excludes: ExcludeRules | None = None,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        """Yield messages for a single channel in chronological (ts-ascending) order.

        Slack's ``conversations.history`` returns messages
        **newest-first within a page** and walks to **older pages**
        via ``response_metadata.next_cursor``. Both axes are reversed
        relative to our resume cursor semantics, which require
        messages to arrive in ts-ascending order so the persisted
        cursor advances monotonically (``cursor[ch] = ts``) even when
        the caller crashes mid-loop.

        Reversing each page individually (the previous strategy) only
        fixed the **intra-page** axis: ``yield`` order across pages
        was still newest-page → oldest-page, so the last cursor
        written to the projection was the **oldest** ts of the
        **oldest** page — strictly less than the latest message the
        caller had already committed. The next sync would then
        re-fetch every message between the persisted (old) cursor
        and the true latest ts, re-emitting :class:`SourceObserved`
        + :class:`ItemEnqueued` events (and inflating ``inbox_items``
        on every run — see issue #339).

        We fix the page-axis bug by buffering **all** pages first,
        then sorting by ``float(ts)`` ascending before yielding.
        Memory cost is O(per-channel-per-sync messages); on a healthy
        resume (``oldest=cursor``) Slack returns only messages newer
        than the cursor, so the buffer is bounded by the activity
        accumulated since the last sync. The cold-start sync (no
        prior cursor) buffers the whole channel history, but that is
        a one-time cost paid in exchange for a cursor that cannot be
        rewound by a mid-sync crash.
        """
        buffered: list[dict[str, Any]] = []
        page_cursor: str | None = None
        while True:
            response = self._call_history(
                client=client,
                channel_id=channel_id,
                oldest=oldest,
                latest=latest,
                limit=limit,
                cursor=page_cursor,
            )
            messages_obj = response.get("messages")
            # The response is typed as ``dict[str, Any]`` so ``messages_obj``
            # is ``Any``; we narrow with ``isinstance`` and then cast to the
            # documented element type (``conversations.history`` always
            # returns a list of JSON objects).
            messages: list[dict[str, Any]] = (
                cast(list[dict[str, Any]], messages_obj) if isinstance(messages_obj, list) else []
            )
            buffered.extend(messages)
            # Walk to the next page only if Slack signals more data.
            # ``has_more`` is the documented stop signal;
            # ``next_cursor`` may be present-but-empty in some response
            # shapes so we check both.
            response_metadata_obj = response.get("response_metadata")
            response_metadata: dict[str, Any] = (
                cast(dict[str, Any], response_metadata_obj)
                if isinstance(response_metadata_obj, dict)
                else {}
            )
            next_cursor = response_metadata.get("next_cursor")
            if not response.get("has_more") or not next_cursor:
                break
            page_cursor = str(next_cursor)

        # Sort the cross-page buffer ts-ascending so the caller's
        # cursor advances monotonically. Malformed messages without
        # ``ts`` are filtered here (rather than mid-yield) so the
        # sort key is total. Using ``float(ts)`` matches Slack's
        # documented ``ts`` format ("seconds.microseconds") and is
        # stable across pages because Slack guarantees ``ts``
        # uniqueness per channel.
        def _ts_key(raw: dict[str, Any]) -> float:
            ts_raw = raw.get("ts")
            try:
                return float(ts_raw) if ts_raw is not None else 0.0
            except (TypeError, ValueError):
                # Malformed ts — sort to the front so the skip
                # branch below drops it before yielding.
                return 0.0

        for raw in sorted(buffered, key=_ts_key):
            ts = str(raw.get("ts", ""))
            if not ts:
                # A message without ``ts`` is malformed — Slack's
                # contract says every message has it. Skip rather
                # than crash so a single bad row doesn't poison
                # the whole sync; the malformed message stays in
                # ``raw`` for forensic inspection. Thread replies are
                # gated on a valid parent ``ts`` (the
                # ``conversations.replies`` request requires it), so
                # the malformed-ts skip arm covers replies too.
                continue
            user_id = str(raw.get("user", ""))
            # Slack populates ``subtype`` for bot messages and system
            # events (``channel_join`` / ``channel_leave`` /
            # ``channel_purpose`` / ``channel_topic`` / ``me_message``
            # / ``bot_message`` / ...). Ordinary user messages omit
            # the key entirely. We normalise the missing key to
            # ``None`` (rather than ``""``) so the mapper's
            # ``raw.subtype is None`` branch is unambiguous.
            subtype_raw = raw.get("subtype")
            subtype = str(subtype_raw) if subtype_raw else None
            # Resolve the author display name with a richer fallback
            # chain than the legacy ``"unknown"`` arm (issue #367).
            # Bot messages don't have a ``user`` id but Slack populates
            # ``bot_profile.name`` (the workspace-visible bot label)
            # and ``bot_id`` — both produce a more useful title than
            # the literal string ``"unknown"``.
            user_display_name = self._resolve_author_display(
                client=client, raw=raw, user_id=user_id
            )
            permalink = self._resolve_permalink(client=client, channel_id=channel_id, ts=ts)
            # ADR-0030 §(c) thread_ts semantics for a parent message
            # from ``conversations.history``: if Slack populated
            # ``thread_ts`` (even when equal to ``ts``, the convention
            # Slack uses to mark a parent that already has replies)
            # we forward the value verbatim; otherwise the parent is a
            # standalone top-level message and ``thread_ts`` stays
            # ``None``. The downstream ``reply-draft`` skill reads
            # this off ``SourceObserved.raw["thread_ts"]`` to assemble
            # sibling-reply context without a projection schema bump.
            parent_thread_ts_raw = raw.get("thread_ts")
            parent_thread_ts = (
                str(parent_thread_ts_raw) if parent_thread_ts_raw is not None else None
            )
            message = RawSlackMessage(
                team_id=self._team_id,
                channel_id=channel_id,
                channel_name=channel_name,
                ts=ts,
                text=str(raw.get("text", "")),
                user_id=user_id,
                user_display_name=user_display_name,
                permalink=permalink,
                raw=raw,
                subtype=subtype,
                thread_ts=parent_thread_ts,
            )
            yield channel_id, message, ts

            # ADR-0030 §(b) thread reply fetch. Slack only returns
            # parent messages via ``conversations.history``; child
            # replies live behind ``conversations.replies``. The
            # ``latest_reply`` field on the parent payload is the
            # documented "this parent has at least one reply" signal
            # (Slack populates it whenever ``reply_count > 0``); we
            # gate the follow-up API call on its presence to keep the
            # Tier-3 budget headroom intact on workspaces dominated by
            # standalone messages.
            #
            # Excludes short-circuit (ADR-0030 §(b)): if the connector
            # would drop the parent on its per-yield filter
            # (``channels`` / ``senders``), the replies would be
            # dropped too (the connector applies the same filter
            # on every yield). Skipping the API call here saves
            # ``conversations.replies`` budget on workspaces where
            # excluded channels are noisy. The connector still
            # applies its own per-yield filter on every reply we do
            # yield, so this guard is purely a budget optimisation
            # and does not change the observable connector behaviour.
            if raw.get("latest_reply") is None:
                continue
            if excludes is not None and (
                excludes.excludes_channel(channel_id) or excludes.excludes_sender(user_id)
            ):
                continue
            yield from self._iter_thread_replies(
                client=client,
                channel_id=channel_id,
                channel_name=channel_name,
                parent_ts=ts,
            )

    def _iter_thread_replies(
        self,
        *,
        client: Any,
        channel_id: str,
        channel_name: str,
        parent_ts: str,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        """Yield thread reply messages for a parent (ADR-0030 §(b) §(c)).

        Calls ``conversations.replies(channel, ts=parent_ts)`` via the
        shared :func:`opshub.connectors.slack._retry.retry_on_rate_limit`
        helper (ADR-0030 §(e), the 4th call site alongside
        :meth:`_call_history`,
        :func:`opshub.connectors.slack.conversations._call_history_oldest`,
        and :func:`opshub.connectors.slack.conversations._call_list`).

        Yield semantics
        ---------------

        * ``messages[0]`` is the parent message itself
          (``conversations.replies`` includes the parent as the
          envelope head). We **skip it** explicitly to avoid the
          duplicate ``external_id = f"{team_id}:{channel_id}:{ts}"`` (parent
          already yielded by :meth:`_iter_channel`). ADR-0030 §不変条件
          3 leaves dedup to the projection's ``UNIQUE`` constraint as
          a fallback, but skipping at the source keeps the event log
          clean and saves one ``users.info`` / ``chat.getPermalink``
          round-trip per parent.
        * Replies yielded carry the parent's ``ts`` as their
          ``thread_ts`` field (ADR-0030 §(c) reply semantics) and the
          parent's ``ts`` as the cursor element (ADR-0030 §(d): the
          per-channel resume cursor is anchored to the channel's
          parent timeline, so reply yields must not advance it past
          the parent).
        """
        response = self._call_replies(
            client=client,
            channel_id=channel_id,
            thread_ts=parent_ts,
        )
        messages_obj = response.get("messages")
        messages: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], messages_obj) if isinstance(messages_obj, list) else []
        )
        # Sort defensively even though Slack documents
        # ``conversations.replies`` as returning messages in ts-ascending
        # order (parent + replies). A bad response shape would otherwise
        # yield replies out of order and silently break the cursor
        # monotonicity contract on the connector side.
        for reply_raw in sorted(messages, key=lambda raw: self._reply_ts_key(raw)):
            reply_ts = str(reply_raw.get("ts", ""))
            if not reply_ts:
                # Defensive skip mirroring the parent-side guard. A
                # reply without ``ts`` cannot be deduplicated nor
                # cursor-tracked.
                continue
            if reply_ts == parent_ts:
                # ``messages[0]`` is the parent itself — skip to keep
                # the event log clean (the parent was already yielded
                # by ``_iter_channel``). Belt-and-braces: the
                # projection's UNIQUE constraint on ``external_id``
                # would catch a regression that forgot this skip
                # (ADR-0030 §不変条件 3), but explicit avoidance saves
                # one ``users.info`` / ``chat.getPermalink`` budget
                # per parent.
                continue
            reply_user_id = str(reply_raw.get("user", ""))
            reply_subtype_raw = reply_raw.get("subtype")
            reply_subtype = str(reply_subtype_raw) if reply_subtype_raw else None
            reply_display = self._resolve_author_display(
                client=client, raw=reply_raw, user_id=reply_user_id
            )
            reply_permalink = self._resolve_permalink(
                client=client, channel_id=channel_id, ts=reply_ts
            )
            reply_message = RawSlackMessage(
                team_id=self._team_id,
                channel_id=channel_id,
                channel_name=channel_name,
                ts=reply_ts,
                text=str(reply_raw.get("text", "")),
                user_id=reply_user_id,
                user_display_name=reply_display,
                permalink=reply_permalink,
                raw=reply_raw,
                subtype=reply_subtype,
                thread_ts=parent_ts,
            )
            # ADR-0030 §(d): yield the parent's ``ts`` as the cursor
            # element, not the reply's own ts, so the per-channel
            # resume cursor stays anchored to the parent timeline.
            # A reply with a ts > parent_ts must NOT advance the
            # cursor past the parent — otherwise a later sync would
            # skip parents whose ts lies between two reply
            # timestamps.
            yield channel_id, reply_message, parent_ts

    @staticmethod
    def _reply_ts_key(raw: dict[str, Any]) -> float:
        """Return a float sort key for a thread reply payload.

        Mirrors the ``_ts_key`` helper inside :meth:`_iter_channel`
        but is hoisted to a static method so the reply-yield loop
        can share the malformed-ts defence without an inner closure.
        """
        ts_raw = raw.get("ts")
        try:
            return float(ts_raw) if ts_raw is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _call_history(
        self,
        *,
        client: Any,
        channel_id: str,
        oldest: str | None,
        limit: int,
        cursor: str | None,
        latest: str | None = None,
    ) -> dict[str, Any]:
        """Call ``conversations.history`` with 429 backoff per phase-7-plan §1 #8.

        Retry policy (3 attempts, ``Retry-After`` honoured, 1s / 2s / 4s
        exponential fallback) lives in
        :func:`opshub.connectors.slack._retry.retry_on_rate_limit` so
        this method and the discovery-side
        :func:`opshub.connectors.slack.conversations._call_history_oldest`
        share one source of truth (#377). Non-429
        :class:`SlackApiError` (and the final 429 after the budget
        is spent) re-raise to the caller's ``except SlackApiError``
        arm in :meth:`fetch_messages`, which maps them to
        :class:`ConnectorFailedError` with a channel-scoped message —
        keeping the rate-limit case uniform with other API failures.
        """
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "limit": limit,
        }
        if oldest is not None:
            kwargs["oldest"] = oldest
        if latest is not None:
            kwargs["latest"] = latest
        if oldest is not None or latest is not None:
            # Slack's ``inclusive`` is a SINGLE boolean covering both the
            # ``oldest`` and ``latest`` bounds (the API has no per-bound
            # flag). We choose it by call kind:
            #
            # * Forward sync (``oldest`` set, ``latest`` is None) →
            #   ``inclusive=False``. ``oldest`` is the last *observed real
            #   message* ts; without exclusion Slack would re-yield it on
            #   every resume, breaking idempotency (the mapper would
            #   re-emit a SourceObserved we already committed).
            # * Gap backfill (``latest`` set, Phase 22-C/D) →
            #   ``inclusive=True``. Both bounds are *synthetic date
            #   floors* (``floor_new`` / ``low_water``) that practically
            #   never coincide with a real message ts, so including them
            #   is safe and makes the backfill window
            #   ``[floor_new, low_water]`` disjoint from the forward set
            #   ``(low_water, now]`` — the forward cold-start used
            #   ``oldest=low_water, inclusive=False`` so it never fetched
            #   ``ts == low_water``. ADR-0038 §(c) (the half-open interval
            #   there is an idealisation; Slack's single ``inclusive``
            #   makes this the faithful realisation).
            kwargs["inclusive"] = latest is not None
        if cursor is not None:
            kwargs["cursor"] = cursor

        def _call() -> Any:
            return client.conversations_history(**kwargs)

        return _as_response_dict(retry_on_rate_limit(_call))

    def fetch_thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        oldest_reply_ts: str | None,
        channel_name: str | None = None,
    ) -> Iterator[RawSlackMessage]:
        """Yield thread replies newer than ``oldest_reply_ts`` (Phase 20-C).

        Phase 20-C ([epic #465](https://github.com/ozzy-labs/opshub/issues/465),
        ADR-0030 §(d) revised) introduces a 2-phase sync:

        1. Phase 1 (``conversations.history``) — covered by
           :meth:`fetch_messages`; new parents + their initial replies
           snapshot.
        2. Phase 2 (``conversations.replies(oldest=last_reply_ts)``) —
           this method; per-thread incremental fetch driven by the
           ``threads`` axis of the compound cursor.

        Parameters
        ----------
        channel_id:
            The Slack channel id the thread lives in.
        thread_ts:
            The parent message's ``ts`` (the Slack thread id).
        oldest_reply_ts:
            The last-observed reply ``ts`` for this thread (the
            ``threads`` cursor value). ``None`` means "no reply
            observed yet" — Slack receives no ``oldest`` parameter and
            returns the full thread (used for cold-start of a thread
            cursor that was just initialised from ``latest_reply_ts``,
            though in practice the connector initialises the cursor to
            the parent's ``latest_reply_ts`` so this branch is rare).
        channel_name:
            Optional human-readable channel name for the yielded
            :class:`RawSlackMessage` rows. Resolved lazily when
            omitted (cached per fetcher lifetime).

        Yields
        ------
        RawSlackMessage
            One row per reply strictly newer than ``oldest_reply_ts``
            (or every reply but the parent when ``oldest_reply_ts`` is
            ``None``). The yield type is just :class:`RawSlackMessage`
            (not the ``(channel_id, message, new_cursor)`` triple
            :meth:`fetch_messages` yields) because the late-reply
            polling phase advances the **threads** axis of the
            compound cursor, not the per-channel ``channels`` axis;
            the caller computes the new threads cursor as
            ``max(prior, reply.ts)`` directly. Replies arrive in
            ts-ascending order (defensive sort applied even though
            Slack documents that order).

        Raises
        ------
        ConnectorFailedError
            Same surface as :meth:`fetch_messages` for the reply path —
            non-429 :class:`SlackApiError`, exhausted retry budget,
            ``missing_scope``. The error message names the offending
            ``channel_id`` + ``thread_ts`` so an operator can map back
            to the specific thread. The bot / user token never appears
            in the surfaced message.
        """
        from slack_sdk import WebClient

        client = WebClient(token=self._auth.token)

        # Lazy channel-name resolution mirrors the per-message path so
        # an operator who configured many threads but few channels
        # doesn't pay N ``conversations.info`` round-trips when the
        # polling phase runs.
        resolved_channel_name = (
            channel_name
            if channel_name is not None
            else self._resolve_channel_name(client, channel_id)
        )

        response = self._call_replies(
            client=client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            oldest=oldest_reply_ts,
        )
        messages_obj = response.get("messages")
        messages: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], messages_obj) if isinstance(messages_obj, list) else []
        )

        for reply_raw in sorted(messages, key=lambda raw: self._reply_ts_key(raw)):
            reply_ts = str(reply_raw.get("ts", ""))
            if not reply_ts:
                # Malformed payload; same defensive skip as the parent
                # path.
                continue
            if reply_ts == thread_ts:
                # ``messages[0]`` is the parent itself — skip to keep
                # the event log clean. Even on the polling phase Slack
                # always returns the parent as the envelope head, and
                # the parent was already observed in Phase 1.
                continue
            if oldest_reply_ts is not None:
                # Defense in depth: Slack's ``oldest`` filter is
                # server-side, but a malformed server response or a
                # future API drift could let a stale reply through.
                # Re-filter client-side so the threads cursor advances
                # monotonically.
                try:
                    if float(reply_ts) <= float(oldest_reply_ts):
                        continue
                except (TypeError, ValueError):
                    # Malformed ts — let it through; ``_max_ts`` on the
                    # caller side will treat it as the new cursor.
                    pass
            reply_user_id = str(reply_raw.get("user", ""))
            reply_subtype_raw = reply_raw.get("subtype")
            reply_subtype = str(reply_subtype_raw) if reply_subtype_raw else None
            reply_display = self._resolve_author_display(
                client=client, raw=reply_raw, user_id=reply_user_id
            )
            reply_permalink = self._resolve_permalink(
                client=client, channel_id=channel_id, ts=reply_ts
            )
            yield RawSlackMessage(
                team_id=self._team_id,
                channel_id=channel_id,
                channel_name=resolved_channel_name,
                ts=reply_ts,
                text=str(reply_raw.get("text", "")),
                user_id=reply_user_id,
                user_display_name=reply_display,
                permalink=reply_permalink,
                raw=reply_raw,
                subtype=reply_subtype,
                thread_ts=thread_ts,
            )

    def _call_replies(
        self,
        *,
        client: Any,
        channel_id: str,
        thread_ts: str,
        oldest: str | None = None,
    ) -> dict[str, Any]:
        """Call ``conversations.replies`` with 429 backoff (ADR-0030 §(e)).

        4th call site of
        :func:`opshub.connectors.slack._retry.retry_on_rate_limit`
        alongside :meth:`_call_history`,
        :func:`opshub.connectors.slack.conversations._call_history_oldest`,
        and :func:`opshub.connectors.slack.conversations._call_list`.
        Maintaining a shared helper keeps the retry policy (3 attempts,
        ``Retry-After`` honoured, 1s / 2s / 4s exponential fallback)
        in one place — any future tweak (jitter, longer budget, etc.)
        propagates uniformly.

        Error surface
        -------------

        Non-429 :class:`SlackApiError` (and the final 429 after the
        budget is spent) is mapped here to :class:`ConnectorFailedError`
        with the offending channel id **and** thread ts in the message
        — distinct from the per-channel error path
        (:meth:`fetch_messages`) so an operator can tell a
        ``conversations.replies`` failure from a
        ``conversations.history`` failure at a glance. ``missing_scope``
        gets the same ADR-0018 + scope catalogue hint as the parent
        path so the operator has one-hop remediation regardless of
        which endpoint reported the miss. The bot / user token never
        appears in the surfaced message regardless of error code (the
        token-leak invariant pinned by the per-channel error tests
        applies here too).
        """
        from slack_sdk.errors import SlackApiError

        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "ts": thread_ts,
        }
        if oldest is not None:
            # Phase 20-C: incremental thread polling. Slack's ``oldest``
            # parameter on ``conversations.replies`` filters out replies
            # whose ``ts <= oldest`` server-side; ``inclusive=False``
            # prevents Slack from re-yielding the boundary reply (the
            # last reply we already observed). Without ``inclusive=False``
            # the polling phase would re-emit the same reply on every
            # sync and ``ItemEnqueued`` would inflate per-run.
            kwargs["oldest"] = oldest
            kwargs["inclusive"] = False

        def _call() -> Any:
            return client.conversations_replies(**kwargs)

        try:
            return _as_response_dict(retry_on_rate_limit(_call))
        except SlackApiError as exc:
            response_any = cast(Any, exc.response)
            error_code = response_any.get("error") or type(exc).__name__
            if error_code == "missing_scope":
                needed = response_any.get("needed") or ""
                raise ConnectorFailedError(
                    f"Slack thread reply fetch failed for channel "
                    f"{channel_id} thread_ts={thread_ts}: missing_scope "
                    f"(needed: {needed!r}). See ADR-0018 §Decision (7) or "
                    f"https://api.slack.com/scopes for the scope catalogue."
                ) from exc
            raise ConnectorFailedError(
                f"Slack thread reply fetch failed for channel "
                f"{channel_id} thread_ts={thread_ts}: {error_code}"
            ) from exc

    def _resolve_author_display(self, *, client: Any, raw: dict[str, Any], user_id: str) -> str:
        """Resolve a human-recognisable author name for any message shape.

        Resolution order (issue #367):

        1. ``user_id`` is present → :meth:`_resolve_user_name` (cached
           ``users.info`` lookup with ``profile.display_name`` →
           ``profile.real_name`` → ``user.name`` fallback chain).
        2. Bot message → ``bot_profile.name`` (Slack's
           workspace-visible bot label, e.g. ``"GitHub"``).
        3. Bot message without ``bot_profile`` but with ``bot_id`` →
           ``f"bot:{bot_id}"`` so the operator can still trace the
           message back to its bot integration.
        4. Final safety net → :data:`_UNKNOWN_USER_DISPLAY`. This
           branch should be unreachable for any real Slack payload —
           it exists only so a malformed message never produces an
           empty-string author in the projection.

        The function is intentionally stateless: caching belongs to
        the per-user :meth:`_resolve_user_name` path (the only branch
        that makes an API call). Bot-profile resolution reads from
        the message payload directly so no extra API budget is spent.
        """
        if user_id:
            return self._resolve_user_name(client, user_id)
        # Bot message: prefer the human-readable ``bot_profile.name``
        # before the opaque ``bot_id``. The two fields can co-exist;
        # the name reads better in the title so we promote it first.
        # ``raw`` is typed as ``dict[str, Any]`` so ``isinstance`` is
        # the runtime narrow; the explicit ``cast`` keeps pyright
        # strict mode happy (without it the narrowed dict is
        # ``dict[Unknown, Unknown]`` and the ``.get`` lookup leaks
        # ``Unknown`` into the return path).
        bot_profile_obj = raw.get("bot_profile")
        if isinstance(bot_profile_obj, dict):
            bot_profile = cast(dict[str, Any], bot_profile_obj)
            bot_name_raw = bot_profile.get("name")
            bot_name = str(bot_name_raw).strip() if bot_name_raw else ""
            if bot_name:
                return bot_name
        bot_id_raw = raw.get("bot_id")
        bot_id = str(bot_id_raw).strip() if bot_id_raw else ""
        if bot_id:
            return f"bot:{bot_id}"
        return _UNKNOWN_USER_DISPLAY

    def _resolve_user_name(self, client: Any, user_id: str) -> str:
        """Look up a Slack user's display name, caching the result.

        Bot / system messages may omit ``user`` entirely; in that
        case we return :data:`_UNKNOWN_USER_DISPLAY` without an API
        call. For real users we prefer ``profile.display_name`` (the
        Slack-canonical handle) and fall back to ``profile.real_name``
        / ``name`` so renamed-but-not-display-set users still surface
        with something useful.

        For bot / system messages :meth:`_resolve_author_display` is
        the preferred entry point — it tries ``bot_profile.name`` /
        ``bot_id`` before falling through to this helper's
        ``_UNKNOWN_USER_DISPLAY`` arm.

        Caching is scoped to this fetcher instance — long enough to
        amortise across one sync run, short enough that an operator
        renaming a user sees the change on the next sync.
        """
        if not user_id:
            return _UNKNOWN_USER_DISPLAY
        cached = self._user_name_cache.get(user_id)
        if cached is not None:
            return cached
        response: Any = client.users_info(user=user_id)
        user_obj = response.get("user")
        user: dict[str, Any] = cast(dict[str, Any], user_obj) if isinstance(user_obj, dict) else {}
        profile_obj = user.get("profile")
        profile: dict[str, Any] = (
            cast(dict[str, Any], profile_obj) if isinstance(profile_obj, dict) else {}
        )
        # ``display_name`` is the user-chosen handle; ``real_name`` is
        # the legal-name fallback Slack sets at account creation. Both
        # may be empty strings, hence the explicit ``or`` chain.
        display_name = (
            str(profile.get("display_name") or "")
            or str(profile.get("real_name") or "")
            or str(user.get("name") or "")
            or _UNKNOWN_USER_DISPLAY
        )
        self._user_name_cache[user_id] = display_name
        return display_name

    def _resolve_channel_name(self, client: Any, channel_id: str) -> str:
        """Look up a channel's name, caching the result for the fetcher lifetime."""
        cached = self._channel_name_cache.get(channel_id)
        if cached is not None:
            return cached
        response: Any = client.conversations_info(channel=channel_id)
        channel_obj = response.get("channel")
        channel: dict[str, Any] = (
            cast(dict[str, Any], channel_obj) if isinstance(channel_obj, dict) else {}
        )
        # Slack returns ``name`` for public / private channels; DMs
        # don't have a name and fall back to the channel id which the
        # mapper will surface as ``#C12345`` (still navigable via the
        # permalink, which we always have).
        name = str(channel.get("name") or channel_id)
        self._channel_name_cache[channel_id] = name
        return name

    def _resolve_permalink(self, *, client: Any, channel_id: str, ts: str) -> str:
        """Resolve a stable permalink for ``(channel_id, ts)``.

        Slack tier-1 rate-limits ``chat.getPermalink`` generously
        (~100 calls / minute), so one-call-per-message is acceptable
        for the Phase 7 MVP. A future Phase 7.x optimisation can
        batch by constructing permalinks from ``team_id`` /
        ``channel_id`` / ``ts`` directly, but the construction rules
        differ between workspaces (legacy ``T...`` vs. enterprise
        grids), so the API call is the safest baseline.
        """
        response: Any = client.chat_getPermalink(channel=channel_id, message_ts=ts)
        permalink = response.get("permalink")
        return str(permalink) if permalink else ""


def _as_response_dict(response: Any) -> dict[str, Any]:
    """Normalise a ``slack_sdk`` ``SlackResponse`` (or dict) into a dict.

    The SDK returns ``SlackResponse`` objects that proxy dict access
    via ``__getitem__`` / ``get``, but the type-checker can't see
    that proxy. Callers — including tests using ``MagicMock`` — pass
    raw dicts. Normalising here keeps the iteration logic in
    :meth:`SlackFetcher._iter_channel` typed as ``dict[str, Any]``
    without sprinkling ``cast`` calls.
    """
    if isinstance(response, dict):
        return cast(dict[str, Any], response)
    data_obj = getattr(response, "data", None)
    if isinstance(data_obj, dict):
        return cast(dict[str, Any], data_obj)
    # As a last resort, build a dict via the documented ``.get`` /
    # iteration interface. This branch is reachable only if the SDK
    # ever returns a non-dict-non-SlackResponse, which the docs say
    # it does not — but defensive coding here is cheap.
    return cast(dict[str, Any], dict(response))
