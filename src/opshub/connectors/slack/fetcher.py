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

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.core.errors import ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.slack.auth import SlackAuth


__all__ = ["RawSlackMessage", "SlackFetcher"]


#: How many ``conversations.history`` retries to attempt after a 429
#: before escalating to :class:`ConnectorFailedError`. Per phase-7-plan
#: §1 #8 the budget is fixed at three attempts with a 1s / 2s / 4s
#: backoff. Exposed as a module constant so tests can pin the value
#: without re-reading the magic number out of the implementation.
_MAX_RETRIES_ON_RATE_LIMIT = 3

#: Fallback display name when a Slack message has no ``user`` field
#: (bot messages, system messages, ...). The A3 mapper renders this
#: as ``"unknown in #channel"`` which is still useful context for the
#: brief / propose paths.
_UNKNOWN_USER_DISPLAY = "unknown"


@dataclass(frozen=True, slots=True)
class RawSlackMessage:
    """One Slack message normalised for the Phase 7 A3 mapper.

    Attributes
    ----------
    channel_id:
        The Slack channel id (``"C..."``). Pairs with ``ts`` to form
        the mapper's ``external_id`` (``f"{channel_id}:{ts}"``).
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
        The author's display name resolved via ``users.info``, cached
        per fetcher lifetime. Falls back to ``"unknown"`` for bot /
        system messages.
    permalink:
        Stable URL (``chat.getPermalink`` result) the mapper persists
        as the source's ``url`` column.
    raw:
        The complete ``conversations.history`` message dict, kept
        verbatim for forensic debugging (mapper test fixtures, future
        backfill of new fields without re-syncing). The mapper must
        not persist this — only its derived fields land in projections.
    """

    channel_id: str
    channel_name: str
    ts: str
    text: str
    user_id: str
    user_display_name: str
    permalink: str
    raw: dict[str, Any]


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

    def __init__(self, auth: SlackAuth, *, channels: list[str]) -> None:
        if not channels:
            # Empty channel list almost certainly means an operator typo
            # in the config (``[connectors.slack] channels = []``). Failing
            # at construction time gives an actionable error instead of a
            # silently-no-op sync that misleads the operator into thinking
            # Slack is configured.
            raise ValueError("SlackFetcher requires at least one channel id")
        self._auth = auth
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
        max_per_channel: int = 100,
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
        max_per_channel:
            Page size passed to the Slack API. Tier-2 Slack methods
            (``conversations.history``) allow up to ~1000 per page,
            but 100 is the SDK default and a comfortable per-call
            budget that avoids the rare large-response timeout.

        Yields
        ------
        tuple[str, RawSlackMessage, str | None]
            The triple ``(channel_id, message, new_cursor)``. The
            third element is the cursor to persist *after* committing
            the SourceObserved event for ``message`` — i.e. the
            message's own ``ts``. The caller writes this back into
            ``connector_cursors`` inside the same UoW that commits
            the event, so a crash here means at-most-once-or-no-loss
            delivery (the next sync resumes from the last committed
            cursor and re-fetches anything not yet committed).

        Raises
        ------
        ConnectorFailedError
            On any Slack API error that is not a recoverable 429
            (e.g. ``invalid_auth``, ``channel_not_found``), or when
            the 429 retry budget is exhausted. The error message
            includes the Slack ``error`` code and the offending
            channel id; it never includes the bot token.
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
            try:
                channel_name = self._resolve_channel_name(client, channel_id)
                yield from self._iter_channel(
                    client=client,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    oldest=oldest,
                    limit=max_per_channel,
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
        limit: int,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        """Yield messages for a single channel, paging via ``next_cursor``.

        Slack returns messages in **reverse-chronological** order
        (newest first) within a page. For our resume semantics we
        need the **oldest unseen** message to come first so the
        cursor (``ts``) advances monotonically as the caller commits
        each event — otherwise a mid-loop crash would leave the
        cursor pointing past unprocessed messages.

        We solve this by buffering the page, walking it in reverse,
        and only then advancing to the next ``next_cursor`` page.
        The page size is bounded by ``limit`` so memory is O(limit)
        per channel, not O(total messages).
        """
        page_cursor: str | None = None
        while True:
            response = self._call_history(
                client=client,
                channel_id=channel_id,
                oldest=oldest,
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
            # Walk oldest → newest so the yielded cursor (``ts``) is
            # monotonically increasing. ``conversations.history``
            # documents the response order as newest-first; reversing
            # gives us the chronological order our cursor semantics
            # require.
            for raw in reversed(messages):
                ts = str(raw.get("ts", ""))
                if not ts:
                    # A message without ``ts`` is malformed — Slack's
                    # contract says every message has it. Skip rather
                    # than crash so a single bad row doesn't poison
                    # the whole sync; the malformed message stays in
                    # ``raw`` for forensic inspection on the next page.
                    continue
                user_id = str(raw.get("user", ""))
                user_display_name = self._resolve_user_name(client, user_id)
                permalink = self._resolve_permalink(client=client, channel_id=channel_id, ts=ts)
                message = RawSlackMessage(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    ts=ts,
                    text=str(raw.get("text", "")),
                    user_id=user_id,
                    user_display_name=user_display_name,
                    permalink=permalink,
                    raw=raw,
                )
                yield channel_id, message, ts
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
                return
            page_cursor = str(next_cursor)

    def _call_history(
        self,
        *,
        client: Any,
        channel_id: str,
        oldest: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """Call ``conversations.history`` with 429 backoff per phase-7-plan §1 #8.

        Retry budget: three attempts, sleeping ``Retry-After`` seconds
        between them (falling back to 1s / 2s / 4s if Slack omits the
        header). On exhaustion we re-raise the final ``SlackApiError``
        so the caller's ``except SlackApiError`` arm in
        :meth:`fetch_messages` can map it to
        :class:`ConnectorFailedError` with a channel-scoped message —
        keeping the rate-limit case uniform with other API failures.
        """
        # Lazy-import the exception class to avoid module-level
        # ``slack_sdk`` import (cold-start guard).
        from slack_sdk.errors import SlackApiError

        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "limit": limit,
        }
        if oldest is not None:
            kwargs["oldest"] = oldest
            # Without ``inclusive=False`` Slack would re-yield the
            # message at ``oldest`` (the boundary message) on every
            # resume, breaking idempotency. The mapper would then
            # re-emit a SourceObserved for an event we already
            # committed last run.
            kwargs["inclusive"] = False
        if cursor is not None:
            kwargs["cursor"] = cursor

        last_error: SlackApiError | None = None
        for attempt in range(_MAX_RETRIES_ON_RATE_LIMIT):
            try:
                response: Any = client.conversations_history(**kwargs)
            except SlackApiError as exc:
                # ``exc.response`` is the ``SlackResponse`` proxy; cast
                # to ``Any`` so the documented ``.status_code`` /
                # ``.headers`` attribute access type-checks under
                # pyright strict mode.
                response_any = cast(Any, exc.response)
                status_code = getattr(response_any, "status_code", None)
                if status_code != 429:
                    # Non-429 errors are not retryable per
                    # phase-3-plan §4 Q3 (fail-fast posture). The
                    # caller maps these to ConnectorFailedError.
                    raise
                last_error = exc
                # Prefer Slack's own Retry-After hint; fall back to a
                # documented exponential schedule (1s / 2s / 4s).
                headers_obj = getattr(response_any, "headers", None)
                headers: dict[str, Any] = (
                    cast(dict[str, Any], headers_obj) if isinstance(headers_obj, dict) else {}
                )
                retry_after_raw = headers.get("Retry-After")
                try:
                    retry_after = int(retry_after_raw) if retry_after_raw else 2**attempt
                except (TypeError, ValueError):
                    # Slack's docs only ever return integer Retry-After
                    # values; this defensive arm covers buggy proxies
                    # that inject malformed headers.
                    retry_after = 2**attempt
                time.sleep(retry_after)
                continue
            return _as_response_dict(response)
        # Retry budget exhausted: re-raise the last 429 so the caller
        # maps it to ConnectorFailedError with the same code path as
        # non-rate-limit errors. ``last_error`` is non-None here
        # because the loop only ``continue``s after assigning it.
        assert last_error is not None
        raise last_error

    def _resolve_user_name(self, client: Any, user_id: str) -> str:
        """Look up a Slack user's display name, caching the result.

        Bot / system messages may omit ``user`` entirely; in that
        case we return :data:`_UNKNOWN_USER_DISPLAY` without an API
        call. For real users we prefer ``profile.display_name`` (the
        Slack-canonical handle) and fall back to ``profile.real_name``
        / ``name`` so renamed-but-not-display-set users still surface
        with something useful.

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
