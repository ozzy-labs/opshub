"""Tests for :class:`opshub.connectors.teams.connector.TeamsConnector`.

These tests pin the connector-level contract independently of the
end-to-end CLI lifecycle:

1. ``name`` matches the registry key the CLI dispatches on.
2. Cursor is forwarded to the fetcher verbatim (Teams uses a single
   opaque delta link, not a JSON dict).
3. The mapper output reaches :meth:`SourceService.observe` keyword-
   for-keyword (no field dropped or rewritten by the connector).
4. Excluded channels / senders skip ``observe`` but the cursor still
   advances — the ADR-0020 §(b) "skip but advance" contract.
5. When the fetcher's fallback ran and produced a
   :attr:`pending_delta_link`, the connector prefers that link as the
   new cursor so the next sync resumes on the delta path.
6. Importing the package registers the connector exactly once.

The ``httpx`` extras are required because the fetcher constructor
imports them; we use ``importorskip`` to gate the file. ``TeamsAuth`` /
``TeamsFetcher`` are patched out so the tests never reach a real
Graph endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "httpx",
    reason="Teams connector tests require the 'connectors-teams' extras",
)

from opshub.connectors.context import ConnectorContext
from opshub.connectors.teams.connector import TeamsConnector
from opshub.connectors.teams.fetcher import RawTeamsChatMessage

# ----- helpers -----------------------------------------------------------


class _RecordingSourceService:
    """Test double for :class:`SourceService` that records ``observe`` calls."""

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
        author_handle: str | None = None,
        author_display: str | None = None,
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
                "author_handle": author_handle,
                "author_display": author_display,
            }
        )


def _raw_message(
    *,
    msg_id: str = "m1",
    chat_id: str = "C1",
    body_html: str = "hello",
    body_content_type: str = "text",
    sender_id: str = "U1",
    sender_name: str = "alice",
    chat_topic: str = "general",
) -> RawTeamsChatMessage:
    return RawTeamsChatMessage(
        id=msg_id,
        chat_id=chat_id,
        chat_topic=chat_topic,
        body_html=body_html,
        body_content_type=body_content_type,
        sender_display_name=sender_name,
        sender_id=sender_id,
        created_datetime_iso="2026-01-01T00:00:00Z",
        last_modified_iso="2026-01-01T00:00:00Z",
        web_url=f"https://teams.microsoft.com/l/message/{msg_id}",
        raw={},
    )


def _context(
    service: _RecordingSourceService, *, cursor_value: str | None = None
) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=cursor_value,
        secrets=None,
        logger=MagicMock(),
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fallback_window_days: int = 30,
) -> None:
    """Patch :class:`OpsHubSettings` so the connector picks up our window."""
    fake_settings = MagicMock()
    fake_settings.connectors.teams.fallback_window_days = fallback_window_days
    monkeypatch.setattr(
        "opshub.core.config.OpsHubSettings",
        lambda: fake_settings,
    )


def _patch_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :class:`TeamsAuth` so construction never reads the keyring."""
    fake_auth_cls = MagicMock()
    fake_auth_cls.return_value.token = "bearer-fake"
    monkeypatch.setattr(
        "opshub.connectors.teams.connector.TeamsAuth",
        fake_auth_cls,
    )


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yields: list[tuple[RawTeamsChatMessage, str]],
    pending_delta_link: str | None = None,
) -> tuple[MagicMock, dict[str, Any]]:
    """Patch :class:`TeamsFetcher` so :meth:`fetch_chat_messages` yields ``yields``.

    Returns the class mock plus a captured-kwargs dict so tests can
    assert the connector forwarded the prior delta link.
    """
    fake_fetcher_cls = MagicMock()
    captured: dict[str, Any] = {}

    def _fetch(*, delta_link: str | None) -> Iterator[tuple[RawTeamsChatMessage, str]]:
        captured["delta_link"] = delta_link
        return iter(yields)

    instance = fake_fetcher_cls.return_value
    instance.fetch_chat_messages.side_effect = _fetch
    # ``MagicMock`` would otherwise return a MagicMock for
    # ``.pending_delta_link``; we want a real value (or ``None``) so the
    # connector's preference branch behaves deterministically.
    instance.pending_delta_link = pending_delta_link
    monkeypatch.setattr(
        "opshub.connectors.teams.connector.TeamsFetcher",
        fake_fetcher_cls,
    )
    return fake_fetcher_cls, captured


def _patch_excludes_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str) -> None:
    """Write an ``excludes.yaml`` and point ``default_config_dir`` at it.

    Mirrors the Slack connector test helper exactly — see that module's
    docstring for the no-arg ``load_excludes()`` rationale (Phase 10
    audit Cluster 3).
    """
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)


# ----- name --------------------------------------------------------------


def test_connector_name_is_teams() -> None:
    """The registry / CLI dispatch key must be exactly ``"teams"``."""
    assert TeamsConnector.name == "teams"
    assert TeamsConnector().name == "teams"


# ----- sync: happy path --------------------------------------------------


def test_sync_forwards_cursor_to_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persisted cursor → ``fetcher.fetch_chat_messages(delta_link=...)``."""
    _patch_settings(monkeypatch)
    _patch_auth(monkeypatch)
    _, captured = _patch_fetcher(monkeypatch, yields=[])

    service = _RecordingSourceService()
    result = TeamsConnector().sync(_context(service, cursor_value="stored-delta-link"))

    # The connector forwarded the stored cursor verbatim.
    assert captured["delta_link"] == "stored-delta-link"
    # No yields → no observations, cursor preserved.
    assert result.observed_count == 0
    assert result.new_cursor == "stored-delta-link"
    assert service.calls == []


def test_sync_observes_each_yielded_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each yield → mapper → :meth:`SourceService.observe` with mapped kwargs.

    Pins the three-step pipeline: a regression that drops the
    ``observe(**kwargs)`` splat or skips the mapper surfaces here.
    """
    _patch_settings(monkeypatch)
    _patch_auth(monkeypatch)
    msg_a = _raw_message(msg_id="m1", body_html="first")
    msg_b = _raw_message(msg_id="m2", body_html="second")
    _patch_fetcher(
        monkeypatch,
        yields=[
            (msg_a, "cursor-1"),
            (msg_b, "cursor-2"),
        ],
    )

    service = _RecordingSourceService()
    result = TeamsConnector().sync(_context(service, cursor_value=None))

    assert result.observed_count == 2
    assert [c["external_id"] for c in service.calls] == ["C1:m1", "C1:m2"]
    assert [c["summary"] for c in service.calls] == ["first", "second"]
    assert all(c["connector_name"] == "teams" for c in service.calls)
    assert all(c["source_type"] == "teams_message" for c in service.calls)
    # Provenance is stamped uniformly.
    assert all(c["provenance_origin"] == "external" for c in service.calls)
    assert all(c["provenance_trust"] == "untrusted" for c in service.calls)
    # Cursor advanced to the last yielded value.
    assert result.new_cursor == "cursor-2"


def test_sync_passes_fallback_window_to_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[connectors.teams] fallback_window_days`` reaches the fetcher constructor.

    Pins the config wiring — a regression that hard-codes the default
    would silently ignore operator overrides.
    """
    _patch_settings(monkeypatch, fallback_window_days=7)
    _patch_auth(monkeypatch)
    fetcher_cls, _captured = _patch_fetcher(monkeypatch, yields=[])

    service = _RecordingSourceService()
    TeamsConnector().sync(_context(service, cursor_value=None))

    fetcher_cls.assert_called_once()
    assert fetcher_cls.call_args.kwargs == {"fallback_window_days": 7}


# ----- sync: excludes ----------------------------------------------------


def test_sync_skips_excluded_channel_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): excluded chat_id never observed; cursor advances.

    The selector key reused here is the Slack ``channels`` selector
    per the Phase 11 plan (Teams reuses the same exclude rule shape).
    """
    _patch_excludes_yaml(monkeypatch, tmp_path, body="channels:\n  - C-secret\n")
    _patch_settings(monkeypatch)
    _patch_auth(monkeypatch)
    public = _raw_message(msg_id="m-pub", chat_id="C1", body_html="public")
    secret = _raw_message(msg_id="m-sec", chat_id="C-secret", body_html="leaked")
    _patch_fetcher(
        monkeypatch,
        yields=[
            (public, "cursor-1"),
            (secret, "cursor-2"),
        ],
    )

    service = _RecordingSourceService()
    result = TeamsConnector().sync(_context(service, cursor_value=None))

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "C1:m-pub"
    # Cursor advanced past the excluded item.
    assert result.new_cursor == "cursor-2"


def test_sync_skips_excluded_sender_but_advances_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): excluded sender never observed; cursor advances."""
    _patch_excludes_yaml(monkeypatch, tmp_path, body="senders:\n  - U-bot\n")
    _patch_settings(monkeypatch)
    _patch_auth(monkeypatch)
    bot = _raw_message(msg_id="m-bot", sender_id="U-bot", body_html="noise")
    human = _raw_message(msg_id="m-human", sender_id="U1", body_html="real")
    _patch_fetcher(
        monkeypatch,
        yields=[
            (bot, "cursor-1"),
            (human, "cursor-2"),
        ],
    )

    service = _RecordingSourceService()
    result = TeamsConnector().sync(_context(service, cursor_value=None))

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "C1:m-human"
    assert result.new_cursor == "cursor-2"


# ----- sync: fallback cursor preference ---------------------------------


def test_sync_prefers_pending_delta_link_when_fallback_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0010 §改訂 (c): a freshly-acquired delta link beats the
    in-flight ``$filter`` cursor as the persisted value, so the next
    sync resumes on the fast delta path."""
    _patch_settings(monkeypatch)
    _patch_auth(monkeypatch)
    msg = _raw_message(msg_id="m-recovered", body_html="recovered")
    _patch_fetcher(
        monkeypatch,
        yields=[(msg, "in-flight-filter-cursor")],
        pending_delta_link="fresh-delta-link",
    )

    service = _RecordingSourceService()
    result = TeamsConnector().sync(_context(service, cursor_value="expired-link"))

    assert result.observed_count == 1
    # Pending delta link wins over the in-flight cursor.
    assert result.new_cursor == "fresh-delta-link"


def test_sync_keeps_in_flight_cursor_when_no_pending_delta_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the fallback did not run, the in-flight cursor persists."""
    _patch_settings(monkeypatch)
    _patch_auth(monkeypatch)
    msg = _raw_message(msg_id="m1")
    _patch_fetcher(
        monkeypatch,
        yields=[(msg, "cursor-1")],
        pending_delta_link=None,
    )

    service = _RecordingSourceService()
    result = TeamsConnector().sync(_context(service, cursor_value="prior-link"))

    assert result.new_cursor == "cursor-1"


# ----- registry ---------------------------------------------------------


def test_teams_subpackage_registers_connector() -> None:
    """Importing :mod:`opshub.connectors.teams` registers the connector.

    Mirrors the Slack precedent — the CLI driver discovers connectors
    purely through the registry. We reload the package after a registry
    reset so the test is robust against earlier tests that may have
    called :func:`unregister_all`.
    """
    import importlib

    import opshub.connectors.teams
    from opshub.connectors import discover_connectors, unregister_all

    unregister_all()
    importlib.reload(opshub.connectors.teams)

    names = {c.name for c in discover_connectors()}
    assert "teams" in names
