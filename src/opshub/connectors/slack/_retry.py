"""Slack 429 retry helper shared by the per-conversation hot paths (#377).

The Slack Web API rate-limits ``conversations.history`` (sync hot path
in :meth:`opshub.connectors.slack.fetcher.SlackFetcher._call_history`)
and the per-conversation activity probe in
:func:`opshub.connectors.slack.conversations._call_history_oldest` with
the same documented budget (3 attempts, ``Retry-After``-honoured, fall
back to 1s / 2s / 4s exponential backoff). Before #377 both call sites
carried near-identical copies of the retry loop; a third
``conversations.history``-class endpoint would have made it a
triple-copy where a policy change had to land in three places at once.

This module centralises the budget + backoff so each call site only
spells out (a) the Slack SDK method to invoke, and (b) its own
non-429 :class:`SlackApiError` translation (``missing_scope`` →
sentinel, otherwise endpoint-named :class:`ConnectorFailedError`).
Listing retry (``users.conversations`` / ``conversations.list``) keeps
its existing loop for now — the helper is shaped so that path can opt
in later without changing the surface (#377 §Out of scope).

Cold-start guard
----------------

``slack_sdk`` is imported lazily inside :func:`retry_on_rate_limit` so
``import opshub.connectors.slack._retry`` never pulls the SDK onto the
cold-start path (ADR-0001 §Implications, Slack module guard mirrored).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = [
    "MAX_RETRIES_ON_RATE_LIMIT",
    "retry_on_rate_limit",
]


#: Default 429 retry budget. Three attempts with ``Retry-After``-honoured
#: (falling back to 1s / 2s / 4s) backoff matches the policy
#: documented at phase-7-plan §1 #8 — the budget the per-conversation
#: hot paths have shipped with since #232 (sync) and #366 (discovery).
#: Exposed as a module constant so tests can pin the value without
#: re-reading it out of every call site.
MAX_RETRIES_ON_RATE_LIMIT = 3


def retry_on_rate_limit[T](
    call: Callable[[], T],
    *,
    max_retries: int = MAX_RETRIES_ON_RATE_LIMIT,
) -> T:
    """Run ``call()`` with Slack 429 + ``Retry-After``-honoured backoff.

    Parameters
    ----------
    call:
        Zero-arg callable that performs one Slack API request and
        returns whatever the call site needs (typically a response
        dict, but :data:`T` is generic so the helper does not
        constrain the return shape). Non-429
        :class:`slack_sdk.errors.SlackApiError` exceptions raised by
        ``call()`` re-raise immediately so the call site can map them
        to the endpoint-specific :class:`ConnectorFailedError`
        flavour (``conversations.history failed: missing_scope`` vs
        ``users.conversations failed: invalid_auth``).
    max_retries:
        Maximum number of 429 attempts before giving up. The default
        :data:`MAX_RETRIES_ON_RATE_LIMIT` matches the documented
        policy; call sites override only for tests.

    Returns
    -------
    T
        Whatever ``call()`` returned on the first non-429 success.

    Raises
    ------
    slack_sdk.errors.SlackApiError
        Non-429 errors re-raise immediately. After ``max_retries``
        consecutive 429s, the last 429 :class:`SlackApiError` is
        re-raised so the same call-site error mapper handles both
        the immediate-failure and exhausted-budget cases through one
        ``except SlackApiError`` arm.

    Notes
    -----
    ``Retry-After`` headers are read as a stringified integer; a
    missing / malformed header falls back to the documented
    1s / 2s / 4s exponential schedule (``2**attempt``). The fallback
    arm matches the prior copy-pasted implementations bit-for-bit so
    extracting the helper is a refactor, not a policy change.
    """
    # Lazy-import keeps ``slack_sdk`` off the cold-start path
    # (ADR-0001). Importing inside the body adds a few microseconds
    # to the first 429 path which is operationally invisible.
    from slack_sdk.errors import SlackApiError

    last_error: SlackApiError | None = None
    for attempt in range(max_retries):
        try:
            return call()
        except SlackApiError as exc:
            response_any = cast(Any, exc.response)
            status_code = getattr(response_any, "status_code", None)
            if status_code != 429:
                # Non-429 errors are not retryable; re-raise so the
                # call site's ``except SlackApiError`` arm can
                # translate to its endpoint-specific
                # :class:`ConnectorFailedError` flavour.
                raise
            last_error = exc
            headers_obj = getattr(response_any, "headers", None)
            headers: dict[str, Any] = (
                cast(dict[str, Any], headers_obj) if isinstance(headers_obj, dict) else {}
            )
            retry_after_raw = headers.get("Retry-After")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw else 2**attempt
            except (TypeError, ValueError):
                # Slack's documented surface only ever emits integer
                # ``Retry-After`` values; this arm defends against
                # buggy proxies that inject malformed strings.
                retry_after = 2**attempt
            time.sleep(retry_after)
    # Budget exhausted. ``last_error`` is non-None here because the
    # loop only ``continue``s after assigning it; assertion gives
    # mypy strict the narrowing it needs without runtime cost.
    assert last_error is not None
    raise last_error
