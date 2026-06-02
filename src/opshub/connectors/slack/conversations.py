"""Slack conversation discovery helper (#366; replaces ``channels`` from #341).

Iterates Slack's ``users.conversations`` API (default) — or
``conversations.list`` when the caller asks for the workspace-wide view via
``all=True`` — and yields :class:`SlackConversation` rows so the CLI surface
can render a human-friendly table / TOML snippet / JSON payload.

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

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from opshub.core.errors import ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.cli._progress import ProgressReporter
    from opshub.connectors.slack.auth import SlackAuth


__all__ = [
    "CONVERSATION_TYPES",
    "ConversationType",
    "SlackConversation",
    "list_conversations",
]


#: Per-page size for both ``users.conversations`` and ``conversations.list``.
#: 200 is the SDK default and Slack's recommended ceiling — higher values
#: trigger occasional gateway timeouts on large workspaces. Exposed as a
#: module constant so tests can pin the value without re-reading the magic
#: number out of :func:`list_conversations`.
_PAGE_SIZE = 200

#: Retry budget for HTTP 429 responses, matching
#: :data:`opshub.connectors.slack.fetcher._MAX_RETRIES_ON_RATE_LIMIT`.
_MAX_RETRIES_ON_RATE_LIMIT = 3


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
        ``opshub.toml``'s ``[connectors.slack] channels = [...]`` list.
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
    """

    id: str
    type: ConversationType
    name: str | None
    display_name: str
    is_private: bool
    is_archived: bool
    purpose: str
    participants: tuple[str, ...] = field(default_factory=tuple)


def list_conversations(
    auth: SlackAuth,
    *,
    types: tuple[ConversationType, ...] = CONVERSATION_TYPES,
    include_archived: bool = False,
    filter_substring: str | None = None,
    limit: int | None = None,
    all: bool = False,
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
            yield conversation
            yielded += 1
            if limit is not None and yielded >= limit:
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
            members.extend(str(uid) for uid in cast(list[Any], page_obj) if uid)
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

    Both endpoints share request shape and pagination semantics, so the
    retry / error-translation path is identical — the only difference is
    which client method we dispatch on. Retry budget mirrors the
    legacy ``_call_list`` helper from ``channels.py``: three 429
    attempts with ``Retry-After``-honoured (or 1/2/4 exponential)
    backoff, then escalation to :class:`ConnectorFailedError`.
    """
    from slack_sdk.errors import SlackApiError

    kwargs: dict[str, Any] = {
        "limit": _PAGE_SIZE,
        "types": api_types,
    }
    if cursor is not None:
        kwargs["cursor"] = cursor

    last_error: SlackApiError | None = None
    for attempt in range(_MAX_RETRIES_ON_RATE_LIMIT):
        try:
            if all:
                response: Any = client.conversations_list(**kwargs)
            else:
                response = client.users_conversations(**kwargs)
        except SlackApiError as exc:
            response_any = cast(Any, exc.response)
            status_code = getattr(response_any, "status_code", None)
            if status_code == 429:
                last_error = exc
                headers_obj = getattr(response_any, "headers", None)
                headers: dict[str, Any] = (
                    cast(dict[str, Any], headers_obj) if isinstance(headers_obj, dict) else {}
                )
                retry_after_raw = headers.get("Retry-After")
                try:
                    retry_after = int(retry_after_raw) if retry_after_raw else 2**attempt
                except (TypeError, ValueError):
                    retry_after = 2**attempt
                time.sleep(retry_after)
                continue
            raise _to_connector_failed(exc, all=all) from exc
        return _as_response_dict(response)

    assert last_error is not None
    raise _to_connector_failed(last_error, all=all) from last_error


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
