"""Tests for :class:`opshub.connectors.slack.connector.SlackConnector`.

These tests pin the connector-level contract independently of the
end-to-end CLI lifecycle (covered by the integration suite):

1. ``name`` matches the registry key the CLI dispatches on.
2. Cursor round-trip via JSON is symmetric — what the connector
   writes back on one sync, the next sync parses without loss.
3. Empty cursor / first-sync path treats ``context.cursor_value =
   None`` as "no resume" and yields the fetcher an empty dict.
4. Empty channel list is a structured warning + no-op (matches the
   GitHub "empty sync preserves prior cursor" contract).
5. Mapper output is forwarded to :meth:`SourceService.observe`
   verbatim — no field is dropped or rewritten by the connector.

The :mod:`slack_sdk` extras are gated with ``pytest.importorskip``
because :class:`RawSlackMessage` lives in the same package as the SDK
import path (even though the connector itself only uses the SDK
indirectly via the fetcher).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "slack_sdk",
    reason="Slack connector tests require the 'connectors-slack' extras",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.slack.connector import (
    SlackConnector,
    _dump_cursors,  # pyright: ignore[reportPrivateUsage]
    _load_cursors,  # pyright: ignore[reportPrivateUsage]
)
from opshub.connectors.slack.fetcher import RawSlackMessage
from opshub.core.errors import ConfigError

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double for :class:`SourceService` that records ``observe`` calls.

    Mirrors the keyword-only signature used by the real service so a
    drift on argument names trips a TypeError immediately.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
        body: str | None = None,
        provenance_origin: str | None = None,
        provenance_trust: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "connector_name": connector_name,
                "external_id": external_id,
                "source_type": source_type,
                "title": title,
                "url": url,
                "summary": summary,
                "body": body,
                "provenance_origin": provenance_origin,
                "provenance_trust": provenance_trust,
            }
        )


def _raw_message(
    *,
    channel_id: str = "C1",
    channel_name: str = "general",
    ts: str = "1700000001.000100",
    text: str = "hello",
    user_id: str = "U1",
    user_display_name: str = "alice",
    permalink: str = "https://acme.slack.com/archives/C1/p1700000001000100",
) -> RawSlackMessage:
    return RawSlackMessage(
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        text=text,
        user_id=user_id,
        user_display_name=user_display_name,
        permalink=permalink,
        raw={},
    )


def _context(
    service: _RecordingSourceService, *, cursor_value: str | None = None
) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=cursor_value,
        secrets=None,
        logger=MagicMock(),  # warning() needs to be callable on the empty-channel path
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    channels: list[str],
) -> None:
    """Patch :class:`OpsHubSettings` so ``_resolve_channels`` returns ``channels``.

    The connector lazy-imports ``OpsHubSettings`` from
    ``opshub.core.config`` *inside* :meth:`_resolve_channels`; patching
    a name on the connector module would silently fail (the import
    binds the name fresh inside the method). Patching the class on
    its defining module catches the lazy import via the usual
    monkeypatch lookup.
    """
    fake_settings = MagicMock()
    fake_settings.connectors.slack.channels = channels
    monkeypatch.setattr(
        "opshub.core.config.OpsHubSettings",
        lambda: fake_settings,
    )


def _patch_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :class:`SlackAuth` so construction never reads the keyring.

    The connector instantiates :class:`SlackAuth` inside :meth:`sync`;
    we replace it with a no-arg double that returns a token-bearing
    fake. The fetcher (also patched) doesn't actually use the token
    — it's just plumbing.
    """
    fake_auth_cls = MagicMock()
    fake_auth_cls.return_value.token = "xoxb-fake"
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackAuth",
        fake_auth_cls,
    )


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yields: list[tuple[str, RawSlackMessage, str | None]],
) -> tuple[MagicMock, dict[str, dict[str, str | None]]]:
    """Patch :class:`SlackFetcher` so :meth:`fetch_messages` yields ``yields``.

    Returns ``(fetcher_cls_mock, captured_kwargs_dict)``. The captured
    dict has a single ``"cursor_per_channel"`` key whose value is a
    **snapshot** of the dict passed to :meth:`fetch_messages` at call
    time. The connector advances the cursor dict in-place as it
    yields messages, so :class:`MagicMock` ``call_args`` would
    otherwise reflect the post-iteration state — losing the
    information the test wants to assert (the *initial* resume
    state). A snapshot taken inside the side_effect preserves it.
    """
    fake_fetcher_cls = MagicMock()
    captured: dict[str, dict[str, str | None]] = {}

    def _fetch_messages(
        *,
        cursor_per_channel: dict[str, str | None],
        max_per_channel: int = 100,
    ) -> Iterator[tuple[str, RawSlackMessage, str | None]]:
        del max_per_channel
        captured["cursor_per_channel"] = dict(cursor_per_channel)
        return iter(yields)

    fake_fetcher_cls.return_value.fetch_messages.side_effect = _fetch_messages
    monkeypatch.setattr(
        "opshub.connectors.slack.connector.SlackFetcher",
        fake_fetcher_cls,
    )
    return fake_fetcher_cls, captured


# ---------------------------------------------------------------------- name


def test_connector_name_is_slack() -> None:
    """The registry / CLI dispatch key must be exactly ``"slack"``."""
    assert SlackConnector.name == "slack"
    assert SlackConnector().name == "slack"


# ---------------------------------------------------------------------- cursor helpers


def test_load_cursors_none_returns_empty_dict() -> None:
    """First-sync (``cursor_value=None``) → empty resume dict.

    Empty dict tells the fetcher "no per-channel resume" and it pulls
    the most recent N messages per channel.
    """
    assert _load_cursors(None) == {}


def test_load_cursors_round_trips_dict() -> None:
    """``_dump → _load`` preserves the cursor dict exactly.

    The serialised form is the only thing the projection persists, so
    losing data on the round-trip would silently re-fetch history on
    every resume. Pinning the symmetry locks the format.
    """
    original: dict[str, str | None] = {
        "C1": "1700000001.000100",
        "C2": "1700000002.000200",
    }
    serialised = _dump_cursors(original)
    assert _load_cursors(serialised) == original


def test_load_cursors_accepts_null_values() -> None:
    """A channel with a ``null`` value (never observed) round-trips.

    A first-sync that found no messages in channel ``C-empty`` leaves
    its entry as ``None`` so the next sync still passes the key to
    the fetcher (helps a future operator-facing diff between
    "channel missing from cursor" vs. "channel observed nothing").
    """
    original: dict[str, str | None] = {"C1": "1700000001.000100", "C-empty": None}
    assert _load_cursors(_dump_cursors(original)) == original


def test_load_cursors_rejects_malformed_json() -> None:
    """Hand-edited / corrupt cursor → :class:`ConfigError`, not silent re-fetch.

    A silently-truncated history (the only alternative) would loudly
    mislead the operator. A hard error tells them to rebuild the
    projection.
    """
    with pytest.raises(ConfigError, match="not valid JSON"):
        _load_cursors("not-json")


def test_load_cursors_rejects_non_dict_root() -> None:
    """Cursor must be a JSON **object** — list / string root rejected."""
    with pytest.raises(ConfigError, match="must be a JSON object"):
        _load_cursors('["C1"]')


def test_load_cursors_rejects_non_string_values() -> None:
    """Values must be ``str | None``; int / bool reject as hand-edit accident."""
    with pytest.raises(ConfigError, match="must be strings or null"):
        _load_cursors('{"C1": 42}')


def test_dump_cursors_is_deterministic() -> None:
    """``sort_keys=True`` → identical input dicts produce identical output strings.

    Determinism matters because the projection row's ``cursor_value``
    becomes a meaningful diff in operator dashboards — a non-stable
    ordering would mark every sync run as "changed" even when no new
    messages arrived.
    """
    a: dict[str, str | None] = {"C2": "ts-2", "C1": "ts-1"}
    b: dict[str, str | None] = {"C1": "ts-1", "C2": "ts-2"}
    assert _dump_cursors(a) == _dump_cursors(b)


# ---------------------------------------------------------------------- sync: happy path


def test_sync_observes_each_yielded_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: fetcher yields → mapper → observe → cursor advances per yield.

    Pins the three-step pipeline so a regression that drops the
    ``observe(**kwargs)`` splat or forgets to advance the cursor
    surfaces immediately.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    msg_a = _raw_message(ts="1700000001.000100", text="first")
    msg_b = _raw_message(ts="1700000002.000200", text="second")
    fetcher_cls, captured = _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", msg_a, "1700000001.000100"),
            ("C1", msg_b, "1700000002.000200"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    # The fetcher was built with the configured channel list and
    # called with an empty resume dict (first-sync semantics).
    fetcher_cls.assert_called_once()
    assert fetcher_cls.call_args.kwargs == {"channels": ["C1"]}
    # ``captured`` holds the cursor dict snapshot taken at fetch_messages
    # entry, before the connector mutates it in-place per yield.
    assert captured["cursor_per_channel"] == {}

    # Both messages reached the service; mapped fields match the
    # mapper contract.
    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == [
        "C1:1700000001.000100",
        "C1:1700000002.000200",
    ]
    assert [c["summary"] for c in service.calls] == ["first", "second"]
    assert all(c["connector_name"] == "slack" for c in service.calls)
    assert all(c["source_type"] == "slack_message" for c in service.calls)

    # New cursor encodes the latest ts per channel.
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "1700000002.000200"}


def test_sync_resumes_from_persisted_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted JSON cursor is parsed and handed to the fetcher.

    Without this, every sync would re-fetch the channel's full
    history. Pinning the kwargs argument shape prevents a regression
    that loses the resume value.
    """
    _patch_settings(monkeypatch, channels=["C1", "C2"])
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    prior_cursor = _dump_cursors({"C1": "1700000001.000100", "C2": None})
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    assert captured["cursor_per_channel"] == {
        "C1": "1700000001.000100",
        "C2": None,
    }
    # No yields → observed_count=0; cursor stays at the prior value.
    assert result.observed_count == 0
    assert result.new_cursor == prior_cursor


def test_sync_advances_cursor_per_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two channels with new messages → both cursors advance independently."""
    _patch_settings(monkeypatch, channels=["C1", "C2"])
    _patch_auth(monkeypatch)
    _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", _raw_message(channel_id="C1", ts="ts-c1-new"), "ts-c1-new"),
            ("C2", _raw_message(channel_id="C2", ts="ts-c2-new"), "ts-c2-new"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    assert result.observed_count == 2
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {
        "C1": "ts-c1-new",
        "C2": "ts-c2-new",
    }


# ---------------------------------------------------------------------- sync: empty channels


def test_sync_with_empty_channels_warns_and_returns_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty channel config → structured warning + cursor preserved.

    The connector must not crash here — an operator who hasn't
    populated ``channels`` yet should see a CLI-level warning, not a
    stack trace. The prior cursor is preserved so flipping
    ``channels = ["C1"]`` and re-running picks up exactly where the
    last sync left off.
    """
    _patch_settings(monkeypatch, channels=[])
    # No fetcher patch — the connector must short-circuit before
    # touching it. A pollution check confirms the short-circuit:
    fetcher_cls = MagicMock(side_effect=AssertionError("fetcher must not be constructed"))
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackFetcher", fetcher_cls)
    auth_cls = MagicMock(side_effect=AssertionError("auth must not be constructed"))
    monkeypatch.setattr("opshub.connectors.slack.connector.SlackAuth", auth_cls)

    service = _RecordingSourceService()
    ctx = _context(service, cursor_value="{}")
    result = SlackConnector().sync(ctx)

    assert result.observed_count == 0
    assert result.new_cursor == "{}"
    assert service.calls == []
    # Warning was emitted via the structured logger.
    ctx.logger.warning.assert_called_once()


# ---------------------------------------------------------------------- sync: no yields


def _patch_excludes_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str) -> None:
    """Write an ``excludes.yaml`` and point ``default_config_dir`` at it.

    ``load_excludes()`` resolves the file through
    :func:`opshub.core.config.default_config_dir` (imported into
    ``opshub.core.excludes`` at module top); monkeypatching that name
    on the excludes module is the documented way to redirect resolution
    for tests (see also :func:`tests.unit.core.test_excludes` for the
    ``config_dir=`` kwarg pattern; here we exercise the no-arg call
    site the Phase 10 audit Cluster 3 mandates).
    """
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


def test_sync_skips_excluded_channel_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): a message in an excluded channel is never observed.

    The cursor still advances past the message so the connector does
    not re-fetch it forever. Pins the "skip but advance" semantics
    explicitly — a regression that early-``continue``\\s without
    updating the cursor dict would re-fetch the excluded channel on
    every sync.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="channels:\n  - C-secret\n")
    _patch_settings(monkeypatch, channels=["C1", "C-secret"])
    _patch_auth(monkeypatch)
    public_msg = _raw_message(channel_id="C1", ts="ts-1", text="public")
    secret_msg = _raw_message(channel_id="C-secret", ts="ts-2", text="leaked")
    _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", public_msg, "ts-1"),
            ("C-secret", secret_msg, "ts-2"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    # Only the public channel's message reaches the source service.
    assert result.observed_count == 1
    assert len(service.calls) == 1
    assert service.calls[0]["external_id"] == "C1:ts-1"
    # Both channels' cursors advance (skip-but-advance contract).
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "ts-1", "C-secret": "ts-2"}


def test_sync_skips_excluded_sender_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): a message from an excluded sender is never observed.

    Same "skip but advance" semantics as the channel-exclude path.
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="senders:\n  - U-bot\n")
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    bot_msg = _raw_message(channel_id="C1", ts="ts-bot", user_id="U-bot", text="noise")
    human_msg = _raw_message(channel_id="C1", ts="ts-human", user_id="U1", text="real")
    _patch_fetcher(
        monkeypatch,
        yields=[
            ("C1", bot_msg, "ts-bot"),
            ("C1", human_msg, "ts-human"),
        ],
    )

    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=None))

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "C1:ts-human"
    assert result.new_cursor is not None
    assert _load_cursors(result.new_cursor) == {"C1": "ts-human"}


def test_sync_with_no_yields_preserves_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured channels but zero new messages → cursor unchanged.

    Mirrors :func:`test_empty_sync_preserves_cursor` in the GitHub
    connector test suite — "no progress, no movement" is the
    documented contract.
    """
    _patch_settings(monkeypatch, channels=["C1"])
    _patch_auth(monkeypatch)
    _patch_fetcher(monkeypatch, yields=[])

    prior_cursor = _dump_cursors({"C1": "ts-old"})
    service = _RecordingSourceService()
    result = SlackConnector().sync(_context(service, cursor_value=prior_cursor))

    assert result.observed_count == 0
    assert result.new_cursor == prior_cursor
    assert service.calls == []


# ---------------------------------------------------------------------- registry


def test_slack_subpackage_registers_connector() -> None:
    """Importing :mod:`opshub.connectors.slack` registers the connector.

    Mirrors the Phase 3 GitHub precedent — the CLI driver discovers
    connectors purely through the registry, so the import side
    effect is the contract that makes ``opshub connector sync slack``
    resolve.

    We :func:`importlib.reload` the package after a registry reset so
    the test is robust against earlier tests that may have called
    :func:`unregister_all` (see ``tests/unit/cli/test_connector.py``
    autouse fixture). Reload re-runs the module body and re-fires
    the ``register_connector(SlackConnector())`` side effect at the
    bottom of the module — that's the contract under test.
    """
    import importlib

    import opshub.connectors.slack
    from opshub.connectors import discover_connectors, unregister_all

    unregister_all()
    importlib.reload(opshub.connectors.slack)

    names = {c.name for c in discover_connectors()}
    assert "slack" in names
