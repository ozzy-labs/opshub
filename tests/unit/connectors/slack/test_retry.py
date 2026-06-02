"""Tests for ``opshub.connectors.slack._retry`` (#377).

The helper centralises the 429 + ``Retry-After`` backoff that
:meth:`SlackFetcher._call_history` (sync hot path) and
:func:`opshub.connectors.slack.conversations._call_history_oldest`
(discovery activity probe) previously implemented as near-identical
copies. The tests pin the behaviour the call sites depend on:

1. ``Retry-After`` header is honoured on 429, then the call succeeds.
2. Missing / malformed ``Retry-After`` falls back to 1s / 2s / 4s.
3. Non-429 :class:`SlackApiError` re-raises immediately (no retry).
4. After ``max_retries`` consecutive 429s, the last 429 re-raises so
   the call-site error mapper handles immediate-failure and
   exhausted-budget through one ``except SlackApiError`` arm.
5. ``max_retries`` is callable-tunable so call sites can pin shorter
   budgets in tests without monkey-patching the constant.
6. Successful calls return the call's value verbatim (helper does
   not unwrap / transform the response).

The :mod:`slack_sdk` extras (``[connectors-slack]``) may not be
installed in every environment, so the file-level
``pytest.importorskip`` gates the whole module.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack retry helper tests require the 'connectors-slack' extras",
)

from opshub.connectors.slack._retry import (  # pyright: ignore[reportPrivateUsage]
    MAX_RETRIES_ON_RATE_LIMIT,
    retry_on_rate_limit,
)


def _rate_limited_error(*, retry_after: str | None = None) -> Any:
    """Build a :class:`SlackApiError` carrying a 429 response shape."""
    from slack_sdk.errors import SlackApiError

    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    resp.get.return_value = "rate_limited"
    return SlackApiError(  # type: ignore[no-untyped-call]
        message="ratelimited", response=resp
    )


def _non_rate_limit_error(error_code: str) -> Any:
    """Build a :class:`SlackApiError` for a non-429 failure (e.g. ``missing_scope``)."""
    from slack_sdk.errors import SlackApiError

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.get.return_value = error_code
    return SlackApiError(  # type: ignore[no-untyped-call]
        message=error_code, response=resp
    )


def test_constant_pinned_at_three() -> None:
    """``MAX_RETRIES_ON_RATE_LIMIT`` matches phase-7-plan §1 #8 (3 attempts)."""
    assert MAX_RETRIES_ON_RATE_LIMIT == 3


def test_returns_value_when_call_succeeds_first_time() -> None:
    """No 429 → ``call`` runs once and its return value is returned verbatim."""
    sentinel = {"ok": True, "messages": [{"ts": "1.0"}]}
    call = MagicMock(return_value=sentinel)

    result = retry_on_rate_limit(call)

    assert result is sentinel
    assert call.call_count == 1


def test_honours_retry_after_header_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with ``Retry-After: 2`` → ``time.sleep(2)``, then the call succeeds."""
    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    success = {"ok": True}
    call = MagicMock(side_effect=[_rate_limited_error(retry_after="2"), success])

    result = retry_on_rate_limit(call)

    assert result is success
    assert call.call_count == 2
    sleep_mock.assert_called_once_with(2)


def test_falls_back_to_exponential_when_retry_after_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``Retry-After`` header → 1s / 2s / 4s sleep schedule (2**attempt)."""
    import time as _stdlib_time

    from slack_sdk.errors import SlackApiError

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    call = MagicMock(
        side_effect=[
            _rate_limited_error(),
            _rate_limited_error(),
            _rate_limited_error(),
        ]
    )

    with pytest.raises(SlackApiError):
        retry_on_rate_limit(call)

    assert [c.args for c in sleep_mock.call_args_list] == [(1,), (2,), (4,)]


def test_falls_back_to_exponential_when_retry_after_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Retry-After: "abc"`` → defensive fallback to ``2**attempt``."""
    import time as _stdlib_time

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    success = {"ok": True}
    call = MagicMock(side_effect=[_rate_limited_error(retry_after="abc"), success])

    retry_on_rate_limit(call)

    # First attempt uses ``2**0 = 1`` (Retry-After unparseable).
    sleep_mock.assert_called_once_with(1)


def test_non_429_error_is_reraised_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-429 :class:`SlackApiError` re-raises on the first attempt — no retry, no sleep.

    Call sites depend on this to translate ``missing_scope`` into
    their own sentinel (e.g. ``_MissingHistoryScopeError`` in the
    discovery module) and other non-429 errors into endpoint-named
    :class:`ConnectorFailedError` flavours.
    """
    import time as _stdlib_time

    from slack_sdk.errors import SlackApiError

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    error = _non_rate_limit_error("missing_scope")
    call = MagicMock(side_effect=error)

    with pytest.raises(SlackApiError) as excinfo:
        retry_on_rate_limit(call)

    assert excinfo.value is error
    assert call.call_count == 1
    assert sleep_mock.call_count == 0


def test_budget_exhaustion_reraises_last_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three consecutive 429s → the last 429 :class:`SlackApiError` is re-raised.

    Same vocabulary the call site uses for non-429 errors, so one
    ``except SlackApiError`` arm catches both "immediate failure" and
    "exhausted budget" via the same code path.
    """
    import time as _stdlib_time

    from slack_sdk.errors import SlackApiError

    monkeypatch.setattr(_stdlib_time, "sleep", MagicMock())

    final = _rate_limited_error(retry_after="1")
    call = MagicMock(
        side_effect=[
            _rate_limited_error(retry_after="1"),
            _rate_limited_error(retry_after="1"),
            final,
        ]
    )

    with pytest.raises(SlackApiError) as excinfo:
        retry_on_rate_limit(call)

    assert excinfo.value is final
    assert call.call_count == MAX_RETRIES_ON_RATE_LIMIT


def test_max_retries_override_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``max_retries=1`` → single attempt, no sleep, error re-raises immediately on 429.

    Used by tests that want to keep call counts tight without monkey-
    patching the module-level constant.
    """
    import time as _stdlib_time

    from slack_sdk.errors import SlackApiError

    sleep_mock = MagicMock()
    monkeypatch.setattr(_stdlib_time, "sleep", sleep_mock)

    error = _rate_limited_error(retry_after="1")
    call = MagicMock(side_effect=error)

    with pytest.raises(SlackApiError):
        retry_on_rate_limit(call, max_retries=1)

    assert call.call_count == 1
    # ``max_retries=1`` still runs the attempt-0 sleep before exiting
    # the loop — the helper sleeps unconditionally on 429 before the
    # loop boundary recheck. Pin the count so a future refactor of
    # the loop shape stays observable.
    assert sleep_mock.call_count == 1


def test_module_does_not_import_slack_sdk_eagerly() -> None:
    """``opshub.connectors.slack._retry`` must not import the SDK at module level (ADR-0001)."""
    import ast
    import sys
    from pathlib import Path

    module_path = Path(sys.modules["opshub.connectors.slack._retry"].__file__ or "")
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] == "slack_sdk":
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module == "slack_sdk":
                offenders.append(f"line {node.lineno}: from {node.module} import ...")

    assert not offenders, (
        "opshub.connectors.slack._retry imports slack_sdk at module level "
        "(must be lazy-loaded):\n  - " + "\n  - ".join(offenders)
    )
