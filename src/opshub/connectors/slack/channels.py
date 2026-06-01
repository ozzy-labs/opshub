"""Slack channel discovery helper (Phase 14.x, #341 PR1).

Iterates Slack's ``conversations.list`` API and yields
:class:`SlackChannel` rows so the CLI surface (PR2) can render a
human-friendly table / TOML snippet / JSON payload that operators
paste into ``opshub.toml``'s ``[connectors.slack] channels = [...]``
list.

The fetcher in :mod:`opshub.connectors.slack.fetcher` paginates
``conversations.history`` (per-channel message stream). This module
fills the inverse gap — *which channels exist at all under the
current token's principal* — without forcing the operator to round
trip via the Slack Web UI or hand-craft API calls. The helper is
read-only by design (`conversations.list` only requires
``channels:read``; `groups:read` is opt-in for private channels) so
it stays within the minimum scope set documented in ADR-0018.

Scope
-----

* Public channels (``"public_channel"``) are always requested.
* Private channels (``"private_channel"``) are added to the
  ``types`` parameter only when ``include_private=True``. Slack
  refuses the type entirely without ``groups:read`` so we surface
  ``missing_scope`` failures with the required scope name (mirroring
  the fetcher's diagnostics for ``conversations.history``).
* DM / MPIM (``"im"`` / ``"mpim"``) are deliberately excluded — the
  issue defers ``im:read`` / ``mpim:read`` scope adds until a real
  use case appears (#341 §Out of scope).
* Archived channels are filtered client-side. Slack's
  ``conversations.list`` does accept a server-side ``exclude_archived``
  query parameter, but we deliberately filter on the per-channel
  ``is_archived`` flag after fetch so the include / exclude decision
  lives in one place: the helper always asks Slack for the full set
  and a single ``--include-archived`` boolean flips the gate
  uniformly (no separate server / client filter paths to keep in
  lockstep, and tests can stub ``is_archived`` without monkey-patching
  the request URL).

Pagination
----------

Slack uses a cursor token (``response_metadata.next_cursor``) to walk
``conversations.list`` pages. We pass ``limit=200`` per page (the SDK
default and Slack's recommended sweet spot — higher values trigger
gateway timeouts on workspaces with thousands of channels). The
optional ``limit`` parameter on :func:`list_channels` caps the **total
yielded count** post-filter, not the per-page size, so the caller's
``--limit 10`` knob stops the API loop as soon as ten visible
channels have been seen.

Cold-start guard
----------------

``slack_sdk`` is imported lazily inside :func:`list_channels` so a
cold ``import opshub.connectors.slack.channels`` never pulls the SDK
onto the cold-start path. The Slack subpackage's ``__init__`` does
not re-export this helper (the CLI handler imports it directly at
call time per ADR-0001).

Token safety
------------

The resolved Slack OAuth token is never echoed in raised exceptions
— we surface the Slack API ``error`` code (a documented short string
such as ``invalid_auth`` / ``missing_scope``) and, for
``missing_scope``, the ``needed`` scope name. This mirrors the
contract pinned by :class:`opshub.connectors.slack.fetcher.SlackFetcher`
so the redaction stance (ADR-0027) is uniform across the connector.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opshub.core.errors import ConnectorFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opshub.connectors.slack.auth import SlackAuth


__all__ = ["SlackChannel", "list_channels"]


#: Per-page size for ``conversations.list``. 200 is the SDK default
#: and Slack's recommended ceiling for the method — higher values
#: trigger occasional gateway timeouts on large workspaces. Exposed
#: as a module constant so tests can pin the value without re-reading
#: the magic number out of :func:`list_channels`.
_PAGE_SIZE = 200

#: Retry budget for HTTP 429 responses, matching :data:`opshub.connectors
#: .slack.fetcher._MAX_RETRIES_ON_RATE_LIMIT`. ``conversations.list`` is
#: a Slack Tier-2 method (the fetcher's ``conversations.history`` is
#: Tier-3) so the same 1s / 2s / 4s backoff schedule is more than
#: generous — keeping the constant separate avoids accidental coupling
#: when one method's tier changes in a future Slack revision.
_MAX_RETRIES_ON_RATE_LIMIT = 3


@dataclass(frozen=True, slots=True)
class SlackChannel:
    """One Slack channel surfaced for the discovery CLI.

    Attributes
    ----------
    id:
        The Slack channel id (``"C..."`` for public, ``"G..."`` for
        legacy private). Pasted verbatim into ``opshub.toml``'s
        ``[connectors.slack] channels = [...]`` list.
    name:
        The channel name without the ``#`` prefix (Slack stores it
        this way). Used both for the human-readable column in the
        table output and as the substring matched by ``--filter``.
    is_private:
        ``True`` for private channels (returned only when the caller
        passes ``include_private=True`` and the token has the
        ``groups:read`` scope). The CLI renders this as ``yes`` /
        ``no`` so the operator can spot private channels at a glance.
    is_archived:
        ``True`` for archived channels (returned only when the caller
        passes ``include_archived=True``). Operators almost never
        want archived channels in the sync list — the default-excluded
        behaviour matches that intuition without forcing them to
        post-filter.
    purpose:
        Free-form purpose text (``channel.purpose.value`` from
        Slack's response). Empty string when the channel has no
        purpose set. Truncation / formatting happens in the CLI
        layer (PR2) so this dataclass stays a faithful row of the
        API response.
    """

    id: str
    name: str
    is_private: bool
    is_archived: bool
    purpose: str


def list_channels(
    auth: SlackAuth,
    *,
    include_private: bool = False,
    include_archived: bool = False,
    filter_substring: str | None = None,
    limit: int | None = None,
) -> Iterator[SlackChannel]:
    """Yield channels the configured token can see, one row at a time.

    Parameters
    ----------
    auth:
        Resolved :class:`SlackAuth`. The token is read at call time
        (no SDK instantiation happens before the function body so the
        cold-start guard holds even for the auth resolution path).
    include_private:
        When ``True``, ``"private_channel"`` is added to the
        ``conversations.list`` ``types`` parameter. Slack requires
        the ``groups:read`` scope for this — without it the call
        fails with ``missing_scope`` and we surface the scope name
        verbatim so the operator can extend their OAuth grant per
        ADR-0018.
    include_archived:
        When ``True``, archived channels are yielded alongside live
        ones. Filtering is applied client-side on the per-channel
        ``is_archived`` flag (rather than via Slack's
        ``exclude_archived`` query parameter) so the include / exclude
        decision lives in one place and ``--include-archived`` flips a
        single boolean across both code paths and tests.
    filter_substring:
        Case-insensitive substring match against ``channel.name``.
        ``None`` (or the empty string) disables filtering. Filtering
        is applied **after** the archived gate so ``--filter`` does
        not silently suppress archived matches when the operator
        forgot ``--include-archived``.
    limit:
        Maximum number of channels to yield post-filter. ``None``
        means "no cap" — the function walks every page Slack returns.
        The limit stops the **outer** loop (so the API call count is
        bounded too); we never over-fetch a page just to drop its
        tail.

    Yields
    ------
    SlackChannel
        One row per channel that survives the archived / private /
        filter gates, in the order Slack returns them (Slack
        documents this as "alphabetical by name", though we do not
        rely on that — the CLI sorts deterministically at the
        formatter level).

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
    # Lazy-imported inside the function so importing this module
    # never pulls slack_sdk onto the cold-start path. Mirrors the
    # fetcher's import strategy; the in-package static guard for
    # the auth module covers the package-level invariant. The
    # ``SlackApiError`` translation happens inside :func:`_call_list`
    # so the caller's exception surface is uniformly
    # :class:`ConnectorFailedError`.
    from slack_sdk import WebClient

    client = WebClient(token=auth.token)

    # Slack's ``types`` parameter is a comma-separated list — we
    # build it programmatically so the request shape is observable in
    # tests (which assert against ``call_args.kwargs["types"]``).
    types_parts = ["public_channel"]
    if include_private:
        types_parts.append("private_channel")
    types = ",".join(types_parts)

    # Normalise the filter once so the hot loop only does the
    # case-insensitive containment check. ``None`` and the empty
    # string both disable filtering — the operator typing
    # ``--filter ""`` should not silently match every channel.
    needle = (filter_substring or "").lower() or None

    yielded = 0
    page_cursor: str | None = None
    while True:
        response = _call_list(
            client=client,
            types=types,
            cursor=page_cursor,
        )

        channels_obj = response.get("channels")
        channels: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], channels_obj) if isinstance(channels_obj, list) else []
        )

        for raw in channels:
            channel = _row_from_dict(raw)
            if channel is None:
                # Malformed row (no id / no name): skip rather than
                # crash so a single bad payload does not poison the
                # discovery output.
                continue
            if channel.is_archived and not include_archived:
                continue
            if needle is not None and needle not in channel.name.lower():
                continue
            yield channel
            yielded += 1
            if limit is not None and yielded >= limit:
                return

        # Walk to the next page only if Slack signals more data.
        # ``response_metadata.next_cursor`` is the documented
        # pagination token; an empty string means "no more pages".
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


def _row_from_dict(raw: dict[str, Any]) -> SlackChannel | None:
    """Build a :class:`SlackChannel` from a ``conversations.list`` row.

    Returns ``None`` when the row is malformed (missing ``id`` /
    ``name``). Defensive against partially-populated payloads — Slack
    documents both fields as always present, but proxies in the wild
    occasionally strip optional sub-objects (``purpose`` / ``topic``).
    """
    channel_id = str(raw.get("id") or "")
    name = str(raw.get("name") or "")
    if not channel_id or not name:
        return None
    # ``is_private`` / ``is_archived`` are documented as booleans;
    # ``bool(...)`` normalises any thin-proxy quirk (e.g. ``"true"``
    # strings) while keeping the happy path identity-equal.
    is_private = bool(raw.get("is_private"))
    is_archived = bool(raw.get("is_archived"))
    # ``purpose`` is a sub-object ``{"value": "...", "creator": "...",
    # "last_set": <ts>}`` — we only render ``value``. Missing or
    # non-dict ``purpose`` falls through to an empty string so the
    # dataclass field type stays ``str`` (no ``str | None`` noise in
    # the CLI formatter).
    purpose_obj = raw.get("purpose")
    if isinstance(purpose_obj, dict):
        purpose_dict = cast(dict[str, Any], purpose_obj)
        purpose = str(purpose_dict.get("value") or "")
    else:
        purpose = ""
    return SlackChannel(
        id=channel_id,
        name=name,
        is_private=is_private,
        is_archived=is_archived,
        purpose=purpose,
    )


def _call_list(
    *,
    client: Any,
    types: str,
    cursor: str | None,
) -> dict[str, Any]:
    """Call ``conversations.list`` with 429 backoff matching the fetcher.

    Retry budget: three attempts, sleeping ``Retry-After`` seconds
    between them (falling back to 1s / 2s / 4s if Slack omits the
    header). On exhaustion we map the final ``SlackApiError`` to
    :class:`ConnectorFailedError` so the caller does not need to know
    SDK-specific exception types. Non-429 errors short-circuit to
    :class:`ConnectorFailedError` immediately — same fail-fast posture
    as :meth:`SlackFetcher._call_history` (phase-3-plan §4 Q3).

    ``missing_scope`` gets a dedicated arm so the operator sees the
    ``needed`` scope name + the ADR-0018 / scope-catalogue links
    inline (mirrors the fetcher's diagnostic surface).
    """
    # Lazy-import the exception class to avoid module-level
    # ``slack_sdk`` import (cold-start guard).
    from slack_sdk.errors import SlackApiError

    kwargs: dict[str, Any] = {
        "limit": _PAGE_SIZE,
        "types": types,
    }
    if cursor is not None:
        kwargs["cursor"] = cursor

    last_error: SlackApiError | None = None
    for attempt in range(_MAX_RETRIES_ON_RATE_LIMIT):
        try:
            response: Any = client.conversations_list(**kwargs)
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
            # Non-429: map to ConnectorFailedError now so callers do
            # not have to import SlackApiError.
            raise _to_connector_failed(exc) from exc
        return _as_response_dict(response)

    # Retry budget exhausted: surface the last 429 via the uniform
    # error type. ``last_error`` is non-None because the loop only
    # ``continue``s after assigning it.
    assert last_error is not None
    raise _to_connector_failed(last_error) from last_error


def _to_connector_failed(exc: Any) -> ConnectorFailedError:
    """Map a :class:`SlackApiError` (or sub) to :class:`ConnectorFailedError`.

    Centralises the ``missing_scope`` diagnostic so the caller stays
    declarative. The token never appears in the message — we only
    surface Slack's documented ``error`` short string and (for
    ``missing_scope``) the ``needed`` scope name, both of which are
    safe to echo per Slack's API contract.
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
            f"Slack conversations.list failed: missing_scope "
            f"(needed: {needed!r}). See ADR-0018 §Decision (7) or "
            f"https://api.slack.com/scopes for the scope catalogue."
        )
    return ConnectorFailedError(f"Slack conversations.list failed: {error_code}")


def _as_response_dict(response: Any) -> dict[str, Any]:
    """Normalise a ``slack_sdk`` ``SlackResponse`` (or dict) into a dict.

    Mirrors :func:`opshub.connectors.slack.fetcher._as_response_dict`
    — kept as a private duplicate (rather than imported) so this
    module stays self-contained and the fetcher's private helper does
    not become an implicit public surface.
    """
    if isinstance(response, dict):
        return cast(dict[str, Any], response)
    data_obj = getattr(response, "data", None)
    if isinstance(data_obj, dict):
        return cast(dict[str, Any], data_obj)
    return cast(dict[str, Any], dict(response))
